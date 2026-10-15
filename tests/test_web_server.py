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
    assert resp.split(b"\r\n")[0].split()[1] == b"403"
    ok = raw_request(server, f"GET /api/designs HTTP/1.1\r\nHost: localhost:{server['port']}\r\n"
                             f"Connection: close\r\n\r\n".encode())
    assert ok.split(b"\r\n")[0].split()[1] == b"200"


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
    for host in ("0.0.0.0", "192.168.1.10", "", " ", "::"):
        with pytest.raises(ValueError):
            serve(manager, host, 0)
    httpd = serve(manager, "localhost", 0)   # resolves only to loopback addresses
    httpd.server_close()


def test_not_ready_explains_what_to_install(server):
    status, data, _ = call(server, "POST", "/api/designs", {"request": "a divider", "model": "not-pulled"})
    assert status == 503 and "ollama pull not-pulled" in data["error"]


# --- the agent loop pieces the UI relies on -------------------------------------------

def _scripted(project, script, **kw):
    from pcbagent.agent.loop import Agent

    class Scripted(Agent):
        def detect_api(self):
            return "openai"

        def chat(self):
            self.calls += 1
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    events = []
    agent = Scripted.__new__(Scripted)
    agent.script, agent.calls = list(script), 0
    Agent.__init__(agent, project, "http://127.0.0.1:9", "scripted", log=lambda *_: None,
                   on_event=events.append, **kw)
    return agent, events


