"""Board geometry for the browser viewer, read straight from a ``.kicad_pcb`` file.

This is the stdlib-only twin of an exporter that walks the board through
pcbnew: the web server runs under ordinary Python, so it cannot import
pcbnew and reads the file with :mod:`pcbagent.sexpr` instead.

File-format conventions this relies on (KiCad 6 to 9; checked against pcbnew
7.0.11 on generated boards):

* ``(footprint ... (at X Y [A]))`` places the footprint; A is degrees,
  counter-clockwise as seen on screen (y axis points down).
* Pad, text and graphic coordinates inside a footprint are already in the
  footprint's local, unrotated frame. A board point is
  ``(X + x cos A + y sin A, Y - x sin A + y cos A)``.
* A pad's ``(at x y [a])`` angle is its absolute orientation, so the angle
  relative to the footprint is ``a - A`` (an omitted ``a`` means 0).
* Back-side footprints store their pads already mirrored, so they need no
  special handling.

Output (all lengths in mm, rounded to 4 decimals; angles in [0, 360))::

    {"outline": [x0, y0, x1, y1] | None,
     "footprints": [{"ref", "value", "fpid", "x", "y", "rot", "side",
                     "pads": [{"n", "net", "shape", "w", "h", "lx", "ly",
                               "lrot", "rr", "drill", "th"}],
                     "court": [x0, y0, x1, y1] | None,
                     "label": {"lx", "ly", "size"}}],
     "tracks": [[x1, y1, x2, y2, width, layer, net]],
     "vias": [[x, y, diameter, drill, net]],
     "zones": [{"layer", "net", "pts": [[x, y], ...]}]}
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from itertools import pairwise
from pathlib import Path

from .. import sexpr
from ..sexpr import Quoted

Point = tuple[float, float]
Xform = Callable[[float, float], Point]

EDGE_LAYER = "Edge.Cuts"
COURTYARD_LAYERS = ("F.CrtYd", "B.CrtYd")
COURTYARD_HEADS = ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc")
BOARD_EDGE_HEADS = ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_curve")
FP_EDGE_HEADS = ("fp_line", "fp_rect", "fp_arc", "fp_circle", "fp_poly", "fp_curve")
PAD_SHAPES = {"rect": "rect", "roundrect": "roundrect", "circle": "circle", "oval": "oval"}
ARC_STEP = math.radians(10.0)  # arc tracks are drawn as chords of at most this sweep


def read_board(path) -> dict:
    """Read a ``.kicad_pcb`` file and return its geometry for the web viewer."""
    return parse_board(Path(path).read_text(encoding="utf-8"))


def parse_board(text: str) -> dict:
    """Parse ``.kicad_pcb`` text and return its geometry for the web viewer."""
    tree = sexpr.loads(text)
    if sexpr.head(tree) != "kicad_pcb":
        raise ValueError(f"not a KiCad board: top-level node is {sexpr.head(tree)!r}")
    nets = {str(n[1]): str(n[2]) for n in sexpr.children(tree, "net") if len(n) >= 3}

    footprints = []
    edge: list[Point] = []
    tracks: list[list] = []
    vias: list[list] = []
    for node in sexpr.children(tree):
        h = sexpr.head(node)
        if h in ("footprint", "module"):
            fp, fp_edge = _footprint(node, nets)
            footprints.append(fp)
            edge += fp_edge
        elif h in BOARD_EDGE_HEADS and _layer(node) == EDGE_LAYER:
            edge += _geometry_points(node, _identity)
        elif h == "segment":
            start, end = _xy(node, "start"), _xy(node, "end")
            if start and end:
                tracks.append(_track(start, end, node, nets))
        elif h == "arc":
            pts = [_xy(node, k) for k in ("start", "mid", "end")]
            if all(pts):
                poly = _arc_polyline(*pts)
                tracks += [_track(a, b, node, nets) for a, b in pairwise(poly)]
        elif h == "via":
            at = _xy(node, "at")
            if at:
                vias.append([_r(at[0]), _r(at[1]), _r(_num(node, "size")), _r(_num(node, "drill")),
                             _net(node, nets)])

    return {"outline": _bbox(edge), "footprints": footprints, "tracks": tracks, "vias": vias,
            "zones": _zones(tree, nets)}


# --- footprints ---------------------------------------------------------------

def _footprint(node: list, nets: dict) -> tuple[dict, list[Point]]:
    at = _floats(node, "at")
    fx, fy = (at + [0.0, 0.0])[:2]
    rot = at[2] if len(at) > 2 else 0.0
    side = "bottom" if _layer(node) == "B.Cu" else "top"
    fpid = str(node[1]) if len(node) > 1 and not isinstance(node[1], list) else ""

    pads = [_pad(p, rot, nets) for p in sexpr.children(node, "pad")]

    # Courtyard bbox in the local frame; prefer the footprint's own side.
    own = "B.CrtYd" if side == "bottom" else "F.CrtYd"
    court = None
    for layer in (own,) + tuple(x for x in COURTYARD_LAYERS if x != own):
        pts = [q for g in sexpr.children(node) if sexpr.head(g) in COURTYARD_HEADS and _layer(g) == layer
               for q in _geometry_points(g, _identity)]
        if pts:
            court = _bbox(pts)
            break

    # Edge.Cuts drawn inside the footprint count toward the board outline.
    to_board = _local_to_board(fx, fy, rot)
    edge = [q for g in sexpr.children(node) if sexpr.head(g) in FP_EDGE_HEADS and _layer(g) == EDGE_LAYER
            for q in _geometry_points(g, to_board)]

    ref_node, ref = _field(node, "reference", "Reference")
    _, value = _field(node, "value", "Value")
    label = {"lx": 0.0, "ly": 0.0, "size": 1.0}
    if ref_node is not None:
        lat = _floats(ref_node, "at") + [0.0, 0.0]
        label = {"lx": _r(lat[0]), "ly": _r(lat[1]), "size": _r(_font_height(ref_node))}

    return ({"ref": ref, "value": value, "fpid": fpid, "x": _r(fx), "y": _r(fy), "rot": _angle(rot),
             "side": side, "pads": pads, "court": court, "label": label}, edge)


def _field(fp: list, text_kind: str, prop_name: str) -> tuple[list | None, str]:
    """Reference/value text: ``(fp_text reference "R1" ...)`` (KiCad <= 7) or
    ``(property "Reference" "R1" ...)`` (KiCad 8+)."""
    found = None
    for c in sexpr.children(fp):
        h = sexpr.head(c)
        if h == "fp_text" and len(c) > 2 and str(c[1]) == text_kind:
            return c, str(c[2])
        if h == "property" and len(c) > 2 and str(c[1]) == prop_name and found is None:
            found = c
    if found is not None:
        return found, str(found[2])
    return None, ""


def _font_height(text_node: list) -> float:
    font = sexpr.child(sexpr.child(text_node, "effects") or [], "font")
    size = _floats(font, "size") if font else []
    return size[0] if size else 1.0  # KiCad writes (size HEIGHT WIDTH)


def _pad(p: list, fp_rot: float, nets: dict) -> dict:
    kind = str(p[2]) if len(p) > 2 and not isinstance(p[2], list) else ""
    raw_shape = str(p[3]) if len(p) > 3 and not isinstance(p[3], list) else "rect"
    raw_at = _floats(p, "at")
    at = raw_at + [0.0, 0.0]
    pad_rot = raw_at[2] if len(raw_at) > 2 else 0.0  # absolute orientation; omitted means 0
    size = _floats(p, "size")
    w = size[0] if size else 0.0
    h = size[1] if len(size) > 1 else w

    shape = PAD_SHAPES.get(raw_shape, "rect")
    chamfer = sexpr.child(p, "chamfer")
    if shape == "roundrect" and chamfer is not None and len(chamfer) > 1:
        shape = "rect"  # pcbnew loads a chamfered roundrect as CHAMFERED_RECT
    rr = 0
    if shape == "roundrect":
        rr = _r(float(sexpr.value(p, "roundrect_rratio", 0.25)))

    drill = 0
    d = sexpr.child(p, "drill")
    if d is not None:
        nums = sexpr.floats(d, 2 if len(d) > 1 and d[1] == "oval" else 1)
        if nums and nums[0]:
            drill = _r(nums[0])

    # pcbnew drops the number of a non-plated hole when it loads the board; do the same.
    number = str(p[1]) if len(p) > 1 and kind != "np_thru_hole" else ""
    return {"n": number, "net": _net(p, nets), "shape": shape,
            "w": _r(w), "h": _r(h), "lx": _r(at[0]), "ly": _r(at[1]), "lrot": _angle(pad_rot - fp_rot),
            "rr": rr, "drill": drill, "th": kind == "thru_hole"}


# --- tracks and zones -----------------------------------------------------------

def _track(a: Point, b: Point, node: list, nets: dict) -> list:
    return [_r(a[0]), _r(a[1]), _r(b[0]), _r(b[1]), _r(_num(node, "width")), _layer(node), _net(node, nets)]


def _zones(tree: list, nets: dict) -> list[dict]:
    out = []
    for z in sexpr.children(tree, "zone"):
        name = sexpr.value(z, "net_name")
        net = str(name) if name is not None else _net(z, nets)
        for fpoly in sexpr.children(z, "filled_polygon"):
            layer = str(sexpr.value(fpoly, "layer", sexpr.value(z, "layer", "")))
            pts = []
            for xy in sexpr.children(sexpr.child(fpoly, "pts") or [], "xy"):
                v = sexpr.floats(xy)
                if len(v) >= 2:
                    pts.append([_r(v[0]), _r(v[1])])
            out.append({"layer": layer, "net": net, "pts": pts})
    return out


def _net(node: list, nets: dict) -> str:
    """Net name from ``(net N)``, ``(net N "name")`` or ``(net "name")``."""
    n = sexpr.child(node, "net")
    if n is None or len(n) < 2:
        return ""
    if len(n) >= 3:
        return str(n[2])
    tok = n[1]
    if isinstance(tok, Quoted):
        return str(tok)
    if str(tok) in nets:
        return nets[str(tok)]
    try:
        int(tok)
        return ""
    except ValueError:
        return str(tok)


# --- geometry ---------------------------------------------------------------------

def _identity(x: float, y: float) -> Point:
    return x, y


def _local_to_board(fx: float, fy: float, deg: float) -> Xform:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return lambda x, y: (fx + x * c + y * s, fy - x * s + y * c)


def _geometry_points(g: list, xf: Xform) -> list[Point]:
    """Points whose bbox is the bbox of graphic ``g`` after mapping it with ``xf``
    (a rotation plus translation, so circles and arcs keep their shape)."""
    h = sexpr.head(g)
    kind = h.split("_", 1)[1] if h and "_" in h else ""
    if kind == "line":
        return [xf(*p) for p in (_xy(g, "start"), _xy(g, "end")) if p]
    if kind == "rect":
        a, b = _xy(g, "start"), _xy(g, "end")
        if not (a and b):
            return []
        return [xf(*p) for p in ((a[0], a[1]), (b[0], a[1]), (b[0], b[1]), (a[0], b[1]))]
    if kind == "circle":
        c, e = _xy(g, "center"), _xy(g, "end")
        if not (c and e):
            return []
        c = xf(*c)
        r = math.dist(c, xf(*e))
        return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
    if kind == "arc":
        arc = _arc_points(g)
        return _arc_extremes(*(xf(*p) for p in arc)) if arc else []
    if kind == "poly":
        return [xf(*p) for p in _pts(sexpr.child(g, "pts"))]
    if kind == "curve":
        ctrl = [xf(*p) for p in _pts(sexpr.child(g, "pts"))]
        if len(ctrl) != 4:
            return ctrl
        return [_bezier(ctrl, i / 32) for i in range(33)]
    return []


def _arc_points(g: list) -> tuple[Point, Point, Point] | None:
    """(start, mid, end) of an arc graphic, from KiCad 6+ or legacy KiCad 5 syntax."""
    s, m, e = _xy(g, "start"), _xy(g, "mid"), _xy(g, "end")
    if s and m and e:
        return s, m, e
    if s and e:  # KiCad 5: (start CENTER) (end ARC_START) (angle DEG), end = rotate(start, -angle)
        angle = _num(g, "angle")
        c, p0 = s, e
        return p0, _rotate_about(p0, c, angle / 2), _rotate_about(p0, c, angle)
    return None


def _rotate_about(p: Point, c: Point, deg: float) -> Point:
    a = math.radians(deg)
    dx, dy = p[0] - c[0], p[1] - c[1]
    return c[0] + dx * math.cos(a) - dy * math.sin(a), c[1] + dx * math.sin(a) + dy * math.cos(a)


def _pts(node) -> list[Point]:
    """Vertices of a ``(pts ...)`` list; ``(arc ...)`` entries contribute their extremes."""
    out: list[Point] = []
    for c in sexpr.children(node or []):
        if sexpr.head(c) == "xy":
            v = sexpr.floats(c)
            if len(v) >= 2:
                out.append((v[0], v[1]))
        elif sexpr.head(c) == "arc":
            arc = _arc_points(c)
            if arc:
                out += _arc_extremes(*arc)
    return out


def _circle(a: Point, b: Point, c: Point) -> tuple[Point, float] | None:
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-12:
        return None
    a2, b2, c2 = a[0] ** 2 + a[1] ** 2, b[0] ** 2 + b[1] ** 2, c[0] ** 2 + c[1] ** 2
    ux = (a2 * (b[1] - c[1]) + b2 * (c[1] - a[1]) + c2 * (a[1] - b[1])) / d
    uy = (a2 * (c[0] - b[0]) + b2 * (a[0] - c[0]) + c2 * (b[0] - a[0])) / d
    return (ux, uy), math.dist((ux, uy), a)


def _sweep(center: Point, s: Point, m: Point, e: Point) -> tuple[float, float]:
    """Start angle and signed sweep (radians) of the arc from s through m to e."""
    a0 = math.atan2(s[1] - center[1], s[0] - center[0])
    a1 = math.atan2(m[1] - center[1], m[0] - center[0])
    a2 = math.atan2(e[1] - center[1], e[0] - center[0])
    tau = 2 * math.pi
    d1, d2 = (a1 - a0) % tau, (a2 - a0) % tau
    if d2 == 0:  # full circle (start == end)
        d2 = tau
    return a0, (d2 if d1 <= d2 else d2 - tau)


def _arc_extremes(s: Point, m: Point, e: Point) -> list[Point]:
    circ = _circle(s, m, e)
    if circ is None:
        return [s, m, e]
    (cx, cy), r = circ
    a0, sweep = _sweep((cx, cy), s, m, e)
    out = [s, m, e]
    for k in range(4):
        q = k * math.pi / 2
        rel = (q - a0) % (2 * math.pi) if sweep >= 0 else (a0 - q) % (2 * math.pi)
        if rel <= abs(sweep):
            out.append((cx + r * math.cos(q), cy + r * math.sin(q)))
    return out


def _arc_polyline(s: Point, m: Point, e: Point) -> list[Point]:
    """Chords approximating the arc s -> m -> e; they pass exactly through all three."""
    circ = _circle(s, m, e)
    if circ is None:
        return [s, e]
    (cx, cy), r = circ
    _, full = _sweep((cx, cy), s, m, e)
    out = [s]
    for p, q in ((s, m), (m, e)):
        a0 = math.atan2(p[1] - cy, p[0] - cx)
        a1 = math.atan2(q[1] - cy, q[0] - cx)
        # Both halves turn the same way as the whole arc.
        d = (a1 - a0) % (2 * math.pi)
        if full < 0 and d:
            d -= 2 * math.pi
        n = max(1, math.ceil(abs(d) / ARC_STEP))
        out += [(cx + r * math.cos(a0 + d * i / n), cy + r * math.sin(a0 + d * i / n)) for i in range(1, n)]
        out.append(q)
    return out


def _bezier(c: list[Point], t: float) -> Point:
    u = 1 - t
    w = (u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3)
    return sum(wi * p[0] for wi, p in zip(w, c)), sum(wi * p[1] for wi, p in zip(w, c))


def _bbox(pts: Iterable[Point]) -> list[float] | None:
    pts = list(pts)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys))]


# --- small accessors ----------------------------------------------------------------

def _floats(node, name: str) -> list[float]:
    c = sexpr.child(node, name) if node else None
    return sexpr.floats(c) if c is not None else []


def _xy(node, name: str) -> Point | None:
    v = _floats(node, name)
    return (v[0], v[1]) if len(v) >= 2 else None


def _num(node, name: str, default: float = 0.0) -> float:
    v = _floats(node, name)
    return v[0] if v else default


def _layer(node) -> str:
    v = sexpr.value(node, "layer")
    return str(v) if v is not None else ""


def _r(v: float) -> float:
    v = round(float(v), 4)
    return v if v else 0.0  # no -0.0


def _angle(deg: float) -> float:
    a = round(float(deg) % 360.0, 4)
    return 0.0 if a >= 360.0 or not a else a
