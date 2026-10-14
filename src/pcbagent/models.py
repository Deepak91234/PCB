"""Part models: the electrical knowledge PCBAgent is allowed to use.

A circuit may only reference parts listed here (plus user model files). Each
model ties together four things that must agree for a design to be honest:

* the pins a circuit connects (``pins``),
* the SPICE model that simulates it (``spice``),
* the KiCad symbol/footprint and pin numbering (``kicad``), and
* how to find it in the JLCPCB catalog (``catalog``).

``ratings`` and ``rules`` turn datasheet limits into checks that run on the
simulated operating point. Values are transcribed from public datasheets and
classic vendor SPICE libraries; ``source`` says where. They are good enough to
catch wrong topologies and overstressed parts, not to replace a datasheet
review before you order boards.

User models: drop JSON files with the same shape into
``$PCBAGENT_HOME/models/`` (default ``~/.pcbagent/models``).
"""

from __future__ import annotations

import copy
import json
import os
from functools import lru_cache
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent

# Footprints per package for the generic passives (KiCad standard library names).
_R_FP = {p: f"Resistor_SMD:R_{p}_{m}Metric" for p, m in
         [("0402", "1005"), ("0603", "1608"), ("0805", "2012"), ("1206", "3216"), ("2512", "6332")]}
_C_FP = {p: f"Capacitor_SMD:C_{p}_{m}Metric" for p, m in
         [("0402", "1005"), ("0603", "1608"), ("0805", "2012"), ("1206", "3216"), ("1210", "3225")]}
_L_FP = {p: f"Inductor_SMD:L_{p}_{m}Metric" for p, m in
         [("0603", "1608"), ("0805", "2012"), ("1206", "3216")]}
_LED_FP = {p: f"LED_SMD:LED_{p}_{m}Metric" for p, m in [("0603", "1608"), ("0805", "2012")]}

# Typical chip-resistor power ratings by package (thick film, 70 C).
RESISTOR_POWER_W = {"0402": 0.0625, "0603": 0.1, "0805": 0.125, "1206": 0.25, "2512": 1.0}

# Behavioural fixed-output LDO. Output follows min(VSET, VIN - dropout(I))
# where dropout = VDO0 + RDO*I. Input current = load current + IQ, so the
# simulated input power and the regulator dissipation are physical.
_LDO_SUBCKT = """\
.subckt PCBA_LDO IN GND OUT VSET=3.3 VDO0=1.0 RDO=0.1 IQ=5m ROUT=0.01
Iq IN GND {IQ}
Bv n1 GND V={max(0, min(VSET, V(IN,GND) - VDO0 - RDO*max(0, I(Vsense))))}
Rout n1 n2 {ROUT}
Vsense n2 OUT 0
Fin IN GND Vsense 1
.ends PCBA_LDO"""

# Same with an active-high enable pin. A floating EN is a wiring error the
# circuit checker reports; the 10 MOhm keeps the matrix solvable regardless.
_LDO_EN_SUBCKT = """\
.subckt PCBA_LDO_EN IN GND EN OUT VSET=3.3 VDO0=0 RDO=0.4 IQ=55u ROUT=0.01 VENTH=1.0
Ren EN GND 10meg
Iq IN GND {IQ}
Bv n1 GND V={(V(EN,GND) > VENTH ? 1 : 0) * max(0, min(VSET, V(IN,GND) - VDO0 - RDO*max(0, I(Vsense))))}
Rout n1 n2 {ROUT}
Vsense n2 OUT 0
Fin IN GND Vsense 1
.ends PCBA_LDO_EN"""

_VT = 0.025852  # thermal voltage at 300 K


def _ldo(id_, title, symbol, sym_pins, footprint, params, ratings, rules, mpn, packages, source,
         subckt="PCBA_LDO", nodes=("IN", "GND", "OUT"), pins=None):
    return {
        "id": id_,
        "kind": "ldo",
        "title": title,
        "ref_prefix": "U",
        "pins": pins or {"IN": "unregulated input", "GND": "ground", "OUT": "regulated output"},
        "kicad": {"symbol": symbol, "pins": sym_pins, "footprint": footprint},
        "spice": {"type": "subckt", "name": subckt, "nodes": list(nodes), "params": params},
        "ratings": ratings,
        "rules": rules,
        "catalog": {"mpn": mpn, "packages": packages},
        "source": source,
    }


