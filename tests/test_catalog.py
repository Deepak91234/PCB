"""Catalog parsing, importing (several real-world formats) and part selection."""

import json
import sqlite3

import pytest

from conftest import load_example, needs_ngspice
from pcbagent import circuit as circuit_mod
from pcbagent import spec as spec_mod
from pcbagent.catalog import db, importer
from pcbagent.catalog.parse import classify, normalize_package, parametric, price_breaks, unit_price
from pcbagent.catalog.select import select


def test_parse_jlc_descriptions():
    r = parametric("resistor", "100mW Thick Film Resistors 75V ±1% ±100ppm/℃ 10kΩ 0603 Chip Resistor - Surface Mount ROHS")
    assert r == {"value": 10e3, "power_rating": 0.1, "tolerance_pct": 1.0}
    c = parametric("capacitor", "50V 100nF X7R ±10% 0603 Multilayer Ceramic Capacitors MLCC - SMD/SMT ROHS")
    assert c["value"] == pytest.approx(100e-9) and c["voltage_rating"] == 50 and c["dielectric"] == "X7R"
    assert parametric("inductor", "1A 10uH ±20% 1206 Inductors (SMD) ROHS")["current_rating"] == 1.0
    assert parametric("led", "Emerald Green 0603 Light Emitting Diodes (LED) ROHS")["color"] == "green"
    attrs = {"Resistance": "4.7kΩ", "Power(Watts)": "1/8W", "Tolerance": "±5%"}
    assert parametric("resistor", "", attrs) == {"value": 4700, "power_rating": 0.125, "tolerance_pct": 5.0}


def test_classify_and_packages():
    assert classify("Resistors", "Chip Resistor - Surface Mount", "") == "resistor"
    assert classify("Resistors", "Resistor Networks & Arrays", "") is None
    assert classify("Capacitors", "Multilayer Ceramic Capacitors MLCC - SMD/SMT", "") == "capacitor"
    assert classify("Power Management ICs", "Linear Voltage Regulators (LDO)", "") == "ldo"
    assert normalize_package("0603(1608 Metric)") == "0603" and normalize_package("SOT-223") == "SOT-223"


def test_price_breaks_formats():
    j = price_breaks('[{"qFrom": 1, "qTo": 9, "price": 0.01}, {"qFrom": 10, "qTo": null, "price": 0.008}]')
    c = price_breaks("1-9:0.01,10-:0.008")
    assert j == c
    assert unit_price(j, 5) == 0.01 and unit_price(j, 100) == 0.008


JLC_CSV = """LCSC Part,First Category,Second Category,MFR.Part,Package,Solder Joint,Manufacturer,Library Type,Description,Datasheet,Price,Stock
C25804,Resistors,Chip Resistor - Surface Mount,0603WAF1002T5E,0603,2,UNI-ROYAL,Basic,100mW Thick Film Resistors 75V ±1% ±100ppm/℃ 10kΩ 0603 Chip Resistor - Surface Mount ROHS,https://example.invalid/ds.pdf,"1-199:0.0006,200-:0.0004",9000000
C9999999,Resistors,Chip Resistor - Surface Mount,FAKE,0603,2,ACME,Extended,100mW Thick Film Resistors ±1% 10kΩ 0603 Chip Resistor - Surface Mount ROHS,,"1-:0.0002",50
"""


def test_import_jlcpcb_csv_export(tmp_path):
    src = tmp_path / "jlc.csv"
    src.write_text(JLC_CSV)
    out = tmp_path / "cat.sqlite"
    res = importer.import_file(src, out)
    assert res["rows_kept"] == 2
    conn = db.connect(out)
    row = dict(conn.execute("SELECT * FROM parts WHERE lcsc='C25804'").fetchone())
    assert row["kind"] == "resistor" and row["value"] == 10e3 and row["library_type"] == "basic"
    assert row["stock"] == 9000000


