"""Generate src/pcbagent/data/sample_catalog.csv.

SAMPLE DATA. Part numbers are fake (SAMPLE-xxxx), stock and prices are
made-up placeholders. It exists so tests and offline demos can exercise the
real importer, parser and selector. Never order from it: run
`pcbagent catalog update` for real JLCPCB data.
"""

import csv
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pcbagent.units import E12, E24, format_value  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "src" / "pcbagent" / "data" / "sample_catalog.csv"
rows = []
counter = itertools.count(1)


def add(mpn, manufacturer, category, subcategory, package, description, stock, price, library_type):
    rows.append({
        "lcsc": f"SAMPLE-{next(counter):04d}", "mpn": mpn, "manufacturer": manufacturer,
        "category": category, "subcategory": subcategory, "package": package,
        "description": description, "datasheet": "", "stock": stock,
        "price": f"1-99:{price:.4f},100-:{price * 0.7:.4f}", "library_type": library_type,
    })


def ohms(v):
    return format_value(v, "ohm", 3)


E96_EXTRA = [10.2e3, 19.6e3, 4.99e3, 49.9e3, 1.65e3, 3.24e3, 6.04e3, 12.4e3, 24.9e3, 2.49e3]
for pkg, power, series in [("0603", "100mW", E24), ("0805", "125mW", E12), ("1206", "250mW", E12),
                           ("0402", "62.5mW", E12)]:
    for exp in range(1, 7):
        for m in series:
            v = round(m * 10 ** exp, 6)
            if v > 1e6:
                continue
            add(f"{pkg}WAF{int(v)}T5E", "UNI-ROYAL", "Resistors", "Chip Resistor - Surface Mount", pkg,
                f"{power} Thick Film Resistors 75V ±1% ±100ppm/℃ {ohms(v)} {pkg} Chip Resistor - Surface Mount ROHS",
                50000, 0.0010, "basic")
for v in E96_EXTRA:
    add(f"0603WAF{int(v)}T5E", "UNI-ROYAL", "Resistors", "Chip Resistor - Surface Mount", "0603",
        f"100mW Thick Film Resistors 75V ±1% ±100ppm/℃ {ohms(v)} 0603 Chip Resistor - Surface Mount ROHS",
        20000, 0.0012, "basic")
add("2512WAF1R0T4E", "UNI-ROYAL", "Resistors", "Chip Resistor - Surface Mount", "2512",
    "1W Thick Film Resistors ±1% ±100ppm/℃ 1Ω 2512 Chip Resistor - Surface Mount ROHS", 3000, 0.02, "extended")

caps = [
    ("0402", "100nF", 16, "X7R"), ("0402", "1uF", 10, "X5R"),
    ("0603", "10pF", 50, "C0G"), ("0603", "22pF", 50, "C0G"), ("0603", "100pF", 50, "C0G"),
    ("0603", "1nF", 50, "X7R"), ("0603", "10nF", 50, "X7R"), ("0603", "100nF", 50, "X7R"),
    ("0603", "1uF", 25, "X7R"), ("0603", "4.7uF", 10, "X5R"), ("0603", "10uF", 10, "X5R"),
    ("0805", "1uF", 50, "X7R"), ("0805", "4.7uF", 25, "X5R"), ("0805", "10uF", 25, "X5R"),
    ("0805", "22uF", 25, "X5R"), ("0805", "47uF", 10, "X5R"),
    ("1206", "22uF", 25, "X5R"), ("1206", "47uF", 16, "X5R"), ("1206", "100uF", 6.3, "X5R"),
]
for pkg, val, volts, diel in caps:
    add(f"CL{pkg}{val}{int(volts)}V", "Samsung Electro-Mechanics", "Capacitors",
        "Multilayer Ceramic Capacitors MLCC - SMD/SMT", pkg,
        f"{volts:g}V {val} {diel} ±10% {pkg} Multilayer Ceramic Capacitors MLCC - SMD/SMT ROHS",
        40000, 0.0030 if "u" not in val else 0.012, "basic")

for pkg, val, cur in [("0805", "1uH", "800mA"), ("0805", "2.2uH", "600mA"), ("0805", "4.7uH", "450mA"),
                      ("0805", "10uH", "300mA"), ("1206", "10uH", "1A")]:
    add(f"LQM{pkg}{val}", "Murata", "Inductors/Coils/Transformers", "Inductors (SMD)", pkg,
        f"{cur} {val} ±20% {pkg} Inductors (SMD) ROHS", 8000, 0.03, "extended")

