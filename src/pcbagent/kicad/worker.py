"""KiCad worker: runs under KiCad's Python (needs the ``pcbnew`` module).

Usage: ``<kicad-python> worker.py job.json`` prints one JSON result line.

Actions
  build   create the board (footprints, nets, outline, net classes, rules),
          save .kicad_pcb/.kicad_pro and export a Specctra DSN
  route   apply a Freerouting session (tracks, vias), add ground pours,
          fill zones, save, and write a DRC report

Kept stdlib + pcbnew only, and compatible with KiCad 7, 8 and 9 (SWIG API).
"""

from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pcbnew  # noqa: E402

from pcbagent.layout import ses as ses_mod  # noqa: E402


def mm(v: float) -> int:
    return pcbnew.FromMM(float(v))


def vec(x: float, y: float):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def _set_field(fp, name: str, value: str) -> None:
    for attempt in ("SetField", "SetProperty"):
        fn = getattr(fp, attempt, None)
        if fn is None:
            continue
        try:
            fn(name, value)
            return
        except Exception:  # noqa: BLE001 - API differs across KiCad versions
            continue


def _outline(board, x0, y0, x1, y1, width=0.1):
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for i in range(4):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetStart(vec(*pts[i]))
        s.SetEnd(vec(*pts[(i + 1) % 4]))
        s.SetWidth(mm(width))
        board.Add(s)


def _patch_project(pro_path: str, job: dict) -> None:
    with open(pro_path) as fh:
        pro = json.load(fh)
    rules = job["rules"]
    ns = pro.setdefault("net_settings", {})
    classes = ns.get("classes") or [{}]
    default = dict(classes[0])
    default.update(name="Default", track_width=rules["track"], clearance=rules["clearance"],
                   via_diameter=rules["via_diameter"], via_drill=rules["via_drill"])
    new_classes = [default]
    patterns = []
    for nc in job.get("netclasses", []):
        c = dict(default)
        c.update(name=nc["name"], track_width=nc["track"], clearance=nc.get("clearance", rules["clearance"]),
                 via_diameter=nc.get("via_diameter", rules["via_diameter"]),
                 via_drill=nc.get("via_drill", rules["via_drill"]))
        new_classes.append(c)
        for net in nc["nets"]:
            patterns.append({"netclass": nc["name"], "pattern": net})
    ns["classes"] = new_classes
    ns["netclass_patterns"] = patterns
    ds = pro.setdefault("board", {}).setdefault("design_settings", {})
    r = ds.setdefault("rules", {})
    r.update(min_clearance=rules["min_clearance"], min_track_width=rules["min_track"],
             min_copper_edge_clearance=rules["edge_clearance"], min_via_diameter=rules["min_via_diameter"],
             min_through_hole_diameter=rules["min_drill"], min_via_annular_width=rules["min_annular"],
             min_hole_clearance=rules.get("hole_clearance", 0.25))
    sev = ds.setdefault("rule_severities", {})
    # Footprints come straight from the installed library files; a headless run
    # has no fp-lib-table, so these library-link checks only produce noise.
    sev.update(lib_footprint_issues="ignore", lib_footprint_mismatch="ignore")
    with open(pro_path, "w") as fh:
        json.dump(pro, fh, indent=2)


def build(job: dict) -> dict:
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    ox, oy = job["origin"]
    w, h = job["width"], job["height"]
    nets = {}
    for name in job["nets"]:
        ni = pcbnew.NETINFO_ITEM(board, name)
        board.Add(ni)
        nets[name] = ni
    placed = []
    for f in job["footprints"]:
        lib, _, name = f["fpid"].partition(":")
        fp = pcbnew.FootprintLoad(f["lib_path"], name)
        if fp is None:
            raise RuntimeError(f"cannot load footprint {f['fpid']} from {f['lib_path']}")
        fp.SetFPID(pcbnew.LIB_ID(lib, name))
        fp.SetReference(f["ref"])
        fp.SetValue(f["value"])
        fp.SetPosition(vec(ox + f["x"], oy + f["y"]))
        fp.SetOrientationDegrees(float(f["rotation"]))
        for k, v in f.get("fields", {}).items():
            _set_field(fp, k, v)
        if f.get("path"):
            # Links the footprint to its schematic symbol (Update PCB from Schematic matches by this).
            fp.SetPath(pcbnew.KIID_PATH(f["path"]))
        board.Add(fp)
        for pad in fp.Pads():
            net = f["pads"].get(pad.GetNumber())
            if net:
                pad.SetNet(nets[net])
        bbox = fp.GetBoundingBox(False, False) if hasattr(fp, "GetBoundingBox") else None
        placed.append({"ref": f["ref"], "x": pcbnew.ToMM(fp.GetPosition().x),
                       "y": pcbnew.ToMM(fp.GetPosition().y),
                       "bbox": [pcbnew.ToMM(bbox.GetX()), pcbnew.ToMM(bbox.GetY()),
                                pcbnew.ToMM(bbox.GetWidth()), pcbnew.ToMM(bbox.GetHeight())] if bbox else None})
    _outline(board, ox, oy, ox + w, oy + h)
    path = job["board_path"]
    if not pcbnew.SaveBoard(path, board):
        raise RuntimeError("SaveBoard failed")
    _patch_project(os.path.splitext(path)[0] + ".kicad_pro", job)
    board = pcbnew.LoadBoard(path)
    ok = pcbnew.ExportSpecctraDSN(board, job["dsn_path"])
    if not ok:
        raise RuntimeError("ExportSpecctraDSN failed")
    classes = {}
    for name in job["nets"]:
        ni = board.FindNet(name)
        if ni:
            classes[name] = ni.GetNetClassName()
    return {"ok": True, "board": path, "dsn": job["dsn_path"], "placed": placed, "netclasses": classes}


