"""Run a bundled example through the whole pipeline with no LLM in the loop.

This is the deterministic half of PCBAgent on its own: the spec, circuit and
constraints are the files in ``examples/<name>/``; everything after that
(simulation, verification, sourcing, placement, routing, DRC) is the same
code the agent uses.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent / "data" / "examples"


def example_dir(name: str) -> Path:
    for base in (EXAMPLES, Path(__file__).resolve().parents[2] / "examples"):
        if (base / name / "spec.json").exists():
            return base / name
    raise SystemExit(f"unknown example '{name}' (have: {', '.join(list_examples())})")


def list_examples() -> list[str]:
    names = set()
    for base in (EXAMPLES, Path(__file__).resolve().parents[2] / "examples"):
        if base.is_dir():
            names.update(p.name for p in base.iterdir() if (p / "spec.json").exists())
    return sorted(names)


def run_demo(args) -> int:
    from .catalog import db, importer
    from .layout import pipeline
    from .project import Project
    from .verify import summarize as vsum

    src = example_dir(args.example)
    root = Path(args.dir or f"designs/demo_{args.example}")
    if root.exists():
        # Only ever replace a previous design, never an arbitrary directory.
        if (root / "pcbagent.json").exists():
            shutil.rmtree(root)
        elif any(root.iterdir()):
            print(f"{root} exists and is not a PCBAgent design; choose another --dir", file=sys.stderr)
            return 2
    if not db.default_path().exists():
        print("no parts catalog installed; using the bundled SAMPLE catalog (fake part numbers).\n"
              "Run `pcbagent catalog update` for real JLCPCB data.\n", file=sys.stderr)
        importer.ensure_sample(db.default_path())
    request = (src / "request.txt").read_text().strip() if (src / "request.txt").exists() else args.example
    p = Project.create(root, request)
    print(f"[1/4] spec: {request}")
    p.write_spec(json.loads((src / "spec.json").read_text()))
    print(f"[2/4] circuit -> ngspice -> verification -> parts")
    result = p.submit_circuit(json.loads((src / "circuit.json").read_text()))
    print("\n".join("      " + ln for ln in result["summary"].splitlines()))
    if not result["passed"]:
        print("demo stopped: circuit did not pass")
        return 3
    print("[3/4] layout: constraints -> placer -> Freerouting -> KiCad DRC")
    cons = json.loads((src / "constraints.json").read_text()) if (src / "constraints.json").exists() \
        else {"constraints": []}
    lay = pipeline.run(p, cons)
    print("\n".join("      " + ln for ln in pipeline.summarize(lay).splitlines()))
    if not lay["passed"]:
        return 3
    if getattr(args, "fab", False):
        from .fab import export
        res = export(p.root, Path(p.state()["layout"]["board"]), p.path("fab"))
        print("[4/4] fabrication files:\n" + "\n".join("      " + f for f in res["files"]))
    else:
        print("[4/4] fabrication export skipped (add --fab)")
    print(f"\nboard: {p.state()['layout']['board']}")
    if getattr(args, "watch", False):
        from .kicad import replay
        info = replay.launch(p.path("layout"), wait=False)
        print(f"replaying in KiCad: {info.get('message')}")
    return 0
