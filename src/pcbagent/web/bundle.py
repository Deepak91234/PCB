"""Turn a design folder into the JSON the web UI renders.

Every section is optional: a design that has only a locked spec, or a
circuit that failed verification, still produces a bundle with whatever
exists so far. Nothing here runs the pipeline; it only reads its outputs.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..project import STATE_FILE

# Files a user may download from a design, relative to its root.
DOWNLOADS = ("bom_jlcpcb.csv", "{name}.net", "{name}.kicad_sch", "layout/{name}.kicad_pcb", "layout/{name}.kicad_pro",
             "layout/{name}.kicad_sch", "layout/drc.rpt", "fab/*.zip", "fab/*.csv")


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _r(v, n=9):
    return None if v is None else round(v, n)


def summary(root: Path) -> dict:
    """The one-line view used in the design list."""
    state = _load(root / STATE_FILE) or {}
    spec = _load(root / "spec.json") or {}
    lay = state.get("layout") or {}
    return {"id": root.name, "title": spec.get("title") or state.get("request") or root.name,
            "request": state.get("request", ""), "phase": state.get("phase"), "created": state.get("created"),
            "updated": (root / STATE_FILE).stat().st_mtime if (root / STATE_FILE).exists() else None,
            "passed": bool(lay.get("passed")), "board_mm": lay.get("board_mm")}


def list_designs(base: Path) -> list[dict]:
    if not base.is_dir():
        return []
    out = [summary(p.parent) for p in base.glob(f"*/{STATE_FILE}")]
    return sorted(out, key=lambda d: d["updated"] or 0, reverse=True)


def _verification(ver: dict) -> dict:
    reqs = []
    for q in ver.get("requirements", []):
        m = q.get("measure") or {}
        b = q.get("bounds") or {}
        target = m.get("net") or (f"{m['ref']}.{m['pin']}" if m.get("ref") and m.get("pin") else m.get("ref")) \
            or (f"{m.get('input')}→{m.get('output')}" if m.get("input") else "")
        reqs.append({"id": q.get("id"), "description": q.get("description", ""), "condition": q.get("condition"),
                     "measure": m.get("type"), "target_of": target, "hz": m.get("hz"), "unit": q.get("unit", ""),
                     "lo": _r(b.get("min")), "hi": _r(b.get("max")), "target": _r(b.get("target")),
                     "nominal": _r(q.get("nominal")), "min": _r(q.get("min")), "max": _r(q.get("max")),
                     "corners": q.get("corners"), "passed": bool(q.get("passed")), "message": q.get("message", "")})
    ratings = [{"ref": c.get("ref"), "check": c.get("check"), "value": _r(c.get("value"), 6),
                "limit": _r(c.get("limit"), 6), "unit": c.get("unit", ""), "passed": bool(c.get("passed")),
                "message": c.get("message", "")} for c in ver.get("ratings", [])]
    sim = ver.get("simulator") or {}
    return {"passed": bool(ver.get("passed")), "simulator": f"ngspice {sim.get('version', '?')}",
            "methods": sorted({run.get("corner_method", "") for run in ver.get("runs", [])} - {""}),
            "raw_files": sum(len(run.get("raw_files", [])) for run in ver.get("runs", [])),
            "elapsed": ver.get("elapsed_s"), "circuit_version": ver.get("circuit_version"),
            "requirements": reqs, "ratings": ratings, "warnings": ver.get("warnings", []),
            "errors": ver.get("simulation_errors", [])[:5]}


def _parts(parts: dict) -> dict:
    bom = [{"refs": ln.get("refs", []), "value": ln.get("value"), "lcsc": ln.get("lcsc"), "mpn": ln.get("mpn"),
            "manufacturer": ln.get("manufacturer"), "footprint": str(ln.get("footprint", "")).split(":", 1)[-1],
            "library_type": ln.get("library_type"), "stock": ln.get("stock") or 0,
            "price": ln.get("unit_price_usd") or 0, "qty": ln.get("qty_per_board")} for ln in parts.get("bom", [])]
    return {"passed": bool(parts.get("passed")), "boards": parts.get("boards"), "bom": bom,
            "cost": parts.get("cost") or {}, "sample": bool((parts.get("catalog") or {}).get("sample")),
            "failures": [f.get("message", str(f)) for f in parts.get("failures", [])][:10]}


def _layout(lay: dict) -> dict:
    fr = lay.get("freerouting") or {}
    return {"passed": bool(lay.get("passed")), "message": lay.get("message"), "board_mm": lay.get("board_mm"),
            "drc": lay.get("drc") or {"errors": None, "unconnected": None, "warnings": None, "warning_types": {}},
            "netclasses": lay.get("netclasses", []), "tracks": lay.get("tracks"), "vias": lay.get("vias"),
            "constraints": [{k: c.get(k) for k in ("type", "ref", "to", "refs", "pin", "side", "max_mm", "min_mm",
                                                   "axis", "auto", "satisfied", "off_mm", "reason")}
                            for c in lay.get("constraints", [])],
            "freerouting_s": fr.get("seconds"), "blocked": fr.get("blocked_network_attempts"),
            "elapsed": lay.get("elapsed_s"), "kicad": lay.get("kicad_version")}


def crop_svg(text: str) -> str:
    """Crop a kicad-cli schematic SVG to its drawing and make it embeddable inline."""
    body = text[text.index("<svg"):]
    xs, ys = [], []
    for d in re.findall(r'<path d="([^"]*)"', body):
        nums = [float(x) for x in re.findall(r"-?\d+\.?\d*", d)]
        xs += nums[0::2]
        ys += nums[1::2]
    page_w = None
    m = re.search(r'viewBox="[\d.\-]+ [\d.\-]+ ([\d.]+) ([\d.]+)"', body)
    if m:
        page_w = float(m.group(1))
    for x, y, w, h in re.findall(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', body):
        x, y, w, h = map(float, (x, y, w, h))
        if page_w and w >= page_w * 0.95:  # the page background
            continue
        xs += [x, x + w]
        ys += [y, y + h]
    if not xs or not ys:
        return body
    pad = 4.0
    x0, y0, x1, y1 = min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad
    body = re.sub(r'width="[\d.]+mm" height="[\d.]+mm" viewBox="[^"]*"',
                  f'viewBox="{x0:.2f} {y0:.2f} {x1 - x0:.2f} {y1 - y0:.2f}" role="img"', body, count=1)
    if page_w:
        body = re.sub(r'<rect x="0(?:\.0+)?" y="0(?:\.0+)?" width="[\d.]+" height="[\d.]+" rx="[\d.]+" />', "",
                      body, count=1)
    body = re.sub(r"<title>.*?</title>", "<title>Schematic</title>", body, flags=re.S)
    # Inline SVG must not carry scripts or external references.
    body = re.sub(r"<script.*?</script>", "", body, flags=re.S | re.I)
    body = re.sub(r"""\son[a-z]+\s*=\s*("[^"]*"|'[^']*')""", "", body, flags=re.I)
    return re.sub(r"\n\s*", "\n", body)


