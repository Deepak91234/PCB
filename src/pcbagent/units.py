"""Engineering-notation values: parsing, formatting and E-series helpers.

Notation follows EE convention, not SPICE: ``M`` is mega and ``m`` is milli.
``meg`` is accepted as mega too. RKM codes such as ``4k7`` and ``4R7`` work.
"""

from __future__ import annotations

import math
import re

_PREFIX = {
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
    "": 1.0,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "meg": 1e6,
    "G": 1e9,
}

_UNIT_WORDS = {
    "ohm": "ohm", "ohms": "ohm", "Ω": "ohm", "r": "ohm", "R": "ohm",
    "F": "F", "f": "F",
    "H": "H", "h": "H",
    "V": "V", "v": "V",
    "A": "A", "a": "A",
    "W": "W", "w": "W",
    "Hz": "Hz", "hz": "Hz",
    "s": "s",
}

_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_PLAIN = re.compile(
    rf"^\s*(?P<num>{_NUM})\s*(?P<prefix>meg|[fpnuµμmkKMG]?)\s*(?P<unit>ohms?|Ω|Hz|hz|[RFHhVvAaWws]?)\s*$"
)
# RKM code: 4k7, 4R7, 2u2, 1M5 (prefix letter acts as the decimal point).
_RKM = re.compile(r"^\s*(?P<a>\d+)(?P<p>[pnuµμmkKMGR])(?P<b>\d+)\s*(?P<unit>ohms?|Ω|[FHVAW]?)\s*$")


class UnitError(ValueError):
    pass


def parse_value(text: str | int | float, unit: str | None = None) -> float:
    """Parse ``text`` into a float in base SI units.

    ``unit`` is the expected unit (``ohm``, ``F``, ``H``, ``V``, ``A``, ``W``, ``Hz``).
    A conflicting unit suffix raises :class:`UnitError`.
    """
    if isinstance(text, bool):
        raise UnitError(f"not a value: {text!r}")
    if isinstance(text, (int, float)):
        if not math.isfinite(float(text)):
            raise UnitError(f"not a finite value: {text!r}")
        return float(text)
    s = str(text).strip().replace(" ", "")
    if not s:
        raise UnitError("empty value")
    m = _RKM.match(s)
    if m:
        p = m.group("p")
        mult = 1.0 if p == "R" else _PREFIX[p]
        value = float(f"{m.group('a')}.{m.group('b')}") * mult
        _check_unit(m.group("unit") or ("ohm" if p == "R" else ""), unit, text)
        return _clean(value)
    m = _PLAIN.match(s)
    if not m:
        raise UnitError(f"cannot parse value {text!r}")
    prefix = m.group("prefix")
    suffix = m.group("unit")
    # A lowercase "f" is always the femto prefix (the regex binds it there);
    # only an uppercase "F" means farad.
    value = float(m.group("num")) * _PREFIX[prefix]
    _check_unit(suffix, unit, text)
    return _clean(value)


def _clean(value: float) -> float:
    """Drop binary noise from prefix multiplication (100n -> 1e-07, not 1.0000000000000001e-07)."""
    return float(f"{value:.12g}")


def _check_unit(suffix: str, expected: str | None, original) -> None:
    if not suffix or expected is None:
        return
    got = _UNIT_WORDS.get(suffix)
    if got is None:
        raise UnitError(f"unknown unit in {original!r}")
    if got != expected:
        raise UnitError(f"{original!r} has unit {got}, expected {expected}")


_FMT_PREFIX = [
    (1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"),
    (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f"),
]

_UNIT_SYMBOL = {"ohm": "Ω", "F": "F", "H": "H", "V": "V", "A": "A", "W": "W", "Hz": "Hz", "s": "s", "": ""}


def format_value(value: float, unit: str = "", digits: int = 4) -> str:
    """Human-readable engineering notation, e.g. ``4.7kΩ`` or ``100nF``."""
    sym = _UNIT_SYMBOL.get(unit, unit)
    if value == 0:
        return f"0{sym}"
    a = abs(value)
    for mult, prefix in _FMT_PREFIX:
        if a >= mult * 0.9999999:
            scaled = value / mult
            text = f"{scaled:.{digits}g}"
            if "e" in text:
                text = f"{scaled:.{digits}f}".rstrip("0").rstrip(".")
            return f"{text}{prefix}{sym}"
    return f"{value:.{digits}g}{sym}"


def bom_value(value: float, unit: str) -> str:
    """Compact value for BOMs and silkscreen: ``10k``, ``4.7u``, ``100n``."""
    text = format_value(value, "", 3)
    return text.replace("µ", "u")


def spice_number(value: float) -> str:
    """Unambiguous SPICE literal (exponent form avoids the M/meg trap)."""
    if value == 0:
        return "0"
    return f"{value:.9g}"


# --- E-series -------------------------------------------------------------

E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]
E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
       3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1]
E96 = [1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30,
       1.33, 1.37, 1.40, 1.43, 1.47, 1.50, 1.54, 1.58, 1.62, 1.65, 1.69, 1.74,
       1.78, 1.82, 1.87, 1.91, 1.96, 2.00, 2.05, 2.10, 2.15, 2.21, 2.26, 2.32,
       2.37, 2.43, 2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87, 2.94, 3.01, 3.09,
       3.16, 3.24, 3.32, 3.40, 3.48, 3.57, 3.65, 3.74, 3.83, 3.92, 4.02, 4.12,
       4.22, 4.32, 4.42, 4.53, 4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49,
       5.62, 5.76, 5.90, 6.04, 6.19, 6.34, 6.49, 6.65, 6.81, 6.98, 7.15, 7.32,
       7.50, 7.68, 7.87, 8.06, 8.25, 8.45, 8.66, 8.87, 9.09, 9.31, 9.53, 9.76]
E6 = [1.0, 1.5, 2.2, 3.3, 4.7, 6.8]

SERIES = {"E6": E6, "E12": E12, "E24": E24, "E96": E96}


def _decade(value: float) -> tuple[float, int]:
    exp = math.floor(math.log10(value))
    return value / 10**exp, exp


def nearest_in_series(value: float, series: str = "E24") -> float:
    if value <= 0:
        raise UnitError("E-series values must be positive")
    mant, exp = _decade(value)
    table = SERIES[series] + [10.0]
    best = min(table, key=lambda t: abs(math.log(t / mant)))
    return round(best * 10**exp, 12)


def in_series(value: float, series: str = "E24", rel_tol: float = 1e-3) -> bool:
    if value <= 0:
        return False
    return math.isclose(nearest_in_series(value, series), value, rel_tol=rel_tol)


def close(a: float, b: float, rel: float = 1e-3) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=1e-18)