def _led(color, vf, if_max, title_color):
    # Generic LED: a diode fitted to Vf at 10 mA. Real parts vary, so the
    # forward voltage carries a corner spread and verification must pass at
    # both ends of it.
    return {
        "id": f"led_{color}",
        "kind": "led",
        "title": f"{title_color} LED, generic (Vf {vf['typ']} V at 10 mA)",
        "ref_prefix": "D",
        "pins": {"A": "anode", "K": "cathode"},
        "kicad": {"symbol": "Device:LED", "pins": {"K": "1", "A": "2"},
                  "footprints": _LED_FP, "default_package": "0603"},
        "spice": {"type": "led", "params": {"VF": vf, "IF0": 0.01, "N": 2.0, "RS": 5.0}},
        "ratings": {"if_max": if_max, "vr_max": 5.0},
        "rules": [],
        "catalog": {"kind": "led", "color": color},
        "source": "Generic fit; Vf spread covers common 0603/0805 parts. Check the chosen part's datasheet.",
    }


def _header(n):
    return {
        "id": f"header_1x{n:02d}",
        "kind": "connector",
        "title": f"2.54 mm pin header, 1x{n}",
        "ref_prefix": "J",
        "pins": {str(i): f"pin {i}" for i in range(1, n + 1)},
        "kicad": {"symbol": f"Connector_Generic:Conn_01x{n:02d}",
                  "pins": {str(i): str(i) for i in range(1, n + 1)},
                  "footprint": f"Connector_PinHeader_2.54mm:PinHeader_1x{n:02d}_P2.54mm_Vertical"},
        "spice": {"type": "none"},
        "ratings": {"i_max": 3.0},
        "rules": [],
        "catalog": {"kind": "connector", "keywords": ["2.54mm", f"1x{n}P", f"1*{n}P"], "optional": True},
        "source": "Standard 2.54 mm header; usually hand-soldered (through-hole).",
    }


