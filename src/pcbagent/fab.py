"""Fabrication outputs: JLCPCB BOM and CPL, Gerbers and drill files via kicad-cli."""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from .kicad.tools import find_kicad_cli


def write_bom(selection: dict, path: Path) -> Path:
    """JLCPCB assembly BOM: Comment, Designator, Footprint, LCSC Part #."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
    for ln in selection["bom"]:
        w.writerow([ln["value"], ",".join(ln["refs"]), ln["footprint"].split(":", 1)[-1], ln["lcsc"]])
    text = buf.getvalue()
    if selection.get("catalog", {}).get("sample"):
        text = "# SAMPLE CATALOG - fake part numbers, do not order\n" + text
    path.write_text(text)
    return path


def write_cpl(placements: list[dict], path: Path, assembled: set[str]) -> Path:
    """JLCPCB pick-and-place: Designator, Mid X, Mid Y, Layer, Rotation.

    Rotation is KiCad's. JLCPCB's reference orientation differs for some
    packages (SOT-223, SOT-23 and polarized parts often show 90/180 degree
    offsets); check their DFM preview before ordering.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
    for p in sorted(placements, key=lambda p: p["ref"]):
        if p["ref"] not in assembled:
            continue
        w.writerow([p["ref"], f"{p['x_mm']:.3f}mm", f"{p['y_mm']:.3f}mm",
                    "Top" if p.get("side", "top") == "top" else "Bottom", f"{p['rotation']:.0f}"])
    path.write_text(buf.getvalue())
    return path


def export(project_root: Path, board: Path, out_dir: Path) -> dict:
    """Gerbers + drill (zipped for upload), BOM and CPL. Requires kicad-cli."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cli = find_kicad_cli()
    result: dict = {"files": [], "warnings": []}
    gerber_dir = out_dir / "gerbers"
    if gerber_dir.exists():
        shutil.rmtree(gerber_dir)
    gerber_dir.mkdir()
    layers = "F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts"
    cmds = [
        [cli, "pcb", "export", "gerbers", "--output", str(gerber_dir) + "/", "--layers", layers,
         "--subtract-soldermask", "--no-x2", str(board)],
        [cli, "pcb", "export", "drill", "--output", str(gerber_dir) + "/", "--format", "excellon",
         "--drill-origin", "absolute", "--excellon-units", "mm", "--generate-map", "--map-format", "gerberx2",
         str(board)],
    ]
    for cmd in cmds:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"kicad-cli failed ({' '.join(cmd[1:4])}): {(proc.stderr or proc.stdout).strip()[:400]}")
    zpath = out_dir / f"{project_root.name}_gerbers.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(gerber_dir.iterdir()):
            zf.write(f, f.name)
    result["files"].append(str(zpath))

    parts_path = project_root / "parts.json"
    layout_path = project_root / "layout" / "placement.json"
    if parts_path.exists():
        sel = json.loads(parts_path.read_text())
        bom = write_bom(sel, out_dir / "bom_jlcpcb.csv")
        result["files"].append(str(bom))
        assembled = {ref for ref, e in sel["parts"].items() if e.get("status") == "selected"}
        if layout_path.exists():
            placements = json.loads(layout_path.read_text())["placements"]
            cpl = write_cpl(placements, out_dir / "cpl_jlcpcb.csv", assembled)
            result["files"].append(str(cpl))
            result["warnings"].append("CPL rotations are KiCad's; verify orientation in JLCPCB's DFM preview.")
        if sel["catalog"].get("sample"):
            result["warnings"].append("BOM uses the SAMPLE catalog (fake part numbers); do not order it.")
    else:
        result["warnings"].append("no parts.json; BOM/CPL skipped")
    return result
