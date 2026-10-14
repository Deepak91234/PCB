"""KiCad design-rule check: run it and parse the result.

KiCad 8+ has ``kicad-cli pcb drc --format json``. KiCad 7 only offers
``pcbnew.WriteDRCReport`` (text), which the worker writes; both are parsed
into the same structure.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .tools import find_kicad_cli, kicad_major

_ITEM = re.compile(r"^\s+@\(\s*([-\d.]+)\s*mm,\s*([-\d.]+)\s*mm\):\s*(.*)$")
_HEAD = re.compile(r"^\[([a-z_]+)\]:\s*(.*)$")
_SECTION = re.compile(r"^\*\* Found (\d+) (DRC violations|unconnected pads|Footprint errors) \*\*")


def parse_text_report(text: str) -> dict:
    out = {"violations": [], "unconnected_items": [], "footprint_errors": []}
    section = None
    current = None
    for line in text.splitlines():
        m = _SECTION.match(line)
        if m:
            section = {"DRC violations": "violations", "unconnected pads": "unconnected_items",
                       "Footprint errors": "footprint_errors"}[m.group(2)]
            current = None
            continue
        m = _HEAD.match(line)
        if m and section:
            current = {"type": m.group(1), "description": m.group(2), "severity": "error", "items": []}
            out[section].append(current)
            continue
        if current is None:
            continue
        if "Severity:" in line:
            current["severity"] = line.split("Severity:")[1].strip().split()[0].lower()
            continue
        m = _ITEM.match(line)
        if m:
            current["items"].append({"x": float(m.group(1)), "y": float(m.group(2)),
                                     "description": m.group(3).strip()})
    return out


def parse_json_report(data: dict) -> dict:
    def conv(v):
        items = [{"x": i.get("pos", {}).get("x"), "y": i.get("pos", {}).get("y"),
                  "description": i.get("description", "")} for i in v.get("items", [])]
        return {"type": v.get("type"), "description": v.get("description"),
                "severity": v.get("severity", "error"), "items": items}
    return {"violations": [conv(v) for v in data.get("violations", [])],
            "unconnected_items": [conv(v) for v in data.get("unconnected_items", [])],
            "footprint_errors": [conv(v) for v in data.get("schematic_parity", [])]}


def run_cli(board: Path, out_json: Path) -> dict | None:
    """kicad-cli DRC (KiCad 8+). Returns parsed result or None if unsupported."""
    if kicad_major() < 8:
        return None
    cmd = [find_kicad_cli(), "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm",
           "-o", str(out_json), str(board)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if not out_json.exists():
        raise RuntimeError(f"kicad-cli pcb drc failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    return parse_json_report(json.loads(out_json.read_text()))


def summarize(parsed: dict) -> dict:
    errors = [v for v in parsed["violations"] if v["severity"] == "error"]
    warnings = [v for v in parsed["violations"] if v["severity"] == "warning"]
    unconnected = [v for v in parsed["unconnected_items"] if v["severity"] != "ignore"]
    by_type: dict[str, int] = {}
    for v in errors:
        by_type[v["type"]] = by_type.get(v["type"], 0) + 1
    warn_types: dict[str, int] = {}
    for v in warnings:
        warn_types[v["type"]] = warn_types.get(v["type"], 0) + 1
    return {"errors": len(errors), "warnings": len(warnings), "unconnected": len(unconnected),
            "error_types": by_type, "warning_types": warn_types,
            "first_errors": [_describe(v) for v in errors[:8]],
            "first_unconnected": [_describe(v) for v in unconnected[:8]]}


def _describe(v: dict) -> str:
    items = "; ".join(i["description"] for i in v["items"][:2])
    return f"[{v['type']}] {v['description']}" + (f" ({items})" if items else "")
