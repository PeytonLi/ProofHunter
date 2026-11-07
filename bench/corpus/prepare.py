"""Explicit dev freeze builder. Not called by benchmark or manifest checker."""

from __future__ import annotations

import json

from bench.corpus import ROOT, Case, Manifest, Witness, revision
from fixtures import catalog
from proofhunter.contracts import canonical_sha256, sha256_hex

# Witnesses are hand-computed independently of the candidate bank and arm oracle.
VARIANTS: list[tuple[str, str, list[int], int, int, int]] = [
    ("per_line", "sum((p * (100 - d) + 50) // 100 for p in prices_cents)",
     [15, 15, 15], 10, 41, 42),
    ("half_even", "round(sum(prices_cents) * (100 - d) / 100)", [1], 50, 1, 0),
    ("floor", "sum(prices_cents) * (100 - d) // 100", [1], 50, 1, 0),
    ("aggregate_order", "sum(prices_cents) - (sum(prices_cents) * d + 50) // 100",
     [1], 50, 1, 0),
    ("ignored_discount", "sum(prices_cents) + 0 * d", [100], 50, 50, 100),
    ("clamped_discount", "(sum(prices_cents) * (100 - min(d, 50)) + 50) // 100",
     [100], 99, 1, 50),
    ("item_count", "(sum(prices_cents[:6] if len(prices_cents) == 7 else prices_cents)\n"
     "            * (100 - d) + 50) // 100", [1] * 7, 0, 7, 6),
    ("full_discount", "sum(prices_cents) if d == 100 else "
     "(sum(prices_cents) * (100 - d) + 50) // 100", [99], 100, 0, 99),
    ("clean_divmod", "divmod(sum(prices_cents) * (100 - d) + 50, 100)[0]",
     [], 0, 0, 0),
    ("clean_split", "sum(prices_cents) * (100 - d) // 100 + "
     "int(sum(prices_cents) * (100 - d) % 100 >= 50)", [], 0, 0, 0),
    ("clean_commute", "(50 + (100 - d) * sum(prices_cents)) // 100", [], 0, 0, 0),
    ("existing", "sum((p * (100 - d) + 50) // 100 for p in prices_cents)",
     [15, 15, 15], 10, 41, 42),
]


def main() -> None:
    base = catalog.revisions()["base"].source_bytes
    cases = []
    for name, expression, prices, discount, expected, head in VARIANTS:
        directory = ROOT / name
        directory.mkdir(exist_ok=True)
        content = ("\ndef total_cents(prices_cents: list[int], discount_percent: int) -> int:\n"
                   "    d = discount_percent\n"
                   f"    return {expression}\n").encode()
        base_bytes = content if name == "existing" else base
        (directory / "checkout.py").write_bytes(content)
        (directory / "base.py").write_bytes(base_bytes)
        clean = name.startswith("clean_")
        cases.append(Case(
            case_id=name, split="dev", template_group="per_line" if name == "existing" else name,
            base_path=f"{name}/base.py", head_path=f"{name}/checkout.py",
            base_revision=revision(name + ":base"),
            head_revision=revision(name + (":v2:head" if name == "item_count" else ":head")),
            base_sha256=sha256_hex(base_bytes), head_sha256=sha256_hex(content),
            contract_sha256=catalog.contract_sha256(), label="clean" if clean else "defective",
            expected_verdict=("no_defect_found" if clean else "confirmed_existing_defect"
                              if name == "existing" else "confirmed_regression"),
            defect_family="clean" if clean else name,
            witness=None if clean else Witness(prices_cents=prices, discount_percent=discount,
                                                expected=expected, head=head),
            review_rationale=("Equivalent nonnegative integer half-up expression; "
                              "divmod quotient, residue threshold, or commuted multiplication."
                              if clean else None)))
    manifest = Manifest(version="checkout-dev-v2", manifest_sha256="", cases=cases,
                        sample=["per_line", "half_even", "item_count", "existing",
                                "clean_divmod", "clean_split"])
    manifest.manifest_sha256 = canonical_sha256(
        manifest.model_dump(exclude={"manifest_sha256"}))
    (ROOT / "manifest.json").write_text(json.dumps(manifest.model_dump(), indent=2) + "\n")


if __name__ == "__main__":
    main()
