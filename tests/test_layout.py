"""Netlist, SES parsing, placement, and the full KiCad + Freerouting pipeline."""

import json
import math
import subprocess
import zipfile
from pathlib import Path

import pytest

from conftest import load_example, needs_kicad, needs_layout, needs_ngspice
from pcbagent import circuit as circuit_mod
from pcbagent import sexpr
from pcbagent import spec as spec_mod
from pcbagent.kicad.libs import Footprint, Pad, parse_footprint
from pcbagent.kicad.netlist import write_netlist
from pcbagent.layout import ses
from pcbagent.layout.pipeline import ipc2221_width_mm, netclasses_from_currents
from pcbagent.layout.placer import Item, Placer, place


def test_kicad_netlist_uses_symbol_pin_numbers(ldo_spec):
    circ = circuit_mod.load(load_example("ldo", "circuit"), ldo_spec)
    tree = sexpr.loads(write_netlist(circ, None, "ldo"))
    nets = {str(sexpr.value(n, "name")): [(str(sexpr.value(x, "ref")), str(sexpr.value(x, "pin")))
                                          for x in sexpr.children(n, "node")]
            for n in sexpr.children(sexpr.child(tree, "nets"), "net")}
    assert ("U1", "2") in nets["VOUT"] and ("U1", "3") in nets["VIN"] and ("U1", "1") in nets["GND"]
    assert ("D1", "2") in nets["LED_A"]  # LED anode is symbol pin 2


SES = """(session board
  (base_design board)
  (placement (resolution um 10)
    (component "Resistor_SMD:R_0603_1608Metric" (place R1 1100000 -1050000 front 90)))
  (routes (resolution um 10)
    (parser (host_cad "KiCad's Pcbnew"))
    (library_out
      (padstack "Via[0-1]_600:300_um" (shape (circle F.Cu 6000 0 0)) (shape (circle B.Cu 6000 0 0))))
    (network_out
      (net VOUT
        (wire (path F.Cu 2500 1141750 -1050000 1130000 -1061750 1100000 -1061750))
        (via "Via[0-1]_600:300_um" 1130000 -1061750)))))
"""


def test_ses_parser_scales_and_flips_y():
    parsed = ses.parse(SES)
    assert parsed["placements"][0] == {"ref": "R1", "x": 110.0, "y": 105.0, "side": "front", "rotation": 90.0}
    w = parsed["wires"][0]
    assert w["net"] == "VOUT" and w["layer"] == "F.Cu" and w["width"] == pytest.approx(0.25)
    assert w["points"][0] == pytest.approx((114.175, 105.0))
    via = parsed["vias"][0]
    assert via["diameter"] == pytest.approx(0.6) and via["drill"] == pytest.approx(0.3)
    assert len(ses.segments(parsed)) == 2


def test_ipc2221_widths():
    assert ipc2221_width_mm(1.0) == pytest.approx(0.30, abs=0.03)
    assert ipc2221_width_mm(2.0) == pytest.approx(0.78, abs=0.05)
    classes = netclasses_from_currents({"VIN": 2.0, "SIG": 0.001, "GND": 2.0}, "GND")
    nets = {n: c["track"] for c in classes for n in c["nets"]}
    assert "SIG" not in nets and nets["VIN"] >= 0.95 and nets["GND"] >= 0.95


def _fp(name, w, h, pads):
    return Footprint(name, [Pad(n, x, y, 0.8, 0.8, 0, "rect", "smd", ["F.Cu"]) for n, x, y in pads],
                     (-w / 2, -h / 2, w / 2, h / 2))


