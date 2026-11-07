"""Pinned source is data: never clone, follow download URLs or execute it."""

import base64
import binascii
import difflib
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import ValidationError

from proofhunter.contracts import (
    ContextFile,
    Enrollment,
    HypothesisMode,
    PinnedRevision,
    PlanningContext,
    PrTarget,
    ReasonCode,
    SourceBundle,
    SourceFile,
    git_blob_sha1,
    sha256_hex,
)
from proofhunter.github.client import GithubSession, obj, positive, utcnow
from proofhunter.github.validation import GithubRepositoryValidator, failure

_CONTRACT = (
    "checkout.total_cents(prices_cents: list[int], discount_percent: int) -> int. "
    "0..8 items of 0..10000 integer cents; discount 0..100 integer percent. "
    "Reject booleans, floats, extra keys and out-of-range inputs. "
    "Round the discounted subtotal once, half up: "
    "(sum(prices_cents) * (100 - discount_percent) + 50) // 100."
)
_SENSITIVE = re.compile(
    r"(?i)(?:-----BEGIN|(?:token|secret|password|api[_-]?key|authorization)\s*[:=]|"
    r"github_pat_|gh[pousr]_|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{16})"
)


def sha(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError
    return value


def verify_repo(value: Any, target: PrTarget) -> None:
    repo = obj(value)
    if (
        positive(repo["id"]) != target.repo_id
        or repo["full_name"].lower() != target.full_name.lower()
        or repo["private"] is not False
    ):
        raise ValueError


def parse_pull(value: Any, target: PrTarget) -> tuple[str, str]:
    try:
        pull = obj(value)
        if type(pull["number"]) is not int or pull["number"] != target.number:
            raise ValueError
        base, head = obj(pull["base"]), obj(pull["head"])
        if head.get("repo") is not None and positive(obj(head["repo"])["id"]) != target.repo_id:
            raise failure(ReasonCode.FORK_UNSUPPORTED, "fork pull requests are unsupported")
        verify_repo(base["repo"], target)
        verify_repo(head["repo"], target)
        if any(
            not isinstance(ref, str) or not 1 <= len(ref) <= 255
            for ref in (base["ref"], head["ref"])
        ):
            raise ValueError
        return sha(base["sha"]), sha(head["sha"])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise failure(
            ReasonCode.REVISION_UNAVAILABLE, "pull request repository or revision invalid"
        ) from None


def planning_context(base: SourceFile, head: SourceFile) -> PlanningContext:
    # SourcePort has no run ID argument; integration replaces this correlation ID.
    run_id = "00000000-0000-4000-8000-000000000000"
    unsafe = any(
        _SENSITIVE.search(s.content) or any(ord(c) < 32 and c not in "\n\r\t" for c in s.content)
        for s in (base, head)
    )
    text = "" if unsafe else head.content.encode()[:12000].decode("utf-8", errors="ignore")
    diff = (
        ""
        if unsafe
        else "".join(
            difflib.unified_diff(
                base.content.splitlines(keepends=True),
                head.content.splitlines(keepends=True),
                fromfile="a/checkout.py",
                tofile="b/checkout.py",
            )
        )
    )
    diff = diff.encode()[:16000].decode("utf-8", errors="ignore")
    return PlanningContext(
        run_id=run_id,
        contract_id="checkout-rounding@1",
        entrypoint="checkout.total_cents",
        contract_text=_CONTRACT,
        files=[ContextFile(path="checkout.py", start_line=1, text=text)] if text else [],
        diff=diff,
        supported_modes=list(HypothesisMode),
        max_hypotheses=6,
    )


class GithubSource:
    def __init__(
        self,
        session: GithubSession,
        validator: GithubRepositoryValidator,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session = session
        self.validator = validator
        self.now = now

    async def aclose(self) -> None:
        await self.session.aclose()

    async def pin(self, target: PrTarget) -> PinnedRevision:
        self.validator.bind(target)
        data = await self.session.request(
            target, "GET", f"/repos/{target.full_name}/pulls/{target.number}"
        )
        base, head = parse_pull(data, target)
        return PinnedRevision(
            **target.model_dump(), base_sha=base, head_sha=head, resolved_at=self.now()
        )

    async def fetch(self, revision: PinnedRevision, enrollment: Enrollment) -> SourceBundle:
        target = PrTarget(**{k: getattr(revision, k) for k in PrTarget.model_fields})
        if self.validator.bind(target) != enrollment:
            raise failure(ReasonCode.REPO_NOT_ENROLLED, "source enrollment mismatch")
        base = await self._file(target, revision.base_sha, "base")
        head = await self._file(target, revision.head_sha, "head")
        return SourceBundle(
            revision=revision, base=base, head=head, context=planning_context(base, head)
        )

    async def _file(
        self, target: PrTarget, commit_sha: str, side: Literal["base", "head"]
    ) -> SourceFile:
        prefix = f"/repos/{target.full_name}"
        commit = await self.session.request(target, "GET", f"{prefix}/git/commits/{commit_sha}")
        try:
            if sha(obj(commit)["sha"]) != commit_sha:
                raise ValueError
            tree_sha = sha(obj(obj(commit)["tree"])["sha"])
        except (KeyError, ValueError, TypeError):
            raise failure(ReasonCode.SOURCE_INVALID, "commit tree binding invalid") from None
        tree = await self.session.request(target, "GET", f"{prefix}/git/trees/{tree_sha}")
        try:
            tree = obj(tree)
            if sha(tree["sha"]) != tree_sha or tree["truncated"] is not False:
                raise ValueError
            entries = tree["tree"]
            if not isinstance(entries, list):
                raise ValueError
            matches = [obj(e) for e in entries if obj(e).get("path") == "checkout.py"]
            if len(matches) != 1:
                raise ValueError
            entry = matches[0]
            if (
                entry["mode"] != "100644"
                or entry["type"] != "blob"
                or type(entry["size"]) is not int
                or not 0 <= entry["size"] <= 65536
            ):
                raise ValueError
            blob_sha = sha(entry["sha"])
        except (KeyError, ValueError, TypeError):
            raise failure(
                ReasonCode.SOURCE_INVALID, "source is not a bounded regular blob"
            ) from None
        data = await self.session.request(
            target, "GET", f"{prefix}/contents/checkout.py", params={"ref": commit_sha}
        )
        try:
            data = obj(data)
            if (
                data["type"] != "file"
                or data["path"] != "checkout.py"
                or data["name"] != "checkout.py"
                or data["encoding"] != "base64"
                or sha(data["sha"]) != blob_sha
                or type(data["size"]) is not int
                or data["size"] != entry["size"]
                or "submodule_git_url" in data
                or "target" in data
            ):
                raise ValueError
            encoded = data["content"]
            if not isinstance(encoded, str) or len(encoded) > 100000:
                raise ValueError
            content = base64.b64decode(encoded.replace("\n", ""), validate=True)
            if (
                len(content) != data["size"]
                or len(content) > 65536
                or git_blob_sha1(content) != blob_sha
                or content.startswith(b"version https://git-lfs.github.com/spec/v1")
                or b"\x00" in content
            ):
                raise ValueError
            return SourceFile(
                side=side,
                commit_sha=commit_sha,
                path="checkout.py",
                blob_sha=blob_sha,
                content=content.decode("utf-8"),
                content_sha256=sha256_hex(content),
                size_bytes=len(content),
            )
        except (KeyError, ValueError, TypeError, UnicodeError, binascii.Error, ValidationError):
            raise failure(ReasonCode.SOURCE_INVALID, "source content verification failed") from None
