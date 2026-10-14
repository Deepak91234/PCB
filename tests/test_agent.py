"""Agent harnesses against a scripted model: the standalone loop and real Pi.

The scripted model (pi/test/fake_model_server.py) makes a wrong circuit, then
stops talking twice. The harness must push it on each time until the board
is done, which is the "runs on its own until done" property.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import needs_layout, needs_ngspice

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "pi" / "test" / "fake_model_server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(params=["clean"])
def fake_model(tmp_path, request):
    port = _free_port()
    log = tmp_path / "model.log"
    env = {**os.environ, "FAKE_MODEL_SLOPPY": "1" if request.param == "sloppy" else "0"}
    proc = subprocess.Popen([sys.executable, str(SERVER), str(port), str(log)], env=env)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", log
    proc.terminate()
    proc.wait(timeout=5)


def actions(log: Path) -> list[str]:
    return [json.loads(line)["action"][1] if json.loads(line)["action"][0] == "tool" else "<text>"
            for line in log.read_text().splitlines()]


@needs_ngspice
def test_standalone_loop_without_layout(tmp_path, fake_model):
    from pcbagent.agent.loop import Agent
    from pcbagent.project import Project
    host, log = fake_model
    p = Project.create(tmp_path / "d", "divider")
    agent = Agent(p, host, "fake-nemotron", include_layout=False, log=lambda *_: None)
    result = agent.run("5 V to 3.3 V divider for an ADC input")
    assert result["ok"] and result["reason"] == "circuit verified and parts sourced"
    assert actions(log)[:5] == ["pcb_models", "pcb_spec", "pcb_circuit", "<text>", "pcb_circuit"]
    assert p.state()["circuit_attempts"] == 2


@needs_layout
def test_standalone_loop_full_run(tmp_path, fake_model):
    from pcbagent.agent.loop import Agent
    from pcbagent.project import Project
    host, log = fake_model
    p = Project.create(tmp_path / "d", "divider")
    result = Agent(p, host, "fake-nemotron", log=lambda *_: None).run("5 V to 3.3 V divider")
    assert result["ok"], result
    assert actions(log) == ["pcb_models", "pcb_spec", "pcb_circuit", "<text>", "pcb_circuit", "<text>",
                            "pcb_layout", "<text>"]
    assert p.state()["phase"] == "done"


def test_loop_refuses_remote_model(tmp_path, monkeypatch):
    from pcbagent.cli import main
    monkeypatch.delenv("PCBAGENT_ALLOW_REMOTE_MODEL", raising=False)
    code = main(["run", "anything", "--host", "http://example.com:11434", "--dir", str(tmp_path / "x")])
    assert code == 2 and not (tmp_path / "x").exists()


PI = ROOT / "node_modules" / ".bin" / "pi"


@needs_ngspice
@pytest.mark.parametrize("fake_model", ["sloppy"], indirect=True)
def test_standalone_loop_unwraps_sloppy_arguments(tmp_path, fake_model):
    from pcbagent.agent.loop import Agent
    from pcbagent.project import Project
    host, log = fake_model
    p = Project.create(tmp_path / "d", "divider")
    result = Agent(p, host, "fake-nemotron", include_layout=False, log=lambda *_: None).run("divider")
    assert result["ok"], result
    assert p.state()["circuit_attempts"] == 2  # the stringified circuit was simulated, not rejected


@needs_layout
@pytest.mark.skipif(not PI.exists(), reason="Pi not installed (npm install)")
@pytest.mark.parametrize("fake_model", ["clean", "sloppy"], indirect=True)
def test_pi_extension_end_to_end(tmp_path, fake_model, sample_catalog):
    host, log = fake_model
    env = {**os.environ, "OLLAMA_HOST": host, "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
           "PI_SKIP_VERSION_CHECK": "1", "PI_CODING_AGENT_DIR": str(tmp_path / "agent"),
           "PCBAGENT_CATALOG": str(sample_catalog), "PCBAGENT_DESIGNS_DIR": str(tmp_path / "designs"),
           "PCBAGENT_PYTHON": sys.executable}
    proc = subprocess.run(
        [str(PI), "-p", "--no-session", "--no-extensions", "--no-skills", "--no-context-files",
         "-e", str(ROOT / "pi" / "extensions" / "pcbagent.ts"), "--model", "ollama/fake-nemotron",
         "/design 5 V to 3.3 V divider for an ADC input"],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert actions(log) == ["pcb_models", "pcb_spec", "pcb_circuit", "<text>", "pcb_circuit", "<text>",
                            "pcb_layout", "<text>"]
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    # During a run the model only sees the PCB tools: no shell, no file edits.
    assert set(requests[0]["tools"]) == {"pcb_models", "pcb_parts_search", "pcb_spec", "pcb_circuit",
                                         "pcb_layout", "pcb_status"}
    design = next((tmp_path / "designs").iterdir())
    state = json.loads((design / "pcbagent.json").read_text())
    assert state["phase"] == "done" and state["layout"]["passed"]
