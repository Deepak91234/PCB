"""Spec validation and circuit electrical-rule checks (no simulator needed)."""

import copy

import pytest

from conftest import load_example
from pcbagent import circuit as circuit_mod
from pcbagent import spec as spec_mod


def test_examples_normalize(divider_spec, ldo_spec):
    assert divider_spec["ground"] == "GND"
    assert ldo_spec["schema"] == "pcbagent/spec@1"
    b = spec_mod.requirement_bounds(divider_spec["requirements"][0])
    assert b.lo == pytest.approx(3.234) and b.hi == pytest.approx(3.366)


def test_hash_is_order_independent(divider_spec):
    shuffled = dict(reversed(list(divider_spec.items())))
    assert spec_mod.content_hash(shuffled) == spec_mod.content_hash(divider_spec)


@pytest.mark.parametrize("mutate,needle", [
    (lambda s: s["ports"].pop(), "ground net 'GND' must be listed"),
    (lambda s: s["requirements"][0].update(condition="nope"), "unknown condition 'nope'"),
    (lambda s: s["requirements"][0].pop("tolerance"), "give target+tolerance"),
    (lambda s: s["requirements"][0]["measure"].pop("net"), "needs 'net'"),
    (lambda s: s["conditions"][0]["sources"][0].update(amps=1), "exactly one of 'volts' or 'amps'"),
    (lambda s: s["conditions"][0]["sources"][0].update(net="VX"), "not a declared port"),
    (lambda s: s["requirements"].append({"id": "ac", "condition": "nominal",
                                         "measure": {"type": "cutoff_hz", "input": "VOUT", "output": "VIN"},
                                         "max": 1}), "needs a voltage source"),
    (lambda s: s["ports"][0].update(net="vin"), "uppercase net name"),
])
def test_spec_errors(mutate, needle):
    raw = load_example("divider", "spec")
    mutate(raw)
    with pytest.raises(spec_mod.SpecError) as exc:
        spec_mod.normalize(raw)
    assert any(needle in e for e in exc.value.errors), exc.value.errors


def test_divider_circuit_loads(divider_spec):
    c = circuit_mod.load(load_example("divider", "circuit"), divider_spec)
    assert c.by_ref("R2").value == 19600
    assert set(c.nets()) == {"VIN", "VOUT", "GND"}


def _ldo():
    return load_example("ldo", "circuit")


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c["components"][2].update(part="LM9999"), "unknown part 'LM9999'"),
    (lambda c: c["components"][2]["pins"].pop("GND"), "pin 'GND' (ground) is not connected"),
    (lambda c: c["components"][3].update(value="1u"), "needs at least 10µF from IN"),
    (lambda c: c["components"][4].pop("value"), "needs a value"),
    (lambda c: c["components"][4].update(value="22V"), "has unit V, expected F"),
    (lambda c: c["components"].pop(0), "spec port VIN has no connector"),
    (lambda c: c["components"].append({"ref": "R9", "part": "resistor", "value": "1k",
                                       "pins": {"1": "VOUT", "2": "FLOAT"}}), "net FLOAT only connects"),
    (lambda c: c["components"].append(copy.deepcopy(c["components"][6])), "duplicate ref R1"),
    (lambda c: c["components"][6]["pins"].update({"2": "VOUT"}), "shorted part"),
])
def test_circuit_errors(ldo_spec, mutate, needle):
    raw = _ldo()
    mutate(raw)
    with pytest.raises(circuit_mod.CircuitError) as exc:
        circuit_mod.load(raw, ldo_spec)
    assert any(needle in e for e in exc.value.errors), exc.value.errors


def test_enable_pin_rules(ldo_spec):
    raw = _ldo()
    raw["components"][2] = {"ref": "U1", "part": "AP2112K-3.3",
                            "pins": {"IN": "VIN", "OUT": "VOUT", "GND": "GND", "EN": "GND"}}
    raw["components"][4]["value"] = "1u"
    with pytest.raises(circuit_mod.CircuitError) as exc:
        circuit_mod.load(raw, ldo_spec)
    assert any("EN is tied to ground" in e for e in exc.value.errors)
    raw["components"][2]["pins"]["NC"] = "VIN"
    with pytest.raises(circuit_mod.CircuitError) as exc:
        circuit_mod.load(raw, ldo_spec)
    assert any("no-connect pin" in e for e in exc.value.errors)


def test_spec_ref_must_exist(ldo_spec):
    raw = load_example("ldo", "spec")
    raw["requirements"].append({"id": "led_current", "condition": "no_load",
                                "measure": {"type": "component_current", "ref": "D7", "pin": "A"}, "max": 0.01})
    spec = spec_mod.normalize(raw)
    with pytest.raises(circuit_mod.CircuitError) as exc:
        circuit_mod.load(_ldo(), spec)
    assert any("has no D7" in e for e in exc.value.errors)