def schematic_svg(root: Path, name: str) -> str | None:
    """Render the design's schematic with kicad-cli, cached next to the design until it changes."""
    sch = root / "layout" / f"{name}.kicad_sch"
    if not sch.exists():
        sch = root / f"{name}.kicad_sch"
    if not sch.exists():
        return None
    cache = root / "agent" / "schematic.svg"
    try:
        if cache.exists() and cache.stat().st_mtime >= sch.stat().st_mtime:
            return cache.read_text()
        from ..kicad.tools import find_kicad_cli
        out_dir = root / "agent" / "sch_svg"
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("*.svg"):
            old.unlink()
        subprocess.run([find_kicad_cli(), "sch", "export", "svg", "--exclude-drawing-sheet", "-o", str(out_dir),
                        str(sch)], capture_output=True, text=True, timeout=60, check=True)
        svg = next(out_dir.glob("*.svg"), None)
        if svg is None:
            return None
        text = crop_svg(svg.read_text())
        cache.write_text(text)
        return text
    except Exception:  # noqa: BLE001 - the schematic view is optional; KiCad may be missing or fail
        return None


def files(root: Path, name: str) -> list[dict]:
    out = []
    for pattern in DOWNLOADS:
        for path in sorted(root.glob(pattern.format(name=name))):
            if path.is_file():
                out.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size})
    return out


def design_bundle(root: Path) -> dict:
    root = Path(root)
    name = root.name
    state = _load(root / STATE_FILE) or {}
    spec = _load(root / "spec.json") if state.get("spec_hash") else None
    circuit = _load(root / "circuit.json")
    ver = _load(root / "verification.json")
    parts = _load(root / "parts.json") if ver and ver.get("passed") else None
    lay = _load(root / "layout" / "result.json")
    trace = _load(root / "layout" / "layout_trace.json")
    out = {
        **summary(root),
        "state": {k: state.get(k) for k in ("phase", "spec_hash", "circuit_attempts", "circuit_version",
                                             "layout_attempts", "parts_ok")},
        "spec": None, "components": (circuit or {}).get("components", []), "verification": None,
        "net_currents": (ver or {}).get("net_currents_a", {}), "parts": None, "layout": None, "board": None,
        "frames": [], "route_order": [], "schematic": None, "files": files(root, name),
    }
    if spec:
        out["spec"] = {"title": spec.get("title"), "summary": spec.get("summary", ""), "ports": spec.get("ports", []),
                       "conditions": [{"id": c.get("id"), "description": c.get("description", ""),
                                       "sources": c.get("sources", []), "loads": c.get("loads", [])}
                                      for c in spec.get("conditions", [])],
                       "requirements": len(spec.get("requirements", []))}
    if ver:
        out["verification"] = _verification(ver)
        # The report describes the circuit that was simulated; show that one, not a newer invalid submission.
    if parts:
        out["parts"] = _parts(parts)
    if lay:
        out["layout"] = _layout(lay)
        board = root / "layout" / f"{name}.kicad_pcb"
        if board.exists() and (lay.get("tracks") is not None or lay.get("passed")):
            try:
                from .board import read_board
                out["board"] = read_board(board)
            except Exception:  # noqa: BLE001 - a board the reader cannot parse still leaves the rest viewable
                out["board"] = None
        if trace:
            out["frames"] = [{ref: [round(p[0], 3), round(p[1], 3), p[2]] for ref, p in f.items()}
                             for f in trace.get("placement_frames", [])]
            out["route_order"] = [g["net"] for g in trace.get("routing", [])]
    if circuit and (root / f"{name}.kicad_sch").exists():
        out["schematic"] = schematic_svg(root, name)
    return out