def _call(name, args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _text(t):
    return {"role": "assistant", "content": t}


def _project_in(tmp_path, phase):
    from pcbagent.project import Project
    p = Project.create(tmp_path / "d", "a divider")
    st = p.state()
    st.update(phase=phase, circuit_attempts=1, layout_attempts=1 if phase == "done" else 0,
              layout={"passed": True} if phase == "done" else None)
    p.save_state(st)
    return p


def test_follow_up_runs_every_tool_call_before_ending(tmp_path):
    p = _project_in(tmp_path, "done")
    agent, events = _scripted(p, [_call("pcb_status", {}), _call("pcb_status", {}), _text("All good.")])
    result = agent.reply("Is everything fine?")
    assert result["ok"] and agent.calls == 3
    assert [e["name"] for e in events if e["type"] == "tool_result"] == ["pcb_status", "pcb_status"]


def test_question_on_unfinished_design_is_answered_without_a_nudge(tmp_path):
    p = _project_in(tmp_path, "circuit")
    agent, events = _scripted(p, [_text("The output is 2.5 V because the divider ratio is 1:1.")])
    result = agent.reply("Why did it fail?")
    assert result == {"ok": True, "reason": "replied", "turns": 1}
    assert not [e for e in events if e["type"] == "nudge"]


def test_tool_errors_go_back_to_the_model(tmp_path, monkeypatch):
    p = _project_in(tmp_path, "done")
    agent, events = _scripted(p, [_call("pcb_parts_search", {"kind": ["resistor"], "limit": "all"}),
                                  _call("pcb_models", {}), _text("Sorry, fixed.")])
    real = agent._call_tool
    monkeypatch.setattr(agent, "_call_tool",
                        lambda n, a: (_ for _ in ()).throw(RuntimeError("boom")) if n == "pcb_models" else real(n, a))
    result = agent.reply("find me a resistor")
    assert result["ok"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert results[1]["ok"] is False and "RuntimeError: boom" in results[1]["text"]
    msgs = json.loads((p.root / "agent" / "messages.json").read_text())["messages"]
    for i, m in enumerate(msgs):   # every tool call is answered, so the saved conversation stays valid
        if m.get("tool_calls"):
            assert msgs[i + 1]["role"] == "tool"


def test_missing_closing_summary_does_not_fail_a_finished_design(tmp_path, monkeypatch):
    from pcbagent.agent.loop import ModelError
    p = _project_in(tmp_path, "done")
    agent, events = _scripted(p, [_call("pcb_layout", {"constraints": []}), ModelError("connection refused")])
    monkeypatch.setattr(agent, "_call_tool", lambda n, a: ("PASS: board routed", True, {}))  # a re-layout that passes
    result = agent.reply("move J1 a bit")
    assert result["ok"] and events[-1]["type"] == "done" and events[-1]["ok"]


def test_rejected_change_is_reported_as_not_made(tmp_path):
    p = _project_in(tmp_path, "done")
    agent, events = _scripted(p, [_call("pcb_spec", {}), _text("Done!"), _text("Done!"), _text("Done!")])
    result = agent.reply("make the output 5 V")
    assert result["ok"] is False and "rejected" in result["reason"]
    assert len([e for e in events if e["type"] == "nudge"]) == 2
    assert p.state()["phase"] == "done"


def test_event_log_survives_damaged_lines_and_marks_interrupted_runs(tmp_path):
    from pcbagent.project import Project
    from pcbagent.web.server import EventLog, Manager
    p = Project.create(tmp_path / "designs" / "d", "x")
    path = p.root / "agent" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"seq": 1, "type": "user", "text": "hi"}\n{"seq": 2, "type": "run_start"}\n'
                     b'{"seq": 3, "type": "thinking"\n{"seq": 4, "type": "assistant", "text": "\xce')
    log = EventLog(path)
    assert [e["seq"] for e in log.events] == [1, 2]
    assert log.interrupted()
    manager = Manager(tmp_path / "designs", "m", "http://127.0.0.1:9")
    log = manager.log_for("d")   # a fresh server closes the run the old one never finished
    assert [e["type"] for e in log.events[-2:]] == ["error", "done"] and log.events[-1]["seq"] == 4
    assert [e["seq"] for e in log.after(2)] == [3, 4]
    assert EventLog(path).last_seq == 4   # the cut-off line was terminated before appending



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

    # A rejected re-layout (bad constraints) and an unchanged circuit both keep the finished board.
    from conftest import load_example
    from pcbagent.layout import pipeline
    from pcbagent.layout.constraints import ConstraintError
    from pcbagent.project import Project
    p = Project.open(server["manager"].designs / design_id)
    with pytest.raises(ConstraintError):
        pipeline.run(p, {"constraints": [{"type": "near", "ref": "NOPE", "to": "R1"}]})
    assert p.state()["phase"] == "done" and p.current_layout()
    assert p.submit_circuit(load_example("divider", "circuit"))["passed"]
    assert p.state()["phase"] == "done" and p.current_layout()
    assert call(server, "GET", f"/api/designs/{design_id}", token=False)[1]["board"]

    # A new circuit makes the board stale: it is no longer shown or exported as current.
    changed = load_example("divider", "circuit")
    for c in changed["components"]:
        if c["ref"] == "R2":
            c["value"] = "20k"
    assert p.submit_circuit(changed)["verification_passed"]
    _, b2, _ = call(server, "GET", f"/api/designs/{design_id}", token=False)
    assert b2["board"] is None and b2["layout"] is None and b2["layout_stale"] and not b2["passed"]
    assert call(server, "POST", f"/api/designs/{design_id}/fab", {})[0] == 400

    # The chat history is on disk, so a restarted server shows it again.
    from pcbagent.web.server import Manager
    again = Manager(server["manager"].designs, "fake-nemotron", "http://127.0.0.1:1")
    assert [e["seq"] for e in again.log_for(design_id).events] == [e["seq"] for e in events]


def test_relayout_forgets_the_old_board_first(tmp_path):
    from pcbagent.web import bundle
    p = _project_in(tmp_path, "done")
    (p.root / "layout").mkdir()
    (p.root / "layout" / "result.json").write_text(json.dumps({"passed": True, "drc": {"errors": 0}}))
    assert bundle.summary(p.root)["passed"]
    p.begin_layout()   # what pipeline.run does before overwriting the board files
    st = p.state()
    assert st["layout"] is None and st["phase"] == "layout" and p.current_layout() is None
    b = bundle.design_bundle(p.root)
    assert b["layout"] is None and b["board"] is None and b["layout_stale"] and not b["passed"]



