"""Choose catalog parts for a verified circuit.

Selection is constrained by verification, not the other way round:

* a passive must have the exact value that was simulated,
* its tolerance must be no worse than the tolerance the corners assumed,
* its power / voltage / current rating must cover the simulated worst case
  with the derating margin from :mod:`pcbagent.verify`,
* stock must cover the build quantity.

Among the parts that qualify, JLCPCB basic parts win (no extended-part
loading fee), then preferred, then the lowest unit price, then stock.
Footprints come from the part model and are checked against the installed
KiCad libraries.
"""

from __future__ import annotations

import math
import sqlite3

from ..circuit import Circuit, Component
from ..kicad import libs
from ..units import format_value
from . import db
from .parse import normalize_mpn, normalize_package, unit_price

LIB_RANK = {"basic": 0, "preferred": 1, "extended": 2}
# Larger packages to try, in order, when a rating does not fit the preferred one.
PACKAGE_LADDER = {
    "resistor": ["0402", "0603", "0805", "1206", "2512"],
    "capacitor": ["0402", "0603", "0805", "1206", "1210"],
    "inductor": ["0603", "0805", "1206"],
    "led": ["0603", "0805"],
}
EXTENDED_FEE_USD = 3.0  # JLCPCB's per-unique-extended-part loading fee at the time of writing; estimate only


class SelectionError(RuntimeError):
    pass


def _candidates_for_packages(comp: Component, default_pkg: str) -> list[str]:
    footprints = comp.model["kicad"].get("footprints", {})
    if comp.package:
        return [comp.package]
    ladder = [p for p in PACKAGE_LADDER.get(comp.kind, list(footprints)) if p in footprints]
    start = default_pkg if default_pkg in ladder else comp.model["kicad"].get("default_package")
    if start in ladder:
        return ladder[ladder.index(start):]
    return ladder


def _rank(row: sqlite3.Row, qty: int):
    price = unit_price(db.row_to_dict(row)["price_breaks"], qty)
    return (LIB_RANK.get(row["library_type"], 3), price if price is not None else math.inf, -(row["stock"] or 0))


def _row_summary(row: sqlite3.Row, qty: int) -> dict:
    d = db.row_to_dict(row)
    return {
        "lcsc": d["lcsc"], "mpn": d["mpn"], "manufacturer": d["manufacturer"], "catalog_package": d["package"],
        "description": d["description"], "stock": d["stock"], "library_type": d["library_type"],
        "unit_price_usd": unit_price(d["price_breaks"], qty), "tolerance_pct": d["tolerance_pct"],
        "voltage_rating": d["voltage_rating"], "power_rating": d["power_rating"],
        "current_rating": d["current_rating"], "datasheet": d["datasheet"], "source": d["source"],
    }


def _passive(conn, comp: Component, need: dict, qty: int, default_pkg: str, basic_only: bool):
    kind = comp.kind
    value = comp.value
    reasons: list[str] = []
    for pkg in _candidates_for_packages(comp, default_pkg):
        rows = conn.execute(
            "SELECT * FROM parts WHERE kind = ? AND package_norm = ? AND value BETWEEN ? AND ?",
            (kind, pkg, value * 0.9995, value * 1.0005)).fetchall()
        if not rows:
            reasons.append(f"no {format_value(value, comp.model['value']['unit'])} {kind} in {pkg}")
            continue
        ok = []
        for r in rows:
            why = []
            tol = need.get("max_tolerance_pct")
            if tol is not None and (r["tolerance_pct"] is None or r["tolerance_pct"] > tol + 1e-9):
                why.append(f"tolerance {r['tolerance_pct']}% > {tol}% assumed in verification")
            if need.get("min_power_w") and (r["power_rating"] or 0) < need["min_power_w"]:
                why.append(f"power rating {r['power_rating']} W < {need['min_power_w']:.3g} W needed")
            if need.get("min_voltage_v") and (r["voltage_rating"] or 0) < need["min_voltage_v"]:
                why.append(f"voltage rating {r['voltage_rating']} V < {need['min_voltage_v']:.3g} V needed")
            if need.get("min_current_a") and (r["current_rating"] or 0) < need["min_current_a"]:
                why.append(f"current rating {r['current_rating']} A < {need['min_current_a']:.3g} A needed")
            if (r["stock"] or 0) < qty:
                why.append(f"stock {r['stock']} < {qty}")
            if basic_only and r["library_type"] not in ("basic", "preferred"):
                why.append("extended part (basic_parts_only)")
            if why:
                reasons.append(f"{r['lcsc']} ({pkg}): " + "; ".join(why))
            else:
                ok.append(r)
        if ok:
            best = min(ok, key=lambda r: _rank(r, qty))
            return best, pkg, reasons
    return None, None, reasons


