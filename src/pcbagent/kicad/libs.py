"""Locate the installed KiCad libraries and read footprint geometry.

Footprint and symbol names used anywhere in PCBAgent are checked against the
libraries that are actually installed. Nothing is invented: if a footprint is
missing, the step fails with the path it looked in.
"""

from __future__ import annotations

import glob
import math
import os
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .. import sexpr

_VERSIONS = ["10", "9", "8", "7", "6"]


def _candidates(kind: str) -> list[Path]:
    """kind: 'footprints' or 'symbols'."""
    env_kind = "FOOTPRINT" if kind == "footprints" else "SYMBOL"
    out: list[Path] = []
    custom = os.environ.get(f"PCBAGENT_KICAD_{env_kind}_DIR")
    if custom:
        out.append(Path(custom))
    for v in _VERSIONS:
        val = os.environ.get(f"KICAD{v}_{env_kind}_DIR")
        if val:
            out.append(Path(val))
    out += [Path(f"/usr/share/kicad/{kind}"), Path(f"/usr/local/share/kicad/{kind}"),
            Path(f"/opt/homebrew/share/kicad/{kind}"),
            Path(f"/Applications/KiCad/KiCad.app/Contents/SharedSupport/{kind}")]
    if sys.platform == "win32":
        for base in sorted(glob.glob(r"C:\Program Files\KiCad\*"), reverse=True):
            out.append(Path(base) / "share" / "kicad" / kind)
    return out


@lru_cache(maxsize=None)
def footprint_dir() -> Path | None:
    for p in _candidates("footprints"):
        if p.is_dir() and any(p.glob("*.pretty")):
            return p
    return None


@lru_cache(maxsize=None)
def symbol_dir() -> Path | None:
    for p in _candidates("symbols"):
        if p.is_dir() and any(p.glob("*.kicad_sym")):
            return p
    return None


class LibraryError(LookupError):
    pass


def footprint_path(fpid: str) -> Path:
    lib, _, name = fpid.partition(":")
    base = footprint_dir()
    if base is None:
        raise LibraryError("KiCad footprint libraries not found; install KiCad or set "
                           "PCBAGENT_KICAD_FOOTPRINT_DIR / KICAD9_FOOTPRINT_DIR")
    path = base / f"{lib}.pretty" / f"{name}.kicad_mod"
    if not path.exists():
        raise LibraryError(f"footprint {fpid} not found (looked for {path})")
    return path


def footprint_exists(fpid: str) -> bool:
    try:
        footprint_path(fpid)
        return True
    except LibraryError:
        return False


_SUBUNIT = re.compile(r"_\d+_\d+$")


@lru_cache(maxsize=64)
def _symbol_index(lib_file: str) -> dict[str, str | None]:
    """symbol name -> name it extends (or None).

    Indentation differs between KiCad versions (spaces in 7, tabs in 8+), so
    top-level symbols are told apart from unit sub-symbols by KiCad's
    ``<name>_<unit>_<style>`` naming of the latter, not by indentation.
    """
    text = Path(lib_file).read_text(errors="replace")
    out: dict[str, str | None] = {}
    for m in re.finditer(r'\(symbol "([^"]+)"', text):
        name = m.group(1)
        if _SUBUNIT.search(name) or ":" in name:
            continue
        tail = text[m.end(): m.end() + 400]
        ext = re.match(r'\s*\(extends "([^"]+)"\)', tail)
        out[name] = ext.group(1) if ext else None
    return out


def symbol_exists(symbol: str) -> bool:
    lib, _, name = symbol.partition(":")
    base = symbol_dir()
    if base is None:
        return False
    path = base / f"{lib}.kicad_sym"
    if not path.exists():
        return False
    return name in _symbol_index(str(path))


def symbol_lib_path(symbol: str) -> Path | None:
    base = symbol_dir()
    if base is None:
        return None
    path = base / f"{symbol.partition(':')[0]}.kicad_sym"
    return path if path.exists() else None


# --- footprint geometry --------------------------------------------------------

@dataclass
class Pad:
    number: str
    x: float
    y: float
    w: float
    h: float
    angle: float
    shape: str
    kind: str            # smd / thru_hole / np_thru_hole / connect
    layers: list[str]
    drill: float = 0.0