BUILTIN_MODELS: list[dict] = [
    {
        "id": "resistor",
        "kind": "resistor",
        "title": "Chip resistor (value required, e.g. \"10k\")",
        "ref_prefix": "R",
        "pins": {"1": "terminal 1", "2": "terminal 2"},
        "value": {"unit": "ohm"},
        "kicad": {"symbol": "Device:R", "pins": {"1": "1", "2": "2"},
                  "footprints": _R_FP, "default_package": "0603"},
        "spice": {"type": "R", "tolerance_pct": 1.0},
        "ratings": {},
        "rules": [],
        "catalog": {"kind": "resistor"},
        "source": "Ideal resistor; tolerance corner from the selected part (default 1%).",
    },
    {
        "id": "capacitor",
        "kind": "capacitor",
        "title": "Ceramic capacitor, MLCC (value required, e.g. \"100n\")",
        "ref_prefix": "C",
        "pins": {"1": "terminal 1", "2": "terminal 2"},
        "value": {"unit": "F"},
        "kicad": {"symbol": "Device:C", "pins": {"1": "1", "2": "2"},
                  "footprints": _C_FP, "default_package": "0603"},
        "spice": {"type": "C", "tolerance_pct": 10.0},
        "ratings": {},
        "rules": [],
        "catalog": {"kind": "capacitor"},
        "source": "Ideal capacitor; DC-bias derating is handled by the voltage-rating margin.",
    },
    {
        "id": "inductor",
        "kind": "inductor",
        "title": "SMD inductor (value required, e.g. \"10u\")",
        "ref_prefix": "L",
        "pins": {"1": "terminal 1", "2": "terminal 2"},
        "value": {"unit": "H"},
        "kicad": {"symbol": "Device:L", "pins": {"1": "1", "2": "2"},
                  "footprints": _L_FP, "default_package": "0805"},
        "spice": {"type": "L", "tolerance_pct": 20.0, "series_ohms": 0.05},
        "ratings": {},
        "rules": [],
        "catalog": {"kind": "inductor"},
        "source": "Inductor with 50 mOhm DCR.",
    },
    _led("red", {"typ": 2.0, "min": 1.8, "max": 2.2}, 0.02, "Red"),
    _led("yellow", {"typ": 2.0, "min": 1.8, "max": 2.2}, 0.02, "Yellow"),
    _led("green", {"typ": 2.1, "min": 1.8, "max": 3.1}, 0.02, "Green"),
    _led("blue", {"typ": 3.0, "min": 2.7, "max": 3.3}, 0.02, "Blue"),
    _led("white", {"typ": 3.0, "min": 2.7, "max": 3.3}, 0.02, "White"),
    {
        "id": "1N4148W",
        "kind": "diode",
        "title": "1N4148W small-signal diode (SOD-123)",
        "ref_prefix": "D",
        "pins": {"A": "anode", "K": "cathode"},
        "kicad": {"symbol": "Device:D", "pins": {"K": "1", "A": "2"},
                  "footprint": "Diode_SMD:D_SOD-123"},
        "spice": {"type": "D", "model": "D1N4148",
                  "lib": ".model D1N4148 D(Is=2.52n Rs=.568 N=1.752 Cjo=4p M=.4 tt=20n)"},
        "ratings": {"if_max": 0.15, "vr_max": 75.0},
        "rules": [],
        "catalog": {"mpn": ["1N4148W"], "packages": ["SOD-123"]},
        "source": "1N4148 model from the onsemi/LTspice standard library.",
    },
    {
        "id": "B5819W",
        "kind": "diode",
        "title": "B5819W 1 A 40 V Schottky diode (SOD-123)",
        "ref_prefix": "D",
        "pins": {"A": "anode", "K": "cathode"},
        "kicad": {"symbol": "Device:D_Schottky", "pins": {"K": "1", "A": "2"},
                  "footprint": "Diode_SMD:D_SOD-123"},
        "spice": {"type": "D", "model": "D1N5819",
                  "lib": ".model D1N5819 D(Is=31.7u Rs=.051 N=1.373 Cjo=110p M=.35 Eg=.69 Xti=2)"},
        "ratings": {"if_max": 1.0, "vr_max": 40.0},
        "rules": [],
        "catalog": {"mpn": ["B5819W"], "packages": ["SOD-123"]},
        "source": "1N5819 model (LTspice standard library) used for the B5819W die.",
    },
    {
        "id": "SS14",
        "kind": "diode",
        "title": "SS14 1 A 40 V Schottky diode (SMA)",
        "ref_prefix": "D",
        "pins": {"A": "anode", "K": "cathode"},
        "kicad": {"symbol": "Device:D_Schottky", "pins": {"K": "1", "A": "2"},
                  "footprint": "Diode_SMD:D_SMA"},
        "spice": {"type": "D", "model": "D1N5819",
                  "lib": ".model D1N5819 D(Is=31.7u Rs=.051 N=1.373 Cjo=110p M=.35 Eg=.69 Xti=2)"},
        "ratings": {"if_max": 1.0, "vr_max": 40.0},
        "rules": [],
        "catalog": {"mpn": ["SS14"], "packages": ["SMA", "DO-214AC", "SMA(DO-214AC)"]},
        "source": "1N5819-class model; SS14 is the same 1 A / 40 V Schottky class.",
    },
    {
        "id": "MMBT3904",
        "kind": "npn",
        "title": "MMBT3904 NPN transistor, 40 V 200 mA (SOT-23)",
        "ref_prefix": "Q",
        "pins": {"B": "base", "E": "emitter", "C": "collector"},
        "kicad": {"symbol": "Device:Q_NPN_BEC", "pins": {"B": "1", "E": "2", "C": "3"},
                  "footprint": "Package_TO_SOT_SMD:SOT-23"},
        "spice": {"type": "Q", "model": "Q2N3904", "nodes": ["C", "B", "E"],
                  "lib": ".model Q2N3904 NPN(IS=6.734f XTI=3 EG=1.11 VAF=74.03 BF=416.4 NE=1.259 ISE=6.734f "
                         "IKF=66.78m XTB=1.5 BR=.7371 NC=2 ISC=0 IKR=0 RC=1 CJC=3.638p MJC=.3085 VJC=.75 FC=.5 "
                         "CJE=4.493p MJE=.2593 VJE=.75 TR=239.5n TF=301.2p ITF=.4 VTF=4 XTF=2 RB=10)"},
        "ratings": {"ic_max": 0.2, "vce_max": 40.0, "pd_max": 0.25, "tj_max": 150, "theta_ja": 500},
        "rules": [],
        "catalog": {"mpn": ["MMBT3904"], "packages": ["SOT-23", "SOT-23-3"]},
        "source": "Classic 2N3904 Gummel-Poon model (vendor PSpice library); MMBT3904 is the SOT-23 version.",
    },
    {
        "id": "2N7002",
        "kind": "nmos",
        "title": "2N7002 N-channel MOSFET, 60 V 115 mA (SOT-23)",
        "ref_prefix": "Q",
        "pins": {"G": "gate", "S": "source", "D": "drain"},
        "kicad": {"symbol": "Device:Q_NMOS_GSD", "pins": {"G": "1", "S": "2", "D": "3"},
                  "footprint": "Package_TO_SOT_SMD:SOT-23"},
        "spice": {"type": "M", "model": "M2N7002", "nodes": ["D", "G", "S"],
                  "lib": ".model M2N7002 VDMOS(Rg=3 Vto=1.6 Rd=0 Rs=.75 Rb=.14 Kp=.17 mtriode=1.25 "
                         "Cgdmax=80p Cgdmin=12p Cgs=50p Cjo=50p Is=.04p)"},
        "ratings": {"id_max": 0.115, "vds_max": 60.0, "vgs_max": 20.0, "pd_max": 0.2, "tj_max": 150,
                    "theta_ja": 625},
        "rules": [],
        "catalog": {"mpn": ["2N7002"], "packages": ["SOT-23", "SOT-23-3"]},
        "source": "VDMOS model from the LTspice standard library (Fairchild 2N7002).",
    },
    _ldo(
        "AMS1117-3.3", "AMS1117-3.3 fixed 3.3 V LDO, 1 A, 1.1 V dropout (SOT-223)",
        "Regulator_Linear:AMS1117-3.3", {"GND": "1", "OUT": "2", "IN": "3"},
        "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
        {"VSET": {"typ": 3.3, "min": 3.235, "max": 3.365}, "VDO0": {"typ": 1.0, "max": 1.2},
         "RDO": 0.125, "IQ": {"typ": 0.005, "max": 0.011}, "ROUT": 0.012},
        {"vin_max": 15.0, "iout_max": 1.0, "tj_max": 125, "theta_ja": 90},
        [{"type": "min_capacitance", "pin": "IN", "farads": 10e-6},
         {"type": "min_capacitance", "pin": "OUT", "farads": 22e-6,
          "note": "Datasheet stability condition is 22 uF tantalum; an MLCC-only output is outside it."}],
        ["AMS1117-3.3"], ["SOT-223", "SOT-223-3", "SOT-223-4"],
        "AMS1117 datasheet (Advanced Monolithic Systems): 3.235-3.365 V over line/load/temp, 1.1 V typ / "
        "1.3 V max dropout at 0.8 A, 5/11 mA quiescent, 15 V max input.",
    ),
    _ldo(
        "AMS1117-5.0", "AMS1117-5.0 fixed 5.0 V LDO, 1 A, 1.1 V dropout (SOT-223)",
        "Regulator_Linear:AMS1117-5.0", {"GND": "1", "OUT": "2", "IN": "3"},
        "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
        {"VSET": {"typ": 5.0, "min": 4.9, "max": 5.1}, "VDO0": {"typ": 1.0, "max": 1.2},
         "RDO": 0.125, "IQ": {"typ": 0.005, "max": 0.011}, "ROUT": 0.012},
        {"vin_max": 15.0, "iout_max": 1.0, "tj_max": 125, "theta_ja": 90},
        [{"type": "min_capacitance", "pin": "IN", "farads": 10e-6},
         {"type": "min_capacitance", "pin": "OUT", "farads": 22e-6,
          "note": "Datasheet stability condition is 22 uF tantalum; an MLCC-only output is outside it."}],
        ["AMS1117-5.0"], ["SOT-223", "SOT-223-3", "SOT-223-4"],
        "AMS1117 datasheet (Advanced Monolithic Systems): 4.90-5.10 V over line/load/temp.",
    ),
    _ldo(
        "XC6206P332MR", "XC6206P332MR fixed 3.3 V CMOS LDO, 200 mA, 1 uA Iq (SOT-23)",
        "Regulator_Linear:XC6206PxxxMR", {"GND": "1", "OUT": "2", "IN": "3"},
        "Package_TO_SOT_SMD:SOT-23-3",
        {"VSET": {"typ": 3.3, "min": 3.234, "max": 3.366}, "VDO0": 0.0, "RDO": {"typ": 2.5, "max": 4.0},
         "IQ": {"typ": 1e-6, "max": 3e-6}, "ROUT": 0.05},
        {"vin_max": 6.0, "iout_max": 0.2, "tj_max": 125, "theta_ja": 400},
        [{"type": "min_capacitance", "pin": "IN", "farads": 1e-6},
         {"type": "min_capacitance", "pin": "OUT", "farads": 1e-6}],
        ["XC6206P332MR"], ["SOT-23", "SOT-23-3"],
        "Torex XC6206 datasheet: +/-2 %, 250 mV typ dropout at 100 mA, 6 V max input, 250 mW (SOT-23).",
    ),
    _ldo(
        "AP2112K-3.3", "AP2112K-3.3 fixed 3.3 V CMOS LDO with enable, 600 mA (SOT-23-5)",
        "Regulator_Linear:AP2112K-3.3", {"IN": "1", "GND": "2", "EN": "3", "NC": "4", "OUT": "5"},
        "Package_TO_SOT_SMD:SOT-23-5",
        {"VSET": {"typ": 3.3, "min": 3.25, "max": 3.35}, "VDO0": 0.0, "RDO": {"typ": 0.42, "max": 0.67},
         "IQ": {"typ": 55e-6, "max": 80e-6}, "ROUT": 0.02, "VENTH": 1.0},
        {"vin_max": 6.0, "iout_max": 0.6, "tj_max": 125, "theta_ja": 250},
        [{"type": "min_capacitance", "pin": "IN", "farads": 1e-6},
         {"type": "min_capacitance", "pin": "OUT", "farads": 1e-6},
         {"type": "pin_driven", "pin": "EN",
          "note": "EN must be tied high (usually to IN) or driven; floating EN leaves the output off."}],
        ["AP2112K-3.3TRG1", "AP2112K-3.3"], ["SOT-23-5", "SOT-25", "SOT-753"],
        "Diodes Inc. AP2112 datasheet: +/-1.5 %, 250 mV typ dropout at 600 mA, 55 uA Iq, 6 V max input.",
        subckt="PCBA_LDO_EN", nodes=("IN", "GND", "EN", "OUT"),
        pins={"IN": "unregulated input", "GND": "ground", "EN": "enable (active high)",
              "OUT": "regulated output", "NC": "no connection (leave unconnected)"},
    ),
    {
        "id": "testpoint",
        "kind": "testpoint",
        "title": "Test point pad, 1.5 mm",
        "ref_prefix": "TP",
        "pins": {"1": "pad"},
        "kicad": {"symbol": "Connector:TestPoint", "pins": {"1": "1"},
                  "footprint": "TestPoint:TestPoint_Pad_D1.5mm"},
        "spice": {"type": "none"},
        "ratings": {},
        "rules": [],
        "catalog": {"kind": "none"},
        "source": "Bare copper pad; not a purchased part.",
    },
    {
        "id": "terminal_1x02",
        "kind": "connector",
        "title": "5.00 mm screw terminal, 2 positions",
        "ref_prefix": "J",
        "pins": {"1": "pin 1", "2": "pin 2"},
        "kicad": {"symbol": "Connector:Screw_Terminal_01x02", "pins": {"1": "1", "2": "2"},
                  "footprint": "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal"},
        "spice": {"type": "none"},
        "ratings": {"i_max": 10.0},
        "rules": [],
        "catalog": {"kind": "connector", "keywords": ["5.0mm", "5mm", "2P", "screw"], "optional": True},
        "source": "Generic 5 mm pitch screw terminal.",
    },
    {
        "id": "jst_ph_1x02",
        "kind": "connector",
        "title": "JST PH 2.0 mm connector, 2 pins, vertical (battery/power lead)",
        "ref_prefix": "J",
        "pins": {"1": "pin 1", "2": "pin 2"},
        "kicad": {"symbol": "Connector_Generic:Conn_01x02", "pins": {"1": "1", "2": "2"},
                  "footprint": "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical"},
        "spice": {"type": "none"},
        "ratings": {"i_max": 2.0},
        "rules": [],
        "catalog": {"mpn": ["B2B-PH-K-S(LF)(SN)", "B2B-PH-K-S"], "packages": [], "optional": True},
        "source": "JST PH series, 2 A per contact.",
    },
] + [_header(n) for n in range(1, 9)]

