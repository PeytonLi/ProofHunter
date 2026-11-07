"""Canonical serializer golden vector and encoding rules (ph-canonical-json-v1)."""

import math

import pytest

from proofhunter.contracts import (
    CandidateInput,
    CanonicalEncodingError,
    canonical_json,
    canonical_sha256,
    git_blob_sha1,
    input_sha256,
)

GOLDEN_VALUE = {
    "z": [3, 1, 2],
    "a": {"y": None, "b": True, "x": 0.1},
    "é": "café ✓",
    "n": -9223372036854775808,
    "f": 0.123456789012,
}
GOLDEN_BYTES = (
    '{"a":{"b":true,"x":0.1,"y":null},"f":0.123456789012,"n":-9223372036854775808,'
    '"z":[3,1,2],"é":"café ✓"}'
).encode()
GOLDEN_SHA256 = "cbee24ee7c63144490c513997aa81df7b9de928783973b4bec5349f9018cc6da"


def test_golden_vector_bytes() -> None:
    assert canonical_json(GOLDEN_VALUE) == GOLDEN_BYTES


def test_golden_vector_hash() -> None:
    assert canonical_sha256(GOLDEN_VALUE) == GOLDEN_SHA256


def test_sentinel_input_hash_is_frozen() -> None:
    data = CandidateInput(prices_cents=[15, 15, 15], discount_percent=10)
    assert canonical_json({"discount_percent": 10, "prices_cents": [15, 15, 15]}) == (
        b'{"discount_percent":10,"prices_cents":[15,15,15]}')
    assert input_sha256(data) == SENTINEL_SHA256


SENTINEL_SHA256 = "326b2a8732d3411ecd4165eeab7faf0abb8efc089d0b3f333c5ec1264635635a"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_rejects_non_finite(bad: float) -> None:
    with pytest.raises(CanonicalEncodingError):
        canonical_json({"x": bad})


def test_rejects_unrounded_float() -> None:
    with pytest.raises(CanonicalEncodingError):
        canonical_json([0.1234567890123])


@pytest.mark.parametrize("bad", [2**63, -(2**63) - 1])
def test_rejects_out_of_range_int(bad: int) -> None:
    with pytest.raises(CanonicalEncodingError):
        canonical_json(bad)


def test_rejects_non_string_keys_and_types() -> None:
    with pytest.raises(CanonicalEncodingError):
        canonical_json({1: "x"})
    with pytest.raises(CanonicalEncodingError):
        canonical_json({"x": b"bytes"})
    with pytest.raises(CanonicalEncodingError):
        canonical_json({"x": {1, 2}})


def test_rejects_lone_surrogate() -> None:
    with pytest.raises(CanonicalEncodingError):
        canonical_json("\ud800")


def test_preserves_array_order_and_explicit_null() -> None:
    assert canonical_json([None, 2, 1]) == b"[null,2,1]"


def test_git_blob_sha1_matches_git() -> None:
    assert git_blob_sha1(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert git_blob_sha1(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