def _nearest_values(conn, comp: Component, pkg: str, n: int = 4) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT value FROM parts WHERE kind = ? AND package_norm = ? AND stock > 0 AND value > 0",
        (comp.kind, pkg)).fetchall()
    vals = sorted({r[0] for r in rows}, key=lambda v: abs(math.log(v / comp.value)))[:n]
    unit = comp.model["value"]["unit"]
    return [format_value(v, unit, 3) for v in sorted(vals)]


def _by_mpn(conn, comp: Component, qty: int, basic_only: bool):
    cat = comp.model["catalog"]
    pkgs = {normalize_package(p) for p in cat.get("packages", [])}
    rows = []
    for mpn in cat["mpn"]:
        norm = normalize_mpn(mpn)
        rows += conn.execute("SELECT * FROM parts WHERE mpn_norm = ? OR mpn_norm LIKE ?",
                             (norm, norm + "%")).fetchall()
    reasons = []
    ok = []
    for r in {r["lcsc"]: r for r in rows}.values():
        why = []
        if pkgs and r["package_norm"] not in pkgs:
            why.append(f"package {r['package']} does not match footprint ({', '.join(sorted(pkgs))})")
        if (r["stock"] or 0) < qty:
            why.append(f"stock {r['stock']} < {qty}")
        if basic_only and r["library_type"] not in ("basic", "preferred"):
            why.append("extended part (basic_parts_only)")
        if why:
            reasons.append(f"{r['lcsc']} {r['mpn']}: " + "; ".join(why))
        else:
            ok.append(r)
    if not rows:
        reasons.append(f"no catalog part with MPN {' / '.join(cat['mpn'])}")
    best = min(ok, key=lambda r: _rank(r, qty)) if ok else None
    return best, reasons


def _led(conn, comp: Component, qty: int, default_pkg: str, basic_only: bool):
    color = comp.model["catalog"]["color"]
    reasons = []
    for pkg in _candidates_for_packages(comp, default_pkg):
        rows = conn.execute("SELECT * FROM parts WHERE kind = 'led' AND color = ? AND package_norm = ?",
                            (color, pkg)).fetchall()
        ok = [r for r in rows if (r["stock"] or 0) >= qty
              and (not basic_only or r["library_type"] in ("basic", "preferred"))]
        if ok:
            return min(ok, key=lambda r: _rank(r, qty)), pkg, reasons
        reasons.append(f"no {color} LED in {pkg} with stock >= {qty}")
    return None, None, reasons


def _connector(conn, comp: Component, qty: int):
    cat = comp.model["catalog"]
    if cat.get("mpn"):
        best, _ = _by_mpn(conn, comp, qty, False)
        return best
    rows = conn.execute("SELECT * FROM parts WHERE kind = 'connector'").fetchall()
    kws = [k.lower() for k in cat.get("keywords", [])]
    ok = [r for r in rows if (r["stock"] or 0) >= qty and kws
          and sum(k in (r["description"] or "").lower() for k in kws) >= 2]
    return min(ok, key=lambda r: _rank(r, qty)) if ok else None


