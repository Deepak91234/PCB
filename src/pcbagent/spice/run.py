"""Run ngspice decks and collect per-corner plots with provenance."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..circuit import Circuit
from . import netlist as nl
from .raw import Plot, RawError, read


class SimulatorMissing(RuntimeError):
    pass


def find_ngspice() -> str:
    env = os.environ.get("PCBAGENT_NGSPICE")
    if env and not (os.path.isfile(env) and os.access(env, os.X_OK)):
        raise SimulatorMissing(f"PCBAGENT_NGSPICE={env} is not an executable file")
    exe = env or shutil.which("ngspice")
    if not exe:
        raise SimulatorMissing(
            "ngspice not found. Install it (apt install ngspice / brew install ngspice / "
            "https://ngspice.sourceforge.io) or set PCBAGENT_NGSPICE.")
    return exe


def ngspice_version(exe: str | None = None) -> str:
    exe = exe or find_ngspice()
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    m = re.search(r"ngspice-?\s*([0-9][0-9.]*\w*)", out)
    return m.group(1) if m else "unknown"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class CornerResult:
    index: int
    labels: dict[str, str]
    values: dict[str, float]
    plot: Plot | None
    raw_path: str | None
    raw_sha256: str | None = None


@dataclass
class ConditionRun:
    condition_id: str
    analysis: str
    deck_path: str
    deck_sha256: str
    method: str
    corners: list[CornerResult]
    params: list[nl.CornerParam]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and all(c.plot is not None for c in self.corners)


_ERR = re.compile(r"(error|singular matrix|timestep too small|no convergence|unknown subckt|"
                  r"unable to find|could not find)", re.I)


def run_condition(circuit: Circuit, condition: dict, ground: str, params: list[nl.CornerParam],
                  analysis: str, workdir: Path, ambient_c: float, ac_input: str | None = None,
                  ac_range: tuple[float, float] = (0.1, 1e8), timeout: float = 120.0) -> ConditionRun:
    exe = find_ngspice()
    workdir.mkdir(parents=True, exist_ok=True)
    tag = f"{condition['id']}_{analysis}"
    deck_text, _ = nl.build_deck(circuit, condition, ground, params, analysis, ambient_c, ac_input)
    vary = nl.varying(params, analysis)
    corners, labels, method = nl.corner_table(vary)
    # Parameters that do not vary for this analysis stay at nominal in every corner.
    deck_text = deck_text.rstrip("\n") + "\n" + nl.control_block(analysis, corners, vary, tag, ac_range)
    deck_path = workdir / f"{tag}.cir"
    deck_path.write_text(deck_text)
    for old in workdir.glob(f"{tag}_c*.raw"):
        old.unlink()
    errors: list[str] = []
    try:
        proc = subprocess.run([exe, "-b", deck_path.name], cwd=workdir, capture_output=True,
                              text=True, timeout=timeout)
        log = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        log = ""
        errors.append(f"ngspice timed out after {timeout:.0f}s on condition {condition['id']}")
    (workdir / f"{tag}.log").write_text(log)
    for line in log.splitlines():
        if _ERR.search(line) and "warning" not in line.lower():
            msg = line.strip()
            if msg and msg not in errors:
                errors.append(msg)
    results: list[CornerResult] = []
    for i, (vals, labs) in enumerate(zip(corners, labels)):
        raw_path = workdir / f"{tag}_c{i}.raw"
        plot = None
        digest = None
        if raw_path.exists():
            try:
                plots = read(raw_path)
                plot = plots[-1] if plots else None
                digest = sha256_file(raw_path)
            except (RawError, ValueError, IndexError) as exc:
                errors.append(f"could not parse {raw_path.name}: {exc}")
        if plot is None and not errors:
            errors.append(f"corner {i} of condition {condition['id']} produced no result "
                          f"(did not converge?)")
        results.append(CornerResult(i, labs, vals, plot, str(raw_path) if plot else None, digest))
    return ConditionRun(condition["id"], analysis, str(deck_path), sha256_file(deck_path),
                        method, results, vary, errors[:10])