for _m in BUILTIN_MODELS:
    # Pins named NC exist on the footprint but must stay unconnected.
    _m.setdefault("nc_pins", [p for p in _m["pins"] if p == "NC"])

SPICE_LIBS = {"PCBA_LDO": _LDO_SUBCKT, "PCBA_LDO_EN": _LDO_EN_SUBCKT}

KIND_PREFIX = {
    "resistor": "R", "capacitor": "C", "inductor": "L", "led": "D", "diode": "D",
    "npn": "Q", "nmos": "Q", "ldo": "U", "connector": "J", "testpoint": "TP",
}


def home_dir() -> Path:
    return Path(os.environ.get("PCBAGENT_HOME", Path.home() / ".pcbagent"))


@lru_cache(maxsize=4)
def _load(user_dir: str | None) -> dict[str, dict]:
    models = {m["id"]: copy.deepcopy(m) for m in BUILTIN_MODELS}
    if user_dir and Path(user_dir).is_dir():
        for path in sorted(Path(user_dir).glob("*.json")):
            data = json.loads(path.read_text())
            for m in data if isinstance(data, list) else [data]:
                m = dict(m)
                m.setdefault("source", f"user model file {path.name}")
                models[m["id"]] = m
    return models


def all_models() -> dict[str, dict]:
    return _load(str(home_dir() / "models"))


