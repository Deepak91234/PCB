"""A design project on disk and its state machine.

    designs/<name>/
      pcbagent.json        state: phase, spec lock hash, attempts, history
      request.txt          the original natural-language request
      spec.json            locked specification
      circuit.json         latest circuit (history/ keeps every version)
      verification.json    latest verification report
      parts.json           part selection, BOM and cost
      <name>.net           KiCad netlist
      layout/              constraints, board, DSN/SES, DRC, replay trace
      fab/                 gerbers, drill, BOM and CPL for JLCPCB

Phases: spec -> circuit -> layout -> done. A circuit only advances when it
passes verification and every part is orderable.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import circuit as circuit_mod
from . import spec as spec_mod
from .catalog import db as catalog_db
from .catalog import select as select_mod
from .verify import summarize as summarize_verification
from .verify import verify

STATE_FILE = "pcbagent.json"


class ProjectError(RuntimeError):
    pass


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s[:max_len].rstrip("_") or "design")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


class Project:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()

    # --- paths ----------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.root.name

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @property
    def state_path(self) -> Path:
        return self.path(STATE_FILE)

    # --- lifecycle ----------------------------------------------------------
    @classmethod
    def create(cls, root: Path | str, request: str = "") -> "Project":
        p = cls(root)
        if p.state_path.exists():
            raise ProjectError(f"{p.root} already contains a design")
        p.root.mkdir(parents=True, exist_ok=True)
        if request:
            p.path("request.txt").write_text(request.strip() + "\n")
        p.save_state({
            "version": 1, "name": p.name, "created": _now(), "request": request.strip(),
            "phase": "spec", "spec_hash": None, "circuit_attempts": 0, "circuit_version": 0,
            "verified_circuit_hash": None, "parts_ok": False, "layout_attempts": 0,
            "layout": None, "history": [],
        })
        return p

    @classmethod
    def open(cls, root: Path | str) -> "Project":
        p = cls(root)
        if not p.state_path.exists():
            raise ProjectError(f"{p.root} is not a PCBAgent design (no {STATE_FILE}); run `pcbagent init` first")
        return p

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def save_state(self, state: dict) -> None:
        _write_json(self.state_path, state)

    def _event(self, state: dict, event: str, **data) -> None:
        state["history"].append({"t": _now(), "event": event, **data})
        state["history"] = state["history"][-200:]

    # --- spec -------------------------------------------------------------
    def write_spec(self, raw: dict) -> dict:
        state = self.state()
        if state.get("spec_hash"):
            raise ProjectError(
                "the spec is locked. Requirements cannot be edited to make a design pass. "
                "Only the user can unlock it (`pcbagent spec-unlock <design>`).")
        spec = spec_mod.normalize(raw)
        _write_json(self.path("spec.json"), spec)
        state["spec_hash"] = spec_mod.content_hash(spec)
        state["spec_locked_at"] = _now()
        state["phase"] = "circuit"
        self._event(state, "spec_locked", hash=state["spec_hash"])
        self.save_state(state)
        return spec

    def unlock_spec(self) -> None:
        state = self.state()
        state["spec_hash"] = None
        state["phase"] = "spec"
        state["verified_circuit_hash"] = None
        state["parts_ok"] = False
        self._event(state, "spec_unlocked")
        self.save_state(state)

    def spec(self) -> dict:
        """The locked spec, after checking it was not modified on disk."""
        state = self.state()
        if not state.get("spec_hash"):
            raise ProjectError("no spec yet; write and lock the spec first (pcb_spec)")
        path = self.path("spec.json")
        if not path.exists():
            raise ProjectError("spec.json is missing")
        spec = json.loads(path.read_text())
        if spec_mod.content_hash(spec) != state["spec_hash"]:
            raise ProjectError(
                "spec.json does not match the locked hash; it was edited after locking. "
                "Restore it or have the user run `pcbagent spec-unlock <design>`.")
        return spec

    # --- circuit ------------------------------------------------------------
    def submit_circuit(self, raw: dict, catalog_path=None) -> dict:
        """Validate, simulate, verify and source a circuit. Returns a result dict."""
        spec = self.spec()
        state = self.state()
        state["circuit_attempts"] += 1
        attempt = state["circuit_attempts"]
        result: dict = {"attempt": attempt, "stage": "validate", "passed": False}
        try:
            circ = circuit_mod.load(raw, spec)
        except circuit_mod.CircuitError as exc:
            result.update(errors=exc.errors, warnings=exc.warnings,
                          summary="FAIL: circuit is invalid (nothing simulated):\n" +
                                  "\n".join(f"  - {e}" for e in exc.errors[:20]))
            self._event(state, "circuit_invalid", attempt=attempt, errors=exc.errors[:10])
            self.save_state(state)
            return result

        state["circuit_version"] += 1
        version = state["circuit_version"]
        lay = state.get("layout") or {}
        if lay and lay.get("circuit_hash") != spec_mod.content_hash(circ.raw):
            # The board was built from a different circuit; it no longer describes this design.
            self._event(state, "layout_invalidated", reason=f"circuit v{version} submitted")
            state["layout"] = None
        _write_json(self.path("circuit.json"), circ.raw)
        _write_json(self.path("history", f"circuit_v{version:03d}.json"), circ.raw)

        result["stage"] = "verify"
        report = verify(spec, circ, self.root)
        report["circuit_version"] = version
        _write_json(self.path("verification.json"), report)
        _write_json(self.path("history", f"verification_v{version:03d}.json"), report)
        text = summarize_verification(report)
        result.update(version=version, verification_passed=report["passed"], summary=text,
                      verification=_compact_verification(report))
        if not report["passed"]:
            self._event(state, "verification_failed", attempt=attempt, version=version,
                        failed=[r["id"] for r in report["requirements"] if not r["passed"]])
            state["verified_circuit_hash"] = None
            state["parts_ok"] = False
            state["phase"] = "circuit"
            self.save_state(state)
            return result

        state["verified_circuit_hash"] = report["circuit_hash"]
        self._event(state, "verification_passed", attempt=attempt, version=version)
        result["stage"] = "parts"
        parts = self._select_parts(circ, report, spec, catalog_path)
        result["parts"] = parts
        result["summary"] = text + "\n" + parts["summary"]
        state["parts_ok"] = parts["passed"]
        if parts["passed"]:
            # Resubmitting the circuit a board was built from keeps that board.
            state["phase"] = "done" if (state.get("layout") or {}).get("passed") else "layout"
            result["passed"] = True
            self._event(state, "parts_selected", version=version, cost=parts.get("cost"))
        else:
            state["phase"] = "circuit"
            self._event(state, "parts_failed", version=version, failures=parts.get("failures"))
        self.save_state(state)
        return result

    def _select_parts(self, circ, report, spec, catalog_path) -> dict:
        from .kicad.netlist import write_netlist
        pkg = spec.get("preferences", {}).get("passive_package", "0603")
        try:
            sel = select_mod.select(circ, report, spec, catalog_path)
        except catalog_db.CatalogMissing as exc:
            self.path(f"{self.name}.net").write_text(write_netlist(circ, None, self.name, pkg))
            self._write_schematic(circ, None, pkg)
            return {"passed": False, "summary": f"FAIL: {exc}", "failures": [{"message": str(exc)}],
                    "catalog_missing": True}
        _write_json(self.path("parts.json"), sel)
        self.path(f"{self.name}.net").write_text(write_netlist(circ, sel["parts"], self.name, pkg))
        self._write_schematic(circ, sel["parts"], pkg)
        from .fab import write_bom
        write_bom(sel, self.path("bom_jlcpcb.csv"))
        return {"passed": sel["passed"], "summary": select_mod.summarize(sel),
                "failures": sel["failures"], "cost": sel["cost"], "sample_catalog": sel["catalog"]["sample"]}

    def _write_schematic(self, circ, parts, pkg, dest: Path | None = None) -> Path | None:
        """KiCad schematic next to the netlist; skipped (not fatal) if the symbol libraries are missing."""
        from .kicad import libs
        from .kicad.schematic import write_schematic
        if libs.symbol_dir() is None:
            return None
        dest = dest or self.path(f"{self.name}.kicad_sch")
        dest.write_text(write_schematic(circ, parts, self.name, pkg))
        return dest

    def circuit(self) -> circuit_mod.Circuit:
        spec = self.spec()
        path = self.path("circuit.json")
        if not path.exists():
            raise ProjectError("no circuit yet")
        return circuit_mod.load(json.loads(path.read_text()), spec)

    def require_verified(self) -> tuple[dict, circuit_mod.Circuit, dict]:
        """Re-check that the current files are the verified, sourced design."""
        spec = self.spec()
        circ = self.circuit()
        state = self.state()
        if spec_mod.content_hash(circ.raw) != state.get("verified_circuit_hash"):
            raise ProjectError("the current circuit has not passed verification; submit it with pcb_circuit first")
        parts_path = self.path("parts.json")
        parts = json.loads(parts_path.read_text()) if parts_path.exists() else None
        if not state.get("parts_ok"):
            raise ProjectError("parts are not all orderable yet; fix the part selection failures first")
        return spec, circ, parts

    # --- status -----------------------------------------------------------------
    def status(self) -> dict:
        state = self.state()
        out = {k: state.get(k) for k in ("name", "phase", "request", "spec_hash", "circuit_attempts",
                                          "circuit_version", "parts_ok", "layout_attempts")}
        out["root"] = str(self.root)
        out["spec_locked"] = bool(state.get("spec_hash"))
        vpath = self.path("verification.json")
        if vpath.exists():
            v = json.loads(vpath.read_text())
            out["verification"] = {"passed": v["passed"], "circuit_version": v.get("circuit_version"),
                                   "failed": [r["message"] for r in v["requirements"] if not r["passed"]]
                                   + [c["message"] for c in v["ratings"] if not c["passed"]]}
        out["layout"] = state.get("layout")
        out["next"] = self.next_step()
        out["recent"] = state["history"][-5:]
        return out

    def next_step(self) -> str:
        state = self.state()
        phase = state["phase"]
        if phase == "spec":
            return "write the spec with pcb_spec"
        if phase == "circuit":
            v = self.path("verification.json")
            if state["circuit_attempts"] == 0:
                return "write the first circuit with pcb_circuit"
            if v.exists() and json.loads(v.read_text())["passed"] and not state.get("parts_ok"):
                return "change part values or packages so every part is orderable, then pcb_circuit again"
            return "fix the failing checks and submit a revised circuit with pcb_circuit"
        if phase == "layout":
            lay = state.get("layout") or {}
            if lay.get("passed"):
                return "done: board routed with zero DRC errors"
            return "write placement constraints with pcb_layout"
        return "done"

    def begin_layout(self) -> None:
        """Forget the current board before a new layout attempt overwrites its files.

        If the attempt is interrupted, the design then says "not laid out" instead of
        presenting a half-written board as DRC clean.
        """
        state = self.state()
        state["layout"] = None
        if state["phase"] == "done":
            state["phase"] = "layout"
        self.save_state(state)

    def current_layout(self) -> dict | None:
        """The layout state if it is DRC clean and was built from the current verified circuit."""
        state = self.state()
        lay = state.get("layout") or {}
        if not lay.get("passed"):
            return None
        if lay.get("circuit_hash") and lay["circuit_hash"] != state.get("verified_circuit_hash"):
            return None
        return lay

    def set_layout(self, info: dict) -> None:
        state = self.state()
        state["layout_attempts"] += 1
        state["layout"] = info
        state["phase"] = "done" if info.get("passed") else "layout"
        self._event(state, "layout", passed=info.get("passed"), drc=info.get("drc_summary"))
        self.save_state(state)

    def archive_layout(self) -> None:
        lay = self.path("layout")
        if lay.exists():
            n = self.state()["layout_attempts"]
            dest = self.path("history", f"layout_v{n:03d}")
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(lay, dest, ignore=shutil.ignore_patterns("*.ses", "*.dsn", "fp-info-cache"))


def _compact_verification(report: dict) -> dict:
    """What the model needs to fix the design, without raw-file noise."""
    return {
        "passed": report["passed"],
        "requirements": [{"id": r["id"], "passed": r["passed"], "message": r["message"],
                          "nominal": r.get("nominal"), "min": r.get("min"), "max": r.get("max"),
                          "expected": r["expected"]} for r in report["requirements"]],
        "rating_failures": [c["message"] for c in report["ratings"] if not c["passed"]],
        "simulation_errors": report["simulation_errors"][:5],
        "warnings": report.get("warnings", [])[:8],
    }


def find_designs(base: Path) -> list[Path]:
    return sorted(p.parent for p in base.glob(f"*/{STATE_FILE}"))
