"""Project state machine: spec lock, tamper detection, attempts and phases."""

import json

import pytest

from conftest import load_example, needs_ngspice
from pcbagent.project import Project, ProjectError


def make(tmp_path, example="divider"):
    p = Project.create(tmp_path / "design", f"example {example}")
    p.write_spec(load_example(example, "spec"))
    return p


def test_spec_is_locked(tmp_path):
    p = make(tmp_path)
    loosened = load_example("divider", "spec")
    loosened["requirements"][0]["tolerance"] = {"pct": 50}
    with pytest.raises(ProjectError, match="locked"):
        p.write_spec(loosened)


def test_tampered_spec_is_detected(tmp_path):
    p = make(tmp_path)
    path = p.path("spec.json")
    spec = json.loads(path.read_text())
    spec["requirements"][0]["tolerance"] = {"pct": 50}
    path.write_text(json.dumps(spec))
    with pytest.raises(ProjectError, match="does not match the locked hash"):
        p.spec()
    with pytest.raises(ProjectError):
        p.submit_circuit(load_example("divider", "circuit"))


def test_unlock_is_explicit(tmp_path):
    p = make(tmp_path)
    p.unlock_spec()
    assert p.state()["phase"] == "spec"
    p.write_spec(load_example("divider", "spec"))
    assert p.state()["phase"] == "circuit"


def test_invalid_circuit_counts_as_attempt_without_simulating(tmp_path):
    p = make(tmp_path)
    res = p.submit_circuit({"components": [{"ref": "R1", "part": "nope", "pins": {}}]})
    assert not res["passed"] and res["stage"] == "validate"
    st = p.state()
    assert st["circuit_attempts"] == 1 and st["circuit_version"] == 0
    assert not p.path("verification.json").exists()


@needs_ngspice
def test_full_circuit_flow(tmp_path):
    p = make(tmp_path)
    bad = load_example("divider", "circuit")
    bad["components"][2]["value"] = "10k"
    r1 = p.submit_circuit(bad)
    assert not r1["passed"] and p.state()["phase"] == "circuit"
    with pytest.raises(ProjectError):
        p.require_verified()
    r2 = p.submit_circuit(load_example("divider", "circuit"))
    assert r2["passed"], r2["summary"]
    st = p.state()
    assert st["phase"] == "layout" and st["circuit_version"] == 2
    assert p.path("history", "circuit_v001.json").exists()
    assert p.path("history", "verification_v002.json").exists()
    assert p.path(f"{p.name}.net").exists() and p.path("bom_jlcpcb.csv").exists()
    spec, circ, parts = p.require_verified()
    assert parts["passed"]
    assert p.next_step().startswith("write placement constraints")


@needs_ngspice
def test_editing_circuit_after_verification_is_caught(tmp_path):
    p = make(tmp_path)
    p.submit_circuit(load_example("divider", "circuit"))
    raw = json.loads(p.path("circuit.json").read_text())
    raw["components"][1]["value"] = "1k"
    p.path("circuit.json").write_text(json.dumps(raw))
    with pytest.raises(ProjectError, match="has not passed verification"):
        p.require_verified()
