"""Install the KiCad replay plugin and open a board with the replay running."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .tools import kicad_version

PLUGIN = Path(__file__).resolve().parent / "plugin" / "pcbagent_replay.py"


def plugin_dir() -> Path:
    env = os.environ.get("PCBAGENT_KICAD_PLUGIN_DIR")
    if env:
        return Path(env)
    major_minor = ".".join(kicad_version().split(".")[:1]) + ".0" if kicad_version() != "unknown" else "9.0"
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Documents" / "KiCad" / major_minor / "scripting" / "plugins"
    if sys.platform == "win32":
        return home / "Documents" / "KiCad" / major_minor / "scripting" / "plugins"
    return home / ".local" / "share" / "kicad" / major_minor / "scripting" / "plugins"


def install_plugin() -> Path:
    dest = plugin_dir()
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PLUGIN, dest / PLUGIN.name)
    return dest / PLUGIN.name


def _pcbnew_exe() -> str | None:
    for c in (os.environ.get("PCBAGENT_PCBNEW"), shutil.which("pcbnew"),
              "/Applications/KiCad/KiCad.app/Contents/Applications/pcbnew.app/Contents/MacOS/pcbnew"):
        if c and Path(c).exists():
            return c
    if sys.platform == "win32":
        import glob
        found = sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\pcbnew.exe"), reverse=True)
        if found:
            return found[0]
    return None


def launch(layout_dir: Path, wait: bool = False, launch: bool = True) -> dict:
    trace = layout_dir / "layout_trace.json"
    boards = sorted(layout_dir.glob("*.kicad_pcb"))
    if not trace.exists() or not boards:
        return {"launched": False, "message": f"no board/trace in {layout_dir}; run layout first"}
    installed = install_plugin()
    board = boards[0]
    exe = _pcbnew_exe()
    info = {"board": str(board), "trace": str(trace), "plugin": str(installed)}
    if not launch:
        info.update(launched=False, message=f"plugin installed; open {board.name} and run 'PCBAgent: replay layout'")
        return info
    if exe is None:
        info.update(launched=False, message="pcbnew not found; open the board in KiCad and run the PCBAgent replay plugin")
        return info
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        info.update(launched=False, message="no display; open the board in KiCad and run the PCBAgent replay plugin")
        return info
    env = dict(os.environ, PCBAGENT_REPLAY=str(trace))
    env.pop("PCBAGENT_REPLAY_STARTED", None)
    proc = subprocess.Popen([exe, str(board)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    if wait:
        proc.wait()
    info.update(launched=True, pid=proc.pid, message=f"KiCad PCB editor opened on {board.name}; replay starts automatically")
    return info
