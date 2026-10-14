"""Circuit IR: components (by part model) and the nets their pins connect to.

The model writes this JSON instead of code. That removes a class of failures
(syntax errors, library bugs, arbitrary code execution) and lets every error
message point at the exact component and pin to fix.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass, field

from . import models as models_mod
from . import schemas
from .units import UnitError, format_value, parse_value
from .validate import validate


class CircuitError(ValueError):
    def __init__(self, errors: list[str], warnings: list[str] | None = None):
        super().__init__("; ".join(errors))
        self.errors = errors
        self.warnings = warnings or []


@dataclass
class Component:
    ref: str
    part: str
    model: dict
    pins: dict[str, str]
    value: float | None = None
    package: str | None = None
    note: str = ""

    @property
    def kind(self) -> str:
        return self.model["kind"]

    def value_text(self) -> str:
        if self.value is None:
            return self.model["id"]
        return format_value(self.value, self.model["value"]["unit"], 3)

    def footprint(self, default_package: str | None = None) -> str:
        kicad = self.model["kicad"]
        if "footprint" in kicad:
            return kicad["footprint"]
        pkg = self.package or default_package or kicad["default_package"]
        return kicad["footprints"][pkg]

    def to_json(self) -> dict:
        out = {"ref": self.ref, "part": self.part, "pins": dict(self.pins)}
        if self.value is not None:
            out["value"] = self.value_text()
        if self.package:
            out["package"] = self.package
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class Circuit:
    name: str
    description: str
    components: list[Component]
    raw: dict
    warnings: list[str] = field(default_factory=list)

    def by_ref(self, ref: str) -> Component | None:
        for c in self.components:
            if c.ref == ref:
                return c
        return None

    def nets(self) -> dict[str, list[tuple[str, str]]]:
        """net -> [(ref, pin), ...] in component order."""
        out: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for c in self.components:
            for pin, net in c.pins.items():
                out[net].append((c.ref, pin))
        return dict(out)


def load(raw: dict, spec: dict | None = None) -> Circuit:
    """Validate raw circuit JSON (optionally against a spec). Raises CircuitError."""
    if not isinstance(raw, dict):
        raise CircuitError(["circuit must be a JSON object"])
    errors = validate(raw, schemas.CIRCUIT_SCHEMA, "circuit")
    if errors:
        raise CircuitError(errors)
    warnings: list[str] = []
    comps: list[Component] = []
    seen_refs: set[str] = set()
    prefs = (spec or {}).get("preferences", {})
    for i, rc in enumerate(raw["components"]):
        where = f"circuit.components[{i}] ({rc['ref']})"
        if rc["ref"] in seen_refs:
            errors.append(f"{where}: duplicate ref {rc['ref']}")
            continue
        seen_refs.add(rc["ref"])
        model = models_mod.get_model(rc["part"])
        if model is None:
            errors.append(f"{where}: unknown part '{rc['part']}'. Call pcb_models to list the parts you can use.")
            continue
        comp = Component(ref=rc["ref"], part=model["id"], model=model, pins=dict(rc["pins"]),
                         note=rc.get("note", ""))
        errors.extend(_check_pins(where, comp))
        errors.extend(_check_value(where, comp, rc, prefs))
        prefix = model.get("ref_prefix")
        letters = "".join(ch for ch in comp.ref if ch.isalpha())
        if prefix and letters != prefix:
            warnings.append(f"{comp.ref}: a {model['kind']} is usually named {prefix}<n>")
        comps.append(comp)
    if errors:
        raise CircuitError(errors, warnings)
    circuit = Circuit(name=raw.get("name", ""), description=raw.get("description", ""),
                      components=comps, raw=copy.deepcopy(raw), warnings=warnings)
    erc_errors, erc_warnings = erc(circuit, spec)
    circuit.warnings.extend(erc_warnings)
    if erc_errors:
        raise CircuitError(erc_errors, circuit.warnings)
    return circuit


def _check_pins(where: str, comp: Component) -> list[str]:
    errors = []
    model_pins = list(comp.model["pins"])
    nc = set(comp.model.get("nc_pins", []))
    for pin in comp.pins:
        if pin not in comp.model["pins"]:
            errors.append(f"{where}: part {comp.part} has no pin '{pin}' (pins: {', '.join(model_pins)})")
        elif pin in nc:
            errors.append(f"{where}: pin '{pin}' is a no-connect pin; leave it out")
    for pin in model_pins:
        if pin not in comp.pins and pin not in nc:
            errors.append(f"{where}: pin '{pin}' ({comp.model['pins'][pin]}) is not connected")
    return errors


def _check_value(where: str, comp: Component, rc: dict, prefs: dict) -> list[str]:
    errors = []
    vspec = comp.model.get("value")
    if vspec:
        if "value" not in rc:
            errors.append(f"{where}: a {comp.kind} needs a value, e.g. \"10k\" or \"100n\"")
        else:
            try:
                comp.value = parse_value(rc["value"], vspec["unit"])
            except UnitError as exc:
                errors.append(f"{where}: {exc}")
            else:
                if comp.value <= 0:
                    errors.append(f"{where}: value must be positive")
        footprints = comp.model["kicad"].get("footprints", {})
        pkg = rc.get("package") or prefs.get("passive_package")
        if pkg and footprints and pkg not in footprints:
            if rc.get("package"):
                errors.append(f"{where}: package '{pkg}' not available (choose {', '.join(sorted(footprints))})")
            pkg = None
        comp.package = rc.get("package") or (pkg if footprints else None)
    else:
        if "value" in rc:
            pass  # informational for fixed parts; ignored
        if rc.get("package"):
            errors.append(f"{where}: {comp.part} has a fixed footprint; remove 'package'")
    return errors


def erc(circuit: Circuit, spec: dict | None) -> tuple[list[str], list[str]]:
    """Electrical rule checks that do not need a simulation."""
    errors: list[str] = []
    warnings: list[str] = []
    nets = circuit.nets()
    ground = (spec or {}).get("ground", "GND")

    if ground not in nets:
        errors.append(f"no component connects to the ground net '{ground}'")

    for net, nodes in sorted(nets.items()):
        if len(nodes) < 2:
            ref, pin = nodes[0]
            errors.append(f"net {net} only connects to {ref}.{pin}; every net needs at least two connections")

    for c in circuit.components:
        connected = set(c.pins.values())
        if len(c.pins) >= 2 and len(connected) == 1:
            errors.append(f"{c.ref}: all pins are on net {next(iter(connected))} (shorted part)")

    if spec:
        port_nets = {p["net"]: p for p in spec["ports"]}
        external = {"connector", "testpoint"}
        for net, port in port_nets.items():
            if net not in nets:
                errors.append(f"spec port {net} is not connected to anything in the circuit")
                continue
            kinds = {circuit.by_ref(ref).kind for ref, _ in nets[net]}
            if not kinds & external:
                errors.append(
                    f"spec port {net} has no connector or test point; add one (e.g. header_1x02) "
                    "so the board can actually be wired up")
        for ref, req_ids in models_spec_refs(spec).items():
            comp = circuit.by_ref(ref)
            if comp is None:
                errors.append(f"spec requirement(s) {', '.join(req_ids)} measure {ref}, but the circuit has no {ref}")
                continue
            for req in spec["requirements"]:
                m = req["measure"]
                if m.get("ref") == ref and m.get("pin") and m["pin"] not in comp.pins:
                    errors.append(f"spec requirement {req['id']} measures pin {m['pin']} of {ref}, "
                                  f"which has pins {', '.join(comp.pins)}")

    errors.extend(_rule_errors(circuit, nets, ground, warnings))
    return errors, warnings


def models_spec_refs(spec: dict) -> dict[str, list[str]]:
    from .spec import referenced_refs
    return referenced_refs(spec)


def _rule_errors(circuit: Circuit, nets, ground: str, warnings: list[str]) -> list[str]:
    """Datasheet rules from part models that are pure topology (capacitance, tie-offs)."""
    errors = []
    for c in circuit.components:
        for rule in c.model.get("rules", []):
            if rule["type"] == "min_capacitance":
                net = c.pins.get(rule["pin"])
                if net is None:
                    continue
                total = 0.0
                for ref, _pin in nets.get(net, []):
                    other = circuit.by_ref(ref)
                    if other.kind == "capacitor" and other.value and set(other.pins.values()) == {net, ground}:
                        total += other.value
                if total < rule["farads"] * 0.999:
                    errors.append(
                        f"{c.ref} ({c.part}) needs at least {format_value(rule['farads'], 'F')} from "
                        f"{rule['pin']} ({net}) to {ground}; found {format_value(total, 'F')}")
                elif rule.get("note"):
                    warnings.append(f"{c.ref}: {rule['note']}")
            elif rule["type"] == "pin_driven":
                net = c.pins.get(rule["pin"])
                if net is None:
                    continue
                others = [n for n in nets.get(net, []) if n != (c.ref, rule["pin"])]
                if not others:
                    errors.append(f"{c.ref}: {rule.get('note') or rule['pin'] + ' must be connected'}")
                elif net == ground:
                    errors.append(f"{c.ref}: {rule['pin']} is tied to ground, so the part is disabled")
    return errors
