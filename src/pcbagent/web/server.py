"""``pcbagent serve``: a chat front-end for the agent, in your browser, on this machine.

The server is stdlib only and listens on the loopback interface. It runs the
same agent loop as ``pcbagent run`` in a background thread and streams what
happens (model messages, tool calls, simulation and layout results) to the
page as server-sent events. Designs are ordinary design folders, so anything
started here can be continued with the CLI or Pi, and the other way round.

Because the agent writes files and starts KiCad, ngspice and Java, the server
only answers requests addressed to localhost (DNS-rebinding defence), and
every request that changes something must carry a per-process token that only
the served page knows (cross-site request defence). The page itself loads
nothing from the network.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import __version__
from ..doctor import is_local_url
from ..project import STATE_FILE, Project, ProjectError, slugify
from . import bundle

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 64 * 1024
DESIGN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
REQUIRED_TOOLS = ("ngspice", "kicad-cli", "kicad python", "footprint library", "java", "freerouting")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class EventLog:
    """The chat history of one design: an append-only list of events, saved as JSON lines."""

    def __init__(self, path: Path):
        self.path = path
        self.cond = threading.Condition()
        self.events: list[dict] = []
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    continue

    def append(self, event: dict) -> dict:
        with self.cond:
            ev = {"seq": len(self.events) + 1, "t": _now(), **event}
            self.events.append(ev)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self.cond.notify_all()
            return ev

    def after(self, seq: int) -> list[dict]:
        with self.cond:
            return self.events[seq:]

    def wait(self, seq: int, timeout: float) -> list[dict]:
        with self.cond:
            if len(self.events) <= seq:
                self.cond.wait(timeout)
            return self.events[seq:]


class Busy(RuntimeError):
    pass


class NotReady(RuntimeError):
    pass


class Manager:
    """Owns the designs folder, the event logs and the (single) active agent run."""

    def __init__(self, designs_dir: Path, model: str, ollama: str, max_attempts: int = 12):
        self.designs = Path(designs_dir).resolve()
        self.designs.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.ollama = ollama.rstrip("/")
        self.max_attempts = max_attempts
        self.lock = threading.Lock()
        self.busy: str | None = None      # design with an active run; reserved before its thread starts
        self.agent = None                 # that run's Agent, once created
        self.logs: dict[str, EventLog] = {}
        self._health: dict | None = None
        self._health_at = 0.0
        self._health_lock = threading.Lock()

    # --- designs ------------------------------------------------------------
    def design_root(self, design_id: str) -> Path:
        if not DESIGN_ID.fullmatch(design_id or ""):
            raise KeyError(design_id)
        root = (self.designs / design_id).resolve()
        if root.parent != self.designs or not (root / STATE_FILE).exists():
            raise KeyError(design_id)
        return root

    def log_for(self, design_id: str) -> EventLog:
        with self.lock:
            if design_id not in self.logs:
                self.logs[design_id] = EventLog(self.design_root(design_id) / "agent" / "events.jsonl")
            return self.logs[design_id]

    def running(self) -> str | None:
        with self.lock:
            return self.busy

    # --- health ---------------------------------------------------------------
    def health(self, refresh: bool = False) -> dict:
        with self._health_lock:
            if self._health and not refresh and time.time() - self._health_at < 30:
                return self._health
            from ..catalog import db
            from ..doctor import run_checks
            checks = {c["name"]: c for c in run_checks()["checks"]}
            missing = [n for n in REQUIRED_TOOLS if not checks.get(n, {}).get("ok")]
            try:
                info = db.info()
            except Exception as exc:  # noqa: BLE001
                info = {"present": False, "error": str(exc)}
            catalog = {"present": bool(info.get("present")), "parts": info.get("parts"),
                       "sample": str(info.get("source", "")).startswith("SAMPLE"), "path": info.get("path")}
            self._health = {"version": __version__, "model": self.model, "ollama": self.ollama_status(),
                            "tools": [{"name": n, "ok": checks.get(n, {}).get("ok", False),
                                       "detail": checks.get(n, {}).get("detail", "")} for n in REQUIRED_TOOLS],
                            "missing_tools": missing, "catalog": catalog, "designs_dir": str(self.designs)}
            self._health_at = time.time()
            return self._health

    def ollama_status(self) -> dict:
        out = {"host": self.ollama, "local": is_local_url(self.ollama), "reachable": False, "models": []}
        if not out["local"] and os.environ.get("PCBAGENT_ALLOW_REMOTE_MODEL") != "1":
            out["error"] = "the model endpoint is not on this machine; set PCBAGENT_ALLOW_REMOTE_MODEL=1 to allow it"
            return out
        try:
            with urllib.request.urlopen(self.ollama + "/api/tags", timeout=3) as resp:
                tags = json.load(resp)
            out["reachable"] = True
            out["models"] = [m.get("name") or m.get("model") for m in tags.get("models", [])]
        except (OSError, ValueError) as exc:
            out["error"] = f"cannot reach Ollama at {self.ollama} ({exc}); start it with `ollama serve`"
        return out

    def check_ready(self, model: str) -> None:
        h = self.health(refresh=True)
        problems = []
        if h["missing_tools"]:
            problems.append("missing tools: " + ", ".join(h["missing_tools"]) + " (run `pcbagent doctor`)")
        if not h["catalog"]["present"]:
            problems.append("no parts catalog: run `pcbagent catalog update` (or `pcbagent catalog sample` "
                            "for a demo with fake parts)")
        o = h["ollama"]
        if not o["reachable"]:
            problems.append(o.get("error") or "Ollama is not reachable")
        elif model not in o["models"] and f"{model}:latest" not in o["models"]:
            problems.append(f"the model {model} is not pulled: run `ollama pull {model}`")
        if problems:
            raise NotReady("; ".join(problems))

    # --- runs -----------------------------------------------------------------
    def _new_root(self, request: str) -> Path:
        root = self.designs / slugify(request)
        n = 2
        while root.exists():
            root = self.designs / f"{slugify(request)}_{n}"
            n += 1
        return root

    def start(self, request: str, model: str | None = None) -> str:
        request = request.strip()
        if not request:
            raise ValueError("describe the board you want")
        model = model or self.model
        self.check_ready(model)
        with self.lock:
            if self.busy:
                raise Busy(self.busy)
            project = Project.create(self._new_root(request), request)
            design_id = project.root.name
            self.busy = design_id
        try:
            self.log_for(design_id).append({"type": "user", "text": request, "model": model})
            self._launch(design_id, project, model, lambda agent: agent.run(request))
        except BaseException:
            self._release(design_id)
            raise
        return design_id

    def reply(self, design_id: str, text: str, model: str | None = None) -> None:
        text = text.strip()
        if not text:
            raise ValueError("write a message")
        root = self.design_root(design_id)
        model = model or self._last_model(design_id) or self.model
        self.check_ready(model)
        with self.lock:
            if self.busy:
                raise Busy(self.busy)
            self.busy = design_id
        try:
            self.log_for(design_id).append({"type": "user", "text": text, "model": model})
            self._launch(design_id, Project.open(root), model, lambda agent: agent.reply(text))
        except BaseException:
            self._release(design_id)
            raise

    def _last_model(self, design_id: str) -> str | None:
        for ev in reversed(self.log_for(design_id).events):
            if ev.get("type") == "user" and ev.get("model"):
                return ev["model"]
        return None

    def _release(self, design_id: str) -> None:
        with self.lock:
            if self.busy == design_id:
                self.busy = None
                self.agent = None

    def _launch(self, design_id: str, project: Project, model: str, work) -> None:
        from ..agent.loop import Agent
        log = self.log_for(design_id)

        def on_event(ev: dict) -> None:
            if ev.get("type") == "done":
                # Free the slot before the page hears "done", so its next request is not refused as busy.
                self._release(design_id)
            log.append(ev)

        try:
            agent = Agent(project, self.ollama, model, max_circuit=self.max_attempts, log=lambda *_: None,
                          on_event=on_event)
        except Exception as exc:  # noqa: BLE001
            log.append({"type": "error", "text": f"could not start the agent: {exc}"})
            on_event({"type": "done", "ok": False, "reason": "could not start"})
            raise

        def target():
            try:
                work(agent)
            except OSError as exc:
                log.append({"type": "error", "text": f"the model stopped answering: {exc}. Check that Ollama is "
                                                     f"running (`ollama serve`) and `{model}` is pulled."})
                on_event({"type": "done", "ok": False, "reason": "model unavailable"})
            except Exception as exc:  # noqa: BLE001 - report every failure in the chat instead of dying silently
                log.append({"type": "error", "text": f"{type(exc).__name__}: {exc}",
                            "detail": traceback.format_exc()[-2000:]})
                on_event({"type": "done", "ok": False, "reason": "internal error"})
            finally:
                self._release(design_id)

        thread = threading.Thread(target=target, name=f"pcbagent-{design_id}", daemon=True)
        with self.lock:
            self.agent = agent
        log.append({"type": "run_start", "model": model, "api": agent.api})
        thread.start()

    def stop(self, design_id: str) -> bool:
        with self.lock:
            if self.busy == design_id and self.agent is not None:
                self.agent.cancel.set()
                return True
        return False

    def export_fab(self, design_id: str) -> dict:
        from ..fab import export
        project = Project.open(self.design_root(design_id))
        lay = project.state().get("layout") or {}
        if not lay.get("passed"):
            raise ProjectError("no DRC-clean board yet")
        return export(project.root, Path(lay["board"]), project.path("fab"))

    def open_in_kicad(self, design_id: str) -> dict:
        from ..kicad import replay
        project = Project.open(self.design_root(design_id))
        if not (project.path("layout") / "layout_trace.json").exists():
            raise ProjectError("no layout yet")
        return replay.launch(project.path("layout"), wait=False, launch=True)


def make_handler(manager: Manager, token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = f"pcbagent/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep the terminal for the URL and errors
            pass

        # --- responses ----------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _error(self, code: int, message: str) -> None:
            self._json({"ok": False, "error": message}, code)

        # --- guards -------------------------------------------------------------
        def _host_ok(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._error(403, "this server only answers requests addressed to localhost")
                return False
            return True

        def _write_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                self._error(403, "cross-origin request refused")
                return False
            if not secrets.compare_digest(self.headers.get("X-PCBAgent-Token", ""), token):
                self._error(403, "missing or wrong request token; reload the page")
                return False
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._error(415, "send JSON")
                return False
            return True

        def _body(self) -> dict | None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self._error(413, "request too large")
                return None
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self._error(400, "invalid JSON")
                return None
            if not isinstance(data, dict):
                self._error(400, "send a JSON object")
                return None
            return data

        # --- routing ------------------------------------------------------------
        def do_GET(self):
            if not self._host_ok():
                return
            url = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
            query = urllib.parse.parse_qs(url.query)
            try:
                if not parts:
                    return self._index()
                if parts == ["api", "health"]:
                    return self._json(manager.health(refresh="refresh" in query))
                if parts == ["api", "designs"]:
                    return self._json({"designs": bundle.list_designs(manager.designs),
                                       "running": manager.running()})
                if len(parts) >= 3 and parts[:2] == ["api", "designs"]:
                    design_id = parts[2]
                    root = manager.design_root(design_id)
                    if len(parts) == 3:
                        data = bundle.design_bundle(root)
                        data["running"] = manager.running() == design_id
                        data["event_count"] = len(manager.log_for(design_id).events)
                        return self._json(data)
                    if parts[3:] == ["events"]:
                        return self._events(design_id, query)
                    if parts[3] == "files" and len(parts) > 4:
                        return self._file(root, parts[4:])
                self._error(404, "not found")
            except KeyError:
                self._error(404, "no such design")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # noqa: BLE001
                self._error(500, f"{type(exc).__name__}: {exc}")

        def do_POST(self):
            if not self._host_ok() or not self._write_ok():
                return
            body = self._body()
            if body is None:
                return
            parts = [urllib.parse.unquote(p) for p in urllib.parse.urlsplit(self.path).path.split("/") if p]
            try:
                if parts == ["api", "designs"]:
                    design_id = manager.start(str(body.get("request", "")), body.get("model") or None)
                    return self._json({"ok": True, "id": design_id}, 201)
                if len(parts) == 4 and parts[:2] == ["api", "designs"]:
                    design_id, action = parts[2], parts[3]
                    manager.design_root(design_id)
                    if action == "messages":
                        manager.reply(design_id, str(body.get("text", "")), body.get("model") or None)
                        return self._json({"ok": True}, 202)
                    if action == "stop":
                        return self._json({"ok": manager.stop(design_id)})
                    if action == "fab":
                        res = manager.export_fab(design_id)
                        return self._json({"ok": True, "files": res.get("files", []),
                                           "warnings": res.get("warnings", [])})
                    if action == "kicad":
                        return self._json({"ok": True, **manager.open_in_kicad(design_id)})
                self._error(404, "not found")
            except KeyError:
                self._error(404, "no such design")
            except Busy as exc:
                self._error(409, f"the agent is already working on {exc}; wait for it or stop it first")
            except NotReady as exc:
                self._error(503, str(exc))
            except (ValueError, ProjectError) as exc:
                self._error(400, str(exc))
            except Exception as exc:  # noqa: BLE001
                self._error(500, f"{type(exc).__name__}: {exc}")

        # --- endpoints ------------------------------------------------------------
        def _index(self):
            html = (STATIC / "index.html").read_text()
            config = json.dumps({"token": token, "model": manager.model, "version": __version__})
            html = html.replace("__PCBAGENT_CONFIG__", config.replace("</", "<\\/"))
            csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; "
                   "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self._send(200, html.encode(), "text/html; charset=utf-8",
                       {"Content-Security-Policy": csp, "X-Frame-Options": "DENY"})

        def _events(self, design_id: str, query: dict):
            log = manager.log_for(design_id)
            try:
                seq = int(self.headers.get("Last-Event-ID") or (query.get("after") or ["0"])[0])
            except ValueError:
                seq = 0
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while not getattr(self.server, "stopping", False):
                events = log.wait(seq, timeout=15)
                chunk = []
                for ev in events:
                    seq = ev["seq"]
                    chunk.append(f"id: {seq}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n")
                if not chunk:
                    chunk.append(f": keep-alive {int(time.time())}\n\n")
                try:
                    self.wfile.write("".join(chunk).encode())
                    self.wfile.flush()
                except OSError:
                    return

        def _file(self, root: Path, rel: list[str]):
            allowed = {f["path"] for f in bundle.files(root, root.name)}
            relpath = "/".join(rel)
            if relpath not in allowed:
                return self._error(404, "no such file")
            path = (root / relpath).resolve()
            if root not in path.parents:
                return self._error(404, "no such file")
            ctype = "application/zip" if path.suffix == ".zip" else "text/plain; charset=utf-8"
            self._send(200, path.read_bytes(), ctype,
                       {"Content-Disposition": f'attachment; filename="{path.name}"'})

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    stopping = False


class Server6(Server):
    address_family = socket.AF_INET6


def serve(manager: Manager, host: str = "127.0.0.1", port: int = 8765) -> Server:
    """Create the server (not yet serving). ``port=0`` picks a free port."""
    if not is_local_url(f"http://[{host}]" if ":" in host else f"http://{host}"):
        raise ValueError(f"refusing to listen on {host}: the UI runs the agent on this machine, so it only "
                         "listens on localhost")
    token = secrets.token_urlsafe(24)
    httpd = (Server6 if ":" in host else Server)((host, port), None)
    real_port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(manager, token, real_port)
    httpd.token = token
    return httpd


def main(args) -> int:
    import sys
    import webbrowser
    host = args.ollama if args.ollama.startswith("http") else f"http://{args.ollama}"
    if not is_local_url(host) and os.environ.get("PCBAGENT_ALLOW_REMOTE_MODEL") != "1":
        print(f"refusing to send designs to a non-local model endpoint ({host}). "
              "Set PCBAGENT_ALLOW_REMOTE_MODEL=1 if you really mean it.", file=sys.stderr)
        return 2
    manager = Manager(Path(args.designs), args.model, host, max_attempts=args.max_attempts)
    try:
        httpd = serve(manager, args.host, args.port)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    shown = {"127.0.0.1": "localhost", "localhost": "localhost", "::1": "[::1]"}.get(args.host, args.host)
    url = f"http://{shown}:{httpd.server_address[1]}/"
    threading.Thread(target=manager.health, daemon=True).start()
    print(f"PCBAgent is running at {url}\n  designs: {manager.designs}\n  model:   {args.model} at {host}\n"
          "Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.stopping = True
        httpd.server_close()
    return 0