@dataclass
class Footprint:
    fpid: str
    pads: list[Pad]
    courtyard: tuple[float, float, float, float]  # xmin, ymin, xmax, ymax (footprint coords, mm)
    through_hole: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def width(self) -> float:
        return self.courtyard[2] - self.courtyard[0]

    @property
    def height(self) -> float:
        return self.courtyard[3] - self.courtyard[1]

    def pads_by_number(self) -> dict[str, list[Pad]]:
        out: dict[str, list[Pad]] = {}
        for p in self.pads:
            out.setdefault(p.number, []).append(p)
        return out


def _bbox_points(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def parse_footprint(text: str, fpid: str = "") -> Footprint:
    root = sexpr.loads(text)
    pads: list[Pad] = []
    crt_points: list[tuple[float, float]] = []
    other_points: list[tuple[float, float]] = []
    for item in sexpr.children(root):
        h = sexpr.head(item)
        if h == "pad":
            number = str(item[1]) if len(item) > 1 else ""
            kind = str(item[2]) if len(item) > 2 else "smd"
            shape = str(item[3]) if len(item) > 3 else "rect"
            at = sexpr.floats(sexpr.child(item, "at") or ["at", 0, 0])
            size = sexpr.floats(sexpr.child(item, "size") or ["size", 0, 0])
            layers_node = sexpr.child(item, "layers") or ["layers"]
            drill_node = sexpr.child(item, "drill")
            drill = 0.0
            if drill_node:
                nums = sexpr.floats(drill_node)
                if not nums and len(drill_node) > 2:  # (drill oval w h)
                    nums = sexpr.floats(drill_node, 2)
                drill = nums[0] if nums else 0.0
            x, y = at[0], at[1]
            angle = at[2] if len(at) > 2 else 0.0
            pads.append(Pad(number, x, y, size[0] if size else 0, size[1] if len(size) > 1 else 0,
                            angle, shape, kind, [str(layer) for layer in layers_node[1:]], drill))
            w, hgt = (size + [0, 0])[:2]
            for dx, dy in ((-w / 2, -hgt / 2), (w / 2, hgt / 2), (-w / 2, hgt / 2), (w / 2, -hgt / 2)):
                rx, ry = _rot(dx, dy, angle)
                other_points.append((x + rx, y + ry))
        elif h in ("fp_line", "fp_rect", "fp_circle", "fp_poly", "fp_arc"):
            layer = str(sexpr.value(item, "layer", ""))
            pts = _graphic_points(item)
            if layer.endswith("CrtYd"):
                crt_points.extend(pts)
            elif layer.endswith(("SilkS", "Fab")):
                other_points.extend(pts)
    if crt_points:
        bbox = _bbox_points(crt_points)
    elif other_points:
        x0, y0, x1, y1 = _bbox_points(other_points)
        bbox = (x0 - 0.25, y0 - 0.25, x1 + 0.25, y1 + 0.25)
    else:
        bbox = (-0.5, -0.5, 0.5, 0.5)
    th = any(p.kind == "thru_hole" for p in pads)
    return Footprint(fpid, pads, bbox, th)


def _rot(x: float, y: float, deg: float) -> tuple[float, float]:
    # KiCad rotates counter-clockwise on screen with y pointing down.
    if not deg:
        return x, y
    a = math.radians(-deg)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def _graphic_points(item) -> list[tuple[float, float]]:
    h = sexpr.head(item)
    pts = []
    if h in ("fp_line", "fp_rect", "fp_arc"):
        for key in ("start", "end", "mid"):
            node = sexpr.child(item, key)
            if node:
                v = sexpr.floats(node)
                pts.append((v[0], v[1]))
    elif h == "fp_circle":
        c = sexpr.floats(sexpr.child(item, "center"))
        e = sexpr.floats(sexpr.child(item, "end"))
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        pts += [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
    elif h == "fp_poly":
        node = sexpr.child(item, "pts")
        for xy in sexpr.children(node, "xy"):
            v = sexpr.floats(xy)
            pts.append((v[0], v[1]))
    return pts


@lru_cache(maxsize=256)
def load_footprint(fpid: str) -> Footprint:
    return parse_footprint(footprint_path(fpid).read_text(errors="replace"), fpid)
