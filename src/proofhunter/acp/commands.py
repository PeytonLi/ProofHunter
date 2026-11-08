"""Bounded plain-text chat command parsing (PRD section 1, chat behavior).

Supported commands: ``verify <PR URL>`` (or a message holding exactly one PR URL),
``status <run_id>``, ``evidence <run_id>`` and ``help``. Parsing is pure and never
raises; anything ambiguous becomes ``InvalidCommand`` with a fixed reason.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

#: Longest chat text we look at. Longer messages are rejected, not truncated.
MAX_COMMAND_CHARS: Final = 1024
MAX_TOKENS: Final = 32

_PR_URL: Final = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100})/pull/(?P<number>[1-9][0-9]{0,9})$"
)
_URLISH: Final = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|www\.|github\.com)")
RUN_ID_PATTERN: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_UUIDISH: Final = re.compile(r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_AGENT_MENTION: Final = re.compile(r"@agent1[023456789acdefghjklmnpqrstuvwxyz]{59}")
_MAX_PR_NUMBER: Final = 2**31 - 1

_VERIFY_WORDS: Final = frozenset({"verify", "check", "investigate", "test"})
_STATUS_WORDS: Final = frozenset({"status", "poll"})
_EVIDENCE_WORDS: Final = frozenset({"evidence", "receipt"})
_HELP_WORDS: Final = frozenset({"help", "?", "hi", "hello", "start", "commands", "usage"})
#: Words tolerated around a verify URL ("please verify this PR: <url>").
_FILLER: Final = frozenset({"please", "can", "you", "this", "the", "pr", "pull", "request",
                            "for", "me", "a", "my", "proofhunter", "run", "on", "of", "it"})


class InvalidReason(StrEnum):
    EMPTY = "empty"
    TOO_LONG = "too_long"
    CONTROL_CHARACTERS = "control_characters"
    NOT_ASCII_URL = "not_ascii_url"
    INVALID_PR_URL = "invalid_pr_url"
    MULTIPLE_URLS = "multiple_urls"
    MISSING_URL = "missing_url"
    MISSING_RUN_ID = "missing_run_id"
    INVALID_RUN_ID = "invalid_run_id"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class VerifyCommand:
    pr_url: str


@dataclass(frozen=True, slots=True)
class StatusCommand:
    run_id: str


@dataclass(frozen=True, slots=True)
class EvidenceCommand:
    run_id: str


@dataclass(frozen=True, slots=True)
class HelpCommand:
    pass


@dataclass(frozen=True, slots=True)
class InvalidCommand:
    reason: InvalidReason


Command = VerifyCommand | StatusCommand | EvidenceCommand | HelpCommand | InvalidCommand


def canonical_pr_url(token: str) -> str | None:
    """Return the canonical PR URL for ``token`` or ``None``.

    Only ``https://github.com/OWNER/REPO/pull/N`` is accepted: no userinfo, port,
    percent escapes, query, fragment, trailing path or non-ASCII characters.
    """
    if not token.isascii() or "%" in token or "@" in token:
        return None
    match = _PR_URL.match(token)
    if match is None or int(match.group("number")) > _MAX_PR_NUMBER:
        return None
    repo = match.group("repo")
    if repo in {".", ".."} or repo.endswith(".git"):
        return None
    return token


def _has_control_chars(text: str) -> bool:
    return any(unicodedata.category(ch) in {"Cc", "Cf"} and ch not in "\t\n\r" for ch in text)


def _strip_wrapping(token: str) -> str:
    """Remove one layer of chat formatting around a token (``<url>``, backticks, quotes)."""
    token = token.strip()
    for left, right in (("**", "**"), ("<", ">"), ("`", "`"), ('"', '"'), ("'", "'"),
                        ("(", ")")):
        if len(token) >= 2 and token.startswith(left) and token.endswith(right):
            return token[len(left):-len(right)]
    return token.rstrip(".,;:!") if _URLISH.search(token) else token


def _run_id(tokens: list[str]) -> str | InvalidReason:
    if not tokens:
        return InvalidReason.MISSING_RUN_ID
    if len(tokens) > 1:
        return InvalidReason.AMBIGUOUS
    candidate = _strip_wrapping(tokens[0])
    if not candidate.isascii() or not _UUIDISH.match(candidate):
        return InvalidReason.INVALID_RUN_ID
    candidate = candidate.lower()
    return candidate if RUN_ID_PATTERN.match(candidate) else InvalidReason.INVALID_RUN_ID


def _verify(tokens: list[str], *, require_only_filler: bool) -> Command:
    urlish = [t for t in tokens if _URLISH.search(t)]
    if not urlish:
        return InvalidCommand(InvalidReason.MISSING_URL)
    if len(urlish) > 1:
        return InvalidCommand(InvalidReason.MULTIPLE_URLS)
    if require_only_filler:
        others = [t.lower().strip(".,;:!?") for t in tokens if t not in urlish]
        if any(t and t not in _FILLER for t in others):
            return InvalidCommand(InvalidReason.AMBIGUOUS)
    raw = urlish[0]
    if not raw.isascii():
        return InvalidCommand(InvalidReason.NOT_ASCII_URL)
    url = canonical_pr_url(raw)
    return InvalidCommand(InvalidReason.INVALID_PR_URL) if url is None else VerifyCommand(url)


def parse_command(text: str) -> Command:
    """Parse one chat message into a command. Never raises."""
    if not isinstance(text, str):
        return InvalidCommand(InvalidReason.UNKNOWN)
    if len(text) > MAX_COMMAND_CHARS:
        return InvalidCommand(InvalidReason.TOO_LONG)
    if _has_control_chars(text):
        return InvalidCommand(InvalidReason.CONTROL_CHARACTERS)
    tokens = [_strip_wrapping(t) for t in text.split()]
    tokens = [t for t in tokens if t]
    # ASI:One may forward the routing mention as part of the command text.
    if tokens and (tokens[0].lower() == "@proofhunter" or _AGENT_MENTION.fullmatch(tokens[0])):
        tokens = tokens[1:]
    if not tokens:
        return InvalidCommand(InvalidReason.EMPTY)
    if len(tokens) > MAX_TOKENS:
        return InvalidCommand(InvalidReason.TOO_LONG)
    verb = tokens[0].lower().rstrip(":,.!")
    rest = tokens[1:]
    if verb in _HELP_WORDS:
        return HelpCommand() if len(rest) <= 3 else InvalidCommand(InvalidReason.AMBIGUOUS)
    if verb in _STATUS_WORDS or verb in _EVIDENCE_WORDS:
        if any(_URLISH.search(t) for t in rest):
            return InvalidCommand(InvalidReason.AMBIGUOUS)
        run_id = _run_id(rest)
        if isinstance(run_id, InvalidReason):
            return InvalidCommand(run_id)
        return StatusCommand(run_id) if verb in _STATUS_WORDS else EvidenceCommand(run_id)
    if verb in _VERIFY_WORDS:
        if any(_UUIDISH.match(t) for t in rest):
            return InvalidCommand(InvalidReason.AMBIGUOUS)
        return _verify(rest, require_only_filler=True)
    if any(_URLISH.search(t) for t in tokens):
        if any(_UUIDISH.match(t) for t in tokens):
            return InvalidCommand(InvalidReason.AMBIGUOUS)
        return _verify(tokens, require_only_filler=True)
    return InvalidCommand(InvalidReason.UNKNOWN)


__all__ = [
    "MAX_COMMAND_CHARS",
    "RUN_ID_PATTERN",
    "Command",
    "EvidenceCommand",
    "HelpCommand",
    "InvalidCommand",
    "InvalidReason",
    "StatusCommand",
    "VerifyCommand",
    "canonical_pr_url",
    "parse_command",
]
