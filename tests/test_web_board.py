"""The web viewer's board reader: .kicad_pcb text -> geometry, without pcbnew."""

import json
import math
import os
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest

from conftest import load_example, needs_layout
from pcbagent.web.board import parse_board, read_board

NETS = '(net 0 "") (net 1 "GND") (net 2 "SIG") (net 3 "VCC")'
EDGE = '(stroke (width 0.1) (type solid)) (layer "Edge.Cuts")'
FONT = "(effects (font (size 1 1) (thickness 0.15)))"


def board(*items: str, nets: str = NETS) -> dict:
    return parse_board(f'(kicad_pcb (version 20221018) (generator pcbnew) {nets}\n' + "\n".join(items) + ")")


def footprint(at: str, *items: str, layer: str = "F.Cu", name: str = "Lib:FP", ref: str = "R1",
              value: str = "1k") -> str:
    texts = (f'(fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") {FONT}) '
             f'(fp_text value "{value}" (at 0 1.5) (layer "F.Fab") {FONT})')
    return f'(footprint "{name}" (layer "{layer}") (at {at}) {texts} ' + " ".join(items) + ")"


def smd(n: str, at: str, net: str = "", size: str = "1 0.6", shape: str = "rect", extra: str = "") -> str:
    return f'(pad "{n}" smd {shape} (at {at}) (size {size}) (layers "F.Cu" "F.Paste" "F.Mask") {extra} {net})'


def rotate(lx, ly, deg):
    """KiCad's footprint transform (y down, angles counter-clockwise on screen)."""
    a = math.radians(deg)
    return lx * math.cos(a) + ly * math.sin(a), -lx * math.sin(a) + ly * math.cos(a)


@pytest.mark.parametrize("rot", [90, 270, -90, 30])
def test_rotated_footprint_keeps_pads_in_local_frame(rot):
    # The file stores pad (at x y a) in the footprint frame, with `a` the pad's absolute orientation.
    fp = footprint(f"10 20 {rot}",
                   smd("1", f"1.5 0.5 {rot}", '(net 1 "GND")'),
                   smd("2", "-1.5 0.5", '(net 2 "SIG")'))  # absolute orientation 0, so the angle is omitted
    got = board(fp)["footprints"][0]
    assert got["rot"] == rot % 360 and 0 <= got["rot"] < 360
    p1, p2 = got["pads"]
    assert (p1["lx"], p1["ly"], p1["lrot"]) == (1.5, 0.5, 0.0)
    assert (p2["lx"], p2["ly"]) == (-1.5, 0.5) and p2["lrot"] == pytest.approx((-rot) % 360)
    assert got["label"] == {"lx": 0.0, "ly": -1.5, "size": 1.0}
    # A renderer recovers the board position with the KiCad transform: for 90 degrees, local +x points up.
    dx, dy = rotate(p1["lx"], p1["ly"], got["rot"])
    if rot == 90:
        assert (dx, dy) == pytest.approx((0.5, -1.5))
    if rot == 270:
        assert (dx, dy) == pytest.approx((-0.5, 1.5))


def test_kicad8_property_reference_and_value():
    fp = ('(footprint "Package_TO_SOT_SMD:SOT-23" (layer "F.Cu") (uuid "x") (at 50 40 180)'
          ' (property "Reference" "U7" (at 0 -2.4 180) (layer "F.SilkS") (uuid "a")'
          '  (effects (font (size 1.27 1.1) (thickness 0.15))))'
          ' (property "Value" "LM317" (at 0 2.4 180) (layer "F.Fab") (uuid "b") (effects (font (size 1 1))))'
          ' (property "Footprint" "Package_TO_SOT_SMD:SOT-23" (at 0 0 0) (layer "F.Fab") (hide yes))'
          ' (pad "1" smd roundrect (at -0.95 1.1 180) (size 0.6 1.1) (layers "F.Cu") (roundrect_rratio 0.2)'
          '  (net 3 "VCC") (uuid "c")))')
    got = board(fp)["footprints"][0]
    assert (got["ref"], got["value"], got["fpid"]) == ("U7", "LM317", "Package_TO_SOT_SMD:SOT-23")
    assert got["label"] == {"lx": 0.0, "ly": -2.4, "size": 1.27}  # (size HEIGHT WIDTH)
    pad = got["pads"][0]
    assert pad["net"] == "VCC" and pad["shape"] == "roundrect" and pad["rr"] == 0.2 and pad["lrot"] == 0.0


def test_pad_net_forms():
    fp = footprint("0 0",
                   smd("1", "0 0", '(net 1 "GND")'),   # KiCad 6-8 pads
                   smd("2", "1 0", "(net 2)"),         # number only: board net table
                   smd("3", "2 0", '(net "VCC")'),     # name only (newer files)
                   smd("4", "3 0"))                    # unconnected
    assert [p["net"] for p in board(fp)["footprints"][0]["pads"]] == ["GND", "SIG", "VCC", ""]


