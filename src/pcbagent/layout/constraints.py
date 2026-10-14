"""Placement constraints: validation plus constraints derived from the design.

The model supplies intent ("C1 next to U1's input", "J1 on the left edge").
Some intent follows from the circuit alone, so it is added automatically and
the layout does not depend on the model remembering it:

* every capacitor named by an IC's datasheet capacitance rule sits within
  3 mm of that IC pin,
* connectors and test points go on a board edge.

Model constraints override automatic ones for the same pair.
"""

from __future__ import annotations

import copy

from .. import schemas
from ..circuit import Circuit
from ..validate import validate


class ConstraintError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


_NEEDS = {
    "near": ["ref", "to"],
    "edge": ["ref"],
    "group": ["refs"],
    "flow": ["refs"],
    "align": ["refs"],
    "apart": ["ref", "to", "min_mm"],
}


def normalize(raw: dict, circuit: Circuit) -> dict:
    if not isinstance(raw, dict):
        raise ConstraintError(["constraints must be a JSON object"])
    errors = validate(raw, schemas.CONSTRAINTS_SCHEMA, "layout")
    if errors:
        raise ConstraintError(errors)
    data = copy.deepcopy(raw)
    refs = {c.ref for c in circuit.components}
    for i, c in enumerate(data["constraints"]):
        where = f"layout.constraints[{i}] ({c['type']})"
        for key in _NEEDS[c["type"]]:
            if key not in c:
                errors.append(f"{where}: needs '{key}'")
        for key in ("ref", "to"):
            if key in c and c[key] not in refs:
                errors.append(f"{where}: no component {c[key]} in the circuit")
        for r in c.get("refs", []):
            if r not in refs:
                errors.append(f"{where}: no component {r} in the circuit")
        if c["type"] == "near":
            c.setdefault("max_mm", 3.0)
            if "pin" in c and c.get("to") in refs:
                comp = circuit.by_ref(c["to"])
                if c["pin"] not in comp.pins:
                    errors.append(f"{where}: {c['to']} has no connected pin '{c['pin']}' "
                                  f"(pins: {', '.join(comp.pins)})")
                else:
                    c["pad"] = comp.model["kicad"]["pins"][c["pin"]]
        if c["type"] == "edge":
            c.setdefault("side", "any")
        if c["type"] in ("flow", "align"):
            c.setdefault("axis", "x")
        if c["type"] == "group":
            c.setdefault("max_mm", 10.0)
    if errors:
        raise ConstraintError(errors)
    data.setdefault("board", {})
    data.setdefault("pour_ground", True)
    return data


def automatic(circuit: Circuit, ground: str = "GND") -> list[dict]:
    out: list[dict] = []
    nets = circuit.nets()
    for c in circuit.components:
        for rule in c.model.get("rules", []):
            if rule["type"] != "min_capacitance":
                continue
            net = c.pins.get(rule["pin"])
            if not net:
                continue
            for ref, _pin in nets.get(net, []):
                other = circuit.by_ref(ref)
                if other.kind == "capacitor" and set(other.pins.values()) == {net, ground}:
                    out.append({"type": "near", "ref": ref, "to": c.ref, "pin": rule["pin"],
                                "pad": c.model["kicad"]["pins"][rule["pin"]], "max_mm": 3.0,
                                "reason": f"datasheet capacitor for {c.ref}.{rule['pin']}", "auto": True})
    for c in circuit.components:
        if c.kind in ("connector",):
            out.append({"type": "edge", "ref": c.ref, "side": "any", "reason": "connector on board edge",
                        "auto": True})
    return out


def merge(model_constraints: list[dict], auto: list[dict]) -> list[dict]:
    def key(c):
        if c["type"] in ("near", "apart"):
            return (c["type"], c["ref"], c["to"])
        if c["type"] == "edge":
            return ("edge", c["ref"])
        return None
    seen = {key(c) for c in model_constraints if key(c)}
    return list(model_constraints) + [c for c in auto if key(c) not in seen]
