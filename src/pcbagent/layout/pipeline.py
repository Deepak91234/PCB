"""Layout pipeline: constraints -> placement -> KiCad board -> Freerouting -> DRC.

The model never emits coordinates or tracks. It writes constraints; the
placer chooses positions, Freerouting draws copper, and KiCad's DRC scores
the board. Track widths come from the simulated net currents (IPC-2221,
external layer, 1 oz copper, 10 C rise).
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import time
from pathlib import Path

from ..circuit import Circuit
from ..kicad import drc as drc_mod
from ..kicad import libs
from ..kicad.tools import ToolMissing, find_kicad_python, kicad_version
from . import constraints as cons_mod
from . import freerouting
from .placer import MET_MM, Item, place

ORIGIN = (100.0, 80.0)  # board top-left on the KiCad page, mm

RULES = {
    # Comfortably inside JLCPCB's standard 2-layer capabilities (0.127 mm track/space, 0.3 mm drill).
    "track": 0.25, "clearance": 0.2, "via_diameter": 0.6, "via_drill": 0.3,
    "min_track": 0.15, "min_clearance": 0.15, "edge_clearance": 0.3,
    "min_via_diameter": 0.5, "min_drill": 0.3, "min_annular": 0.1, "hole_clearance": 0.25,
}

WORKER = Path(__file__).resolve().parent.parent / "kicad" / "worker.py"


class LayoutError(RuntimeError):
    pass


def ipc2221_width_mm(amps: float, rise_c: float = 10.0, oz: float = 1.0) -> float:
    """Minimum external-layer track width for a current (IPC-2221)."""
    if amps <= 0:
        return 0.0
    area_mil2 = (amps / (0.048 * rise_c ** 0.44)) ** (1 / 0.725)
    width_mil = area_mil2 / (1.378 * oz)
    return width_mil * 0.0254


def netclasses_from_currents(currents: dict[str, float], ground: str) -> list[dict]:
    classes: dict[float, list[str]] = {}
    for net, amps in currents.items():
        width = ipc2221_width_mm(amps * 1.25)  # 25 % headroom over the simulated worst case
        width = max(RULES["track"], math.ceil(width * 20) / 20)  # round up to 0.05 mm
        if net == ground or amps >= 0.25:
            width = max(width, 0.4)
        if width > RULES["track"] + 1e-9:
            classes.setdefault(min(width, 2.0), []).append(net)
    out = []
    for width, nets in sorted(classes.items()):
        big = width >= 0.5
        out.append({"name": f"W{int(round(width * 100)):03d}", "nets": sorted(nets), "track": width,
                    "via_diameter": 0.8 if big else RULES["via_diameter"],
                    "via_drill": 0.4 if big else RULES["via_drill"]})
    return out


def _run_worker(job: dict, workdir: Path, timeout: float = 300.0) -> dict:
    py = find_kicad_python()
    job_path = workdir / f"job_{job['action']}.json"
    job["result_path"] = str(workdir / f"result_{job['action']}.json")
    job_path.write_text(json.dumps(job, indent=1))
    proc = subprocess.run([py, str(WORKER), str(job_path)], capture_output=True, text=True, timeout=timeout)
    rp = Path(job["result_path"])
    if not rp.exists():
        raise LayoutError(f"KiCad worker ({job['action']}) crashed: {(proc.stderr or proc.stdout)[-800:]}")
    result = json.loads(rp.read_text())
    if not result.get("ok"):
        raise LayoutError(f"KiCad worker ({job['action']}) failed: {result.get('error')}\n{result.get('traceback', '')[-800:]}")
    return result


def build_items(circuit: Circuit, parts: dict | None, default_pkg: str) -> list[Item]:
    items = []
    for c in circuit.components:
        sel = (parts or {}).get("parts", {}).get(c.ref, {}) if parts else {}
        fpid = sel.get("footprint") or c.footprint(default_pkg)
        fp = libs.load_footprint(fpid)
        pad_nets = {c.model["kicad"]["pins"][pin]: net for pin, net in c.pins.items()}
        items.append(Item(ref=c.ref, fp=fp, pad_nets=pad_nets))
    return items


def run(project, raw_constraints: dict, seed: int = 1, max_passes: int = 40, progress=None) -> dict:
    """Place, route and DRC the verified design. ``progress(text)`` is called as each stage starts."""
    started = time.time()
    say = progress or (lambda _text: None)
    spec, circuit, parts = project.require_verified()
    circuit_hash = project.state().get("verified_circuit_hash")
    ground = spec["ground"]
    prefs = spec.get("preferences", {})
    default_pkg = prefs.get("passive_package", "0603")
    data = cons_mod.normalize(raw_constraints, circuit)
    auto = cons_mod.automatic(circuit, ground)
    merged = cons_mod.merge(data["constraints"], auto)

    layout_dir = project.path("layout")
    layout_dir.mkdir(parents=True, exist_ok=True)
    (layout_dir / "constraints.json").write_text(json.dumps(data, indent=2))

    board_limits = dict(spec.get("board", {}))
    board_limits.update({k: v for k, v in data.get("board", {}).items()})
    verification = json.loads(project.path("verification.json").read_text())
    currents = verification.get("net_currents_a", {})
    port_roles = {p["net"]: p["role"] for p in spec["ports"]}
    weights = {}
    for net in circuit.nets():
        if net == ground:
            weights[net] = 0.4 if data.get("pour_ground", True) else 1.0
        elif port_roles.get(net, "").startswith("power") or currents.get(net, 0) >= 0.1:
            weights[net] = 1.6
        else:
            weights[net] = 1.0

    items = build_items(circuit, parts, default_pkg)
    say(f"placing {len(items)} parts against {len(merged)} constraints")
    placed = place(items, merged, board_limits, seed=seed, net_weights=weights)
    issues = list(placed.issues)
    cons_report = []
    for k, c in enumerate(merged):
        off = placed.cost["violation_mm"].get(k, 0.0)
        cons_report.append({**{kk: vv for kk, vv in c.items() if kk != "auto"}, "auto": c.get("auto", False),
                            "satisfied": off <= MET_MM, "off_mm": round(off, 2),
                            "penalty": round(placed.cost["per_constraint"].get(k, 0.0), 3)})

    from ..kicad.schematic import footprint_paths
    sym_paths = footprint_paths(circuit)
    ox, oy = ORIGIN
    placements = []
    fp_jobs = []
    for c in circuit.components:
        x, y, rot = placed.positions[c.ref]
        item = next(it for it in items if it.ref == c.ref)
        sel = (parts or {}).get("parts", {}).get(c.ref, {}) if parts else {}
        fields = {k: sel[k2] for k, k2 in (("LCSC", "lcsc"), ("MPN", "mpn")) if sel.get(k2)}
        lib = item.fp.fpid.partition(":")[0]
        fp_jobs.append({"ref": c.ref, "value": c.value_text(), "fpid": item.fp.fpid,
                        "lib_path": str(libs.footprint_dir() / f"{lib}.pretty"),
                        "x": x, "y": y, "rotation": rot, "pads": item.pad_nets, "fields": fields,
                        "path": sym_paths[c.ref]})
        placements.append({"ref": c.ref, "x_mm": ox + x, "y_mm": oy + y, "rotation": rot, "side": "top",
                           "footprint": item.fp.fpid})
    (layout_dir / "placement.json").write_text(json.dumps(
        {"origin": ORIGIN, "board_mm": [placed.width, placed.height], "placements": placements}, indent=2))

    name = project.name
    board_path = layout_dir / f"{name}.kicad_pcb"
    dsn_path = layout_dir / f"{name}.dsn"
    ses_path = layout_dir / f"{name}.ses"
    routed_path = layout_dir / f"{name}.kicad_pcb"
    netclasses = netclasses_from_currents(currents, ground)
    result: dict = {
        "passed": False, "board_mm": [placed.width, placed.height], "placement_legal": placed.legal,
        "placement_issues": issues, "constraints": cons_report, "netclasses": netclasses,
        "kicad_version": kicad_version(), "circuit_hash": circuit_hash,
    }
    trace = {"version": 1, "board": board_path.name, "origin": list(ORIGIN), "size": [placed.width, placed.height],
             "placement_frames": [{ref: [ox + p[0], oy + p[1], p[2]] for ref, p in f["positions"].items()}
                                  for f in placed.frames],
             "routing": []}
    if not placed.legal:
        result["message"] = "placement could not be legalized: " + "; ".join(issues[:5])
        _finish(project, layout_dir, result, trace, started)
        return result

    try:
        # From here on the board files are overwritten: forget the old board first, so an
        # interrupted attempt never leaves a half-written board marked DRC clean.
        project.begin_layout()
        say(f"building the {placed.width:g} x {placed.height:g} mm board in KiCad")
        _run_worker({"action": "build", "board_path": str(board_path),
                     "origin": list(ORIGIN), "width": placed.width, "height": placed.height,
                     "nets": sorted(circuit.nets()), "footprints": fp_jobs, "rules": RULES,
                     "netclasses": netclasses}, layout_dir)
        exported = _run_worker({"action": "dsn", "board_path": str(board_path), "dsn_path": str(dsn_path),
                                "nets": sorted(circuit.nets())}, layout_dir)
        result["assigned_netclasses"] = exported["netclasses"]
        wrong = [f"{net} (wanted {nc['name']}, got {exported['netclasses'].get(net)})"
                 for nc in netclasses for net in nc["nets"] if exported["netclasses"].get(net) != nc["name"]]
        if wrong:
            # Routing at the default width would silently undersize power tracks.
            raise LayoutError("KiCad did not apply the current-based net classes: " + ", ".join(wrong))
        # Same basename as the board, so KiCad opens schematic + PCB as one project.
        sch = project._write_schematic(circuit, (parts or {}).get("parts"), default_pkg,
                                       layout_dir / f"{name}.kicad_sch")
        if sch:
            result["schematic"] = str(sch)
        say("routing with Freerouting")
        pad_dsn_clearance(dsn_path)
        fr = freerouting.route(dsn_path, ses_path, max_passes=max_passes)
        result["freerouting"] = {k: v for k, v in fr.items() if k != "command"}
        if not fr["ok"]:
            result["message"] = f"Freerouting failed (exit {fr['returncode']}); see layout/freerouting.log"
            _finish(project, layout_dir, result, trace, started)
            return result
        drc_report = layout_dir / "drc.rpt"
        say("importing the routes, pouring ground and running KiCad DRC")
        pour = {"net": ground, "layers": ["F.Cu", "B.Cu"], "clearance": 0.3} if data.get("pour_ground", True) \
            and ground in circuit.nets() else None
        routed = _run_worker({"action": "route", "board_path": str(board_path), "ses_path": str(ses_path),
                              "out_path": str(routed_path), "origin": list(ORIGIN), "width": placed.width,
                              "height": placed.height, "pour": pour, "drc_report": str(drc_report)}, layout_dir)
    except (ToolMissing, LayoutError, subprocess.TimeoutExpired) as exc:
        result["message"] = str(exc)
        _finish(project, layout_dir, result, trace, started)
        return result

    stubs = routed.get("stubs_removed", [])
    trace["routing"] = _routing_frames(routed["wires"], routed["via_list"], stubs)
    result.update(tracks=routed["tracks"], vias=routed["vias"], zones=routed["zones"],
                  stubs_removed=len(stubs), missing_nets=routed["missing_nets"])
    cli = drc_mod.run_cli(routed_path, layout_dir / "drc.json")
    if cli is not None:
        parsed = cli
        result["drc_source"] = "kicad-cli pcb drc"
    else:
        parsed = drc_mod.parse_text_report(drc_report.read_text())
        result["drc_source"] = "pcbnew.WriteDRCReport"
    (layout_dir / "drc_parsed.json").write_text(json.dumps(parsed, indent=1))
    summary = drc_mod.summarize(parsed)
    result["drc"] = summary
    result["passed"] = summary["errors"] == 0 and summary["unconnected"] == 0 and not routed["missing_nets"]
    if result["passed"]:
        vias = routed["vias"]
        result["message"] = (f"board {placed.width:g} x {placed.height:g} mm routed: {routed['tracks']} track "
                             f"segments, {vias} via{'s' if vias != 1 else ''}, DRC 0 errors, 0 unconnected "
                             f"({summary['warnings']} warnings)")
    else:
        result["message"] = (f"DRC: {summary['errors']} error(s), {summary['unconnected']} unconnected item(s). "
                             + " ".join(summary["first_errors"][:3] + summary["first_unconnected"][:3]))
    _finish(project, layout_dir, result, trace, started)
    return result


def pad_dsn_clearance(dsn_path: Path, margin_mm: float = 0.01) -> int:
    """Ask Freerouting for slightly more clearance than KiCad's DRC rule.

    Freerouting rounds geometry to its own grid and occasionally places a track a couple of
    micrometres inside the clearance it was given (0.1982 mm against 0.2 mm), which KiCad's DRC
    then reports as an error. A 10 um margin is invisible in practice and removes that failure.
    Returns the number of clearance rules changed.
    """
    text = dsn_path.read_text()
    m = re.search(r"\(resolution\s+(um|mm|mil|inch|cm)\s", text)
    per_mm = {"um": 1000.0, "mm": 1.0, "mil": 1000 / 25.4, "inch": 1 / 25.4, "cm": 0.1}[m.group(1) if m else "um"]
    margin = margin_mm * per_mm
    count = 0

    def bump(match):
        nonlocal count
        count += 1
        return f"(clearance {float(match.group(1)) + margin:g}"

    text = re.sub(r"\(clearance\s+([0-9]+(?:\.[0-9]+)?)", bump, text)
    dsn_path.write_text(text)
    return count


def _routing_frames(wires: list[dict], vias: list[dict], stubs: list | None = None) -> list[dict]:
    def is_stub(x1, y1, x2, y2, layer, net):
        for sx1, sy1, sx2, sy2, slayer, snet in stubs or []:
            if (snet, slayer) != (net, layer):
                continue
            for (ax, ay, bx, by) in ((sx1, sy1, sx2, sy2), (sx2, sy2, sx1, sy1)):
                if max(abs(ax - x1), abs(ay - y1), abs(bx - x2), abs(by - y2)) < 0.002:
                    return True
        return False

    order: list[str] = []
    by_net: dict[str, dict] = {}
    for w in wires:
        if w["net"] not in by_net:
            order.append(w["net"])
            by_net[w["net"]] = {"net": w["net"], "tracks": [], "vias": []}
        pts = w["points"]
        for a, b in zip(pts, pts[1:]):
            if is_stub(a[0], a[1], b[0], b[1], w["layer"], w["net"]):
                continue
            by_net[w["net"]]["tracks"].append([round(a[0], 4), round(a[1], 4), round(b[0], 4), round(b[1], 4),
                                               round(w["width"], 4), w["layer"]])
    for v in vias:
        by_net.setdefault(v["net"], {"net": v["net"], "tracks": [], "vias": []})
        if v["net"] not in order:
            order.append(v["net"])
        by_net[v["net"]]["vias"].append([round(v["x"], 4), round(v["y"], 4), v["diameter"], v["drill"]])
    return [by_net[n] for n in order]


def _finish(project, layout_dir: Path, result: dict, trace: dict, started: float) -> None:
    result["elapsed_s"] = round(time.time() - started, 2)
    (layout_dir / "layout_trace.json").write_text(json.dumps(trace))
    (layout_dir / "result.json").write_text(json.dumps(result, indent=2))
    project.set_layout({"passed": result["passed"], "message": result.get("message"),
                        "board": str(layout_dir / f"{project.name}.kicad_pcb"),
                        "drc_summary": result.get("drc"), "board_mm": result.get("board_mm"),
                        "circuit_hash": result.get("circuit_hash")})
    project.archive_layout()


def summarize(result: dict) -> str:
    lines = [("PASS" if result["passed"] else "FAIL") + ": " + (result.get("message") or "")]
    if result.get("board_mm"):
        lines.append(f"  board {result['board_mm'][0]:g} x {result['board_mm'][1]:g} mm")
    bad = [c for c in result.get("constraints", []) if not c["satisfied"]]
    for c in bad[:6]:
        who = c.get("ref") or ",".join(c.get("refs", []))
        lines.append(f"  constraint not met: {c['type']} {who}" + (f" -> {c['to']}" if c.get("to") else "")
                     + f" (off by {c['off_mm']} mm)")
    if result.get("netclasses"):
        lines.append("  track widths from simulated currents: " + ", ".join(
            f"{'/'.join(nc['nets'])} {nc['track']} mm" for nc in result["netclasses"]))
    fr = result.get("freerouting")
    if fr:
        lines.append(f"  freerouting {fr['seconds']} s, network attempts blocked: {fr['blocked_network_attempts']}")
    d = result.get("drc")
    if d and d.get("warning_types"):
        lines.append("  DRC warnings: " + ", ".join(f"{k} x{v}" for k, v in d["warning_types"].items()))
    return "\n".join(lines)