def test_tracks_and_vias_resolve_nets():
    got = board('(segment (start 1 2) (end 3 2) (width 0.25) (layer "F.Cu") (net 2) (tstamp t))',
                '(segment (start 3 2) (end 3 5) (width 0.4) (layer "B.Cu") (net "VCC"))',
                '(via (at 3 2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (tstamp v))',
                '(via blind (at 4 4) (size 0.45) (drill 0.2) (layers "F.Cu" "In1.Cu") (net 0))')
    assert got["tracks"] == [[1.0, 2.0, 3.0, 2.0, 0.25, "F.Cu", "SIG"], [3.0, 2.0, 3.0, 5.0, 0.4, "B.Cu", "VCC"]]
    assert got["vias"] == [[3.0, 2.0, 0.6, 0.3, "GND"], [4.0, 4.0, 0.45, 0.2, ""]]


def test_arc_track_becomes_chords_through_start_mid_end():
    # Half circle of radius 2 around (10, 10), bulging upwards (smaller y).
    got = board('(arc (start 8 10) (mid 10 8) (end 12 10) (width 0.3) (layer "B.Cu") (net 3) (tstamp a))')
    segs = got["tracks"]
    assert len(segs) >= 8
    assert all(s[4:] == [0.3, "B.Cu", "VCC"] for s in segs)
    assert segs[0][:2] == [8.0, 10.0] and segs[-1][2:4] == [12.0, 10.0]
    assert all(a[2:4] == b[:2] for a, b in pairwise(segs))  # a connected polyline
    verts = [s[:2] for s in segs] + [segs[-1][2:4]]
    assert [10.0, 8.0] in verts
    assert all(math.dist(v, (10, 10)) == pytest.approx(2, abs=1e-3) for v in verts)
    assert all(v[1] <= 10 + 1e-9 for v in verts)  # follows the arc through mid, not the other half


def test_zone_filled_polygons():
    zone = ('(zone (net 1) (net_name "GND") (layers "F.Cu" "B.Cu") (tstamp z) (hatch edge 0.5)'
            ' (fill yes (thermal_gap 0.3)) (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))'
            ' (filled_polygon (layer "F.Cu") (pts (xy 0.5 0.5) (xy 9.5 0.5) (xy 9.5 9.512345)))'
            ' (filled_polygon (layer "B.Cu") (island) (pts (xy 1 1) (xy 2 1) (xy 2 2))))')
    unfilled = '(zone (net 2) (net_name "SIG") (layer "F.Cu") (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))'
    assert board(zone, unfilled)["zones"] == [
        {"layer": "F.Cu", "net": "GND", "pts": [[0.5, 0.5], [9.5, 0.5], [9.5, 9.5123]]},
        {"layer": "B.Cu", "net": "GND", "pts": [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0]]},
    ]


def test_edge_cuts_outline():
    assert board(f"(gr_rect (start 100 80) (end 125 98) {EDGE})")["outline"] == [100.0, 80.0, 125.0, 98.0]
    lines = [f"(gr_line (start {a}) (end {b}) {EDGE})"
             for a, b in [("100 80", "130 80"), ("130 80", "130 95"), ("130 95", "100 95"), ("100 95", "100 80")]]
    silk = '(gr_line (start 0 0) (end 200 200) (stroke (width 0.1) (type solid)) (layer "F.SilkS"))'
    assert board(*lines, silk)["outline"] == [100.0, 80.0, 130.0, 95.0]
    # An arc bulges past its end points; a circle and a footprint-level edge count too.
    arc = f"(gr_arc (start 130 80) (mid 132 87.5) (end 130 95) {EDGE})"
    circle = '(gr_circle (center 115 100) (end 117 100) (stroke (width 0.1)) (fill none) (layer "Edge.Cuts"))'
    fp = footprint("110 70 90", f"(fp_line (start 0 0) (end 0 -4) {EDGE})")  # local -y is board -x at 90 degrees
    assert board(*lines, arc, circle, fp)["outline"] == [100.0, 70.0, 132.0, 102.0]
    assert board(fp)["outline"] == [106.0, 70.0, 110.0, 70.0]
    assert board(silk)["outline"] is None