def test_import_jlcparts_sqlite(tmp_path):
    src = tmp_path / "cache.sqlite3"
    conn = sqlite3.connect(src)
    conn.executescript("""
        CREATE TABLE categories (id INTEGER PRIMARY KEY, category TEXT, subcategory TEXT);
        CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE components (lcsc INTEGER PRIMARY KEY, category_id INTEGER, mfr TEXT, package TEXT,
            joints INTEGER, manufacturer_id INTEGER, basic INTEGER, description TEXT, datasheet TEXT,
            stock INTEGER, price TEXT, last_update INTEGER, extra TEXT, preferred INTEGER);
        INSERT INTO categories VALUES (1, 'Capacitors', 'Multilayer Ceramic Capacitors MLCC - SMD/SMT');
        INSERT INTO categories VALUES (2, 'Power Management ICs', 'Linear Voltage Regulators (LDO)');
        INSERT INTO manufacturers VALUES (1, 'Samsung'), (2, 'AMS');
    """)
    extra = json.dumps({"attributes": {"Capacitance": "10uF", "Voltage Rated": "25V", "Tolerance": "±10%"}})
    conn.execute("INSERT INTO components VALUES (15850, 1, 'CL21A106KAYNNNC', '0805', 2, 1, 1, '', '', 100000,"
                 " ?, 0, ?, 0)", (json.dumps([{"qFrom": 1, "qTo": None, "price": 0.01}]), extra))
    conn.execute("INSERT INTO components VALUES (6186, 2, 'AMS1117-3.3', 'SOT-223', 3, 2, 1, "
                 "'1A 3.3V LDO', '', 50000, '[]', 0, NULL, 0)")
    conn.commit()
    conn.close()
    out = tmp_path / "cat.sqlite"
    importer.import_file(src, out)
    c = db.connect(out)
    cap = dict(c.execute("SELECT * FROM parts WHERE lcsc='C15850'").fetchone())
    assert cap["kind"] == "capacitor" and cap["value"] == pytest.approx(10e-6) and cap["voltage_rating"] == 25
    ldo = dict(c.execute("SELECT * FROM parts WHERE lcsc='C6186'").fetchone())
    assert ldo["kind"] == "ldo" and ldo["mpn_norm"] == "AMS1117-3.3" and ldo["manufacturer"] == "AMS"


@needs_ngspice
def test_selection_respects_verification(tmp_path, sample_catalog):
    from pcbagent.verify import verify
    spec = spec_mod.normalize(load_example("ldo", "spec"))
    circ = circuit_mod.load(load_example("ldo", "circuit"), spec)
    rep = verify(spec, circ, tmp_path)
    res = select(circ, rep, spec, sample_catalog)
    assert res["passed"], res["failures"]
    assert res["catalog"]["sample"] is True
    parts = res["parts"]
    assert parts["U1"]["mpn"] == "AMS1117-3.3"
    assert parts["C1"]["voltage_rating"] >= 7.5  # 1.5x the 5 V it sees in simulation
    assert parts["R1"]["tolerance_pct"] <= 1.0
    assert parts["J1"]["status"] == "selected"
    lines = {",".join(ln["refs"]): ln for ln in res["bom"]}
    assert "J1,J2" in lines and lines["J1,J2"]["qty_per_board"] == 2


@needs_ngspice
def test_power_rating_escalates_package(tmp_path, sample_catalog):
    from pcbagent.verify import verify
    spec = spec_mod.normalize({
        "title": "dummy load", "ports": [{"net": "VIN", "role": "power_in"}, {"net": "GND", "role": "ground"}],
        "conditions": [{"id": "on", "sources": [{"net": "VIN", "volts": 5}]}],
        "requirements": [{"id": "i", "condition": "on", "measure": {"type": "source_current", "net": "VIN"},
                          "max": 1}]})
    circ = circuit_mod.load({"components": [
        {"ref": "J1", "part": "header_1x02", "pins": {"1": "VIN", "2": "GND"}},
        {"ref": "R1", "part": "resistor", "value": "220", "pins": {"1": "VIN", "2": "GND"}}]}, spec)
    rep = verify(spec, circ, tmp_path)
    # 25/220 ~ 114 mW (more at the -1 % corner) -> needs >= 0.23 W with 50 % derating -> 1206 (0.25 W)
    res = select(circ, rep, spec, sample_catalog)
    assert res["passed"], res["failures"]
    assert res["parts"]["R1"]["package"] == "1206"


