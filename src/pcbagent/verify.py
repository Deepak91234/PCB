"""Verification: locked spec vs. ngspice results, across tolerance corners.

A requirement passes only if every simulated corner lands inside its bounds.
Every number in the report is read from a raw file written by ngspice in this
run; the report records the file, its SHA-256 and the vectors used, so a
value cannot be copied in from anywhere else.

On top of the spec, the operating point is checked against part ratings
(datasheet limits for ICs and discretes) and turned into minimum ratings for
the passives, which part selection must then honour.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from . import spec as spec_mod
from .circuit import Circuit
from .spice import netlist as nl
from .spice.raw import Plot
from .spice.run import ConditionRun, ngspice_version, run_condition
from .units import format_value

# Derating policy (fraction of the rating a part may see in the worst corner).
DERATE = {
    "resistor_power": 0.5,      # resistor dissipation <= 50 % of rated power
    "capacitor_voltage": 1 / 1.5,  # MLCC rated >= 1.5x working voltage (DC-bias loss)
    "inductor_current": 0.8,
    "diode_current": 0.8,
    "diode_reverse": 0.8,
    "transistor": 0.8,
    "tj_margin_c": 10.0,        # junction stays 10 C below its maximum
}


def _ambient(spec: dict, cond: dict) -> float:
    return float(cond.get("ambient_c", spec.get("preferences", {}).get("ambient_c", 25.0)))


class OpView:
    """Read node voltages and pin currents from an operating-point plot."""

    def __init__(self, plot: Plot, ground: str):
        self.plot = plot
        self.ground = ground
        self.used: set[str] = set()

    def _get(self, vec: str) -> float:
        self.used.add(vec)
        return self.plot.scalar(vec)

    def v(self, net: str) -> float:
        if net == self.ground:
            return 0.0
        return self._get(f"v({nl.node(net, self.ground)})")

    def v_pin(self, ref: str, pin: str) -> float:
        return self._get(f"v({nl.pin_node(ref, pin)})")

    def i_pin(self, ref: str, pin: str) -> float:
        """Current flowing into the component through this pin."""
        return self._get(f"i({nl.sense_name(ref, pin)})")

    def source_current(self, src: dict) -> float:
        if "amps" in src:
            return float(src["amps"])
        return -self._get(f"i(v_src_{src['net'].lower()})")

    def load_power(self, load: dict) -> float:
        v = self.v(load["net"])
        if "ohms" in load:
            return v * v / load["ohms"]
        return v * load["amps"]

    def component_power(self, comp) -> float:
        return sum(self.v_pin(comp.ref, pin) * self.i_pin(comp.ref, pin) for pin in comp.pins)


def _ac_gain_db(plot: Plot, ground: str, inp: str, out: str) -> tuple[list[float], list[float], list[str]]:
    freq = [abs(f) for f in plot.vector("frequency")]
    vin_name = f"v({nl.node(inp, ground)})"
    vout_name = f"v({nl.node(out, ground)})"
    vin = plot.vector(vin_name)
    vout = plot.vector(vout_name)
    gain = []
    for a, b in zip(vin, vout):
        mag = abs(b) / abs(a) if abs(a) > 0 else 0.0
        gain.append(20 * math.log10(mag) if mag > 0 else -400.0)
    return freq, gain, ["frequency", vin_name, vout_name]


def _interp_log(freq: list[float], gain: list[float], hz: float) -> float | None:
    if hz < freq[0] or hz > freq[-1]:
        return None
    for i in range(1, len(freq)):
        if freq[i] >= hz:
            f0, f1 = math.log10(freq[i - 1]), math.log10(freq[i])
            t = 0.0 if f1 == f0 else (math.log10(hz) - f0) / (f1 - f0)
            return gain[i - 1] + t * (gain[i] - gain[i - 1])
    return gain[-1]


def _cutoff(freq: list[float], gain: list[float]) -> float | None:
    gmax = max(gain)
    k = gain.index(gmax)
    level = gmax - 10 * math.log10(2)

    def cross(i0, i1):
        g0, g1 = gain[i0], gain[i1]
        t = (level - g0) / (g1 - g0) if g1 != g0 else 0.0
        lf = math.log10(freq[i0]) + t * (math.log10(freq[i1]) - math.log10(freq[i0]))
        return 10 ** lf

    for i in range(k + 1, len(gain)):
        if gain[i] <= level:
            return cross(i - 1, i)
    for i in range(k - 1, -1, -1):
        if gain[i] <= level:
            return cross(i + 1, i)
    return None


def _describe_corner(run: ConditionRun, idx: int) -> dict:
    labels = run.corners[idx].labels
    pmap = {p.name: p.label for p in run.params}
    return {pmap.get(k, k): v for k, v in labels.items() if v != "nom"}


def verify(spec: dict, circuit: Circuit, workdir: Path, tolerances: dict[str, float] | None = None) -> dict:
    """Simulate every condition at every corner and judge the locked spec."""
    started = time.time()
    ground = spec["ground"]
    workdir.mkdir(parents=True, exist_ok=True)
    params = nl.corner_params(circuit, tolerances)
    conds = {c["id"]: c for c in spec["conditions"]}

    # Which AC inputs each condition needs.
    ac_inputs: dict[str, set[str]] = {}
    freqs: list[float] = []
    for req in spec["requirements"]:
        m = req["measure"]
        if m["type"] in ("gain_db", "cutoff_hz"):
            ac_inputs.setdefault(req["condition"], set()).add(m["input"])
            if "hz" in m:
                freqs.append(m["hz"])
    ac_range = nl.ac_frequency_range(freqs)

    op_runs: dict[str, ConditionRun] = {}
    ac_runs: dict[tuple[str, str], ConditionRun] = {}
    sim_errors: list[str] = []
    for cond in spec["conditions"]:
        amb = _ambient(spec, cond)
        run = run_condition(circuit, cond, ground, params, "op", workdir / "sim", amb)
        op_runs[cond["id"]] = run
        sim_errors.extend(f"[{cond['id']}] {e}" for e in run.errors)
        for inp in sorted(ac_inputs.get(cond["id"], ())):
            sub = workdir / "sim"
            acr = run_condition(circuit, dict(cond, id=f"{cond['id']}_{inp.lower()}"), ground, params,
                                "ac", sub, amb, ac_input=inp, ac_range=ac_range)
            ac_runs[(cond["id"], inp)] = acr
            sim_errors.extend(f"[{cond['id']} ac] {e}" for e in acr.errors)

    req_results = [_judge(req, spec, circuit, conds, op_runs, ac_runs, ground) for req in spec["requirements"]]
    net_currents = _net_currents(spec, circuit, conds, op_runs, ground)
    ratings, selection = _ratings(spec, circuit, conds, op_runs, ground)

    failed_reqs = [r for r in req_results if not r["passed"]]
    failed_ratings = [r for r in ratings if not r["passed"]]
    passed = not failed_reqs and not failed_ratings and not sim_errors

    runs_summary = []
    for run in list(op_runs.values()) + list(ac_runs.values()):
        runs_summary.append({
            "condition": run.condition_id, "analysis": run.analysis, "deck": _rel(run.deck_path, workdir),
            "deck_sha256": run.deck_sha256, "corner_method": run.method,
            "varied": [p.label for p in run.params],
            "raw_files": [{"corner": c.index, "file": _rel(c.raw_path, workdir), "sha256": c.raw_sha256}
                          for c in run.corners if c.raw_path],
        })

    return {
        "passed": passed,
        "simulator": {"name": "ngspice", "version": ngspice_version()},
        "spec_hash": spec_mod.content_hash(spec),
        "circuit_hash": spec_mod.content_hash(circuit.raw),
        "requirements": req_results,
        "ratings": ratings,
        "selection_requirements": selection,
        "net_currents_a": net_currents,
        "simulation_errors": sim_errors,
        "warnings": list(circuit.warnings),
        "runs": runs_summary,
        "elapsed_s": round(time.time() - started, 3),
    }


def _rel(path: str | None, base: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(Path(path).resolve().relative_to(base.resolve()))
    except ValueError:
        return path


def _measure_op(m: dict, view: OpView, cond: dict, circuit: Circuit) -> float:
    t = m["type"]
    if t == "voltage":
        return view.v(m["net"])
    if t == "source_current":
        src = next(s for s in cond["sources"] if s["net"] == m["net"])
        return view.source_current(src)
    if t == "source_power":
        src = next(s for s in cond["sources"] if s["net"] == m["net"])
        return view.v(m["net"]) * view.source_current(src)
    if t == "load_power":
        ld = next(x for x in cond["loads"] if x["net"] == m["net"])
        return view.load_power(ld)
    if t == "efficiency":
        p_in = sum(view.v(s["net"]) * view.source_current(s) for s in cond["sources"])
        p_out = sum(view.load_power(ld) for ld in cond["loads"])
        return 100.0 * p_out / p_in if p_in > 0 else 0.0
    if t == "component_current":
        return view.i_pin(m["ref"], m["pin"])
    if t == "component_power":
        comp = circuit.by_ref(m["ref"])
        return view.component_power(comp)
    raise ValueError(t)


def _judge(req, spec, circuit, conds, op_runs, ac_runs, ground) -> dict:
    m = req["measure"]
    unit = spec_mod.measure_unit(m)
    bounds = spec_mod.requirement_bounds(req)
    cond = conds[req["condition"]]
    values: list[tuple[float | None, int, list[str], str | None, str | None]] = []
    error = None
    run_used: ConditionRun | None = None
    try:
        if m["type"] in ("gain_db", "cutoff_hz"):
            run_used = ac_runs[(req["condition"], m["input"])]
            for c in run_used.corners:
                if c.plot is None:
                    values.append((None, c.index, [], None, None))
                    continue
                freq, gain, vecs = _ac_gain_db(c.plot, ground, m["input"], m["output"])
                val = _interp_log(freq, gain, m["hz"]) if m["type"] == "gain_db" else _cutoff(freq, gain)
                values.append((val, c.index, vecs, c.raw_path, c.raw_sha256))
        else:
            run_used = op_runs[req["condition"]]
            base_run = op_runs.get(m.get("from_condition", ""))
            for c in run_used.corners:
                if c.plot is None:
                    values.append((None, c.index, [], None, None))
                    continue
                view = OpView(c.plot, ground)
                if m["type"] == "voltage_change":
                    other = base_run.corners[c.index].plot if base_run else None
                    if other is None:
                        values.append((None, c.index, [], None, None))
                        continue
                    bview = OpView(other, ground)
                    val = view.v(m["net"]) - bview.v(m["net"])
                    vecs = sorted(view.used) + [f"{m['from_condition']}:{v}" for v in sorted(bview.used)]
                else:
                    val = _measure_op(m, view, cond, circuit)
                    vecs = sorted(view.used)
                values.append((val, c.index, vecs, c.raw_path, c.raw_sha256))
    except KeyError as exc:
        error = f"simulation output has no vector {exc} for this measurement"

    good = [v for v in values if v[0] is not None]
    result = {
        "id": req["id"],
        "description": req.get("description", ""),
        "condition": req["condition"],
        "measure": m,
        "unit": unit,
        "bounds": {"min": bounds.lo, "max": bounds.hi, "target": bounds.target},
        "expected": bounds.describe(unit),
    }
    if error or not good or len(good) != len(values):
        result.update(passed=False, nominal=None,
                      error=error or "no simulated value (simulation failed or measurement undefined)",
                      message=f"{req['id']}: could not be measured: "
                              f"{error or 'simulation failed or the response has no such point'}")
        return result
    nominal = good[0][0]
    vals = [v[0] for v in good]
    lo_v, hi_v = min(vals), max(vals)
    failing = [v for v in good if not bounds.contains(v[0])]
    passed = not failing

    def worst_of(items):
        def dist(x):
            d = 0.0
            if bounds.lo is not None and x < bounds.lo:
                d = bounds.lo - x
            if bounds.hi is not None and x > bounds.hi:
                d = max(d, x - bounds.hi)
            return d
        return max(items, key=lambda v: (dist(v[0]), abs(v[0] - (bounds.target if bounds.target is not None else nominal))))

    worst = worst_of(failing or good)
    result.update(
        passed=passed,
        nominal=nominal,
        min=lo_v,
        max=hi_v,
        corners=len(good),
        worst={"value": worst[0], "corner": worst[1], "corner_params": _describe_corner(run_used, worst[1])},
        provenance=[{"corner": v[1], "raw": v[3], "sha256": v[4], "vectors": v[2]}
                    for v in (good[0], worst) if v[3]],
    )
    if passed:
        result["message"] = (f"{req['id']}: {format_value(lo_v, unit)} to {format_value(hi_v, unit)} "
                             f"over {len(good)} corner(s), within {bounds.describe(unit)}")
    else:
        where = f"nominal {format_value(nominal, unit)}" if bounds.contains(nominal) is False \
            else f"nominal {format_value(nominal, unit)} passes, but worst corner gives {format_value(worst[0], unit)}"
        params_txt = ", ".join(f"{k}={v}" for k, v in result["worst"]["corner_params"].items())
        result["message"] = (f"{req['id']}: FAIL, {where}; need {bounds.describe(unit)}"
                             + (f" (worst corner: {params_txt})" if params_txt else ""))
    return result


def _net_currents(spec, circuit, conds, op_runs, ground) -> dict[str, float]:
    """Worst-case current carried by each net (max pin / port current), for track sizing."""
    out: dict[str, float] = {}
    nets = circuit.nets()

    def bump(net, value):
        if value > out.get(net, 0.0):
            out[net] = value

    for cond_id, run in op_runs.items():
        cond = conds[cond_id]
        for c in run.corners:
            if c.plot is None:
                continue
            view = OpView(c.plot, ground)
            for net, nodes in nets.items():
                for ref, pin in nodes:
                    comp = circuit.by_ref(ref)
                    if not nl.simulated(comp):
                        continue
                    try:
                        bump(net, abs(view.i_pin(ref, pin)))
                    except KeyError:
                        pass
            total = 0.0
            for s in cond["sources"]:
                try:
                    i = abs(view.source_current(s))
                except KeyError:
                    continue
                bump(s["net"], i)
                total += i
            for ld in cond.get("loads", []):
                try:
                    v = view.v(ld["net"])
                except KeyError:
                    continue
                i = abs(v / ld["ohms"]) if "ohms" in ld else abs(ld["amps"])
                bump(ld["net"], i)
            bump(ground, total)
    return {k: round(v, 6) for k, v in sorted(out.items())}


def _ratings(spec, circuit, conds, op_runs, ground) -> tuple[list[dict], dict]:
    """Worst-case stress per component over all conditions and corners."""
    checks: list[dict] = []
    selection: dict[str, dict] = {}
    worst: dict[tuple[str, str], tuple[float, str, int]] = {}

    def track(ref, key, value, cond_id, corner):
        cur = worst.get((ref, key))
        if cur is None or value > cur[0]:
            worst[(ref, key)] = (value, cond_id, corner)

    for cond_id, run in op_runs.items():
        amb = _ambient(spec, conds[cond_id])
        for c in run.corners:
            if c.plot is None:
                continue
            view = OpView(c.plot, ground)
            for comp in circuit.components:
                if not nl.simulated(comp):
                    continue
                try:
                    p = comp.pins
                    k = comp.kind
                    power = view.component_power(comp)
                    if k == "resistor":
                        track(comp.ref, "power", abs(power), cond_id, c.index)
                    elif k == "capacitor":
                        track(comp.ref, "voltage", abs(view.v_pin(comp.ref, "1") - view.v_pin(comp.ref, "2")),
                              cond_id, c.index)
                    elif k == "inductor":
                        track(comp.ref, "current", abs(view.i_pin(comp.ref, "1")), cond_id, c.index)
                    elif k in ("led", "diode"):
                        track(comp.ref, "forward_current", view.i_pin(comp.ref, "A"), cond_id, c.index)
                        track(comp.ref, "reverse_voltage", view.v_pin(comp.ref, "K") - view.v_pin(comp.ref, "A"),
                              cond_id, c.index)
                    elif k == "npn":
                        track(comp.ref, "ic", view.i_pin(comp.ref, "C"), cond_id, c.index)
                        track(comp.ref, "vce", view.v_pin(comp.ref, "C") - view.v_pin(comp.ref, "E"), cond_id, c.index)
                        track(comp.ref, "tj", amb + power * comp.model["ratings"]["theta_ja"], cond_id, c.index)
                    elif k == "nmos":
                        track(comp.ref, "id", view.i_pin(comp.ref, "D"), cond_id, c.index)
                        track(comp.ref, "vds", view.v_pin(comp.ref, "D") - view.v_pin(comp.ref, "S"), cond_id, c.index)
                        track(comp.ref, "vgs", abs(view.v_pin(comp.ref, "G") - view.v_pin(comp.ref, "S")),
                              cond_id, c.index)
                        track(comp.ref, "tj", amb + power * comp.model["ratings"]["theta_ja"], cond_id, c.index)
                    elif k == "ldo":
                        track(comp.ref, "vin", view.v_pin(comp.ref, "IN") - view.v_pin(comp.ref, "GND"),
                              cond_id, c.index)
                        track(comp.ref, "iout", -view.i_pin(comp.ref, "OUT"), cond_id, c.index)
                        track(comp.ref, "power", power, cond_id, c.index)
                        track(comp.ref, "tj", amb + power * comp.model["ratings"]["theta_ja"], cond_id, c.index)
                        if "EN" in p:
                            track(comp.ref, "ven", view.v_pin(comp.ref, "EN") - view.v_pin(comp.ref, "GND"),
                                  cond_id, c.index)
                except KeyError:
                    continue

    def add(ref, check, value, limit, unit, ok, msg, at):
        checks.append({"ref": ref, "check": check, "value": value, "limit": limit, "unit": unit,
                       "passed": ok, "message": msg, "condition": at[1], "corner": at[2]})

    for comp in circuit.components:
        r = comp.model.get("ratings", {})
        ref = comp.ref
        k = comp.kind
        sel: dict = {}
        if k == "resistor" and (ref, "power") in worst:
            w = worst[(ref, "power")]
            sel["min_power_w"] = w[0] / DERATE["resistor_power"]
        elif k == "capacitor" and (ref, "voltage") in worst:
            w = worst[(ref, "voltage")]
            sel["min_voltage_v"] = w[0] / DERATE["capacitor_voltage"]
        elif k == "inductor" and (ref, "current") in worst:
            w = worst[(ref, "current")]
            sel["min_current_a"] = w[0] / DERATE["inductor_current"]
        elif k in ("led", "diode"):
            if (ref, "forward_current") in worst and "if_max" in r:
                w = worst[(ref, "forward_current")]
                lim = r["if_max"] * DERATE["diode_current"]
                add(ref, "forward_current", w[0], lim, "A", w[0] <= lim,
                    f"{ref} forward current up to {format_value(w[0], 'A')} (limit {format_value(lim, 'A')}, "
                    f"80% of {format_value(r['if_max'], 'A')})", w)
            if (ref, "reverse_voltage") in worst and "vr_max" in r:
                w = worst[(ref, "reverse_voltage")]
                lim = r["vr_max"] * DERATE["diode_reverse"]
                add(ref, "reverse_voltage", w[0], lim, "V", w[0] <= lim,
                    f"{ref} reverse voltage up to {format_value(w[0], 'V')} (limit {format_value(lim, 'V')})", w)
        elif k in ("npn", "nmos"):
            pairs = [("ic", "ic_max", "A"), ("vce", "vce_max", "V")] if k == "npn" else \
                [("id", "id_max", "A"), ("vds", "vds_max", "V"), ("vgs", "vgs_max", "V")]
            for key, rkey, unit in pairs:
                if (ref, key) in worst and rkey in r:
                    w = worst[(ref, key)]
                    lim = r[rkey] * DERATE["transistor"]
                    add(ref, key, w[0], lim, unit, w[0] <= lim,
                        f"{ref} {key} up to {format_value(w[0], unit)} (limit {format_value(lim, unit)})", w)
        if k in ("npn", "nmos", "ldo") and (ref, "tj") in worst and "tj_max" in r:
            w = worst[(ref, "tj")]
            lim = r["tj_max"] - DERATE["tj_margin_c"]
            add(ref, "junction_temperature", w[0], lim, "C", w[0] <= lim,
                f"{ref} junction reaches {w[0]:.0f} C (limit {lim:.0f} C at theta_JA "
                f"{r.get('theta_ja')} C/W)", w)
        if k == "ldo":
            if (ref, "vin") in worst:
                w = worst[(ref, "vin")]
                add(ref, "input_voltage", w[0], r["vin_max"], "V", w[0] <= r["vin_max"],
                    f"{ref} input up to {format_value(w[0], 'V')} (max {format_value(r['vin_max'], 'V')})", w)
            if (ref, "iout") in worst:
                w = worst[(ref, "iout")]
                add(ref, "output_current", w[0], r["iout_max"], "A", w[0] <= r["iout_max"],
                    f"{ref} output current up to {format_value(w[0], 'A')} "
                    f"(max {format_value(r['iout_max'], 'A')})", w)
        if comp.model.get("value"):
            sp = comp.model["spice"]
            sel["max_tolerance_pct"] = sp.get("tolerance_pct")
            sel["value"] = comp.value
            sel["package"] = comp.package
        if sel:
            selection[ref] = sel

    # Enable pins must actually be high in every corner where the output matters.
    for comp in circuit.components:
        if comp.kind == "ldo" and "EN" in comp.pins and (comp.ref, "ven") in worst:
            th = comp.model["spice"]["params"].get("VENTH", 1.0)
            lows = []
            for cond_id, run in op_runs.items():
                for c in run.corners:
                    if c.plot is None:
                        continue
                    view = OpView(c.plot, ground)
                    try:
                        ven = view.v_pin(comp.ref, "EN") - view.v_pin(comp.ref, "GND")
                    except KeyError:
                        continue
                    if ven < th + 0.5:
                        lows.append((ven, cond_id, c.index))
            if lows:
                ven, cond_id, idx = min(lows)
                add(comp.ref, "enable_high", ven, th + 0.5, "V", False,
                    f"{comp.ref} EN is only {format_value(ven, 'V')} in condition {cond_id}; "
                    f"tie it to the input so the regulator is on", (ven, cond_id, idx))
    return checks, selection


def summarize(report: dict, max_lines: int = 40) -> str:
    """Plain-text summary for the model and the terminal."""
    lines = [("PASS" if report["passed"] else "FAIL") + f": verification against the locked spec "
             f"(ngspice {report['simulator']['version']})"]
    for r in report["requirements"]:
        lines.append(("  ok   " if r["passed"] else "  FAIL ") + r["message"])
    bad = [c for c in report["ratings"] if not c["passed"]]
    for c in bad:
        lines.append("  FAIL " + c["message"])
    good = [c for c in report["ratings"] if c["passed"]]
    if good:
        lines.append(f"  ok   {len(good)} rating check(s) within limits")
    for e in report["simulation_errors"][:5]:
        lines.append("  SIM  " + e)
    for w in report.get("warnings", [])[:5]:
        lines.append("  note " + w)
    return "\n".join(lines[:max_lines])
