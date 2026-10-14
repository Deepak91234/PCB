"""ngspice-backed verification: real simulations, real numbers."""

import copy
import hashlib
from pathlib import Path

import pytest

from conftest import load_example, needs_ngspice
from pcbagent import circuit as circuit_mod
from pcbagent import spec as spec_mod
from pcbagent.verify import verify

pytestmark = needs_ngspice


def run(spec_raw, circuit_raw, tmp_path):
    spec = spec_mod.normalize(spec_raw)
    circ = circuit_mod.load(circuit_raw, spec)
    return verify(spec, circ, tmp_path)


def req(report, rid):
    return next(r for r in report["requirements"] if r["id"] == rid)


def test_divider_passes_every_corner(tmp_path):
    rep = run(load_example("divider", "spec"), load_example("divider", "circuit"), tmp_path)
    assert rep["passed"], rep
    vout = req(rep, "vout")
    # Hand calculation: 5 * (19.6k || 1M) / (10k + 19.6k || 1M) = 3.2675..3.311 over +/-1 % corners
    assert vout["nominal"] == pytest.approx(5 * (1 / (1 / 19600 + 1 / 1e6)) / (10000 + 1 / (1 / 19600 + 1 / 1e6)),
                                            rel=1e-4)
    assert vout["corners"] == 5  # nominal + 2^2 exhaustive corners


def test_divider_wrong_ratio_fails_with_measured_value(tmp_path):
    bad = load_example("divider", "circuit")
    bad["components"][2]["value"] = "10k"
    rep = run(load_example("divider", "spec"), bad, tmp_path)
    assert not rep["passed"]
    vout = req(rep, "vout")
    assert not vout["passed"]
    assert vout["nominal"] == pytest.approx(2.4876, rel=1e-3)
    assert "FAIL" in vout["message"] and "2.488V" in vout["message"]


def test_provenance_points_at_real_raw_files(tmp_path):
    rep = run(load_example("divider", "spec"), load_example("divider", "circuit"), tmp_path)
    prov = req(rep, "vout")["provenance"][0]
    raw = Path(prov["raw"])
    assert raw.exists()
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == prov["sha256"]
    assert "v(n_vout)" in prov["vectors"]


def test_ldo_passes_and_reports_stress(tmp_path):
    rep = run(load_example("ldo", "spec"), load_example("ldo", "circuit"), tmp_path)
    assert rep["passed"], [r["message"] for r in rep["requirements"]]
    tj = next(c for c in rep["ratings"] if c["ref"] == "U1" and c["check"] == "junction_temperature")
    # (5 - 3.3) V * 0.5 A + 5 V * Iq ~ 0.9 W, at 90 C/W from 25 C ambient
    assert 95 < tj["value"] < 115
    assert rep["selection_requirements"]["C1"]["min_voltage_v"] == pytest.approx(7.5)
    assert rep["net_currents_a"]["VOUT"] >= 0.5


def test_ldo_overload_fails_ratings(tmp_path):
    spec = load_example("ldo", "spec")
    for cond in spec["conditions"]:
        for ld in cond.get("loads", []):
            ld["amps"] = 1.5
    rep = run(spec, load_example("ldo", "circuit"), tmp_path)
    assert not rep["passed"]
    failed = {c["check"] for c in rep["ratings"] if not c["passed"]}
    assert {"output_current", "junction_temperature"} <= failed


def test_hot_ambient_fails_thermal(tmp_path):
    spec = load_example("ldo", "spec")
    spec["preferences"] = {"ambient_c": 50}
    rep = run(spec, load_example("ldo", "circuit"), tmp_path)
    tj = next(c for c in rep["ratings"] if c["check"] == "junction_temperature")
    assert not tj["passed"]


RC_SPEC = {
    "title": "RC low-pass, 1 kHz",
    "ports": [{"net": "VIN", "role": "signal_in"}, {"net": "VOUT", "role": "signal_out"},
              {"net": "GND", "role": "ground"}],
    "conditions": [{"id": "ac", "sources": [{"net": "VIN", "volts": 0}], "loads": [{"net": "VOUT", "ohms": 1e7}]}],
    "requirements": [
        {"id": "fc", "condition": "ac", "measure": {"type": "cutoff_hz", "input": "VIN", "output": "VOUT"},
         "target": 1000, "tolerance": {"pct": 15}},
        {"id": "stopband", "condition": "ac", "measure": {"type": "gain_db", "input": "VIN", "output": "VOUT",
                                                          "hz": 10000}, "max": -15},
    ],
}
RC = {"components": [
    {"ref": "J1", "part": "header_1x03", "pins": {"1": "VIN", "2": "GND", "3": "VOUT"}},
    {"ref": "R1", "part": "resistor", "value": "1.6k", "pins": {"1": "VIN", "2": "VOUT"}},
    {"ref": "C1", "part": "capacitor", "value": "100n", "pins": {"1": "VOUT", "2": "GND"}},
]}


