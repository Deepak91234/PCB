"""Toolchain and privacy check (``pcbagent doctor``)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

from .catalog import db
from .kicad import libs, tools
from .spice.run import SimulatorMissing, find_ngspice, ngspice_version


def _check(name, fn):
    try:
        detail = fn()
        return {"name": name, "ok": True, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "detail": str(exc).splitlines()[0][:200]}


def _java_version() -> str:
    java = tools.find_java()
    out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30,
                         env={k: v for k, v in os.environ.items() if k != "JAVA_TOOL_OPTIONS"})
    text = out.stderr + out.stdout
    m = re.search(r'version "(\d+)', text)
    major = int(m.group(1)) if m else 0
    if major and major < 21:
        raise RuntimeError(f"Java {major} found; Freerouting 2.x needs Java 21+")
    return f"Java {major or '?'} ({java})"


def is_local_url(url: str) -> bool:
    host = urllib.parse.urlparse(url if "://" in url else f"http://{url}").hostname or ""
    if host in ("localhost", "") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback


def _ollama() -> str:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    if not host.startswith("http"):
        host = "http://" + host
    if not is_local_url(host):
        raise RuntimeError(f"OLLAMA_HOST={host} is not this machine: prompts and designs would leave it")
    with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=3) as resp:
        tags = json.load(resp)
    names = [m["name"] for m in tags.get("models", [])]
    nemo = [n for n in names if "nemotron" in n.lower()]
    if not names:
        return f"running at {host}, no models pulled (try: ollama pull nemotron-3-nano)"
    return f"running at {host}; models: {', '.join(nemo or names[:5])}"


def _catalog() -> str:
    info = db.info()
    if not info.get("present"):
        raise RuntimeError(f"no catalog at {info['path']}; run `pcbagent catalog update`")
    src = info.get("source", "?")
    if str(src).startswith("SAMPLE"):
        raise RuntimeError("only the SAMPLE catalog is installed (fake parts); run `pcbagent catalog update`")
    return f"{info['parts']} parts from {src} (imported {info.get('imported_at')})"


def _pi() -> str:
    pi = shutil.which("pi")
    if not pi:
        raise RuntimeError("pi not on PATH (npm i -g @earendil-works/pi-coding-agent); optional, `pcbagent run` works without it")
    out = subprocess.run([pi, "--version"], capture_output=True, text=True, timeout=30)
    return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else pi


def run_checks() -> dict:
    checks = [
        {"name": "python", "ok": sys.version_info >= (3, 10), "detail": sys.version.split()[0]},
        _check("ngspice", lambda: f"ngspice {ngspice_version(find_ngspice())} ({find_ngspice()})"),
        _check("kicad-cli", lambda: f"KiCad {tools.kicad_version()} ({tools.find_kicad_cli()})"),
        _check("kicad python", lambda: tools.find_kicad_python()),
        _check("footprint library", lambda: str(libs.footprint_dir() or _raise("not found"))),
        _check("symbol library", lambda: str(libs.symbol_dir() or _raise("not found"))),
        _check("java", _java_version),
        _check("freerouting", tools.find_freerouting),
        _check("parts catalog", _catalog),
        _check("ollama (local)", _ollama),
        _check("pi", _pi),
    ]
    required = {"python", "ngspice", "kicad-cli", "kicad python", "footprint library", "java", "freerouting"}
    ready = all(c["ok"] for c in checks if c["name"] in required)
    privacy = ["privacy:"]
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    privacy.append(f"  {'ok  ' if is_local_url(host) else 'WARN'} model endpoint {host}")
    for var, want in (("PI_OFFLINE", "1"), ("PI_TELEMETRY", "0"), ("PI_SKIP_VERSION_CHECK", "1")):
        val = os.environ.get(var)
        ok = val is not None and val.lower() in ((want,) if want == "1" else ("0", "false", "no"))
        privacy.append(f"  {'ok  ' if ok else 'WARN'} {var}={val or '(unset)'}  (set {var}={want} before starting pi)")
    privacy.append("  ok   Freerouting runs with analytics off, a private settings dir and a network jail")
    privacy.append("  ok   the only network call in pcbagent is `pcbagent catalog update` (public parts list)")
    return {"ready": ready, "checks": checks, "privacy": privacy}


def _raise(msg):
    raise RuntimeError(msg)