@needs_ngspice
def test_unstocked_value_suggests_neighbours(tmp_path, sample_catalog):
    from pcbagent.verify import verify
    spec = spec_mod.normalize(load_example("divider", "spec"))
    raw = load_example("divider", "circuit")
    raw["components"][2]["value"] = "19.4k"  # not an E96 value, not stocked
    circ = circuit_mod.load(raw, spec)
    rep = verify(spec, circ, tmp_path)
    res = select(circ, rep, spec, sample_catalog)
    assert not res["passed"]
    fail = res["failures"][0]
    assert fail["ref"] == "R2" and "19.6kΩ" in fail["nearest_in_stock"]


def test_import_jlcparts_v2_and_cdfer_csv(tmp_path):
    """Current jlcparts source DB (jlc_components) and the CDFER CSV dumped from it."""
    src = tmp_path / "cache_v2.sqlite3"
    conn = sqlite3.connect(src)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE jlc_components (lcsc INTEGER PRIMARY KEY, fetched_at INTEGER, present INTEGER,
            category TEXT, subcategory TEXT, mfr TEXT, package TEXT, joints INTEGER, manufacturer TEXT,
            library_type TEXT, preferred INTEGER, last_on_stock INTEGER, description TEXT, datasheet TEXT,
            stock INTEGER, price TEXT, attributes TEXT, rohs INTEGER, eccn TEXT, assembly INTEGER,
            assembly_process TEXT, assembly_mode TEXT, website_component_id TEXT, attrition TEXT);
    """)
    rows = [
        (25804, 0, 1, "Resistors", "Chip Resistor - Surface Mount", "0603WAF1002T5E", "0603", 2, "UNI-ROYAL",
         "base", 0, 0, "", "", 1000000, "1-199:0.0006,200-:0.0004",
         json.dumps({"Resistance": "10kΩ", "Power(Watts)": "100mW", "Tolerance": "±1%"}),
         1, "", 1, "SMT", "", "", "{}"),
        (1525, 0, 1, "Capacitors", "Multilayer Ceramic Capacitors MLCC - SMD/SMT", "CL05B104KO5NNNC", "0402", 2,
         "Samsung", "base", 0, 0, "", "", 500000, "1-:0.001",
         json.dumps({"Capacitance": "100nF", "Voltage Rated": "16V", "Tolerance": "±10%",
                     "Temperature Coefficient": "X7R"}), 1, "", 1, "SMT", "", "", "{}"),
        (999, 0, 0, "Resistors", "Chip Resistor - Surface Mount", "GONE", "0603", 2, "X", "expand", 0, 0,
         "", "", 0, "", "{}", 1, "", 1, "SMT", "", "", "{}"),
    ]
    conn.executemany(f"INSERT INTO jlc_components VALUES ({','.join('?' * 24)})", rows)
    conn.commit()
    out = tmp_path / "cat.sqlite"
    res = importer.import_file(src, out)
    assert res["rows_kept"] == 2  # the present = 0 part is skipped
    c = db.connect(out)
    r = dict(c.execute("SELECT * FROM parts WHERE lcsc='C25804'").fetchone())
    assert (r["value"], r["power_rating"], r["tolerance_pct"], r["library_type"]) == (10e3, 0.1, 1.0, "basic")
    cap = dict(c.execute("SELECT * FROM parts WHERE lcsc='C1525'").fetchone())
    assert cap["value"] == pytest.approx(100e-9) and cap["voltage_rating"] == 16 and cap["dielectric"] == "X7R"

    # The CDFER CSV is `SELECT * FROM jlc_components` plus a basic flag and assembly columns.
    header = [d[1] for d in sqlite3.connect(src).execute("PRAGMA table_info(jlc_components)")]
    import csv
    csv_path = tmp_path / "jlcpcb-components-basic-preferred.csv"
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header + ["basic", "Assembly Process", "Min Order Qty", "Attrition Qty"])
        for row in rows[:2]:
            w.writerow(list(row) + [1, "SMT", 20, 10])
    out2 = tmp_path / "cat2.sqlite"
    assert importer.import_file(csv_path, out2)["rows_kept"] == 2
    r2 = dict(db.connect(out2).execute("SELECT * FROM parts WHERE lcsc='C25804'").fetchone())
    assert r2["value"] == 10e3 and r2["library_type"] == "basic"