def test_rc_filter_ac_measurements(tmp_path):
    rep = run(RC_SPEC, RC, tmp_path)
    fc = req(rep, "fc")
    assert fc["nominal"] == pytest.approx(1 / (2 * 3.14159265 * 1600 * 100e-9), rel=0.02)
    # Capacitor tolerance (10 %) widens the corner spread; all corners must stay within 15 %.
    assert fc["passed"], fc["message"]
    assert req(rep, "stopband")["nominal"] == pytest.approx(-20.0, abs=0.5)


def test_capacitor_tolerance_corner_can_fail(tmp_path):
    tight = copy.deepcopy(RC_SPEC)
    tight["requirements"][0]["tolerance"] = {"pct": 5}
    rep = run(tight, RC, tmp_path)
    fc = req(rep, "fc")
    assert fc["nominal"] == pytest.approx(994.7, rel=0.01)  # nominal is fine...
    assert not fc["passed"]  # ...but a 10 % capacitor pushes a corner outside +/-5 %
    assert "worst corner" in fc["message"]


def test_led_current_over_vf_spread(tmp_path):
    spec = {
        "title": "status LED on 3.3 V",
        "ports": [{"net": "V3V3", "role": "power_in"}, {"net": "GND", "role": "ground"}],
        "conditions": [{"id": "on", "sources": [{"net": "V3V3", "volts": 3.3}]}],
        "requirements": [{"id": "led_i", "condition": "on",
                          "measure": {"type": "component_current", "ref": "D1", "pin": "A"},
                          "min": 0.001, "max": 0.004}],
    }
    circ = {"components": [
        {"ref": "J1", "part": "header_1x02", "pins": {"1": "V3V3", "2": "GND"}},
        {"ref": "R1", "part": "resistor", "value": "470", "pins": {"1": "V3V3", "2": "A"}},
        {"ref": "D1", "part": "led_red", "pins": {"A": "A", "K": "GND"}},
    ]}
    rep = run(spec, circ, tmp_path)
    r = req(rep, "led_i")
    assert r["passed"], r["message"]
    # (3.3 - 2.0) / 470 ~ 2.7 mA nominal; the Vf corners (1.8..2.2 V) spread it.
    assert 0.0025 < r["nominal"] < 0.003
    assert r["max"] - r["min"] > 0.0007


def test_enable_pin_tied_high_works(tmp_path):
    circ = load_example("ldo", "circuit")
    circ["components"][2] = {"ref": "U1", "part": "AP2112K-3.3",
                             "pins": {"IN": "VIN", "OUT": "VOUT", "GND": "GND", "EN": "VIN"}}
    rep = run(load_example("ldo", "spec"), circ, tmp_path)
    assert req(rep, "vout_full_load")["passed"]
    assert not any(c["check"] == "enable_high" and not c["passed"] for c in rep["ratings"])


EXAMPLES = sorted(p.name for p in (Path(__file__).resolve().parents[1] / "examples").iterdir()
                  if (p / "spec.json").exists())


@pytest.mark.parametrize("example", EXAMPLES)
def test_every_bundled_example_verifies(tmp_path, example):
    rep = run(load_example(example, "spec"), load_example(example, "circuit"), tmp_path)
    assert rep["passed"], [r["message"] for r in rep["requirements"] if not r["passed"]]


def test_led_example_with_390_ohms_fails_only_at_a_corner(tmp_path):
    """The worked case from the README: nominal passes, worst-case corner does not."""
    circ = load_example("led_indicator", "circuit")
    circ["components"][1]["value"] = "390"
    rep = run(load_example("led_indicator", "spec"), circ, tmp_path)
    r = req(rep, "led_current")
    assert 0.002 <= r["nominal"] <= 0.004 and not r["passed"]
    assert r["max"] > 0.004