def get_model(model_id: str) -> dict | None:
    models = all_models()
    if model_id in models:
        return models[model_id]
    low = model_id.lower()
    for key, m in models.items():
        if key.lower() == low:
            return m
    return None


def param_value(spec, which: str = "typ") -> float:
    """A model parameter is a number or {"typ","min","max"}."""
    if isinstance(spec, dict):
        if which in spec:
            return float(spec[which])
        return float(spec["typ"])
    return float(spec)


def summary(model: dict) -> dict:
    """Compact description handed to the LLM by the models tool."""
    out = {"id": model["id"], "kind": model["kind"], "title": model["title"],
           "pins": model["pins"]}
    if model.get("value"):
        out["value_unit"] = model["value"]["unit"]
        out["packages"] = sorted(model["kicad"].get("footprints", {}))
    if model.get("ratings"):
        out["ratings"] = model["ratings"]
    rules = [r.get("note") or _rule_text(r) for r in model.get("rules", [])]
    if rules:
        out["rules"] = rules
    params = model.get("spice", {}).get("params")
    if params and model["kind"] == "ldo":
        out["vout"] = param_value(params["VSET"])
    return out


def _rule_text(rule: dict) -> str:
    if rule["type"] == "min_capacitance":
        from .units import format_value
        return f"at least {format_value(rule['farads'], 'F')} from {rule['pin']} to GND"
    if rule["type"] == "pin_driven":
        return f"{rule['pin']} must be connected"
    return rule["type"]
