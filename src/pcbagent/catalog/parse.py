"""Extract parametric data (value, tolerance, ratings, colour) from catalog rows.

JLCPCB descriptions look like
``100mW Thick Film Resistors 75V ±1% ±100ppm/℃ 10kΩ 0603 Chip Resistor - Surface Mount ROHS``
or ``50V 100nF X7R ±10% 0603 Multilayer Ceramic Capacitors MLCC - SMD/SMT ROHS``.
The jlcparts database also carries an ``attributes`` dict in its ``extra``
JSON, which is preferred when present.
"""

from __future__ import annotations

import json
import re

from ..units import UnitError, parse_value

_NUM = r"(\d+(?:\.\d+)?)"

_RES = re.compile(_NUM + r"\s*([mkKMG]?)\s*(?:Ω|ohm|Ohm|R\b)")
_CAP = re.compile(_NUM + r"\s*([pnuµμm]?)F\b")
_IND = re.compile(_NUM + r"\s*([nuµμm]?)H\b")
_VOLT = re.compile(r"(?<![\d.±])" + _NUM + r"\s*V\b")
_TOL = re.compile(r"±\s*" + _NUM + r"\s*%")
_POW = re.compile(r"(?:(\d+)/(\d+)|" + _NUM + r"\s*(m?))\s*W\b")
_CUR = re.compile(_NUM + r"\s*(m?)A\b")
_DIEL = re.compile(r"\b(X7R|X5R|X6S|X7S|X8R|C0G|NP0|NPO|Y5V|Z5U)\b", re.I)
_COLORS = ["red", "green", "blue", "white", "yellow", "orange", "amber", "warm white", "pink", "purple"]


def _scaled(num: str, prefix: str, unit: str) -> float | None:
    try:
        return parse_value(f"{num}{prefix}", None)
    except UnitError:
        return None


def classify(category: str, subcategory: str, description: str) -> str | None:
    """Map a catalog row to a PCBAgent kind, or None if irrelevant."""
    cat = f"{category} / {subcategory}".lower()
    desc = description.lower()
    if "resistor" in cat and ("chip" in cat or "surface mount" in cat or "smd" in cat) \
            and "array" not in cat and "network" not in cat and "varistor" not in cat \
            and "thermistor" not in cat and "potentiometer" not in cat:
        return "resistor"
    if ("ceramic" in cat or "mlcc" in cat) and "capacitor" in cat:
        return "capacitor"
    if "inductor" in cat and "common mode" not in cat and "bead" not in cat:
        return "inductor"
    if "light emitting" in cat or ("led" in cat.split() and "driver" not in cat) or "led indication" in cat:
        if "infrared" not in desc and "uv" not in desc.split():
            return "led"
    if "ldo" in cat or "linear voltage regulator" in cat or "low drop out" in cat:
        return "ldo"
    if "diode" in cat and "led" not in cat and "zener" not in cat and "tvs" not in cat:
        return "diode"
    if "bipolar" in cat or "bjt" in cat:
        return "transistor"
    if "mosfet" in cat:
        return "transistor"
    if "header" in cat or "pin header" in desc or "screw terminal" in cat or "wire to board" in cat:
        return "connector"
    return None


def _attr(attrs: dict, *names: str) -> str | None:
    for key, value in attrs.items():
        k = key.lower()
        if any(n in k for n in names) and isinstance(value, str) and value.strip() not in ("", "-"):
            return value
    return None


