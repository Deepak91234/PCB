"""Find external tools: kicad-cli, KiCad's Python (with pcbnew), Java, Freerouting.

pcbnew's SWIG module is compiled against the Python that ships with (or was
built for) KiCad, which is often not the Python running PCBAgent. So the
board work runs in a separate worker process under that interpreter.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from ..models import home_dir


class ToolMissing(RuntimeError):
    pass


@lru_cache(maxsize=None)
def find_kicad_cli() -> str:
    env = os.environ.get("PCBAGENT_KICAD_CLI")
    cands = [env] if env else []
    cands += [shutil.which("kicad-cli"),
              "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
              "/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"]
    if sys.platform == "win32":
        cands += sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\kicad-cli.exe"), reverse=True)
    for c in cands:
        if c and Path(c).exists():
            return c
    raise ToolMissing("kicad-cli not found; install KiCad 7+ or set PCBAGENT_KICAD_CLI")


@lru_cache(maxsize=None)
def kicad_version() -> str:
    try:
        out = subprocess.run([find_kicad_cli(), "version"], capture_output=True, text=True, timeout=30).stdout
    except (ToolMissing, OSError, subprocess.TimeoutExpired):
        return "unknown"
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", out)
    return m.group(1) if m else "unknown"


def kicad_major() -> int:
    v = kicad_version()
    try:
        return int(v.split(".")[0])
    except ValueError:
        return 0


def _python_has_pcbnew(exe: str) -> bool:
    try:
        proc = subprocess.run([exe, "-c", "import pcbnew; print(pcbnew.Version())"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


@lru_cache(maxsize=None)
def find_kicad_python() -> str:
    env = os.environ.get("PCBAGENT_KICAD_PYTHON")
    if env:
        if not _python_has_pcbnew(env):
            raise ToolMissing(f"PCBAGENT_KICAD_PYTHON={env} cannot import pcbnew")
        return env
    cands = [sys.executable]
    for name in ("python3", "python3.13", "python3.12", "python3.11", "python3.10", "python"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    cands += glob.glob("/usr/bin/python3.*")
    cands += ["/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"]
    if sys.platform == "win32":
        cands += sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\python.exe"), reverse=True)
    seen = set()
    for c in cands:
        if not c or c in seen or not Path(c).exists() or c.endswith("-config"):
            continue
        seen.add(c)
        if _python_has_pcbnew(c):
            return c
    raise ToolMissing("no Python interpreter with KiCad's pcbnew module found; "
                      "set PCBAGENT_KICAD_PYTHON to KiCad's python")


@lru_cache(maxsize=None)
def find_java() -> str:
    env = os.environ.get("PCBAGENT_JAVA")
    if env and Path(env).exists():
        return env
    java_home = os.environ.get("JAVA_HOME")
    if java_home and (Path(java_home) / "bin" / "java").exists():
        return str(Path(java_home) / "bin" / "java")
    j = shutil.which("java")
    if j:
        return j
    raise ToolMissing("java not found; Freerouting needs Java 21+ (or set PCBAGENT_JAVA)")


@lru_cache(maxsize=None)
def find_freerouting() -> str:
    env = os.environ.get("PCBAGENT_FREEROUTING_JAR")
    cands = [env] if env else []
    for base in (home_dir() / "tools", Path("/opt/freerouting"), Path.home() / ".local/share/freerouting"):
        cands += sorted(glob.glob(str(base / "freerouting*.jar")), reverse=True)
    for c in cands:
        if c and Path(c).exists():
            return c
    raise ToolMissing(
        "Freerouting jar not found. Download freerouting-2.1.0.jar from "
        "https://github.com/freerouting/freerouting/releases into ~/.pcbagent/tools/ "
        "or set PCBAGENT_FREEROUTING_JAR")
