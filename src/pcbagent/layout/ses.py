"""Parse Freerouting's Specctra session (.ses) into tracks and vias (KiCad mm).

Specctra's y axis points up; KiCad's points down. KiCad's DSN exporter writes
y as -y, so the inverse is a negation. Coordinates are scaled by the declared
``(resolution <unit> <n>)``.

Stdlib-only: the KiCad worker imports this under KiCad's Python.
"""

from __future__ import annotations

import re

from .. import sexpr

_UNIT_MM = {"um": 1e-3, "mm": 1.0, "mil": 0.0254, "inch": 25.4, "cm": 10.0}


def _resolution(node) -> float:
    res = sexpr.child(node, "resolution")
    if res is None or len(res) < 3:
        return 1e-3  # um, 1 per unit
    unit = str(res[1]).lower()
    per = float(res[2])
    return _UNIT_MM.get(unit, 1e-3) / per


def parse(text: str) -> dict:
    root = sexpr.load_specctra(text)
    routes = sexpr.child(root, "routes")
    out = {"wires": [], "vias": [], "placements": []}
    placement = sexpr.child(root, "placement")
    if placement is not None:
        pscale = _resolution(placement)
        for comp in sexpr.children(placement, "component"):
            for pl in sexpr.children(comp, "place"):
                nums = sexpr.floats(pl, 2)
                out["placements"].append({
                    "ref": str(pl[1]), "x": nums[0] * pscale, "y": -nums[1] * pscale,
                    "side": str(pl[4]) if len(pl) > 4 else "front",
                    "rotation": float(pl[5]) if len(pl) > 5 else 0.0})
    if routes is None:
        return out
    scale = _resolution(routes)
    padstacks: dict[str, float] = {}
    lib = sexpr.child(routes, "library_out")
    if lib is not None:
        for ps in sexpr.children(lib, "padstack"):
            name = str(ps[1])
            for shape in sexpr.children(ps, "shape"):
                circ = sexpr.child(shape, "circle")
                if circ is not None:
                    nums = sexpr.floats(circ, 2)
                    if nums:
                        padstacks[name] = nums[0] * scale
                        break
    network = sexpr.child(routes, "network_out")
    if network is None:
        return out
    for net in sexpr.children(network, "net"):
        name = str(net[1])
        for wire in sexpr.children(net, "wire"):
            path = sexpr.child(wire, "path")
            if path is None:
                continue
            layer = str(path[1])
            width = float(path[2]) * scale
            nums = sexpr.floats(path, 3)
            pts = [(nums[i] * scale, -nums[i + 1] * scale) for i in range(0, len(nums) - 1, 2)]
            if len(pts) >= 2:
                out["wires"].append({"net": name, "layer": layer, "width": width, "points": pts})
        for via in sexpr.children(net, "via"):
            ps = str(via[1])
            nums = sexpr.floats(via, 2)
            dia = padstacks.get(ps)
            drill = None
            m = re.search(r"_(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)_um", ps)
            if m:
                dia = dia or float(m.group(1)) / 1000
                drill = float(m.group(2)) / 1000
            out["vias"].append({"net": name, "x": nums[0] * scale, "y": -nums[1] * scale,
                                "diameter": dia or 0.6, "drill": drill or 0.3, "padstack": ps})
    return out


def segments(parsed: dict) -> list[dict]:
    """Flatten wires into straight segments."""
    segs = []
    for w in parsed["wires"]:
        pts = w["points"]
        for a, b in zip(pts, pts[1:]):
            if a != b:
                segs.append({"net": w["net"], "layer": w["layer"], "width": w["width"],
                             "start": a, "end": b})
    return segs
