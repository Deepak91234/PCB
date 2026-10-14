"""``pcbagent`` command line. Every command takes ``--json`` for machine output.

The Pi extension drives the same commands, passing tool arguments on stdin,
so the agent and a human at a terminal go through identical code paths.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

from . import __version__
from . import models as models_mod
from . import schemas


def _read_input(args) -> dict:
    if getattr(args, "file", None) and args.file != "-":
        text = Path(args.file).read_text()
    else:
        text = sys.stdin.read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(_emit(args, {"ok": False, "error": f"input is not valid JSON: {exc}"}, 2))
    return data


def _emit(args, data: dict, code: int = 0, text: str | None = None) -> int:
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, default=str))
    else:
        print(text if text is not None else json.dumps(data, indent=2, ensure_ascii=False, default=str))
    return code


def _project(args):
    from .project import Project
    return Project.open(args.dir)


# --- commands -------------------------------------------------------------------

def cmd_init(args) -> int:
    from .project import Project, slugify
    request = args.request or ""
    if args.dir:
        root = Path(args.dir)
    else:
        base = Path(args.designs or "designs")
        slug = slugify(request) if request else "design"
        root = base / slug
        n = 2
        while (root / "pcbagent.json").exists():
            root = base / f"{slug}_{n}"
            n += 1
    p = Project.create(root, request)
    return _emit(args, {"ok": True, "dir": str(p.root), "name": p.name}, 0, f"created {p.root}")


def cmd_spec(args) -> int:
    from .project import ProjectError
    from .spec import SpecError
    p = _project(args)
    raw = _read_input(args)
    try:
        spec = p.write_spec(raw)
    except SpecError as exc:
        return _emit(args, {"ok": False, "errors": exc.errors},
                     1, "spec rejected:\n" + "\n".join(f"  - {e}" for e in exc.errors))
    except ProjectError as exc:
        return _emit(args, {"ok": False, "error": str(exc)}, 1, f"error: {exc}")
    st = p.state()
    text = (f"spec locked ({len(spec['requirements'])} requirements, {len(spec['conditions'])} conditions), "
            f"sha256 {st['spec_hash'][:12]}")
    return _emit(args, {"ok": True, "spec_hash": st["spec_hash"], "requirements": len(spec["requirements"]),
                        "conditions": [c["id"] for c in spec["conditions"]], "summary": text}, 0, text)


def cmd_spec_unlock(args) -> int:
    p = _project(args)
    if not args.yes:
        print("Unlocking lets the requirements change. Only do this for a genuine spec mistake.", file=sys.stderr)
        if input("type 'unlock' to continue: ").strip() != "unlock":
            return 1
    p.unlock_spec()
    return _emit(args, {"ok": True}, 0, "spec unlocked")


def cmd_circuit(args) -> int:
    from .project import ProjectError
    p = _project(args)
    raw = _read_input(args)
    try:
        result = p.submit_circuit(raw)
    except ProjectError as exc:
        return _emit(args, {"ok": False, "error": str(exc)}, 1, f"error: {exc}")
    result["ok"] = True
    result["next"] = p.next_step()
    return _emit(args, result, 0 if result["passed"] else 3, result["summary"] + f"\nnext: {result['next']}")


def cmd_layout(args) -> int:
    from .layout import pipeline
    from .layout.constraints import ConstraintError
    from .project import ProjectError
    p = _project(args)
    if args.file or not sys.stdin.isatty():
        raw = _read_input(args)
    else:
        saved = p.path("layout", "constraints.json")
        raw = json.loads(saved.read_text()) if saved.exists() else {"constraints": []}
    try:
        result = pipeline.run(p, raw, seed=args.seed, max_passes=args.passes)
    except ConstraintError as exc:
        return _emit(args, {"ok": False, "errors": exc.errors}, 1,
                     "constraints rejected:\n" + "\n".join(f"  - {e}" for e in exc.errors))
    except ProjectError as exc:
        return _emit(args, {"ok": False, "error": str(exc)}, 1, f"error: {exc}")
    result["ok"] = True
    result["summary"] = pipeline.summarize(result)
    code = _emit(args, result, 0 if result["passed"] else 3, result["summary"])
    if args.demo and result["passed"]:
        from .kicad import replay
        replay.launch(p.path("layout"), wait=False)
    return code


def cmd_fab(args) -> int:
    from .fab import export
    p = _project(args)
    st = p.state()
    lay = st.get("layout") or {}
    if not lay.get("passed"):
        return _emit(args, {"ok": False, "error": "no DRC-clean board yet; run layout first"}, 1,
                     "error: no DRC-clean board yet; run layout first")
    res = export(p.root, Path(lay["board"]), p.path("fab"))
    res["ok"] = True
    text = "fabrication files:\n" + "\n".join(f"  {f}" for f in res["files"]) + \
        "".join(f"\n  note: {w}" for w in res["warnings"])
    return _emit(args, res, 0, text)


def cmd_status(args) -> int:
    p = _project(args)
    st = p.status()
    lines = [f"{st['name']}: phase {st['phase']} (spec {'locked' if st['spec_locked'] else 'not written'}, "
             f"{st['circuit_attempts']} circuit attempt(s))"]
    v = st.get("verification")
    if v:
        lines.append(f"  verification v{v['circuit_version']}: {'PASS' if v['passed'] else 'FAIL'}")
        lines += [f"    {m}" for m in v["failed"][:8]]
    if st.get("layout"):
        lines.append(f"  layout: {st['layout'].get('message')}")
    lines.append(f"  next: {st['next']}")
    return _emit(args, st, 0, "\n".join(lines))


def cmd_models(args) -> int:
    models = [models_mod.summary(m) for m in models_mod.all_models().values()
              if not args.kind or m["kind"] == args.kind]
    if args.json:
        return _emit(args, {"ok": True, "models": models})
    lines = []
    for m in models:
        pins = ", ".join(m["pins"])
        lines.append(f"{m['id']:<16} {m['kind']:<10} pins [{pins}]  {m['title']}")
    return _emit(args, {}, 0, "\n".join(lines))


def cmd_schema(args) -> int:
    if args.write:
        out = Path(args.write)
        out.mkdir(parents=True, exist_ok=True)
        for name in schemas.ALL:
            (out / f"{name}.schema.json").write_text(json.dumps(schemas.tool_schema(name), indent=2) + "\n")
        from .agent import prompts
        (out / "prompts.json").write_text(json.dumps(prompts.export(), indent=2) + "\n")
        return _emit(args, {"ok": True, "dir": str(out)}, 0, f"wrote schemas to {out}")
    return _emit(args, schemas.tool_schema(args.name), 0, json.dumps(schemas.tool_schema(args.name), indent=2))


def cmd_catalog(args) -> int:
    from .catalog import db, importer
    if args.action == "update":
        res = importer.update(full=args.full)
        return _emit(args, {"ok": True, **res}, 0, f"catalog: {res['rows_kept']} parts from {res['downloaded']}")
    if args.action == "import":
        res = importer.import_file(Path(args.path))
        return _emit(args, {"ok": True, **res}, 0, f"catalog: {res['rows_kept']} parts imported")
    if args.action == "sample":
        res = importer.ensure_sample(db.default_path())
        return _emit(args, {"ok": True, **res}, 0,
                     f"SAMPLE catalog installed at {res['catalog']} ({res['rows_kept']} fake parts, tests/demos only)")
    if args.action == "info":
        info = db.info()
        return _emit(args, {"ok": True, **info}, 0, json.dumps(info, indent=2))
    if args.action == "search":
        return _catalog_search(args)
    return 2


def _catalog_search(args) -> int:
    from .catalog import db
    from .catalog.parse import unit_price
    from .units import UnitError, parse_value
    conn = db.connect()
    where, params = [], []
    if args.kind:
        where.append("kind = ?")
        params.append(args.kind)
    if args.package:
        where.append("package_norm = ?")
        params.append(args.package)
    if args.value:
        try:
            v = parse_value(args.value)
            where.append("value BETWEEN ? AND ?")
            params += [v * 0.9995, v * 1.0005]
        except UnitError:
            pass
    for word in (args.query or "").split():
        where.append("(mpn LIKE ? OR description LIKE ? OR lcsc = ?)")
        params += [f"%{word}%", f"%{word}%", word.upper()]
    sql = "SELECT * FROM parts" + (" WHERE " + " AND ".join(where) if where else "") + \
        " ORDER BY CASE library_type WHEN 'basic' THEN 0 WHEN 'preferred' THEN 1 ELSE 2 END, stock DESC LIMIT ?"
    params.append(args.limit)
    rows = [db.row_to_dict(r) for r in conn.execute(sql, params)]
    conn.close()
    out = [{"lcsc": r["lcsc"], "mpn": r["mpn"], "kind": r["kind"], "package": r["package"],
            "description": r["description"], "stock": r["stock"], "library_type": r["library_type"],
            "unit_price_usd": unit_price(r["price_breaks"], 10)} for r in rows]
    text = "\n".join(f"{r['lcsc']:<12} {r['mpn'][:22]:<22} {r['package'][:10]:<10} {r['library_type']:<9} "
                     f"stock {r['stock']:<8} {r['description'][:70]}" for r in out) or "no matches"
    return _emit(args, {"ok": True, "parts": out}, 0, text)


def cmd_doctor(args) -> int:
    from .doctor import run_checks
    report = run_checks()
    text = "\n".join(f"{'ok  ' if c['ok'] else 'MISS'} {c['name']:<22} {c['detail']}" for c in report["checks"])
    text += "\n\n" + "\n".join(report["privacy"])
    return _emit(args, report, 0 if report["ready"] else 1, text)


def cmd_replay(args) -> int:
    from .kicad import replay
    p = _project(args)
    info = replay.launch(p.path("layout"), wait=args.wait, launch=not args.no_launch)
    return _emit(args, {"ok": True, **info}, 0, json.dumps(info, indent=2))


def cmd_plugin(args) -> int:
    from .kicad import replay
    dest = replay.install_plugin()
    return _emit(args, {"ok": True, "installed": str(dest)}, 0, f"KiCad replay plugin installed in {dest}")


def cmd_demo(args) -> int:
    from .demo import run_demo
    return run_demo(args)


def cmd_run(args) -> int:
    from .agent.loop import main as agent_main
    return agent_main(args)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pcbagent", description="Local, verified PCB design.")
    ap.add_argument("--version", action="version", version=f"pcbagent {__version__}")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("init", cmd_init, "create a design directory")
    sp.add_argument("dir", nargs="?")
    sp.add_argument("--request", "-r", help="the natural-language request")
    sp.add_argument("--designs", help="parent directory when dir is omitted (default ./designs)")

    sp = add("spec", cmd_spec, "validate and lock the spec (JSON on stdin or --file)")
    sp.add_argument("dir")
    sp.add_argument("--file", "-f")

    sp = add("spec-unlock", cmd_spec_unlock, "unlock the spec (human decision)")
    sp.add_argument("dir")
    sp.add_argument("--yes", action="store_true")

    sp = add("circuit", cmd_circuit, "validate, simulate, verify and source a circuit")
    sp.add_argument("dir")
    sp.add_argument("--file", "-f")

    sp = add("layout", cmd_layout, "place, route and DRC the verified design")
    sp.add_argument("dir")
    sp.add_argument("--file", "-f", help="constraints JSON (default: stdin, or the saved constraints)")
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("--passes", type=int, default=40, help="Freerouting max passes")
    sp.add_argument("--demo", action="store_true", help="replay placement and routing in KiCad afterwards")

    sp = add("fab", cmd_fab, "export Gerbers, drill, BOM and CPL")
    sp.add_argument("dir")

    sp = add("status", cmd_status, "show design state and the next step")
    sp.add_argument("dir")

    sp = add("models", cmd_models, "list part models a circuit may use")
    sp.add_argument("--kind")

    sp = add("schema", cmd_schema, "print or export JSON schemas")
    sp.add_argument("name", nargs="?", default="spec", choices=sorted(schemas.ALL))
    sp.add_argument("--write", metavar="DIR")

    sp = add("catalog", cmd_catalog, "manage the local JLCPCB parts catalog")
    sp.add_argument("action", choices=["update", "import", "info", "search", "sample"])
    sp.add_argument("path", nargs="?", help="file for import")
    sp.add_argument("--full", action="store_true", help="update: download the full in-stock database (~1 GB)")
    sp.add_argument("--kind")
    sp.add_argument("--package")
    sp.add_argument("--value")
    sp.add_argument("--query", "-q")
    sp.add_argument("--limit", type=int, default=20)

    add("doctor", cmd_doctor, "check the toolchain and privacy settings")

    sp = add("replay", cmd_replay, "replay placement and routing in KiCad's PCB editor")
    sp.add_argument("dir")
    sp.add_argument("--wait", action="store_true")
    sp.add_argument("--no-launch", action="store_true")

    add("install-plugin", cmd_plugin, "install the KiCad replay action plugin")

    sp = add("demo", cmd_demo, "run a bundled example end to end without an LLM")
    sp.add_argument("example", nargs="?", default="ldo")
    sp.add_argument("--dir", help="where to create the design (default ./designs/demo_<example>)")
    sp.add_argument("--watch", action="store_true", help="replay the layout in KiCad when done")
    sp.add_argument("--fab", action="store_true", help="also export fabrication files")

    sp = add("run", cmd_run, "standalone agent loop against a local Ollama model (no Pi needed)")
    sp.add_argument("request")
    sp.add_argument("--model", default=os.environ.get("PCBAGENT_MODEL", "nemotron-3-nano"))
    sp.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    sp.add_argument("--dir")
    sp.add_argument("--max-attempts", type=int, default=12)
    sp.add_argument("--no-layout", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    try:
        return args.fn(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 - surface every failure as structured output
        from .catalog.db import CatalogMissing
        from .kicad.tools import ToolMissing
        from .project import ProjectError
        from .spice.run import SimulatorMissing
        known = (ProjectError, CatalogMissing, ToolMissing, SimulatorMissing, FileNotFoundError)
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(exc, known):
            payload["traceback"] = traceback.format_exc()[-2000:]
        _emit(args, payload, 1, f"error: {exc}" + ("" if isinstance(exc, known) else "\n" + payload["traceback"]))
        return 1


if __name__ == "__main__":
    sys.exit(main())