def test_placer_is_legal_deterministic_and_honours_near():
    items = [
        Item("U1", _fp("ic", 6, 6, [("1", -2.5, -2), ("2", -2.5, 0), ("3", -2.5, 2), ("4", 2.5, 0)]),
             {"1": "GND", "2": "OUT", "3": "IN", "4": "OUT"}),
        Item("C1", _fp("c", 2, 1, [("1", -0.8, 0), ("2", 0.8, 0)]), {"1": "IN", "2": "GND"}),
        Item("C2", _fp("c", 2, 1, [("1", -0.8, 0), ("2", 0.8, 0)]), {"1": "OUT", "2": "GND"}),
        Item("J1", _fp("j", 3, 6, [("1", 0, -1.3), ("2", 0, 1.3)]), {"1": "IN", "2": "GND"}),
        Item("J2", _fp("j", 3, 6, [("1", 0, -1.3), ("2", 0, 1.3)]), {"1": "OUT", "2": "GND"}),
    ]
    cons = [{"type": "near", "ref": "C1", "to": "U1", "pad": "3", "max_mm": 2.5},
            {"type": "edge", "ref": "J1", "side": "left"}, {"type": "edge", "ref": "J2", "side": "right"}]
    a = place(items, cons, seed=3)
    b = place(items, cons, seed=3)
    assert a.legal, a.issues
    assert a.positions == b.positions
    assert a.cost["per_constraint"][0] < 0.05  # C1 within 2.5 mm of U1 pin IN
    jx = a.positions["J1"][0]
    assert jx < a.width / 3 and a.positions["J2"][0] > 2 * a.width / 3


def test_footprint_parse_from_text():
    text = """(footprint "R_0603" (layer "F.Cu")
      (fp_line (start -1.48 -0.73) (end 1.48 -0.73) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))
      (fp_line (start -1.48 0.73) (end 1.48 0.73) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))
      (pad "1" smd roundrect (at -0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask"))
      (pad "2" smd roundrect (at 0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask")))"""
    fp = parse_footprint(text, "R_0603")
    assert fp.courtyard == (-1.48, -0.73, 1.48, 0.73)
    assert [p.number for p in fp.pads] == ["1", "2"] and not fp.through_hole


