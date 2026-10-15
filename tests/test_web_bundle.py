"""The design bundle behind the web UI, on designs at every stage (no tools needed)."""

import json

from pcbagent.project import Project
from pcbagent.web import bundle


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_new_design_has_only_the_request(tmp_path):
    p = Project.create(tmp_path / "d", "a 5 V to 3.3 V regulator")
    b = bundle.design_bundle(p.root)
    assert b["id"] == "d" and b["phase"] == "spec" and b["title"] == "a 5 V to 3.3 V regulator"
    assert b["spec"] is None and b["verification"] is None and b["parts"] is None and b["board"] is None
    assert b["files"] == [] and b["schematic"] is None
    assert bundle.list_designs(tmp_path)[0]["id"] == "d"


def test_failed_verification_hides_stale_parts(tmp_path):
    from conftest import load_example
    p = Project.create(tmp_path / "d", "divider")
    p.write_spec(load_example("divider", "spec"))
    _write(p.root / "verification.json", {
        "passed": False, "circuit_version": 3, "simulator": {"version": "42"}, "runs": [],
        "requirements": [{"id": "vout", "condition": "nominal", "measure": {"type": "voltage", "net": "VOUT"},
                          "unit": "V", "bounds": {"min": 3.2, "max": 3.4, "target": 3.3}, "nominal": 2.5,
                          "min": 2.4, "max": 2.6, "corners": 5, "passed": False, "message": "vout: FAIL"}],
        "ratings": []})
    _write(p.root / "parts.json", {"passed": True, "bom": [{"refs": ["R1"]}]})   # left over from an older circuit
    b = bundle.design_bundle(p.root)
    assert b["spec"]["requirements"] == 2 and b["spec"]["title"]
    v = b["verification"]
    assert v["passed"] is False and v["circuit_version"] == 3
    assert v["requirements"][0]["lo"] == 3.2 and v["requirements"][0]["target_of"] == "VOUT"
    assert b["parts"] is None


def test_crop_svg_keeps_the_drawing_and_drops_scripts():
    svg = ('<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="420mm" height="297mm" '
           'viewBox="0 0 419.989 297.002" onload="alert(1)">\n<title>x</title>\n'
           '<rect x="0.000000" y="0.000000" width="419.989000" height="297.002000" rx="0.000000" />\n'
           '<script>alert(2)</script>\n<path d="M100.0 50.0\nL120.0 70.0\n" />\n'
           '<text x="110" y="60" onclick=\'alert(3)\'>R1</text>\n</svg>')
    out = bundle.crop_svg(svg)
    assert out.startswith("<svg") and "alert" not in out and "<script" not in out.lower()
    assert 'viewBox="96.00 46.00 28.00 28.00"' in out
    assert 'width="419.989000"' not in out


def test_only_listed_files_are_offered(tmp_path):
    p = Project.create(tmp_path / "d", "x")
    for rel in ("bom_jlcpcb.csv", "layout/d.kicad_pcb", "fab/d_gerbers.zip", "spec.json", "agent/messages.json"):
        (p.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (p.root / rel).write_text("x")
    assert {f["path"] for f in bundle.files(p.root, "d")} == {"bom_jlcpcb.csv", "layout/d.kicad_pcb",
                                                               "fab/d_gerbers.zip"}