def test_follow_up_that_only_looks_things_up_is_cut_short(tmp_path):
    from pcbagent.agent import loop
    p = _project_in(tmp_path, "done")
    agent, events = _scripted(p, [_call("pcb_status", {})] * 20)
    result = agent.reply("Which resistors are in stock?")
    assert not result["ok"] and "without making progress" in result["reason"]
    assert agent.calls == loop.STALL_STOP
    assert len([e for e in events if e["type"] == "nudge"]) == 1


def test_continue_resumes_an_unfinished_design_but_a_question_does_not(tmp_path):
    from pcbagent.web.server import RESUME
    assert RESUME.match("Please continue.") and RESUME.match("try again") and RESUME.match("Finish the board")
    assert not RESUME.match("Why did it stop?") and not RESUME.match("continuity of GND?")
    p = _project_in(tmp_path, "circuit")
    agent, events = _scripted(p, [_text("I will think about it.")] * 5)
    result = agent.reply("Continue the design.", resume=True)
    assert result["reason"] == "the model stopped calling tools"   # it was pushed on, not just answered
    assert len([e for e in events if e["type"] == "nudge"]) == 3


def test_damaged_design_folders_do_not_break_the_list(tmp_path):
    from pcbagent.project import Project
    from pcbagent.web import bundle
    good = Project.create(tmp_path / "good", "a regulator")
    odd = Project.create(tmp_path / "odd", "x")
    st = odd.state()
    st["layout"] = {"passed": True, "board_mm": [None, "<img src=x onerror=alert(1)>"]}
    odd.save_state(st)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "pcbagent.json").write_text("[1, 2")
    designs = {d["id"]: d for d in bundle.list_designs(tmp_path)}
    assert set(designs) == {"good", "odd", "broken"} and designs["odd"]["board_mm"] is None
    assert designs["good"]["title"] == "a regulator"
    assert bundle.design_bundle(odd.root)["id"] == "odd" and bundle.design_bundle(good.root)["phase"] == "spec"


def test_slow_clients_are_dropped_after_the_header_deadline(server):
    import pcbagent.web.server as srv
    assert srv.HEADER_DEADLINE <= 15
    s = socket.create_connection(("127.0.0.1", server["port"]), timeout=30)
    s.sendall(b"GET / HT")      # never finishes the request line
    start = time.time()
    s.settimeout(srv.HEADER_DEADLINE + 10)
    data = s.recv(100)          # the server hangs up instead of waiting forever
    assert data == b"" or data.startswith(b"HTTP/1.0 4")
    assert time.time() - start < srv.HEADER_DEADLINE + 5
    s.close()


def test_deleted_or_damaged_designs_are_left_alone(tmp_path):
    import shutil
    from pcbagent.project import Project
    from pcbagent.web.server import EventLog, Manager
    gone = Project.create(tmp_path / "designs" / "gone", "x")
    log = EventLog(gone.root / "agent" / "events.jsonl")
    shutil.rmtree(gone.root)
    log.append({"type": "assistant", "text": "still talking"})   # must not re-create the folder
    assert not gone.root.exists()
    bad = Project.create(tmp_path / "designs" / "bad", "x")
    (bad.root / "pcbagent.json").write_text("[1, 2")
    manager = Manager(tmp_path / "designs", "m", "http://127.0.0.1:9")
    assert manager.log_for("bad").events == []


def test_a_page_ahead_of_the_server_is_told_to_reload(server):
    from pcbagent.project import Project
    Project.create(server["manager"].designs / "d", "x")
    with urllib.request.urlopen(f"{server['base']}/api/designs/d/events?after=99", timeout=10) as resp:
        assert json.loads(resp.readline().decode()[6:]) == {"type": "resync"}