def attributes_from_extra(extra) -> dict:
    if not extra:
        return {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            return {}
    if isinstance(extra, dict):
        attrs = extra.get("attributes")
        if isinstance(attrs, dict):
            return attrs
    return {}


def parametric(kind: str, description: str, attrs: dict | None = None) -> dict:
    """Return {value, tolerance_pct, voltage_rating, power_rating, current_rating, dielectric, color}."""
    attrs = attrs or {}
    d = description or ""
    out: dict = {}

    def first(regex, text):
        m = regex.search(text)
        return m

    if kind == "resistor":
        text = _attr(attrs, "resistance") or d
        m = first(_RES, text)
        if m:
            out["value"] = _scaled(m.group(1), m.group(2), "ohm")
        elif re.search(r"\b0\s*Ω|\b0R\b|jumper", d, re.I):
            out["value"] = 0.0
        p = _attr(attrs, "power") or d
        m = _POW.search(p)
        if m:
            if m.group(1):
                out["power_rating"] = int(m.group(1)) / int(m.group(2))
            else:
                out["power_rating"] = float(m.group(3)) * (1e-3 if m.group(4) == "m" else 1.0)
    elif kind == "capacitor":
        text = _attr(attrs, "capacitance") or d
        m = first(_CAP, text)
        if m:
            out["value"] = _scaled(m.group(1), m.group(2), "F")
        v = _attr(attrs, "voltage rated", "rated voltage", "voltage") or d
        m = first(_VOLT, v)
        if m:
            out["voltage_rating"] = float(m.group(1))
        m = _DIEL.search(_attr(attrs, "temperature coefficient", "dielectric") or d)
        if m:
            out["dielectric"] = m.group(1).upper().replace("NPO", "C0G").replace("NP0", "C0G")
    elif kind == "inductor":
        text = _attr(attrs, "inductance") or d
        m = first(_IND, text)
        if m:
            out["value"] = _scaled(m.group(1), m.group(2), "H")
        c = _attr(attrs, "rated current", "current rating", "saturation current") or d
        m = first(_CUR, c)
        if m:
            out["current_rating"] = float(m.group(1)) * (1e-3 if m.group(2) == "m" else 1.0)
    elif kind == "led":
        text = (_attr(attrs, "color", "colour") or d).lower()
        for color in _COLORS:
            if re.search(rf"\b{color}\b", text):
                out["color"] = color.split()[-1] if color != "warm white" else "white"
                break
    tol = _attr(attrs, "tolerance") or d
    m = _TOL.search(tol)
    if m:
        out["tolerance_pct"] = float(m.group(1))
    return {k: v for k, v in out.items() if v is not None}


def normalize_package(pkg: str) -> str:
    """'0603' stays; 'R0603' / '0603(1608 Metric)' / 'LED_0603' -> '0603'; SOT-223-3 kept."""
    if not pkg:
        return ""
    p = pkg.strip()
    m = re.search(r"\b(0201|0402|0603|0805|1206|1210|1812|2010|2512)\b", p)
    if m and len(p) <= 24:
        return m.group(1)
    return p.upper().replace(" ", "")


def normalize_mpn(mpn: str) -> str:
    return re.sub(r"[\s]", "", (mpn or "").upper())


def price_breaks(raw) -> list[dict]:
    """Accept jlcparts JSON ([{"qFrom":1,"qTo":9,"price":0.01}]) or JLC CSV ("1-9:0.01,10-:0.008")."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, (int, float)):
        return [{"qFrom": 1, "qTo": None, "price": float(raw)}]
    if isinstance(raw, list):
        data = raw
    else:
        text = str(raw).strip()
        if text.startswith("["):
            try:
                data = json.loads(text)
            except ValueError:
                return []
        else:
            data = []
            for chunk in text.split(","):
                if ":" not in chunk:
                    continue
                rng, price = chunk.split(":", 1)
                lo, _, hi = rng.partition("-")
                try:
                    data.append({"qFrom": int(lo), "qTo": int(hi) if hi.strip() else None,
                                 "price": float(price)})
                except ValueError:
                    continue
            if not data:
                try:
                    return [{"qFrom": 1, "qTo": None, "price": float(text)}]
                except ValueError:
                    return []
    out = []
    for b in data:
        try:
            out.append({"qFrom": int(b.get("qFrom") or 1),
                        "qTo": (int(b["qTo"]) if b.get("qTo") not in (None, "", 0) else None),
                        "price": float(b["price"])})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda b: b["qFrom"])


def unit_price(breaks: list[dict], qty: int) -> float | None:
    if not breaks:
        return None
    chosen = breaks[0]
    for b in breaks:
        if b["qFrom"] <= qty:
            chosen = b
    return chosen["price"]