for pkg, color in [("0603", "Red"), ("0603", "Green"), ("0603", "Blue"), ("0603", "White"), ("0603", "Yellow"),
                   ("0805", "Red"), ("0805", "Green")]:
    add(f"KT-{pkg}{color[0]}", "Hubei KENTO Elec", "Optoelectronics", "Light Emitting Diodes (LED)", pkg,
        f"{color} {pkg} Light Emitting Diodes (LED) ROHS", 30000, 0.004, "basic")

add("AMS1117-3.3", "Advanced Monolithic Systems", "Power Management ICs", "Linear Voltage Regulators (LDO)",
    "SOT-223", "1A Fixed 3.3V Positive electrode 15V SOT-223 Linear Voltage Regulators (LDO) ROHS", 90000, 0.07, "basic")
add("AMS1117-5.0", "Advanced Monolithic Systems", "Power Management ICs", "Linear Voltage Regulators (LDO)",
    "SOT-223", "1A Fixed 5V Positive electrode 15V SOT-223 Linear Voltage Regulators (LDO) ROHS", 60000, 0.07, "basic")
add("XC6206P332MR", "Torex Semicon", "Power Management ICs", "Linear Voltage Regulators (LDO)", "SOT-23",
    "200mA Fixed 3.3V Positive electrode 6V SOT-23 Linear Voltage Regulators (LDO) ROHS", 50000, 0.03, "basic")
add("AP2112K-3.3TRG1", "Diodes Incorporated", "Power Management ICs", "Linear Voltage Regulators (LDO)",
    "SOT-23-5", "600mA Fixed 3.3V Positive electrode 6V SOT-23-5 Linear Voltage Regulators (LDO) ROHS",
    20000, 0.12, "extended")
add("1N4148W", "Changjiang Electronics Tech (CJ)", "Diodes", "Switching Diodes", "SOD-123",
    "100V 150mA 1.25V@150mA SOD-123 Switching Diodes ROHS", 80000, 0.006, "basic")
add("B5819W", "Changjiang Electronics Tech (CJ)", "Diodes", "Schottky Barrier Diodes (SBD)", "SOD-123",
    "40V 1A 450mV@1A SOD-123 Schottky Barrier Diodes (SBD) ROHS", 70000, 0.01, "basic")
add("SS14", "MDD(Microdiode Electronics)", "Diodes", "Schottky Barrier Diodes (SBD)", "SMA(DO-214AC)",
    "40V 1A 500mV@1A SMA(DO-214AC) Schottky Barrier Diodes (SBD) ROHS", 70000, 0.012, "basic")
add("MMBT3904", "Changjiang Electronics Tech (CJ)", "Transistors", "Bipolar Transistors - BJT", "SOT-23",
    "40V 200mA 350mW NPN SOT-23 Bipolar Transistors - BJT ROHS", 60000, 0.008, "basic")
add("2N7002", "Changjiang Electronics Tech (CJ)", "Transistors", "MOSFETs", "SOT-23",
    "60V 115mA 200mW 7.5Ω@10V N Channel SOT-23 MOSFETs ROHS", 60000, 0.01, "basic")
for n in range(2, 5):
    add(f"PZ254V-11-{n:02d}P", "XFCN", "Connectors", "Pin Headers", "Plugin,P=2.54mm",
        f"Pin Header 1x{n}P 2.54mm Straight Through Hole Pin Headers ROHS", 10000, 0.02, "extended")
add("KF128-5.0-2P", "Cixi Kefa Elec", "Connectors", "Screw terminal", "Plugin,P=5mm",
    "2P 5.0mm screw terminal block Plugin,P=5mm ROHS", 5000, 0.08, "extended")
add("B2B-PH-K-S(LF)(SN)", "JST", "Connectors", "Wire To Board Connector", "Plugin,P=2mm",
    "1x2P PH 2.0mm Vertical Wire To Board Connector ROHS", 5000, 0.05, "extended")

OUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUT, "w", newline="", encoding="utf-8") as fh:
    fh.write("# SAMPLE CATALOG: fake part numbers, placeholder stock and prices. Not real JLCPCB data.\n")
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
print(f"wrote {len(rows)} rows to {OUT}")
