from __future__ import annotations

import pytest

from proofhunter.acp import (
    EvidenceCommand,
    HelpCommand,
    InvalidCommand,
    InvalidReason,
    StatusCommand,
    VerifyCommand,
    parse_command,
)
from proofhunter.acp.commands import MAX_COMMAND_CHARS

RUN = "00000000-05e7-4001-8000-000000000001"
PR = "https://github.com/proofhunter-demo/checkout-demo/pull/1"
MENTION = "@agent1qt7qj4vrjwxjax7cd4n4ajgn9mrf97ke2lzaau72p8f4exydamxdxhxru5y"


@pytest.mark.parametrize(("text", "expected"), [
    (f"verify {PR}", VerifyCommand(PR)),
    (f"status {RUN}", StatusCommand(RUN)),
    (f"evidence {RUN}", EvidenceCommand(RUN)),
    ("help", HelpCommand()),
])
@pytest.mark.parametrize("mention", [MENTION, "@proofhunter", "@ProofHunter", "**@proofhunter**"])
def test_asi_routing_mention_is_not_a_second_command(
        mention: str, text: str, expected: object) -> None:
    assert parse_command(f"{mention} {text}") == expected


@pytest.mark.parametrize("text", [
    f"{MENTION} verify {PR} and status {RUN}",
    f"{MENTION} {MENTION} verify {PR}",
    f"@someone verify {PR}",
    f"@proofhunter @proofhunter verify {PR}",
    f"@proofhunter verify {PR} and status {RUN}",
    f"**@proofhunter** **@proofhunter** status {RUN}",
    f"**@proofhunter** verify {PR} and status {RUN}",
])
def test_mentions_do_not_allow_ambiguous_commands(text: str) -> None:
    assert isinstance(parse_command(text), InvalidCommand)


@pytest.mark.parametrize("text", [
    f"Verify {PR}", f"verify {PR}", f"  VERIFY   {PR}  ", PR,
    f"verify <{PR}>", f"verify {PR}.",
])
def test_verify_accepts_one_canonical_url(text: str) -> None:
    assert parse_command(text) == VerifyCommand(pr_url=PR)


@pytest.mark.parametrize("text", [
    "verify http://github.com/a/b/pull/1",
    "verify https://gitlab.com/a/b/pull/1",
    "verify https://github.com/a/b/issues/1",
    "verify https://github.com/a/b/pull/0",
    "verify https://github.com/a/b/pull/99999999999",
    "verify https://github.com/a/b/pull/1?x=1",
    "verify https://github.com/a/b/pull/1#frag",
    "verify https://user:pw@github.com/a/b/pull/1",
    "verify https://github.com:444/a/b/pull/1",
    "verify https://github.com/a/b/pull/1/files",
    "verify https://github.com/../b/pull/1",
    "verify https://github.com/a/b/pull/１",
])
def test_verify_rejects_non_canonical(text: str) -> None:
    result = parse_command(text)
    assert isinstance(result, InvalidCommand)


def test_multiple_urls_rejected() -> None:
    assert parse_command(f"verify {PR} {PR.replace('/1', '/2')}") == InvalidCommand(
        InvalidReason.MULTIPLE_URLS)


def test_verify_without_url_is_invalid() -> None:
    assert isinstance(parse_command("verify"), InvalidCommand)


@pytest.mark.parametrize(("text", "expected"), [
    (f"status {RUN}", StatusCommand(run_id=RUN)),
    (f"Status  {RUN}", StatusCommand(run_id=RUN)),
    (f"evidence {RUN}", EvidenceCommand(run_id=RUN)),
    (f"Evidence {RUN.upper()}", EvidenceCommand(run_id=RUN)),
])
def test_status_and_evidence(text: str, expected: object) -> None:
    assert parse_command(text) == expected


@pytest.mark.parametrize("text", ["status", "status abc", f"status {RUN} {RUN}",
                                  "evidence 00000000-05e7-1001-8000-000000000001"])
def test_bad_run_ids(text: str) -> None:
    assert isinstance(parse_command(text), InvalidCommand)


@pytest.mark.parametrize("text", ["help", "HELP", " Help "])
def test_help(text: str) -> None:
    assert parse_command(text) == HelpCommand()


@pytest.mark.parametrize("text", [f"verify {PR}/", f"please verify {PR}"])
def test_strict_forms_are_not_guessed(text: str) -> None:
    assert isinstance(parse_command(text), InvalidCommand)


@pytest.mark.parametrize(("text", "reason"), [
    ("", InvalidReason.EMPTY), ("   \n ", InvalidReason.EMPTY),
    ("x" * (MAX_COMMAND_CHARS + 1), InvalidReason.TOO_LONG),
    (f"verify {PR}\x00", InvalidReason.CONTROL_CHARACTERS),
    ("status \x1b[31m", InvalidReason.CONTROL_CHARACTERS),
])
def test_malformed(text: str, reason: InvalidReason) -> None:
    assert parse_command(text) == InvalidCommand(reason)


def test_ambiguous_and_unknown() -> None:
    assert isinstance(parse_command(f"verify {PR} and status {RUN}"), InvalidCommand)
    assert isinstance(parse_command("tell me a joke"), InvalidCommand)
    many = " ".join(["word"] * 100)
    assert isinstance(parse_command(many), InvalidCommand)
