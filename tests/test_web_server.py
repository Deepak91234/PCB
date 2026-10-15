"""The local web UI (`pcbagent serve`): request guards, the design API and a full chat run.

The full run uses the scripted model from pi/test/fake_model_server.py, which
always designs the divider: a wrong circuit first, then the right one, then
the layout.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import needs_layout

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "pi" / "test" / "fake_model_server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_ollama(tmp_path):
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(FAKE), str(port), str(tmp_path / "model.log")],
                            env={**os.environ, "FAKE_MODEL_NATIVE": "1"})
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def server(tmp_path, fake_ollama):
    from pcbagent.web.server import Manager, serve
    manager = Manager(tmp_path / "designs", "fake-nemotron", fake_ollama)
    httpd = serve(manager, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    yield {"base": f"http://127.0.0.1:{port}", "port": port, "token": httpd.token, "manager": manager}
    httpd.stopping = True
    httpd.shutdown()
    httpd.server_close()


def call(srv, method, path, body=None, token=True, headers=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(srv["base"] + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-PCBAgent-Token", srv["token"])
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(raw) if "json" in ctype else raw), resp.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw), exc.headers
        except ValueError:
            return exc.code, raw, exc.headers


def raw_request(srv, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", srv["port"]), timeout=10) as s:
        s.sendall(request)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
            if b"\r\n\r\n" in out:
                head, _, rest = out.partition(b"\r\n\r\n")
                n = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                          if line.lower().startswith(b"content-length:")), None)
                if n is not None and len(rest) >= n:
                    break
        return out


# --- guards ------------------------------------------------------------------------

def test_index_is_self_contained_and_carries_the_token(server):
    status, body, headers = call(server, "GET", "/", token=False)
    html = body.decode()
    assert status == 200 and server["token"] in html
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    # Nothing on the page may come from the network: no external scripts, styles or fonts.
    assert "https://" not in html and "http://" not in html.replace("http://www.w3.org/2000/svg", "")


def test_requests_for_other_hosts_are_refused(server):
    # A DNS-rebinding page would reach us with its own host name in the Host header.
    resp = raw_request(server, f"GET /api/designs HTTP/1.1\r\nHost: evil.example:{server['port']}\r\n"
                               f"Connection: close\r\n\r\n".encode())
    assert resp.startswith(b"HTTP/1.1 403")
    ok = raw_request(server, f"GET /api/designs HTTP/1.1\r\nHost: localhost:{server['port']}\r\n"
                             f"Connection: close\r\n\r\n".encode())
    assert ok.startswith(b"HTTP/1.1 200")


def test_writes_need_the_token_same_origin_and_json(server):
    body = {"request": "anything"}
    assert call(server, "POST", "/api/designs", body, token=False)[0] == 403
    status, data, _ = call(server, "POST", "/api/designs", body, headers={"Origin": "https://evil.example"})
    assert status == 403 and "cross-origin" in data["error"]
    req = urllib.request.Request(server["base"] + "/api/designs", data=b"request=x", method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("X-PCBAgent-Token", server["token"])
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 415
    assert call(server, "POST", "/api/designs", b"x" * 70000)[0] == 413
    assert not list((server["manager"].designs).iterdir())


def test_paths_cannot_escape_the_designs_folder(server, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve")
    for path in ("/api/designs/..", "/api/designs/%2e%2e", "/api/designs/..%2fsecret.txt", "/api/designs/.hidden",
                 "/api/designs/nope/files/..%2f..%2fsecret.txt"):
        status, _, _ = call(server, "GET", path, token=False)
        assert status == 404, path


def test_refuses_to_listen_beyond_localhost(tmp_path):
    from pcbagent.web.server import Manager, serve
    manager = Manager(tmp_path / "d", "m", "http://127.0.0.1:1")
    for host in ("0.0.0.0", "192.168.1.10"):
        with pytest.raises(ValueError):
            serve(manager, host, 0)


def test_not_ready_explains_what_to_install(server):
    status, data, _ = call(server, "POST", "/api/designs", {"request": "a divider", "model": "not-pulled"})
    assert status == 503 and "ollama pull not-pulled" in data["error"]


# --- the agent loop pieces the UI relies on -------------------------------------------

def test_stopped_run_ends_with_a_done_event(tmp_path, fake_ollama):
    from pcbagent.agent.loop import Agent
    from pcbagent.project import Project
    events = []
    p = Project.create(tmp_path / "d", "divider")
    agent = Agent(p, fake_ollama, "fake-nemotron", log=lambda *_: None, on_event=events.append)
    agent.cancel.set()
    result = agent.run("divider")
    assert result == {"ok": False, "reason": "stopped by the user", "turns": 0}
    assert events[-1]["type"] == "done"
    saved = json.loads((p.root / "agent" / "messages.json").read_text())
    assert saved["messages"][0]["role"] == "system"


# --- a full chat ---------------------------------------------------------------------

def wait_for(server, design_id, n_done, timeout=300):
    log = server["manager"].log_for(design_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        done = [e for e in log.events if e["type"] == "done"]
        if len(done) >= n_done and server["manager"].running() is None:
            return log.events
        time.sleep(0.25)
    raise AssertionError(f"no done event: {log.events[-5:]}")


def read_sse(server, design_id, after, until_seq, timeout=20):
    req = urllib.request.Request(f"{server['base']}/api/designs/{design_id}/events?after={after}")
    seen = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        assert resp.headers["Content-Type"] == "text/event-stream"
        for line in resp:
            line = line.decode().strip()
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                seen.append(ev)
                if ev["seq"] >= until_seq:
                    break
    return seen


@needs_layout
def test_chat_designs_a_board_end_to_end(server):
    status, data, _ = call(server, "POST", "/api/designs", {"request": "5 V to 3.3 V divider for an ADC input"})
    assert status == 201
    design_id = data["id"]
    # One run at a time: a second request while the first runs is refused, not queued silently.
    assert call(server, "POST", "/api/designs", {"request": "another"})[0] == 409
    events = wait_for(server, design_id, 1)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "user" and kinds[1] == "run_start" and kinds[-1] == "done" and events[-1]["ok"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert [r["name"] for r in results] == ["pcb_models", "pcb_spec", "pcb_circuit", "pcb_circuit", "pcb_layout"]
    assert results[2]["ok"] is False and results[2]["facts"]["failed"]          # the wrong circuit is caught
    assert results[3]["ok"] and results[3]["facts"]["cost"]["parts_usd"] > 0   # the fix passes and is sourced
    assert results[4]["ok"] and results[4]["facts"]["drc"]["errors"] == 0
    assert any(e["type"] == "progress" and "Freerouting" in e["text"] for e in events)
    assert "nudge" in kinds  # the scripted model stops twice; the loop pushes it on

    # The page can replay the whole conversation from the event stream.
    streamed = read_sse(server, design_id, 0, events[-1]["seq"])
    assert [e["seq"] for e in streamed] == list(range(1, events[-1]["seq"] + 1))

    status, b, _ = call(server, "GET", f"/api/designs/{design_id}", token=False)
    assert status == 200 and b["phase"] == "done" and not b["running"]
    assert b["verification"]["passed"] and len(b["verification"]["requirements"]) == 2
    assert b["parts"]["sample"] and b["parts"]["bom"]
    assert b["layout"]["passed"] and b["layout"]["drc"]["errors"] == 0
    assert {f["ref"] for f in b["board"]["footprints"]} == {"J1", "R1", "R2"} and b["board"]["tracks"]
    assert len(b["frames"]) > 10 and b["route_order"]
    assert b["schematic"] and b["schematic"].lstrip().startswith("<svg") and "<script" not in b["schematic"]
    files = {f["path"] for f in b["files"]}
    assert f"layout/{design_id}.kicad_pcb" in files

    status, body, headers = call(server, "GET", f"/api/designs/{design_id}/files/layout/{design_id}.kicad_pcb",
                                 token=False)
    assert status == 200 and body.startswith(b"(kicad_pcb") and "attachment" in headers["Content-Disposition"]
    assert call(server, "GET", f"/api/designs/{design_id}/files/pcbagent.json", token=False)[0] == 404

    # A follow-up continues the same conversation with the saved transcript.
    status, _, _ = call(server, "POST", f"/api/designs/{design_id}/messages", {"text": "How much do the parts cost?"})
    assert status == 202
    events = wait_for(server, design_id, 2)
    tail = events[[i for i, e in enumerate(events) if e["type"] == "user"][-1]:]
    assert tail[0]["text"] == "How much do the parts cost?" and tail[-1]["type"] == "done" and tail[-1]["ok"]
    transcript = json.loads((server["manager"].designs / design_id / "agent" / "messages.json").read_text())
    assert any("How much do the parts cost?" in m.get("content", "") for m in transcript["messages"])

    # Gerbers from the browser.
    status, data, _ = call(server, "POST", f"/api/designs/{design_id}/fab", {})
    assert status == 200 and any(f.endswith(".zip") for f in data["files"])

    # The chat history is on disk, so a restarted server shows it again.
    from pcbagent.web.server import Manager
    again = Manager(server["manager"].designs, "fake-nemotron", "http://127.0.0.1:1")
    assert [e["seq"] for e in again.log_for(design_id).events] == [e["seq"] for e in events]