def _layer(board, name: str):
    lid = board.GetLayerID(name)
    if lid < 0:
        raise RuntimeError(f"unknown layer {name}")
    return lid


def _add_zone(board, net, layer_name: str, x0, y0, x1, y1, clearance: float, min_width: float):
    zone = pcbnew.ZONE(board)
    zone.SetLayer(_layer(board, layer_name))
    zone.SetNet(net)
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        outline.Append(mm(x), mm(y))
    try:
        zone.SetLocalClearance(mm(clearance))
    except Exception:  # noqa: BLE001 - KiCad 9 takes an optional
        pass
    zone.SetMinThickness(mm(min_width))
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
    zone.SetThermalReliefGap(mm(0.3))
    zone.SetThermalReliefSpokeWidth(mm(0.4))
    try:
        zone.SetIsFilled(False)
    except Exception:  # noqa: BLE001
        pass
    board.Add(zone)
    return zone


def route(job: dict) -> dict:
    board = pcbnew.LoadBoard(job["board_path"])
    with open(job["ses_path"]) as fh:
        parsed = ses_mod.parse(fh.read())
    tracks = vias = 0
    missing = set()
    for seg in ses_mod.segments(parsed):
        net = board.FindNet(seg["net"])
        if net is None:
            missing.add(seg["net"])
            continue
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(vec(*seg["start"]))
        t.SetEnd(vec(*seg["end"]))
        t.SetWidth(mm(seg["width"]))
        t.SetLayer(_layer(board, seg["layer"]))
        t.SetNet(net)
        board.Add(t)
        tracks += 1
    for v in parsed["vias"]:
        net = board.FindNet(v["net"])
        if net is None:
            missing.add(v["net"])
            continue
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(vec(v["x"], v["y"]))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetDrill(mm(v["drill"]))
        try:
            via.SetWidth(mm(v["diameter"]))
        except Exception:  # noqa: BLE001
            via.SetWidth(mm(v["diameter"]), pcbnew.F_Cu)
        via.SetNet(net)
        board.Add(via)
        vias += 1
    zones = 0
    pour = job.get("pour")
    if pour:
        net = board.FindNet(pour["net"])
        if net is not None:
            ox, oy = job["origin"]
            inset = pour.get("edge_inset", 0.4)
            for layer in pour.get("layers", ["F.Cu", "B.Cu"]):
                _add_zone(board, net, layer, ox + inset, oy + inset, ox + job["width"] - inset,
                          oy + job["height"] - inset, pour.get("clearance", 0.3), pour.get("min_width", 0.25))
                zones += 1
            filler = pcbnew.ZONE_FILLER(board)
            filler.Fill(board.Zones())
    if not pcbnew.SaveBoard(job["out_path"], board):
        raise RuntimeError("SaveBoard failed")
    report = None
    if job.get("drc_report"):
        board = pcbnew.LoadBoard(job["out_path"])
        try:
            board.BuildConnectivity()
        except Exception:  # noqa: BLE001
            pass
        if hasattr(pcbnew, "WriteDRCReport"):
            units = getattr(pcbnew, "EDA_UNITS_MILLIMETRES", None)
            if units is None:
                units = getattr(pcbnew, "EDA_UNITS_MM", 1)
            pcbnew.WriteDRCReport(board, job["drc_report"], units, True)
            report = job["drc_report"]
    return {"ok": True, "board": job["out_path"], "tracks": tracks, "vias": vias, "zones": zones,
            "missing_nets": sorted(missing), "drc_report": report, "wires": parsed["wires"], "via_list": parsed["vias"]}


def main() -> int:
    job_path = sys.argv[1]
    with open(job_path) as fh:
        job = json.load(fh)
    try:
        result = {"build": build, "route": route}[job["action"]](job)
    except Exception as exc:  # noqa: BLE001 - report everything to the parent process
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
    result["kicad_version"] = pcbnew.Version() if hasattr(pcbnew, "Version") else "unknown"
    with open(job["result_path"], "w") as fh:
        json.dump(result, fh)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
