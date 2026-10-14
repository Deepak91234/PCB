"""A scripted, OpenAI-compatible "model" for end-to-end tests of the Pi extension.

It speaks just enough of Ollama's API (``/api/tags`` and the OpenAI-style
``/v1/chat/completions`` with streaming tool calls) for Pi to drive it. The
script exercises the parts of the harness that matter:

1. pcb_models, then pcb_spec (the divider example),
2. a wrong circuit (10k/10k gives 2.5 V, so verification fails),
3. stops with plain text, so the extension's auto-continue must nudge it,
4. the corrected circuit (passes, parts sourced),
5. stops again, so auto-continue must push it into layout,
6. pcb_layout, then a final message.

Usage: python3 fake_model_server.py PORT LOGFILE
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "examples" / "divider" / "spec.json").read_text())
GOOD = json.loads((ROOT / "examples" / "divider" / "circuit.json").read_text())
BAD = json.loads(json.dumps(GOOD))
for c in BAD["components"]:
    if c["ref"] == "R2":
        c["value"] = "10k"
CONSTRAINTS = json.loads((ROOT / "examples" / "divider" / "constraints.json").read_text())
LOG = None
# FAKE_MODEL_SLOPPY=1: send arguments the way small models often do (wrapped object, JSON string).
SLOPPY = __import__("os").environ.get("FAKE_MODEL_SLOPPY") == "1"
# FAKE_MODEL_NATIVE=1: also serve Ollama's native /api/version and /api/chat.
NATIVE = __import__("os").environ.get("FAKE_MODEL_NATIVE") == "1"


def decide(messages):
    calls = []
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                calls.append(tc["function"]["name"])
    last = messages[-1] if messages else {}
    nudged = last.get("role") == "user" and "not finished" in json.dumps(last.get("content"))
    n_circuit = calls.count("pcb_circuit")
    if "pcb_models" not in calls:
        return ("tool", "pcb_models", {})
    if "pcb_spec" not in calls:
        return ("tool", "pcb_spec", {"spec": SPEC} if SLOPPY else SPEC)
    if n_circuit == 0:
        return ("tool", "pcb_circuit", {"circuit": json.dumps(BAD)} if SLOPPY else BAD)
    if n_circuit == 1:
        if nudged:
            return ("tool", "pcb_circuit", GOOD)
        return ("text", "The output is too low. I will think about it.", None)
    if "pcb_layout" not in calls:
        if nudged:
            return ("tool", "pcb_layout", CONSTRAINTS)
        return ("text", "The circuit passed verification and parts are sourced.", None)
    return ("text", "Done: the board is routed and DRC clean.", None)


def chunk(delta, finish=None):
    return {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": "fake-nemotron", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._json({"models": [{"name": "fake-nemotron", "model": "fake-nemotron"}]})
        elif self.path.startswith("/api/version") and NATIVE:
            self._json({"version": "0.0.0-fake"})
        elif self.path.startswith("/v1/models"):
            self._json({"object": "list", "data": [{"id": "fake-nemotron", "object": "model"}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            self._post()
        except Exception:  # noqa: BLE001 - log for debugging the test
            import traceback
            with open(LOG + ".err", "a") as fh:
                fh.write(traceback.format_exc())
            raise

    def _post(self):
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        messages = req.get("messages", [])
        kind, a, b = decide(messages)
        tools = [t["function"]["name"] for t in req.get("tools", [])]
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"n_messages": len(messages), "tools": tools, "action": [kind, a],
                                 "path": self.path, "options": req.get("options")}) + "\n")
        if self.path.startswith("/api/chat"):
            msg = {"role": "assistant", "content": a if kind == "text" else ""}
            if kind == "tool":
                msg["tool_calls"] = [{"function": {"name": a, "arguments": b}}]
            self._json({"model": "fake-nemotron", "message": msg, "done": True})
            return
        if not req.get("stream", False):
            msg = {"role": "assistant", "content": a if kind == "text" else None}
            if kind == "tool":
                msg["tool_calls"] = [{"id": f"call_{len(messages)}", "type": "function",
                                      "function": {"name": a, "arguments": json.dumps(b)}}]
            self._json({"id": "chatcmpl-fake", "object": "chat.completion", "model": "fake-nemotron",
                        "choices": [{"index": 0, "message": msg,
                                     "finish_reason": "tool_calls" if kind == "tool" else "stop"}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def send(obj):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()

        if kind == "tool":
            args = json.dumps(b)
            send(chunk({"role": "assistant", "content": None, "tool_calls": [
                {"index": 0, "id": f"call_{len(messages)}", "type": "function",
                 "function": {"name": a, "arguments": ""}}]}))
            for i in range(0, len(args), 400):
                send(chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[i:i + 400]}}]}))
            send(chunk({}, "tool_calls"))
        else:
            send(chunk({"role": "assistant", "content": a}))
            send(chunk({}, "stop"))
        usage = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": "fake-nemotron", "choices": [],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
        send(usage)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    port = int(sys.argv[1])
    LOG = sys.argv[2]
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
