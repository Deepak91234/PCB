"""Turn a circuit + one spec condition into an ngspice deck.

Design rules for the generated deck:

* Only the board's own parts come from the circuit. Ideal sources and loads
  come from the spec condition (the test bench), so a circuit cannot "pass"
  by containing a voltage source on its output: the IR has no such part.
* Every component pin gets a 0 V sense source. That gives the current into
  each pin, hence per-component power and stress, without guessing.
* Toleranced quantities are ``.param`` values so tolerance corners can be
  swept with ``alterparam`` in one ngspice process.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .. import models as models_mod
from ..circuit import Circuit, Component
from ..units import spice_number

_VT = 0.025852


def node(net: str, ground: str) -> str:
    return "0" if net == ground else f"n_{net.lower()}"


def pin_node(ref: str, pin: str) -> str:
    return f"p_{ref.lower()}_{pin.lower()}"


def sense_name(ref: str, pin: str) -> str:
    return f"v_s_{ref.lower()}_{pin.lower()}"


@dataclass
class CornerParam:
    name: str          # .param name, lowercase
    nominal: float
    low: float
    high: float
    ref: str
    label: str         # human-readable, e.g. "R1 value" or "U1 VSET"
    dc: bool = True    # affects the DC operating point
    ac: bool = True    # affects the AC response

    def value(self, which: str) -> float:
        return {"nom": self.nominal, "low": self.low, "high": self.high}[which]


def corner_params(circuit: Circuit, tolerances: dict[str, float] | None = None) -> list[CornerParam]:
    """Every quantity with a spread. ``tolerances`` overrides passive tolerance (pct) by ref."""
    tolerances = tolerances or {}
    out: list[CornerParam] = []
    for c in circuit.components:
        sp = c.model["spice"]
        t = sp["type"]
        if t in ("R", "C", "L"):
            pct = tolerances.get(c.ref, sp.get("tolerance_pct", 0.0))
            v = c.value
            dc = t == "R"
            out.append(CornerParam(f"p_{c.ref.lower()}", v, v * (1 - pct / 100), v * (1 + pct / 100),
                                   c.ref, f"{c.ref} value (±{pct:g}%)", dc=dc, ac=True))
        elif t in ("led", "subckt"):
            for pname, pspec in sp.get("params", {}).items():
                nom = models_mod.param_value(pspec, "typ")
                lo = models_mod.param_value(pspec, "min") if isinstance(pspec, dict) and "min" in pspec else nom
                hi = models_mod.param_value(pspec, "max") if isinstance(pspec, dict) and "max" in pspec else nom
                out.append(CornerParam(f"p_{c.ref.lower()}_{pname.lower()}", nom, lo, hi, c.ref,
                                       f"{c.ref} {pname}"))
    return out


def varying(params: list[CornerParam], analysis: str) -> list[CornerParam]:
    key = "dc" if analysis == "op" else "ac"
    return [p for p in params if getattr(p, key) and (p.low != p.nominal or p.high != p.nominal)]


def _component_lines(c: Component, ground: str, libs: dict[str, str]) -> list[str]:
    sp = c.model["spice"]
    t = sp["type"]
    pn = {pin: pin_node(c.ref, pin) for pin in c.pins}
    r = c.ref.lower()
    if t == "R":
        return [f"r_{r} {pn['1']} {pn['2']} {{p_{r}}}"]
    if t == "C":
        return [f"c_{r} {pn['1']} {pn['2']} {{p_{r}}}"]
    if t == "L":
        return [f"l_{r} {pn['1']} {r}_mid {{p_{r}}}",
                f"r_{r}_dcr {r}_mid {pn['2']} {spice_number(sp.get('series_ohms', 0.05))}"]
    if t == "led":
        p = sp["params"]
        if0, n, rs = p["IF0"], p["N"], p["RS"]
        vf = f"p_{r}_vf"
        libs[f"dmod_{r}"] = (f".model dmod_{r} D(IS={{{if0}/exp(({vf}-{if0}*{rs})/({n}*{_VT}))}} "
                             f"N={n} RS={rs})")
        return [f"d_{r} {pn['A']} {pn['K']} dmod_{r}"]
    if t == "D":
        libs[sp["model"].lower()] = sp["lib"]
        return [f"d_{r} {pn['A']} {pn['K']} {sp['model']}"]
    if t == "Q":
        libs[sp["model"].lower()] = sp["lib"]
        nodes = " ".join(pn[x] for x in sp["nodes"])
        return [f"q_{r} {nodes} {sp['model']}"]
    if t == "M":
        libs[sp["model"].lower()] = sp["lib"]
        nodes = " ".join(pn[x] for x in sp["nodes"])
        return [f"m_{r} {nodes} {sp['model']}"]
    if t == "subckt":
        libs[sp["name"].lower()] = models_mod.SPICE_LIBS[sp["name"]]
        nodes = " ".join(pn[x] for x in sp["nodes"])
        params = " ".join(f"{k}={{p_{r}_{k.lower()}}}" for k in sp.get("params", {}))
        return [f"x_{r} {nodes} {sp['name']} {params}"]
    if t == "none":
        return []
    raise ValueError(f"unsupported SPICE type {t} for {c.ref}")


def simulated(c: Component) -> bool:
    return c.model["spice"]["type"] != "none"


def build_deck(circuit: Circuit, condition: dict, ground: str, params: list[CornerParam],
               analysis: str, ambient_c: float, ac_input: str | None = None,
               ac_range: tuple[float, float] = (0.1, 1e8)) -> tuple[str, list[str]]:
    """Return (deck text with no .control block, list of netlist body lines)."""
    lines = [f"* pcbagent deck: condition {condition['id']} ({analysis})",
             f".options temp={spice_number(ambient_c)} tnom={spice_number(ambient_c)}",
             ".options rshunt=1e12"]
    for p in params:
        lines.append(f".param {p.name}={spice_number(p.nominal)}")
    libs: dict[str, str] = {}
    body: list[str] = []
    for c in circuit.components:
        if not simulated(c):
            continue
        body.append(f"* {c.ref} {c.part}")
        for pin, net in c.pins.items():
            body.append(f"{sense_name(c.ref, pin)} {node(net, ground)} {pin_node(c.ref, pin)} 0")
        body.extend(_component_lines(c, ground, libs))
    body.append("* test bench (not part of the board)")
    for s in condition["sources"]:
        n = node(s["net"], ground)
        name = s["net"].lower()
        ac = " ac 1" if (ac_input == s["net"]) else ""
        if "volts" in s:
            if s.get("series_ohms"):
                body.append(f"v_src_{name} n_src_{name} 0 dc {spice_number(s['volts'])}{ac}")
                body.append(f"r_src_{name} n_src_{name} {n} {spice_number(s['series_ohms'])}")
            else:
                body.append(f"v_src_{name} {n} 0 dc {spice_number(s['volts'])}{ac}")
        else:
            body.append(f"i_src_{name} 0 {n} dc {spice_number(s['amps'])}")
    for ld in condition.get("loads", []):
        n = node(ld["net"], ground)
        name = ld["net"].lower()
        if "ohms" in ld:
            body.append(f"r_load_{name} {n} 0 {spice_number(ld['ohms'])}")
        else:
            body.append(f"i_load_{name} {n} 0 dc {spice_number(ld['amps'])}")
    for text in libs.values():
        lines.append(text)
    lines.extend(body)
    return "\n".join(lines) + "\n", body


def control_block(analysis: str, corners: list[dict[str, float]], params: list[CornerParam],
                  raw_prefix: str, ac_range: tuple[float, float] = (0.1, 1e8)) -> str:
    """One analysis per corner, each written to its own raw file.

    ``destroy all`` before every run means a corner that fails to converge
    leaves no file behind, instead of silently re-writing the previous plot.
    """
    lines = [".control", "set filetype=ascii", "set noaskquit"]
    if analysis == "op":
        cmd = "op"
    else:
        f0, f1 = ac_range
        cmd = f"ac dec 40 {spice_number(f0)} {spice_number(f1)}"
    for i, corner in enumerate(corners):
        for p in params:
            if p.name in corner:
                lines.append(f"alterparam {p.name}={spice_number(corner[p.name])}")
        if i > 0 or corner:
            lines.append("reset")
        lines.append("destroy all")
        lines.append(cmd)
        lines.append(f"write {raw_prefix}_c{i}.raw all")
    lines += ["quit", ".endc", ".end"]
    return "\n".join(lines) + "\n"


def corner_table(params: list[CornerParam], limit_full: int = 8, samples: int = 96,
                 seed: int = 7) -> tuple[list[dict[str, float]], list[dict[str, str]], str]:
    """Corner assignments. Returns (values, labels, method).

    Corner 0 is always nominal. With <= ``limit_full`` varying parameters
    every combination of low/high is simulated (exhaustive worst case).
    Beyond that, all-low, all-high and a seeded random sample of extremes
    are used and the method is reported as sampled.
    """
    import itertools
    import random

    values = [{p.name: p.nominal for p in params}]
    labels = [{p.name: "nom" for p in params}]
    if not params:
        return values, labels, "nominal"
    if len(params) <= limit_full:
        combos = itertools.product(("low", "high"), repeat=len(params))
        method = f"exhaustive ({2 ** len(params)} corners)"
    else:
        rng = random.Random(seed)
        combos_set = {tuple(["low"] * len(params)), tuple(["high"] * len(params))}
        while len(combos_set) < samples:
            combos_set.add(tuple(rng.choice(("low", "high")) for _ in params))
        combos = sorted(combos_set)
        method = f"sampled ({len(combos_set)} of {2 ** len(params)} corners)"
    for combo in combos:
        values.append({p.name: p.value(w) for p, w in zip(params, combo)})
        labels.append({p.name: w for p, w in zip(params, combo)})
    return values, labels, method


def ac_frequency_range(freqs: list[float]) -> tuple[float, float]:
    if not freqs:
        return (0.1, 1e8)
    lo = min(freqs) / 1000.0
    hi = max(freqs) * 1000.0
    lo = max(1e-3, 10 ** math.floor(math.log10(lo)))
    hi = min(1e10, 10 ** math.ceil(math.log10(hi)))
    return (min(lo, 0.1), max(hi, 1e8))