def test_through_hole_and_non_plated_pads():
    fp = footprint(
        "5 5",
        '(pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") (net 1 "GND"))',
        '(pad "2" thru_hole oval (at 2.54 0) (size 1.7 2.4) (drill oval 0.8 1.4 (offset 0 0.2)) (layers "*.Cu"))',
        '(pad "MH" np_thru_hole circle (at 0 5) (size 3.2 3.2) (drill 3.2) (layers "*.Cu" "*.Mask"))',
        smd("4", "1 1", extra="(chamfer_ratio 0.2) (chamfer top_left)", shape="roundrect"),
        smd("5", "2 2", shape="trapezoid"),
        smd("6", "3 3", shape="custom"),
        smd("7", "4 4", shape="circle", size="1 1"),
    )
    pads = board(fp)["footprints"][0]["pads"]
    assert [(p["th"], p["drill"]) for p in pads[:3]] == [(True, 1.0), (True, 0.8), (False, 3.2)]
    assert pads[2]["n"] == "" and pads[2]["shape"] == "circle"  # pcbnew drops a non-plated hole's number
    assert [p["shape"] for p in pads] == ["rect", "oval", "circle", "rect", "rect", "rect", "circle"]
    assert all(p["rr"] == 0 for p in pads) and all(p["drill"] == 0 and not p["th"] for p in pads[3:])
    assert (pads[1]["w"], pads[1]["h"]) == (1.7, 2.4)


def test_courtyard_and_back_side_footprint():
    crt = ('(fp_rect (start -2 -1) (end 2 1) (stroke (width 0.05)) (fill none) (layer "F.CrtYd"))'
           ' (fp_circle (center 0 0) (end 2.5 0) (stroke (width 0.05)) (fill none) (layer "F.Fab"))')
    top = board(footprint("0 0", crt, smd("1", "0 0")))["footprints"][0]
    assert top["court"] == [-2.0, -1.0, 2.0, 1.0] and top["side"] == "top"
    assert board(footprint("0 0", smd("1", "0 0")))["footprints"][0]["court"] is None
    # Back-side parts: KiCad has already mirrored the local coordinates, so they read the same way.
    back = footprint("20 10 90",
                     '(fp_line (start -1.5 0.8) (end 1.5 -0.8) (stroke (width 0.05)) (layer "B.CrtYd"))',
                     '(pad "1" smd rect (at -0.8 0 90) (size 0.9 1) (layers "B.Cu" "B.Paste" "B.Mask") (net 1 "GND"))',
                     layer="B.Cu")
    got = board(back)["footprints"][0]
    assert got["side"] == "bottom" and got["court"] == [-1.5, -0.8, 1.5, 0.8]
    assert (got["pads"][0]["lx"], got["pads"][0]["ly"], got["pads"][0]["lrot"]) == (-0.8, 0.0, 0.0)


def test_rejects_non_board_text():
    with pytest.raises(ValueError):
        parse_board('(kicad_sch (version 20230121))')


# --- against pcbnew ----------------------------------------------------------------

