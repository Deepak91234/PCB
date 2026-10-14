"""SQLite store for the local parts catalog."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from ..models import home_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS parts (
    lcsc TEXT PRIMARY KEY,
    mpn TEXT, mpn_norm TEXT, manufacturer TEXT,
    category TEXT, subcategory TEXT,
    package TEXT, package_norm TEXT,
    description TEXT, datasheet TEXT,
    stock INTEGER, price_breaks TEXT,
    library_type TEXT,
    kind TEXT, value REAL, tolerance_pct REAL,
    voltage_rating REAL, power_rating REAL, current_rating REAL,
    dielectric TEXT, color TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS parts_kind ON parts(kind, package_norm, value);
CREATE INDEX IF NOT EXISTS parts_mpn ON parts(mpn_norm);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class CatalogMissing(RuntimeError):
    pass


def default_path() -> Path:
    env = os.environ.get("PCBAGENT_CATALOG")
    if env:
        return Path(env)
    return home_dir() / "catalog.sqlite"


def connect(path: Path | None = None, create: bool = False) -> sqlite3.Connection:
    path = Path(path or default_path())
    if not path.exists() and not create:
        raise CatalogMissing(
            f"no parts catalog at {path}. Run `pcbagent catalog update` once (downloads the public "
            "JLCPCB basic/preferred list; nothing about your design is sent), or "
            "`pcbagent catalog import <file>` on an offline machine.")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    if create:
        conn.executescript(SCHEMA)
    return conn


def meta(conn: sqlite3.Connection) -> dict:
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.OperationalError:
        return {}


def info(path: Path | None = None) -> dict:
    path = Path(path or default_path())
    if not path.exists():
        return {"path": str(path), "present": False}
    conn = connect(path)
    try:
        counts = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM parts GROUP BY kind ORDER BY kind")}
        total = conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
        return {"path": str(path), "present": True, "parts": total, "by_kind": counts, **meta(conn)}
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["price_breaks"] = json.loads(d["price_breaks"] or "[]")
    return d
