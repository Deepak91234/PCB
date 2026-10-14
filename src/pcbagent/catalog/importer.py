"""Import JLCPCB part lists into the local catalog.

Supported inputs (auto-detected):

* CDFER ``jlcpcb-components-basic-preferred.csv`` (daily snapshot of JLCPCB
  basic and preferred parts, derived from yaqwsx/jlcparts),
* jlcparts / CDFER SQLite databases (``components`` table or ``v_components``
  / ``jlc_components``),
* the JLCPCB parts-library CSV export ("LCSC Part", "MFR.Part", ...),
* PCBAgent's own normalized CSV (used for the bundled sample catalog).

The only network access in PCBAgent is :func:`download`, run explicitly by
``pcbagent catalog update``. It fetches a public file and sends nothing about
your designs.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

from . import db
from .parse import (attributes_from_extra, classify, normalize_mpn, normalize_package, parametric,
                    price_breaks)

CDFER_BASIC_CSV = "https://cdfer.github.io/jlcpcb-parts-database/jlcpcb-components-basic-preferred.csv"
CDFER_FULL_SQLITE = "https://cdfer.github.io/jlcpcb-parts-database/jlcpcb-components.sqlite3"

# Column aliases, lowercase.
ALIASES = {
    "lcsc": ["lcsc", "lcsc part", "lcsc part #", "lcsc_part", "component code", "componentcode"],
    "mpn": ["mfr", "mpn", "mfr.part", "mfr part", "manufacturer part", "mfr.part #"],
    "manufacturer": ["manufacturer", "mfr name", "brand"],
    "category": ["category", "first category", "firstcategory"],
    "subcategory": ["subcategory", "second category", "secondcategory"],
    "package": ["package", "footprint"],
    "description": ["description", "desc"],
    "datasheet": ["datasheet"],
    "stock": ["stock", "quantity", "stock qty"],
    "price": ["price", "prices"],
    "library_type": ["library_type", "library type", "component library type"],
    "basic": ["basic"],
    "preferred": ["preferred"],
    "extra": ["extra", "jlc_extra"],
    "attributes": ["attributes"],
    "present": ["present"],
}


def _resolve_columns(header: list[str]) -> dict[str, str]:
    low = {h.lower().strip(): h for h in header}
    out = {}
    for key, names in ALIASES.items():
        for n in names:
            if n in low:
                out[key] = low[n]
                break
    return out


def _lcsc(value) -> str:
    s = str(value).strip()
    if not s:
        return ""
    if s.upper().startswith("C") and s[1:].isdigit():
        return s.upper()
    if s.isdigit():
        return f"C{s}"
    return s


def _library_type(rec: dict) -> str:
    lt = str(rec.get("library_type") or "").lower()
    basic = str(rec.get("basic") or "").strip().lower() in ("1", "true", "yes")
    preferred = str(rec.get("preferred") or "").strip().lower() in ("1", "true", "yes")
    if basic or lt in ("base", "basic"):
        return "basic"
    if preferred or lt == "preferred":
        return "preferred"
    if lt in ("expand", "extended"):
        return "extended"
    return lt or "extended"


def normalize_record(rec: dict, source: str) -> dict | None:
    lcsc = _lcsc(rec.get("lcsc", ""))
    if not lcsc:
        return None
    if str(rec.get("present", "1")).strip() in ("0", "False", "false"):
        return None  # jlcparts v2 keeps parts JLCPCB no longer lists, flagged present = 0
    category = str(rec.get("category") or "")
    subcategory = str(rec.get("subcategory") or "")
    attrs = attributes_from_extra(rec.get("extra"))
    # jlcparts v2 (and the CDFER CSV derived from it) stores JLCPCB's parameters as a JSON object
    direct = rec.get("attributes")
    if isinstance(direct, str) and direct.strip().startswith("{"):
        try:
            direct = json.loads(direct)
        except ValueError:
            direct = None
    if isinstance(direct, dict):
        attrs = {**attrs, **{k: v for k, v in direct.items() if isinstance(v, str)}}
    description = str(rec.get("description") or "")
    if not description and rec.get("extra"):
        try:
            description = json.loads(rec["extra"]).get("description", "") if isinstance(rec["extra"], str) else ""
        except ValueError:
            description = ""
    kind = classify(category, subcategory, description)
    if kind is None:
        return None
    params = parametric(kind, description, attrs)
    try:
        stock = int(float(rec.get("stock") or 0))
    except ValueError:
        stock = 0
    package = str(rec.get("package") or "")
    mpn = str(rec.get("mpn") or "")
    return {
        "lcsc": lcsc, "mpn": mpn, "mpn_norm": normalize_mpn(mpn),
        "manufacturer": str(rec.get("manufacturer") or ""),
        "category": category, "subcategory": subcategory,
        "package": package, "package_norm": normalize_package(package),
        "description": description, "datasheet": str(rec.get("datasheet") or ""),
        "stock": stock, "price_breaks": json.dumps(price_breaks(rec.get("price"))),
        "library_type": _library_type(rec), "kind": kind,
        "value": params.get("value"), "tolerance_pct": params.get("tolerance_pct"),
        "voltage_rating": params.get("voltage_rating"), "power_rating": params.get("power_rating"),
        "current_rating": params.get("current_rating"), "dielectric": params.get("dielectric"),
        "color": params.get("color"), "source": source,
    }


_COLS = ["lcsc", "mpn", "mpn_norm", "manufacturer", "category", "subcategory", "package", "package_norm",
         "description", "datasheet", "stock", "price_breaks", "library_type", "kind", "value",
         "tolerance_pct", "voltage_rating", "power_rating", "current_rating", "dielectric", "color", "source"]


def _insert(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        f"INSERT OR REPLACE INTO parts ({', '.join(_COLS)}) VALUES ({', '.join('?' for _ in _COLS)})",
        [tuple(r[c] for c in _COLS) for r in rows])


def _iter_csv(path: Path):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(line for line in fh if not line.startswith("#"))
        cols = _resolve_columns(reader.fieldnames or [])
        if "lcsc" not in cols:
            raise ValueError(f"{path.name}: no LCSC part number column found in {reader.fieldnames}")
        for row in reader:
            yield {k: row.get(v) for k, v in cols.items()}


def _iter_sqlite(path: Path):
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if "v_components" in tables:
            query = "SELECT * FROM v_components"
        elif "jlc_components" in tables:
            query = "SELECT * FROM jlc_components"
        elif "components" in tables:
            joins, cols = "", ["c.*"]
            if "categories" in tables:
                joins += " LEFT JOIN categories cat ON c.category_id = cat.id"
                cols += ["cat.category AS category", "cat.subcategory AS subcategory"]
            if "manufacturers" in tables:
                joins += " LEFT JOIN manufacturers m ON c.manufacturer_id = m.id"
                cols += ["m.name AS manufacturer"]
            query = f"SELECT {', '.join(cols)} FROM components c{joins}"
        else:
            raise ValueError(f"{path.name}: no components table (tables: {sorted(tables)})")
        cur = src.execute(query)
        header = [d[0] for d in cur.description]
        cols = _resolve_columns(header)
        for row in cur:
            d = dict(zip(header, row))
            yield {k: d.get(v) for k, v in cols.items()}
    finally:
        src.close()


def import_file(path: Path, catalog_path: Path | None = None, replace: bool = True,
                source: str | None = None, progress=None) -> dict:
    path = Path(path)
    catalog_path = Path(catalog_path or db.default_path())
    with open(path, "rb") as fh:
        magic = fh.read(16)
    iterator = _iter_sqlite(path) if magic.startswith(b"SQLite format 3") else _iter_csv(path)
    tmp = catalog_path.with_suffix(".importing")
    if tmp.exists():
        tmp.unlink()
    conn = db.connect(tmp, create=True)
    seen = kept = 0
    batch: list[dict] = []
    try:
        if not replace and catalog_path.exists():
            conn.execute("ATTACH DATABASE ? AS old", (str(catalog_path),))
            conn.execute("INSERT OR REPLACE INTO parts SELECT * FROM old.parts")
            conn.commit()
            conn.execute("DETACH DATABASE old")
        for rec in iterator:
            seen += 1
            norm = normalize_record(rec, source or path.name)
            if norm:
                batch.append(norm)
                kept += 1
            if len(batch) >= 5000:
                _insert(conn, batch)
                batch.clear()
                if progress:
                    progress(seen, kept)
        if batch:
            _insert(conn, batch)
        meta = {"source": source or str(path), "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "rows_seen": str(seen), "rows_kept": str(kept)}
        conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", meta.items())
        conn.commit()
    finally:
        conn.close()
    if kept == 0:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"{path.name}: no usable parts found ({seen} rows read)")
    tmp.replace(catalog_path)
    return {"catalog": str(catalog_path), "rows_seen": seen, "rows_kept": kept}


def download(url: str, dest: Path, timeout: float = 120.0, progress=None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "pcbagent-catalog/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if progress:
                progress(got, total)
    tmp.replace(dest)
    return dest


def update(full: bool = False, catalog_path: Path | None = None, keep_download: bool = True) -> dict:
    from ..models import home_dir
    url = CDFER_FULL_SQLITE if full else CDFER_BASIC_CSV
    dest = home_dir() / "downloads" / url.rsplit("/", 1)[-1]

    def show(got, total):
        if sys.stderr.isatty():
            pct = f"{100 * got / total:.0f}%" if total else f"{got >> 20} MiB"
            print(f"\rdownloading {dest.name}: {pct}", end="", file=sys.stderr, flush=True)

    download(url, dest, progress=show)
    if sys.stderr.isatty():
        print(file=sys.stderr)
    result = import_file(dest, catalog_path, source=url)
    if not keep_download:
        dest.unlink(missing_ok=True)
    result["downloaded"] = str(dest)
    return result


def sample_catalog_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "sample_catalog.csv"


def ensure_sample(catalog_path: Path) -> dict:
    """Build a catalog from the bundled SAMPLE data (fake part numbers, for tests and demos)."""
    return import_file(sample_catalog_path(), catalog_path, source="SAMPLE (not real JLCPCB data)")


def read_text_csv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))