PCBNEW_EXPORT = r"""
import json, math, sys
sys.path.insert(0, sys.argv[2])
import pcbnew
from pcbagent import sexpr

path = sys.argv[1]
b = pcbnew.LoadBoard(path)
mm = pcbnew.ToMM
SHAPES = {pcbnew.PAD_SHAPE_RECT: "rect", pcbnew.PAD_SHAPE_ROUNDRECT: "roundrect",
          pcbnew.PAD_SHAPE_CIRCLE: "circle", pcbnew.PAD_SHAPE_OVAL: "oval"}
r = lambda v: round(v, 4)

def to_local(dx, dy, deg):
    a = math.radians(deg)
    return dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)

fps = []
for fp in b.GetFootprints():
    fx, fy = mm(fp.GetPosition().x), mm(fp.GetPosition().y)
    rot = fp.GetOrientationDegrees()
    pads = []
    for p in fp.Pads():
        lx, ly = to_local(mm(p.GetPosition().x) - fx, mm(p.GetPosition().y) - fy, rot)
        pads.append({"n": p.GetNumber(), "net": p.GetNetname(), "shape": SHAPES.get(p.GetShape(), "rect"),
                     "w": r(mm(p.GetSize().x)), "h": r(mm(p.GetSize().y)), "lx": r(lx), "ly": r(ly),
                     "lrot": r(p.GetOrientationDegrees() - rot),
                     "rr": r(p.GetRoundRectRadiusRatio()) if p.GetShape() == pcbnew.PAD_SHAPE_ROUNDRECT else 0,
                     "drill": r(mm(p.GetDrillSize().x)) if p.GetDrillSize().x else 0,
                     "th": p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH})
    court = None
    poly = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
    if poly.OutlineCount():
        ol = poly.Outline(0)
        pts = [to_local(mm(ol.CPoint(i).x) - fx, mm(ol.CPoint(i).y) - fy, rot) for i in range(ol.PointCount())]
        court = [r(min(q[0] for q in pts)), r(min(q[1] for q in pts)), r(max(q[0] for q in pts)),
                 r(max(q[1] for q in pts))]
    ref = fp.Reference()
    rlx, rly = to_local(mm(ref.GetPosition().x) - fx, mm(ref.GetPosition().y) - fy, rot)
    fps.append({"ref": fp.GetReference(), "value": fp.GetValue(), "fpid": str(fp.GetFPID().GetUniStringLibId()),
                "x": r(fx), "y": r(fy), "rot": r(rot), "pads": pads, "court": court,
                "label": {"lx": r(rlx), "ly": r(rly), "size": r(mm(ref.GetTextHeight()))}})
tracks, vias = [], []
for t in b.GetTracks():
    if t.GetClass() == "PCB_VIA":
        vias.append([r(mm(t.GetPosition().x)), r(mm(t.GetPosition().y)), r(mm(t.GetWidth())),
                     r(mm(t.GetDrillValue())), t.GetNetname()])
    else:
        tracks.append([r(mm(t.GetStart().x)), r(mm(t.GetStart().y)), r(mm(t.GetEnd().x)), r(mm(t.GetEnd().y)),
                       r(mm(t.GetWidth())), b.GetLayerName(t.GetLayer()), t.GetNetname()])
zones = []
for z in sexpr.children(sexpr.loads(open(path).read()), "zone"):
    for fpoly in sexpr.children(z, "filled_polygon"):
        zones.append({"layer": str(sexpr.value(fpoly, "layer", "")), "net": str(sexpr.value(z, "net_name", "")),
                      "pts": [[r(v[0]), r(v[1])] for v in map(sexpr.floats,
                                                               sexpr.children(sexpr.child(fpoly, "pts"), "xy"))]})
edge = [d for d in b.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts]
xs = [mm(p.x) for d in edge for p in (d.GetStart(), d.GetEnd())]
ys = [mm(p.y) for d in edge for p in (d.GetStart(), d.GetEnd())]
print(json.dumps({"outline": [r(min(xs)), r(min(ys)), r(max(xs)), r(max(ys))], "footprints": fps,
                  "tracks": tracks, "vias": vias, "zones": zones}))
"""

ANGLE_KEYS = {"rot", "lrot"}


def mismatches(ref, got, path="$", key=None) -> list[str]:
    """Every value in ``ref`` must be in ``got`` (extra keys allowed); numbers within 1e-3, angles mod 360."""
    if isinstance(ref, dict):
        if not isinstance(got, dict):
            return [f"{path}: {got!r} is not a dict"]
        return [m for k, v in ref.items()
                for m in ([f"{path}.{k}: missing"] if k not in got else mismatches(v, got[k], f"{path}.{k}", k))]
    if isinstance(ref, list):
        if not isinstance(got, list) or len(got) != len(ref):
            return [f"{path}: {len(got) if isinstance(got, list) else got!r} items, want {len(ref)}"]
        return [m for i, (a, b) in enumerate(zip(ref, got)) for m in mismatches(a, b, f"{path}[{i}]", key)]
    if isinstance(ref, (int, float)) and not isinstance(ref, bool):
        if isinstance(got, bool) or not isinstance(got, (int, float)):
            return [f"{path}: {got!r} != {ref!r}"]
        d = abs(got - ref)
        if key in ANGLE_KEYS:
            d = min(d % 360, 360 - d % 360)
        return [] if d <= 1e-3 else [f"{path}: {got!r} != {ref!r}"]
    return [] if type(got) is type(ref) and got == ref else [f"{path}: {got!r} != {ref!r}"]


@needs_layout
def test_read_board_matches_pcbnew_on_a_routed_board(tmp_path):
    from pcbagent.kicad.tools import find_kicad_python
    from pcbagent.layout import pipeline
    from pcbagent.project import Project
    p = Project.create(tmp_path / "divider", "divider")
    p.write_spec(load_example("divider", "spec"))
    assert p.submit_circuit(load_example("divider", "circuit"))["passed"]
    assert pipeline.run(p, load_example("divider", "constraints"))["passed"]
    board_path = Path(p.state()["layout"]["board"])

    src = Path(__file__).resolve().parents[1] / "src"
    out = subprocess.run([find_kicad_python(), "-c", PCBNEW_EXPORT, str(board_path), str(src)],
                         capture_output=True, text=True, timeout=120, env={**os.environ}, check=False)
    assert out.returncode == 0, out.stderr[-1000:]
    want = json.loads(out.stdout.strip().splitlines()[-1])
    got = read_board(board_path)

    assert want["footprints"] and want["tracks"] and want["zones"]
    assert mismatches(want, got) == []
    assert all(0 <= f["rot"] < 360 and all(0 <= q["lrot"] < 360 for q in f["pads"]) for f in got["footprints"])