@needs_kicad
@pytest.mark.parametrize("example", ["divider", "ldo"])
def test_schematic_round_trips_through_kicad(tmp_path, example):
    """KiCad parses the generated schematic and derives exactly the circuit's nets from it."""
    from pcbagent.kicad.schematic import write_schematic
    from pcbagent.kicad.tools import find_kicad_cli
    spec = spec_mod.normalize(load_example(example, "spec"))
    circ = circuit_mod.load(load_example(example, "circuit"), spec)
    sch = tmp_path / f"{example}.kicad_sch"
    sch.write_text(write_schematic(circ, None, example))
    out = tmp_path / "from_kicad.net"
    proc = subprocess.run([find_kicad_cli(), "sch", "export", "netlist", "-o", str(out), str(sch)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    tree = sexpr.loads(out.read_text())
    got = {str(sexpr.value(n, "name")).lstrip("/"): sorted((str(sexpr.value(x, "ref")), str(sexpr.value(x, "pin")))
                                                          for x in sexpr.children(n, "node"))
           for n in sexpr.children(sexpr.child(tree, "nets"), "net")}
    want: dict = {}
    for c in circ.components:
        for pin, net in c.pins.items():
            want.setdefault(net, []).append((c.ref, c.model["kicad"]["pins"][pin]))
    assert got == {k: sorted(v) for k, v in want.items()}
    comps = {str(sexpr.value(c, "ref")): str(sexpr.value(c, "footprint"))
             for c in sexpr.children(sexpr.child(tree, "components"), "comp")}
    assert comps["J1"].startswith("Connector_PinHeader_2.54mm:")


ALL_EXAMPLES = sorted(p.name for p in (Path(__file__).resolve().parents[1] / "examples").iterdir()
                      if (p / "constraints.json").exists())


@needs_layout
@pytest.mark.parametrize("example", ALL_EXAMPLES)
def test_full_pipeline_routes_drc_clean(tmp_path, example):
    from pcbagent.layout import pipeline
    from pcbagent.project import Project
    p = Project.create(tmp_path / example, example)
    p.write_spec(load_example(example, "spec"))
    assert p.submit_circuit(load_example(example, "circuit"))["passed"]
    res = pipeline.run(p, load_example(example, "constraints"))
    assert res["passed"], res.get("message")
    assert res["drc"]["errors"] == 0 and res["drc"]["unconnected"] == 0
    assert res["freerouting"]["ok"]
    board = Path(p.state()["layout"]["board"])
    assert board.exists() and board.with_suffix(".kicad_pro").exists()
    trace = json.loads((board.parent / "layout_trace.json").read_text())
    assert len(trace["placement_frames"]) > 10 and trace["routing"]
    assert p.state()["phase"] == "done"
    # Tracks on current-carrying nets are routed at least as wide as their IPC-2221 net class.
    widths = {n: nc["track"] for nc in res["netclasses"] for n in nc["nets"]}
    board_tree = sexpr.loads(board.read_text())
    net_names = {str(n[1]): str(n[2]) for n in sexpr.children(board_tree, "net") if len(n) > 2}
    for seg in sexpr.children(board_tree, "segment"):
        net = net_names.get(str(sexpr.value(seg, "net")))
        if net in widths:
            assert float(sexpr.value(seg, "width")) >= widths[net] - 1e-6, (net, sexpr.value(seg, "width"))
    assert all(res["assigned_netclasses"][n] == nc["name"] for nc in res["netclasses"] for n in nc["nets"])
    # Footprints carry the UUID path of their schematic symbol, so KiCad links PCB and schematic.
    sch = board.with_suffix(".kicad_sch")
    assert sch.exists()
    sym_uuids = {str(sexpr.value(s, "uuid")) for s in sexpr.children(sexpr.loads(sch.read_text()), "symbol")}
    fp_paths = {str(sexpr.value(f, "path")).lstrip("/") for f in sexpr.children(sexpr.loads(board.read_text()),
                                                                                 "footprint")}
    assert fp_paths == sym_uuids
    # Replay must end in exactly the saved board: run the plugin headless under KiCad's Python.
    from pcbagent.kicad.tools import find_kicad_python
    plugin_dir = Path(__file__).resolve().parents[1] / "src" / "pcbagent" / "kicad" / "plugin"
    script = f"""
import sys, json
sys.path.insert(0, {str(plugin_dir)!r})
import pcbnew
b = pcbnew.LoadBoard({str(board)!r})
key = lambda b: sorted((f.GetReference(), round(pcbnew.ToMM(f.GetPosition().x), 3), round(pcbnew.ToMM(f.GetPosition().y), 3), f.GetOrientationDegrees()) for f in b.GetFootprints())
before = (key(b), len(b.GetTracks()))
import pcbagent_replay as R
R.Replayer(b, json.load(open({str(board.parent / 'layout_trace.json')!r}))).run_blocking()
after = (key(b), len(b.GetTracks()))
print(json.dumps(before == after))
"""
    out = subprocess.run([find_kicad_python(), "-c", script], capture_output=True, text=True, timeout=120,
                         env={**__import__("os").environ, "PCBAGENT_REPLAY_NO_REGISTER": "1"})
    assert out.stdout.strip().splitlines()[-1] == "true", out.stderr[-500:]


@needs_layout
def test_fab_export(tmp_path):
    from pcbagent.fab import export
    from pcbagent.layout import pipeline
    from pcbagent.project import Project
    p = Project.create(tmp_path / "ldo", "ldo")
    p.write_spec(load_example("ldo", "spec"))
    p.submit_circuit(load_example("ldo", "circuit"))
    assert pipeline.run(p, load_example("ldo", "constraints"))["passed"]
    res = export(p.root, Path(p.state()["layout"]["board"]), p.path("fab"))
    names = zipfile.ZipFile(res["files"][0]).namelist()
    assert any(n.endswith("-F_Cu.gtl") for n in names) and any(n.endswith(".drl") for n in names)
    assert any(n.endswith("-Edge_Cuts.gm1") for n in names)
    bom = (p.path("fab") / "bom_jlcpcb.csv").read_text()
    assert "Comment,Designator,Footprint,LCSC Part #" in bom and "SAMPLE" in bom
    cpl = (p.path("fab") / "cpl_jlcpcb.csv").read_text().splitlines()
    assert cpl[0] == "Designator,Mid X,Mid Y,Layer,Rotation" and any(r.startswith("U1,") for r in cpl)