def select(circuit: Circuit, verification: dict, spec: dict, catalog_path=None) -> dict:
    prefs = spec.get("preferences", {})
    boards = int(prefs.get("quantity", 5))
    default_pkg = prefs.get("passive_package", "0603")
    basic_only = bool(prefs.get("basic_parts_only", False))
    need_all = verification.get("selection_requirements", {})
    conn = db.connect(catalog_path)
    meta = db.meta(conn)
    chosen: dict[str, dict] = {}
    failures: list[dict] = []
    try:
        counts: dict[str, int] = {}
        for c in circuit.components:
            counts[c.part] = counts.get(c.part, 0) + 1
        for comp in circuit.components:
            qty = boards  # per reference; identical parts are merged in the BOM
            cat = comp.model["catalog"]
            entry = {"ref": comp.ref, "part": comp.part, "kind": comp.kind, "value": comp.value_text()}
            if cat.get("kind") == "none":
                entry.update(status="not_purchased", footprint=comp.footprint(default_pkg),
                             note="bare copper / no part to buy")
                chosen[comp.ref] = entry
                continue
            row, pkg, reasons = None, None, []
            if comp.model.get("value"):
                row, pkg, reasons = _passive(conn, comp, need_all.get(comp.ref, {}), qty, default_pkg, basic_only)
            elif comp.kind == "led":
                row, pkg, reasons = _led(conn, comp, qty, default_pkg, basic_only)
            elif comp.kind == "connector":
                row = _connector(conn, comp, qty)
                if row is None:
                    entry.update(status="hand_solder", footprint=comp.footprint(),
                                 note="no matching catalog part; fit by hand (not in the assembly BOM)")
                    chosen[comp.ref] = entry
                    continue
            else:
                row, reasons = _by_mpn(conn, comp, qty, basic_only)
            if row is None:
                fail = {"ref": comp.ref, "part": comp.part, "value": comp.value_text(), "reasons": reasons[-6:]}
                if comp.model.get("value"):
                    pkg_try = comp.package or default_pkg
                    fail["nearest_in_stock"] = _nearest_values(conn, comp, pkg_try)
                    fail["message"] = (f"{comp.ref}: no orderable {comp.value_text()} {comp.kind} that meets the "
                                       f"verified ratings; in-stock values near it in {pkg_try}: "
                                       f"{', '.join(fail['nearest_in_stock']) or 'none'}")
                else:
                    fail["message"] = f"{comp.ref}: {comp.part} not orderable: {'; '.join(reasons[-3:]) or 'not in catalog'}"
                failures.append(fail)
                continue
            if pkg:
                comp_pkg = pkg
            else:
                comp_pkg = comp.package
            footprint = comp.footprint(comp_pkg or default_pkg)
            if not libs.footprint_exists(footprint):
                failures.append({"ref": comp.ref, "part": comp.part,
                                 "message": f"{comp.ref}: footprint {footprint} is not in the installed KiCad libraries"})
                continue
            entry.update(status="selected", footprint=footprint, package=comp_pkg,
                         symbol=comp.model["kicad"]["symbol"], **_row_summary(row, qty))
            chosen[comp.ref] = entry
    finally:
        conn.close()

    lines = _bom_lines(chosen, boards)
    for ln in lines:
        need = ln["qty_per_board"] * boards
        if (ln["stock"] or 0) < need:
            failures.append({"ref": ",".join(ln["refs"]), "part": ln["mpn"],
                             "message": f"{','.join(ln['refs'])}: {ln['lcsc']} has {ln['stock']} in stock, "
                                        f"{need} needed for {boards} board(s)"})
    sample = any(str(e.get("source", "")).startswith("SAMPLE") for e in chosen.values())
    return {
        "passed": not failures,
        "boards": boards,
        "catalog": {"source": meta.get("source"), "imported_at": meta.get("imported_at"), "sample": sample},
        "parts": chosen,
        "failures": failures,
        "bom": lines,
        "cost": _cost(lines, boards),
    }


def _bom_lines(chosen: dict[str, dict], boards: int) -> list[dict]:
    groups: dict[str, dict] = {}
    for ref, e in chosen.items():
        if e["status"] != "selected":
            continue
        key = e["lcsc"]
        g = groups.setdefault(key, {"lcsc": e["lcsc"], "mpn": e["mpn"], "manufacturer": e["manufacturer"],
                                    "value": e["value"], "footprint": e["footprint"], "refs": [],
                                    "library_type": e["library_type"], "unit_price_usd": e["unit_price_usd"],
                                    "stock": e["stock"], "source": e.get("source")})
        g["refs"].append(ref)
    out = []
    for g in groups.values():
        g["refs"] = sorted(g["refs"], key=_ref_key)
        g["qty_per_board"] = len(g["refs"])
        out.append(g)
    return sorted(out, key=lambda g: _ref_key(g["refs"][0]))


def _ref_key(ref: str):
    letters = "".join(ch for ch in ref if ch.isalpha())
    digits = "".join(ch for ch in ref if ch.isdigit())
    return (letters, int(digits or 0))


def _cost(lines: list[dict], boards: int) -> dict:
    parts = sum((ln["unit_price_usd"] or 0) * ln["qty_per_board"] * boards for ln in lines)
    extended = sum(1 for ln in lines if ln["library_type"] == "extended")
    return {"parts_usd": round(parts, 4), "extended_parts": extended,
            "extended_fees_usd_estimate": extended * EXTENDED_FEE_USD,
            "note": "Estimate from catalog price breaks; excludes PCB, assembly and shipping."}


def summarize(result: dict) -> str:
    lines = [("PASS" if result["passed"] else "FAIL") + f": part selection for {result['boards']} board(s)"]
    if result["catalog"].get("sample"):
        lines.append("  WARNING: SAMPLE catalog (fake part numbers). Run `pcbagent catalog update` for real data.")
    for ln in result["bom"]:
        price = ln["unit_price_usd"]
        lines.append(f"  {','.join(ln['refs']):<12} {ln['value']:<12} {ln['lcsc']:<12} {ln['mpn'][:24]:<24} "
                     f"{ln['library_type']:<9} stock {ln['stock']:<8} ${price if price is not None else '?'}")
    for e in result["parts"].values():
        if e["status"] in ("hand_solder", "not_purchased"):
            lines.append(f"  {e['ref']:<12} {e['value']:<12} {e['status']}: {e['note']}")
    for f in result["failures"]:
        lines.append("  FAIL " + f["message"])
    c = result["cost"]
    lines.append(f"  parts ${c['parts_usd']:.2f} for {result['boards']} board(s), {c['extended_parts']} extended part(s)")
    return "\n".join(lines)
