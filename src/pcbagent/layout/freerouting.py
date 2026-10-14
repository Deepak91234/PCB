"""Run Freerouting offline.

Freerouting 2.x ships usage analytics (Segment and api.freerouting.app; the
payload includes command-line arguments, file details and board statistics)
and a GitHub version check, and it stores a persistent user id in its
settings file. PCBAgent runs it with three layers of containment:

1. a private settings directory (``java.io.tmpdir``) seeded with telemetry
   off, so the user's global Freerouting profile is never touched,
2. ``--usage_and_diagnostic_data.disable_analytics=true`` and GUI off,
3. a network jail: every HTTP(S) request is routed to a closed local port,
   so even code paths that ignore the settings (the version check does)
   cannot reach the internet.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from ..kicad.tools import find_freerouting, find_java

_SETTINGS = {
    "profile": {"id": "00000000-0000-0000-0000-000000000000", "email": "", "allow_telemetry": False,
                "allow_contact": False},
    "gui": {"enabled": False},
    "usage_and_diagnostic_data": {"disable_analytics": True},
    "api_server": {"enabled": False, "http_allowed": False, "endpoints": []},
    "feature_flags": {"snapshots": False, "save_jobs": False},
}

JAIL = ["-Dhttp.proxyHost=127.0.0.1", "-Dhttp.proxyPort=9", "-Dhttps.proxyHost=127.0.0.1",
        "-Dhttps.proxyPort=9", "-Dhttp.nonProxyHosts=", "-Djava.net.useSystemProxies=false",
        "-Djava.awt.headless=true"]


def route(dsn: Path, ses: Path, max_passes: int = 40, timeout: float = 300.0, threads: int = 1) -> dict:
    java = find_java()
    jar = find_freerouting()
    private = dsn.parent / ".freerouting"
    private.mkdir(exist_ok=True)
    settings_dir = private / "freerouting"
    settings_dir.mkdir(exist_ok=True)
    (settings_dir / "freerouting.json").write_text(json.dumps(_SETTINGS, indent=2))
    if ses.exists():
        ses.unlink()
    cmd = [java, f"-Djava.io.tmpdir={private}", f"-Duser.home={private}", *JAIL, "-jar", jar,
           "-de", str(dsn), "-do", str(ses), "-mp", str(max_passes), "-mt", str(threads),
           "--gui.enabled=false", "--usage_and_diagnostic_data.disable_analytics=true"]
    env = {k: v for k, v in os.environ.items() if k not in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS")}
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=dsn.parent)
        log = proc.stdout + proc.stderr
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        log = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        code = -1
    (dsn.parent / "freerouting.log").write_text(log)
    blocked = len(re.findall(r"ConnectException", log))
    stats = _stats(log)
    return {"ok": ses.exists() and code in (0, None), "returncode": code, "seconds": round(time.time() - started, 2),
            "blocked_network_attempts": blocked, "stats": stats, "log": str(dsn.parent / "freerouting.log"),
            "command": " ".join(cmd[:1] + ["..."] + cmd[-10:])}


def _stats(log: str) -> dict:
    out = {}
    m = re.findall(r"pass #(\d+).*?score of (\d+(?:\.\d+)?)", log)
    if m:
        out["passes"] = int(m[-1][0])
        out["score"] = float(m[-1][1])
    m = re.search(r"(\d+) unrouted", log)
    if m:
        out["unrouted"] = int(m.group(1))
    return out
