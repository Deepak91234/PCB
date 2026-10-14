"""Circuit specification: what the board must do, as checkable numbers.

The spec is written once and then locked by hash. Verification only ever
compares simulator output against the locked spec, so a failing design cannot
pass by loosening its own requirements.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

from . import schemas
from .validate import validate


class SpecError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def canonical_json(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(data) -> str:
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


@dataclass
class Bounds:
    lo: float | None
    hi: float | None
    target: float | None

    def contains(self, x: float) -> bool:
        if self.lo is not None and x < self.lo:
            return False
        if self.hi is not None and x > self.hi:
            return False
        return True

    def describe(self, unit: str) -> str:
        from .units import format_value
        if self.lo is not None and self.hi is not None:
            if self.target is not None:
                return f"{format_value(self.target, unit)} ({format_value(self.lo, unit)} to {format_value(self.hi, unit)})"
            return f"{format_value(self.lo, unit)} to {format_value(self.hi, unit)}"
        if self.lo is not None:
            return f">= {format_value(self.lo, unit)}"
        return f"<= {format_value(self.hi, unit)}"


def requirement_bounds(req: dict) -> Bounds:
    lo = req.get("min")
    hi = req.get("max")
    target = req.get("target")
    tol = req.get("tolerance")
    if target is not None and tol:
        delta = abs(target) * tol["pct"] / 100 if "pct" in tol else tol["abs"]
        lo = target - delta if lo is None else max(lo, target - delta)
        hi = target + delta if hi is None else min(hi, target + delta)
    return Bounds(lo, hi, target)


def measure_unit(measure: dict) -> str:
    return schemas.MEASURE_UNITS[measure["type"]]


def normalize(raw: dict) -> dict:
    """Validate and return a normalized copy (defaults filled in). Raises SpecError."""
    if not isinstance(raw, dict):
        raise SpecError(["spec must be a JSON object"])
    errors = validate(raw, schemas.SPEC_SCHEMA, "spec")
    if errors:
        raise SpecError(errors)
    spec = copy.deepcopy(raw)
    spec["schema"] = "pcbagent/spec@1"
    spec.setdefault("ground", "GND")
    spec.setdefault("summary", "")
    spec.setdefault("preferences", {})
    for cond in spec["conditions"]:
        cond.setdefault("loads", [])
    errors = _semantic_errors(spec)
    if errors:
        raise SpecError(errors)
    return spec


def _semantic_errors(spec: dict) -> list[str]:
    errors: list[str] = []
    ground = spec["ground"]
    port_nets = [p["net"] for p in spec["ports"]]
    if len(set(port_nets)) != len(port_nets):
        errors.append("spec.ports: each net may appear only once")
    if ground not in port_nets:
        errors.append(f"spec.ports: the ground net '{ground}' must be listed as a port with role 'ground'")
    for p in spec["ports"]:
        if p["net"] == ground and p["role"] != "ground":
            errors.append(f"spec.ports: '{ground}' must have role 'ground'")
        if p["role"] == "ground" and p["net"] != ground:
            errors.append(f"spec.ports: '{p['net']}' has role 'ground' but spec.ground is '{ground}'")
    ports = set(port_nets)

    cond_ids = [c["id"] for c in spec["conditions"]]
    if len(set(cond_ids)) != len(cond_ids):
        errors.append("spec.conditions: ids must be unique")
    conds = {c["id"]: c for c in spec["conditions"]}
    for i, cond in enumerate(spec["conditions"]):
        where = f"spec.conditions[{i}] ({cond['id']})"
        seen = set()
        for s in cond["sources"]:
            if ("volts" in s) == ("amps" in s):
                errors.append(f"{where}: each source needs exactly one of 'volts' or 'amps'")
            if s["net"] not in ports:
                errors.append(f"{where}: source net '{s['net']}' is not a declared port")
            if s["net"] == ground:
                errors.append(f"{where}: cannot drive the ground net")
            if s["net"] in seen:
                errors.append(f"{where}: two sources on '{s['net']}'")
            seen.add(s["net"])
        for ld in cond["loads"]:
            if ("ohms" in ld) == ("amps" in ld):
                errors.append(f"{where}: each load needs exactly one of 'ohms' or 'amps'")
            if ld["net"] not in ports:
                errors.append(f"{where}: load net '{ld['net']}' is not a declared port")
            if ld["net"] == ground:
                errors.append(f"{where}: a load on the ground net does nothing")

    req_ids = [r["id"] for r in spec["requirements"]]
    if len(set(req_ids)) != len(req_ids):
        errors.append("spec.requirements: ids must be unique")
    for i, req in enumerate(spec["requirements"]):
        where = f"spec.requirements[{i}] ({req['id']})"
        cond = conds.get(req["condition"])
        if cond is None:
            errors.append(f"{where}: unknown condition '{req['condition']}' (have: {', '.join(cond_ids)})")
            continue
        has_bound = "min" in req or "max" in req or ("target" in req and "tolerance" in req)
        if not has_bound:
            errors.append(f"{where}: give target+tolerance, or min and/or max")
        if "tolerance" in req and "target" not in req:
            errors.append(f"{where}: tolerance needs a target")
        tol = req.get("tolerance")
        if tol is not None and len(tol) != 1:
            errors.append(f"{where}: tolerance takes exactly one of 'pct' or 'abs'")
        if "min" in req and "max" in req and req["min"] > req["max"]:
            errors.append(f"{where}: min is greater than max")
        errors.extend(_measure_errors(where, req["measure"], req["condition"], cond, conds, ports, ground))
    return errors


_NEEDS = {
    "voltage": ["net"],
    "source_current": ["net"],
    "source_power": ["net"],
    "load_power": ["net"],
    "efficiency": [],
    "component_current": ["ref", "pin"],
    "component_power": ["ref"],
    "voltage_change": ["net", "from_condition"],
    "gain_db": ["input", "output", "hz"],
    "cutoff_hz": ["input", "output"],
}


def _measure_errors(where, m, cond_id, cond, conds, ports, ground) -> list[str]:
    errors = []
    t = m["type"]
    for key in _NEEDS[t]:
        if key not in m:
            errors.append(f"{where}.measure: type '{t}' needs '{key}'")
    allowed = set(_NEEDS[t]) | {"type"}
    for key in m:
        if key not in allowed:
            errors.append(f"{where}.measure: '{key}' does not apply to type '{t}'")
    if errors:
        return errors
    for key in ("net", "input", "output"):
        if key in m and m[key] not in ports:
            errors.append(f"{where}.measure: '{m[key]}' is not a declared port")
    if t in ("source_current", "source_power"):
        if not any(s["net"] == m["net"] for s in cond["sources"]):
            errors.append(f"{where}.measure: condition '{cond_id}' has no source on '{m['net']}'")
    if t == "load_power":
        if not any(ld["net"] == m["net"] for ld in cond["loads"]):
            errors.append(f"{where}.measure: condition '{cond_id}' has no load on '{m['net']}'")
    if t == "efficiency" and not cond["loads"]:
        errors.append(f"{where}.measure: efficiency needs at least one load in '{cond_id}'")
    if t == "voltage_change":
        if m["from_condition"] not in conds:
            errors.append(f"{where}.measure: unknown from_condition '{m['from_condition']}'")
        elif m["from_condition"] == cond_id:
            errors.append(f"{where}.measure: from_condition must differ from the requirement's condition")
    if t in ("gain_db", "cutoff_hz"):
        src = next((s for s in cond["sources"] if s["net"] == m["input"]), None)
        if src is None or "volts" not in src:
            errors.append(f"{where}.measure: AC input '{m['input']}' needs a voltage source in '{cond_id}'")
        if m["output"] == ground:
            errors.append(f"{where}.measure: AC output cannot be ground")
    return errors


def referenced_refs(spec: dict) -> dict[str, list[str]]:
    """Component refs the spec measures directly, with the requirement ids using them."""
    out: dict[str, list[str]] = {}
    for req in spec["requirements"]:
        ref = req["measure"].get("ref")
        if ref:
            out.setdefault(ref, []).append(req["id"])
    return out
