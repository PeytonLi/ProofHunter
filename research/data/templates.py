# ruff: noqa: RUF005, E501
"""Reviewed, deterministic, parameterized ``checkout.py`` variant templates (Track A).

Every rendered source defines ``total_cents(prices_cents, discount_percent)`` for the
``checkout-rounding@1`` contract. A template is ``<family>__<shape>``: a *shape* fixes how a
correct implementation is written (rounding helper style, subtotal style, numerator form) and a
*family* is one reviewed defect operator (or ``clean``) applied to it. Cosmetic params (names,
docstring, empty guard) vary the text without changing behavior. Rendering only formats strings;
nothing here executes generated code.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from research.schemas.models import Split

ParamValue = int | bool | str | list[int]
Params = Mapping[str, ParamValue]

TEMPLATE_REGISTRY_VERSION: Final = "checkout-templates-v1"
TEMPLATE_VERSION: Final = "1"
ROUND_OK: Final = 50


@dataclass(frozen=True)
class Shape:
    shape_id: str
    split: Split
    rounding: Literal["addfloor", "divmod", "float", "fraction", "decimal", "splitmod"]
    subtotal: Literal["sum", "loop", "reduce", "while", "listcomp"]
    numerator: Literal["mul", "commuted", "split"]


SHAPES: Final[tuple[Shape, ...]] = (
    Shape("s01", Split.TRAIN, "addfloor", "sum", "mul"),
    Shape("s02", Split.TRAIN, "divmod", "loop", "mul"),
    Shape("s03", Split.TRAIN, "float", "sum", "commuted"),
    Shape("s04", Split.TRAIN, "fraction", "reduce", "mul"),
    Shape("s05", Split.TRAIN, "splitmod", "while", "split"),
    Shape("s06", Split.TRAIN, "addfloor", "loop", "split"),
    Shape("s07", Split.TRAIN, "decimal", "listcomp", "mul"),
    Shape("s08", Split.VALIDATION, "divmod", "reduce", "commuted"),
    Shape("s09", Split.VALIDATION, "addfloor", "while", "commuted"),
    Shape("s10", Split.TEST, "fraction", "loop", "split"),
    Shape("s11", Split.TEST, "decimal", "while", "commuted"),
    Shape("s12", Split.TEST, "float", "reduce", "split"),
)
SHAPE_BY_ID: Final = {s.shape_id: s for s in SHAPES}
SUBTOTALS: Final = ("sum", "loop", "reduce", "while", "listcomp")
NUMERATORS: Final = ("mul", "commuted", "split")

# (items, discount, subtotal, total, helper)
NAMES: Final = (
    ("items", "d", "subtotal", "total", "_round_half_up"),
    ("lines", "pct", "gross", "result", "_round_cents"),
    ("basket", "off", "amount", "cents", "_half_up"),
)
DOCS: Final = (
    "Checkout totals: discount the subtotal once, round half-up to whole cents.",
    "Compute the discounted checkout total in cents.",
    "Checkout pricing for checkout-rounding@1.",
)
ALT_HELPER: Final = "_round_bulk"

# ---------------------------------------------------------------------------
# Defect families. point: where the operator is injected. cond: allowed condition set.
# ---------------------------------------------------------------------------

CondSet = Literal["none", "ld", "all"]


@dataclass(frozen=True)
class Family:
    family_id: str
    split: Split
    point: Literal["items", "discount", "round", "post"]
    cond: CondSet
    values: tuple[ParamValue, ...]
    description: str


def _r(a: int, b: int, step: int = 1) -> tuple[int, ...]:
    return tuple(range(a, b, step))


FAMILIES: Final[tuple[Family, ...]] = (
    # train (14)
    Family("per_line_rounding", Split.TRAIN, "round", "all", (1,),
           "discount and round each line separately, then sum"),
    Family("floor_rounding", Split.TRAIN, "round", "all", (0,), "truncate instead of half-up"),
    Family("half_down_rounding", Split.TRAIN, "round", "all", (51,), "round half-cent ties down"),
    Family("offset_low_rounding", Split.TRAIN, "round", "all", _r(1, 50),
           "rounding threshold below the half cent"),
    Family("discount_clamp_max", Split.TRAIN, "discount", "ld", _r(1, 100),
           "discount silently capped"),
    Family("discount_off_by_one", Split.TRAIN, "discount", "ld", (1, -1),
           "discount shifted by one point"),
    Family("discount_ignored", Split.TRAIN, "discount", "ld", (0,), "discount dropped"),
    Family("bulk_discount_extra", Split.TRAIN, "discount", "ld", (2, 3, 5, 10, 15, 20, 25),
           "unspecified extra discount"),
    Family("price_cap", Split.TRAIN, "items", "ld",
           (100, 250, 500, 999, 1000, 2000, 2500, 5000, 7500, 9999), "line prices capped"),
    Family("small_price_dropped", Split.TRAIN, "items", "ld", (1, 2, 5, 10, 25, 50, 100, 500),
           "cheap lines dropped"),
    Family("dedupe_prices", Split.TRAIN, "items", "ld", (0,), "equal-priced lines merged"),
    Family("empty_basket_charge", Split.TRAIN, "post", "none", _r(1, 100),
           "empty basket charged"),
    Family("min_charge", Split.TRAIN, "post", "all", (1, 5, 10, 25, 50, 99, 100, 199, 250, 500),
           "minimum charge applied"),
    Family("full_discount_special", Split.TRAIN, "post", "none", _r(1, 51) + (-1,),
           "full discount special-cased (-1 means undiscounted subtotal)"),
    # validation (4)
    Family("ceil_rounding", Split.VALIDATION, "round", "all", (99,), "round every fraction up"),
    Family("discount_floor_min", Split.VALIDATION, "discount", "ld", _r(1, 100),
           "minimum discount forced"),
    Family("skip_index", Split.VALIDATION, "items", "ld", _r(0, 8), "one line index skipped"),
    Family("drop_max_item", Split.VALIDATION, "items", "ld", (0,), "most expensive line dropped"),
    # test (6)
    Family("half_even_rounding", Split.TEST, "round", "all", (-2,), "banker's rounding"),
    Family("offset_high_rounding", Split.TEST, "round", "all", _r(52, 99),
           "rounding threshold above the half cent"),
    Family("discount_step", Split.TEST, "discount", "ld", (2, 3, 4, 5, 10, 20, 25, 50),
           "discount truncated to a step"),
    Family("item_cap", Split.TEST, "items", "ld", _r(1, 8), "only the first k lines counted"),
    Family("total_cap", Split.TEST, "post", "none", _r(50, 80001, 150), "total capped"),
    Family("subtotal_residue_glitch", Split.TEST, "post", "none",
           tuple(m * 1000 + r * 10 + s for m in (7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47)
                 for r in range(3) for s in (0, 1)),
           "off-by-one cent when subtotal % m == r (value = m*1000 + r*10 + sign)"),
)
FAMILY_BY_ID: Final = {f.family_id: f for f in FAMILIES}

CONDS_LD: Final[tuple[tuple[str, int], ...]] = (
    (("always", 0),)
    + tuple(("min_items", k) for k in range(2, 9))
    + tuple(("max_items", k) for k in range(1, 8))
    + tuple(("disc_ge", v) for v in (10, 25, 50, 75, 90, 99))
    + tuple(("disc_le", v) for v in (5, 10, 25, 50, 75))
    + tuple(("disc_eq", v) for v in (10, 15, 20, 25, 33, 50, 100))
)
CONDS_ALL: Final = (
    CONDS_LD
    + tuple(("sub_ge", s) for s in (100, 500, 1000, 2500, 5000, 10000, 20000, 40000))
    + tuple(("sub_lt", s) for s in (100, 1000, 5000, 20000))
)
COND_SETS: Final = {"none": (("always", 0),), "ld": CONDS_LD, "all": CONDS_ALL}


@dataclass(frozen=True)
class TemplateInfo:
    template_id: str
    template_version: str
    template_sha256: str
    family_id: str
    kind: Literal["defect", "clean"]
    split: Split
    shape_id: str


def _module_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def template_sha256(template_id: str) -> str:
    text = f"{TEMPLATE_REGISTRY_VERSION}\n{template_id}\n{_module_sha256()}"
    return hashlib.sha256(text.encode()).hexdigest()


def clean_family_id(shape_id: str) -> str:
    return f"clean_{shape_id}"


def list_templates() -> Sequence[TemplateInfo]:
    out: list[TemplateInfo] = []
    for fam in FAMILIES:
        for shape in SHAPES:
            if shape.split is fam.split:
                tid = f"{fam.family_id}__{shape.shape_id}"
                out.append(TemplateInfo(tid, TEMPLATE_VERSION, template_sha256(tid),
                                        fam.family_id, "defect", fam.split, shape.shape_id))
    for shape in SHAPES:
        tid = f"clean__{shape.shape_id}"
        out.append(TemplateInfo(tid, TEMPLATE_VERSION, template_sha256(tid),
                                clean_family_id(shape.shape_id), "clean", shape.split,
                                shape.shape_id))
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Style:
    names: int = 0
    doc: int = 0
    guard: int = 0
    subtotal: str | None = None
    numerator: str | None = None


def _helper_body(style: str, mode: int) -> list[str]:
    """mode in 0..99 = threshold offset (50 is correct half-up); -2 = half-even."""
    if mode == -2:
        return {
            "addfloor": ["q, r = divmod(n, 100)",
                         "return q + (1 if r > 50 or (r == 50 and q % 2 == 1) else 0)"],
            "divmod": ["q, r = divmod(n, 100)", "if r > 50 or (r == 50 and q % 2 == 1):",
                       "    q += 1", "return q"],
            "float": ["return round(n / 100)"],
            "fraction": ["return round(Fraction(n, 100))"],
            "decimal": ["return int((Decimal(n) / 100).to_integral_value(rounding=ROUND_HALF_EVEN))"],
            "splitmod": ["q, r = n // 100, n % 100",
                         "return q + (1 if r > 50 or (r == 50 and q % 2 == 1) else 0)"],
        }[style]
    if not 0 <= mode <= 99:
        raise ValueError("rounding offset out of range")
    t = mode
    if style == "decimal":
        named = {50: "ROUND_HALF_UP", 0: "ROUND_FLOOR", 99: "ROUND_CEILING", 51: "ROUND_HALF_DOWN"}
        if t in named:
            return [f"return int((Decimal(n) / 100).to_integral_value(rounding={named[t]}))"]
        return [f"return int((Decimal(n + {t}) / 100).to_integral_value(rounding=ROUND_FLOOR))"]
    return {
        "addfloor": [f"return (n + {t}) // 100"],
        "divmod": ["q, r = divmod(n, 100)", f"if r + {t} >= 100:", "    q += 1", "return q"],
        "float": [f"return int((n + {t}) / 100)"],
        "fraction": [f"return math.floor(Fraction(n + {t}, 100))"],
        "splitmod": [f"return n // 100 + (1 if n % 100 + {t} >= 100 else 0)"],
    }[style]


def _cond_expr(kind: str, value: int, n: Mapping[str, str]) -> str | None:
    i, d, s = n["i"], n["d"], n["s"]
    return {
        "always": None,
        "min_items": f"len({i}) >= {value}",
        "max_items": f"len({i}) <= {value}",
        "disc_ge": f"{d} >= {value}",
        "disc_le": f"{d} <= {value}",
        "disc_eq": f"{d} == {value}",
        "sub_ge": f"{s} >= {value}",
        "sub_lt": f"{s} < {value}",
    }[kind]


def _guarded(cond: str | None, body: list[str]) -> list[str]:
    if cond is None:
        return body
    return [f"if {cond}:"] + ["    " + line for line in body]


def _defect_lines(fam: Family, value: int, n: Mapping[str, str], numer: str) -> list[str]:
    i, d, s, t, h = n["i"], n["d"], n["s"], n["t"], n["h"]
    fid = fam.family_id
    if fid == "discount_clamp_max":
        return [f"{d} = min({d}, {value})"]
    if fid == "discount_floor_min":
        return [f"{d} = max({d}, {value})"]
    if fid == "discount_off_by_one":
        return [f"{d} = min(100, {d} + 1)"] if value == 1 else [f"{d} = max(0, {d} - 1)"]
    if fid == "discount_ignored":
        return [f"{d} = 0"]
    if fid == "bulk_discount_extra":
        return [f"{d} = min(100, {d} + {value})"]
    if fid == "discount_step":
        return [f"{d} = {d} // {value} * {value}"]
    if fid == "price_cap":
        return [f"{i} = [min(price, {value}) for price in {i}]"]
    if fid == "small_price_dropped":
        return [f"{i} = [price for price in {i} if price >= {value}]"]
    if fid == "dedupe_prices":
        return [f"{i} = list(dict.fromkeys({i}))"]
    if fid == "skip_index":
        return [f"{i} = {i}[:{value}] + {i}[{value} + 1:]"]
    if fid == "drop_max_item":
        return [f"if {i}:", f"    {i}.remove(max({i}))"]
    if fid == "item_cap":
        return [f"{i} = {i}[:{value}]"]
    if fid == "per_line_rounding":
        return [f"{t} = sum({h}(price * (100 - {d})) for price in {i})"]
    if fam.point == "round":
        return [f"{t} = {ALT_HELPER}({numer})"]
    if fid == "empty_basket_charge":
        return [f"if not {i}:", f"    {t} = {value}"]
    if fid == "min_charge":
        return [f"{t} = max({t}, {value})"]
    if fid == "full_discount_special":
        rhs = s if value == -1 else str(value)
        return [f"if {d} == 100:", f"    {t} = {rhs}"]
    if fid == "total_cap":
        return [f"{t} = min({t}, {value})"]
    if fid == "subtotal_residue_glitch":
        m, r, sign = value // 1000, value // 10 % 100, value % 10
        step = [f"    {t} += 1"] if sign == 0 else [f"    {t} = max(0, {t} - 1)"]
        return [f"if {s} % {m} == {r}:"] + step
    raise ValueError(f"unknown family {fid}")


def _subtotal_lines(kind: str, i: str, s: str) -> list[str]:
    return {
        "sum": [f"{s} = sum({i})"],
        "loop": [f"{s} = 0", f"for price in {i}:", f"    {s} += price"],
        "reduce": [f"{s} = reduce(operator.add, {i}, 0)"],
        "while": [f"{s} = 0", "index = 0", f"while index < len({i}):", f"    {s} += {i}[index]",
                  "    index += 1"],
        "listcomp": [f"{s} = sum([price for price in {i}])"],
    }[kind]


def _numerator(kind: str, s: str, d: str) -> str:
    return {"mul": f"{s} * (100 - {d})", "commuted": f"(100 - {d}) * {s}",
            "split": f"{s} * 100 - {s} * {d}"}[kind]


def _source(shape: Shape, style: Style, defect: tuple[Family, int, str, int] | None) -> bytes:
    names = NAMES[style.names]
    n = dict(zip(("i", "d", "s", "t", "h"), names, strict=True))
    subtotal = style.subtotal or shape.subtotal
    numer = _numerator(style.numerator or shape.numerator, n["s"], n["d"])
    fam = defect[0] if defect else None
    helper_mode = ROUND_OK
    inject: dict[str, list[str]] = {}
    alt_mode: int | None = None
    if defect is not None:
        fam, value, cond_kind, cond_value = defect
        cond = _cond_expr(cond_kind, cond_value, n)
        if fam.point == "round" and fam.family_id != "per_line_rounding" and cond is None:
            helper_mode = value
        else:
            if fam.point == "round" and fam.family_id != "per_line_rounding":
                alt_mode = value
            inject[fam.point] = _guarded(cond, _defect_lines(fam, value, n, numer))
    body: list[str] = []
    if style.guard:
        body += ["if not prices_cents:", "    return 0"]
    body += [f"{n['i']} = list(prices_cents)", f"{n['d']} = discount_percent"]
    body += inject.get("items", []) + inject.get("discount", [])
    body += _subtotal_lines(subtotal, n["i"], n["s"])
    body += [f"{n['t']} = {n['h']}({numer})"]
    body += inject.get("round", []) + inject.get("post", [])
    body += [f"return {n['t']}"]
    helpers = [(n["h"], helper_mode)] + ([(ALT_HELPER, alt_mode)] if alt_mode is not None else [])
    funcs: list[str] = []
    for name, mode in helpers:
        funcs += ["", "", f"def {name}(n: int) -> int:"]
        funcs += ["    " + x for x in _helper_body(shape.rounding, mode)]
    funcs += ["", "", "def total_cents(prices_cents: list[int], discount_percent: int) -> int:"]
    funcs += ["    " + x for x in body]
    code = "\n".join(funcs)
    imports: list[str] = []
    if "math.floor" in code:
        imports.append("import math")
    if "operator.add" in code:
        imports.append("import operator")
    decimal_names = sorted({w for w in ("ROUND_CEILING", "ROUND_FLOOR", "ROUND_HALF_DOWN",
                                        "ROUND_HALF_EVEN", "ROUND_HALF_UP") if w in code})
    if "Decimal(" in code:
        imports.append(f"from decimal import {', '.join(decimal_names + ['Decimal'])}")
    if "Fraction(" in code:
        imports.append("from fractions import Fraction")
    if "reduce(" in code:
        imports.append("from functools import reduce")
    head = [f'"""{DOCS[style.doc]}"""']
    if imports:
        head += [""] + sorted(i for i in imports if i.startswith("import")) + sorted(
            i for i in imports if i.startswith("from"))
    return ("\n".join(head) + "\n" + code + "\n").encode()


def _int(params: Params, key: str, default: int | None = None) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"param {key} must be an int")
    return value


def _style(params: Params, prefix: str) -> Style:
    sub = params.get(prefix + "subtotal")
    num = params.get(prefix + "numerator")
    if sub is not None and sub not in SUBTOTALS:
        raise ValueError("bad subtotal style")
    if num is not None and num not in NUMERATORS:
        raise ValueError("bad numerator style")
    style = Style(_int(params, prefix + "names", 0), _int(params, prefix + "doc", 0),
                  _int(params, prefix + "guard", 0), sub if isinstance(sub, str) else None,
                  num if isinstance(num, str) else None)
    if not (0 <= style.names < len(NAMES) and 0 <= style.doc < len(DOCS) and style.guard in (0, 1)):
        raise ValueError("cosmetic param out of range")
    return style


def render(template_id: str, params: Params) -> tuple[bytes, bytes]:
    """Return ``(base_source, head_source)`` for one template instance.

    Defect params: ``value``, ``cond``, ``cond_value``, cosmetic ``names``/``doc``/``guard`` and
    ``kind`` (``introduced`` or ``existing``; existing renders the defect on both sides with the
    ``head_*`` cosmetic params on the head). Clean params: base cosmetics plus ``head_*``
    cosmetics and optional ``head_subtotal``/``head_numerator`` refactors.
    """
    family_id, sep, shape_id = template_id.partition("__")
    if not sep or shape_id not in SHAPE_BY_ID:
        raise ValueError(f"unknown template {template_id}")
    shape = SHAPE_BY_ID[shape_id]
    base_style = _style(params, "")
    head_style = _style(params, "head_")
    if family_id == "clean":
        return _source(shape, base_style, None), _source(shape, head_style, None)
    fam = FAMILY_BY_ID.get(family_id)
    if fam is None or fam.split is not shape.split:
        raise ValueError(f"unknown template {template_id}")
    value = _int(params, "value")
    cond_kind = params.get("cond", "always")
    cond_value = _int(params, "cond_value", 0)
    if value not in fam.values or (cond_kind, cond_value) not in COND_SETS[fam.cond]:
        raise ValueError("param outside the reviewed template domain")
    defect = (fam, value, str(cond_kind), cond_value)
    kind = params.get("kind", "introduced")
    if kind == "introduced":
        return _source(shape, base_style, None), _source(shape, base_style, defect)
    if kind == "existing":
        return _source(shape, base_style, defect), _source(shape, head_style, defect)
    raise ValueError("bad kind")


def iter_defect_params(fam: Family) -> Iterator[dict[str, ParamValue]]:
    for value in fam.values:
        for cond_kind, cond_value in COND_SETS[fam.cond]:
            yield {"value": value, "cond": cond_kind, "cond_value": cond_value}
