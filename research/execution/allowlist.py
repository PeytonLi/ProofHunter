"""Only byte-identical reviewed Track A template sources may run.

A source runs only when (1) its sha256 is on an allowlist built from a reviewed
origin (the frozen bench corpus review in ``bench/policy.json`` or a re-render of
a reviewed research template), (2) the bytes equal the reviewed bytes and (3) it
passes a static AST screen for bounded first-party integer checkout code. The
screen is defence in depth; the hash allowlist is the gate.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from proofhunter.contracts import MAX_SOURCE_BYTES, sha256_hex
from research.schemas.interfaces import CaseSources

ALLOWED_CALLS: Final = frozenset(
    {"abs", "divmod", "enumerate", "int", "len", "list", "max", "min", "range", "reversed",
     "round", "sorted", "sum", "tuple", "zip"}
)
ALLOWED_ANNOTATIONS: Final = frozenset({"int", "list", "tuple", "bool"})
_NODES: Final = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign,
    ast.AnnAssign, ast.For, ast.If, ast.IfExp, ast.Expr, ast.BinOp, ast.UnaryOp, ast.BoolOp,
    ast.Compare, ast.Call, ast.Name, ast.Constant, ast.Subscript, ast.Slice, ast.List, ast.Tuple,
    ast.ListComp, ast.GeneratorExp, ast.comprehension, ast.Load, ast.Store, ast.operator,
    ast.unaryop, ast.boolop, ast.cmpop, ast.expr_context, ast.Pass, ast.Break, ast.Continue,
)


class ExecutionRefused(PermissionError):
    """Raised (or reported as ``denied``) when code is not reviewed and allowlisted."""


def screen_source(data: bytes) -> str | None:
    """Return a refusal reason, or ``None`` if the source is a plain integer checkout module."""
    if len(data) > MAX_SOURCE_BYTES:
        return "source_too_large"
    try:
        tree = ast.parse(data.decode("utf-8"), filename="checkout.py")
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return "unparseable"
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    if "total_cents" not in names:
        return "missing_entrypoint"
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if not isinstance(stmt, ast.FunctionDef):
            return "top_level_statement"
    for node in ast.walk(tree):
        if not isinstance(node, _NODES):
            return f"node_{type(node).__name__.lower()}"
        if isinstance(node, ast.FunctionDef) and (node.decorator_list or node.type_params):
            return "decorator"
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return "dunder_name"
        if isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Name) or (
                func.id not in ALLOWED_CALLS and func.id not in names
            ):
                return "call_not_allowed"
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes | complex):
            return "constant_not_allowed"
    return None


@dataclass(frozen=True)
class ReviewedSourceAllowlist:
    """sha256 -> reviewed bytes, plus where the review came from."""

    reviewed: Mapping[str, bytes]
    origin: str

    def __post_init__(self) -> None:
        for digest, data in self.reviewed.items():
            if sha256_hex(data) != digest:
                raise ExecutionRefused("allowlist bytes/hash mismatch")
            reason = screen_source(data)
            if reason is not None:
                raise ExecutionRefused(f"reviewed source fails static screen: {reason}")

    def refusal(self, digest: str, data: bytes) -> str | None:
        """``None`` if ``data`` may run; otherwise a stable refusal reason."""
        if sha256_hex(data) != digest:
            return "hash_mismatch"
        reviewed = self.reviewed.get(digest)
        if reviewed is None:
            return "not_allowlisted"
        if reviewed != data:
            return "bytes_differ_from_review"
        return screen_source(data)

    def union(self, other: ReviewedSourceAllowlist) -> ReviewedSourceAllowlist:
        return ReviewedSourceAllowlist({**self.reviewed, **other.reviewed},
                                       f"{self.origin}+{other.origin}")


def from_bench_corpus() -> ReviewedSourceAllowlist:
    """Frozen first-party bench corpus, filtered to ``bench/policy.json`` reviewed hashes."""
    from bench.corpus import ROOT, load
    from bench.freeze import load_policy

    reviewed = frozenset(load_policy().reviewed_source_sha256)
    data: dict[str, bytes] = {}
    for case in load(ROOT).cases:
        for side in ("base", "head"):
            raw = (ROOT / getattr(case, f"{side}_path")).read_bytes()
            if sha256_hex(raw) in reviewed:
                data[sha256_hex(raw)] = raw
    return ReviewedSourceAllowlist(data, "bench-policy-reviewed")


def from_template_render(sources: Iterable[CaseSources]) -> ReviewedSourceAllowlist:
    """Re-render each case from the reviewed dataset templates; allow only identical bytes."""
    try:
        templates = importlib.import_module("research.data.templates")
    except ModuleNotFoundError as error:
        raise ExecutionRefused("research.data.templates is not available") from error
    known = {t.template_id: t for t in templates.list_templates()}
    data: dict[str, bytes] = {}
    for item in sources:
        case = item.case
        info = known.get(case.template_id)
        if (info is None or info.template_sha256 != case.template_sha256
                or str(info.template_version) != case.template_version):
            raise ExecutionRefused(f"{case.case_id}: template not in reviewed registry")
        base, head = templates.render(case.template_id, dict(case.params))
        if (base, head) != (item.base_source, item.head_source):
            raise ExecutionRefused(f"{case.case_id}: sources differ from reviewed render")
        if (sha256_hex(base), sha256_hex(head)) != (case.base_source_sha256,
                                                    case.head_source_sha256):
            raise ExecutionRefused(f"{case.case_id}: source hashes differ from case record")
        data[sha256_hex(base)] = base
        data[sha256_hex(head)] = head
    origin = f"research-templates@{templates.TEMPLATE_REGISTRY_VERSION}"
    return ReviewedSourceAllowlist(data, origin)
