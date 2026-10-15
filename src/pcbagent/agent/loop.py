"""Standalone agent loop: ``pcbagent run "<request>"`` against a local model.

Pi is the primary front-end; this loop exists for machines without Node and
for headless runs. It uses the same tools, rules and auto-continue policy:
the model only ever emits JSON tool calls, the Python runners decide what is
true, and the loop keeps nudging the model until the board passes DRC or the
attempt budget is spent.

It speaks the OpenAI-compatible chat API that Ollama serves at
``/v1/chat/completions`` and refuses non-local endpoints unless
``PCBAGENT_ALLOW_REMOTE_MODEL=1``, because a remote endpoint would ship the
design off the machine.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import models as models_mod
from .. import schemas
from ..doctor import is_local_url
from . import prompts

TOOLS = [
    {"name": "pcb_models", "description": "List the part models a circuit may use (ids, pins, ratings, rules).",
     "parameters": {"type": "object", "properties": {"kind": {"type": "string"}}}},
    {"name": "pcb_parts_search", "description": "Search the local JLCPCB catalog for stocked values/packages.",
     "parameters": {"type": "object", "properties": {
         "kind": {"type": "string"}, "package": {"type": "string"}, "value": {"type": "string"},
         "query": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "pcb_spec", "description": "Write and lock the specification (once).",
     "parameters": schemas.tool_schema("spec")},
    {"name": "pcb_circuit", "description": "Submit the full circuit: validate, simulate across tolerance corners, "
                                           "check ratings, source parts.",
     "parameters": schemas.tool_schema("circuit")},
    {"name": "pcb_layout", "description": "Place and route from placement constraints; KiCad DRC scores it.",
     "parameters": schemas.tool_schema("constraints")},
    {"name": "pcb_status", "description": "Phase, last failures and next step.",
     "parameters": {"type": "object", "properties": {}}},
]


class Agent:
    """One design conversation with a local model.

    ``run(request)`` starts it; ``reply(text)`` continues it with a follow-up
    from the user (the web UI's chat). ``on_event`` receives structured events
    for a live view; ``log`` gets the same story as plain text for the
    terminal. The model messages are saved to ``agent/messages.json`` in the
    design folder after every turn, so a conversation survives a restart.
    """

    def __init__(self, project, host: str, model: str, include_layout: bool = True,
                 max_circuit: int = 12, max_layout: int = 6, log=print, on_event=None):
        self.project = project
        self.host = host.rstrip("/")
        self.model = model
        self.include_layout = include_layout
        self.max_circuit = max_circuit
        self.max_layout = max_layout
        self.log = log
        self.on_event = on_event
        self.cancel = threading.Event()
        self.messages: list[dict] = []
        self.num_ctx = int(os.environ.get("PCBAGENT_NUM_CTX", "32768"))
        self._base = (0, 0)   # circuit and layout attempts when the current turn started
        self.api = self.detect_api()

    def emit(self, kind: str, **data) -> None:
        if self.on_event:
            try:
                self.on_event({"type": kind, **data})
            except Exception:  # noqa: BLE001 - a broken viewer must never stop the design run
                pass

    # --- transcript -----------------------------------------------------------
    @property
    def transcript_path(self) -> Path:
        return self.project.path("agent", "messages.json")

    def save_transcript(self) -> None:
        path = self.transcript_path
        path.parent.mkdir(exist_ok=True)   # not parents: a design folder deleted mid-run stays deleted
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"model": self.model, "messages": self.messages}, ensure_ascii=False))
        tmp.replace(path)

    def load_transcript(self) -> bool:
        try:
            data = json.loads(self.transcript_path.read_text())
        except (OSError, ValueError):
            return False
        self.messages = list(data.get("messages") or [])
        return bool(self.messages)

    # --- model --------------------------------------------------------------
    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(f"{self.host}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"})
        try:
            with urllib.request.urlopen(req, timeout=float(os.environ.get("PCBAGENT_MODEL_TIMEOUT", "900"))) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # Ollama explains itself in the body ("model 'x' not found, try pulling it first").
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ModelError(f"model endpoint returned HTTP {exc.code}: {detail}") from exc
        except (OSError, ValueError) as exc:   # refused, reset, timed out, bad URL
            raise ModelError(f"cannot reach the model endpoint {self.host}: {exc}") from exc
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ModelError(f"the model endpoint sent something that is not JSON: {raw[:120]!r}") from exc

    def detect_api(self) -> str:
        """Ollama's native API lets us set the context window per request; prefer it when present.

        Ollama's default context is small and it truncates silently, which would cut the
        instructions and tool schemas out of a long design session.
        """
        forced = os.environ.get("PCBAGENT_MODEL_API")
        if forced in ("ollama", "openai"):
            return forced
        try:
            with urllib.request.urlopen(f"{self.host}/api/version", timeout=3) as resp:
                if "version" in json.load(resp):
                    return "ollama"
        except (OSError, ValueError):
            pass
        return "openai"

    def chat(self) -> dict:
        try:
            return self._chat()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ModelError(f"the model endpoint sent a reply PCBAgent cannot read ({type(exc).__name__}: {exc})") \
                from exc

    def _chat(self) -> dict:
        tools = [{"type": "function", "function": t} for t in TOOLS]
        if self.api == "ollama":
            msgs = []
            for m in self.messages:
                m = dict(m)
                if m.get("tool_calls"):
                    m["tool_calls"] = [{"function": {"name": c["function"]["name"],
                                                     "arguments": _as_obj(c["function"].get("arguments"))}}
                                       for c in m["tool_calls"]]
                if m["role"] == "tool":
                    m = {"role": "tool", "content": m["content"], "tool_name": m.get("name")}
                msgs.append(m)
            data = self._post("/api/chat", {"model": self.model, "messages": msgs, "tools": tools, "stream": False,
                                            "options": {"num_ctx": self.num_ctx, "temperature": 0.2}})
            if "message" not in data and data.get("error"):
                raise ModelError(f"the model endpoint reported an error: {str(data['error'])[:300]}")
            msg = data["message"]
            calls = []
            for i, c in enumerate(msg.get("tool_calls") or []):
                # A malformed call becomes a call the loop rejects back to the model, not a crash.
                fn = c.get("function") if isinstance(c, dict) and isinstance(c.get("function"), dict) else {}
                args = fn.get("arguments")
                calls.append({"id": f"call_{len(self.messages)}_{i}", "type": "function",
                              "function": {"name": str(fn.get("name") or ""),
                                           "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
            return {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        data = self._post("/v1/chat/completions", {"model": self.model, "messages": self.messages, "stream": False,
                                                   "temperature": 0.2, "tools": tools})
        if "choices" not in data and data.get("error"):
            raise ModelError(f"the model endpoint reported an error: {str(data['error'])[:300]}")
        return data["choices"][0]["message"]

    # --- tools --------------------------------------------------------------
    def call_tool(self, name: str, args: dict) -> str:
        return self._call_tool(name, args)[0]

    def _call_tool(self, name: str, args: dict) -> tuple[str, bool, dict]:
        """Run one tool. Returns (text for the model, whether it succeeded, facts for a live view)."""
        from ..catalog.db import CatalogMissing
        from ..circuit import CircuitError
        from ..layout import pipeline
        from ..layout.constraints import ConstraintError
        from ..project import ProjectError
        from ..spec import SpecError
        from ..cli import unwrap
        p = self.project
        key = {"pcb_spec": "spec", "pcb_circuit": "circuit", "pcb_layout": "constraints"}.get(name)
        if key:
            args = unwrap(args, key)
        try:
            if name == "pcb_models":
                kind = args.get("kind")
                rows = [json.dumps(models_mod.summary(m)) for m in models_mod.all_models().values()
                        if not kind or m["kind"] == kind]
                return "\n".join(rows), True, {"count": len(rows)}
            if name == "pcb_parts_search":
                text = _search(args)
                return text, not text.startswith("no matching"), {}
            if name == "pcb_spec":
                spec = p.write_spec(args)
                return (f"spec locked ({len(spec['requirements'])} requirements). "
                        "Next: call pcb_circuit with the full circuit."), True, \
                    {"title": spec.get("title"), "requirements": len(spec["requirements"])}
            if name == "pcb_circuit":
                res = p.submit_circuit(args)
                v = res.get("verification") or {}
                parts = res.get("parts") or {}
                facts = {"attempt": res.get("attempt"), "version": res.get("version"), "stage": res.get("stage"),
                         "verification_passed": res.get("verification_passed"),
                         "failed": [r["message"] for r in v.get("requirements", []) if not r["passed"]]
                         + v.get("rating_failures", []) + (res.get("errors") or [])[:8],
                         "parts_passed": parts.get("passed"), "cost": parts.get("cost"),
                         "sample_catalog": parts.get("sample_catalog")}
                return res["summary"] + f"\nNext: {p.next_step()}", bool(res.get("passed")), facts
            if name == "pcb_layout":
                res = pipeline.run(p, args, progress=lambda text: self.emit("progress", text=text))
                facts = {k: res.get(k) for k in ("passed", "board_mm", "drc", "tracks", "vias", "message")}
                facts["unmet"] = [c for c in res.get("constraints", []) if not c.get("satisfied")]
                return pipeline.summarize(res), bool(res.get("passed")), facts
            if name == "pcb_status":
                return json.dumps(p.status(), indent=1, default=str), True, {}
            return f"unknown tool {name}", False, {}
        except (SpecError, CircuitError, ConstraintError) as exc:
            return "Rejected:\n" + "\n".join(f"- {e}" for e in exc.errors), False, {"errors": exc.errors[:8]}
        except (ProjectError, CatalogMissing) as exc:
            return f"Error: {exc}", False, {"errors": [str(exc)]}

    # --- loop -----------------------------------------------------------------
    def done(self) -> tuple[bool, str]:
        st = self.project.status()
        circuit_tries = st["circuit_attempts"] - self._base[0]
        layout_tries = st["layout_attempts"] - self._base[1]
        if st["phase"] == "done":
            return True, "board routed, DRC clean"
        if st["phase"] == "layout" and not self.include_layout:
            return True, "circuit verified and parts sourced"
        if st["phase"] != "layout" and circuit_tries >= self.max_circuit:
            return True, f"stopped after {circuit_tries} circuit attempts"
        if st["phase"] == "layout" and layout_tries >= self.max_layout:
            return True, f"stopped after {layout_tries} layout attempts"
        return False, ""

    def run(self, request: str, max_turns: int = 80) -> dict:
        self.messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompts.kickoff(request, str(self.project.root), self.include_layout)},
        ]
        self.save_transcript()
        return self._loop(max_turns)

    def reply(self, text: str, max_turns: int = 80, resume: bool = False) -> dict:
        """Continue the conversation with a follow-up from the user.

        A follow-up is a question or a change request: if the model answers without changing the
        design, the turn ends. ``resume=True`` ("continue", or the page's Continue button) instead
        keeps pushing the model until the design is finished, like the first run.
        """
        if not self.messages and not self.load_transcript():
            # A design started elsewhere (Pi, or before transcripts existed): give the model the context afresh.
            request = self.project.state().get("request") or text
            self.messages = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompts.kickoff(request, str(self.project.root), self.include_layout)},
            ]
        self.messages.append({"role": "user", "content": prompts.follow_up(text, self.project.status())})
        self.save_transcript()
        return self._loop(max_turns, follow_up=not resume)

    def _finish(self, result: dict) -> dict:
        self.save_transcript()
        self.emit("done", **result)
        return result

    def _run_tool(self, name: str, args: dict) -> tuple[str, bool, dict]:
        """Run one tool call; any failure becomes a message the model can act on, never a crash."""
        try:
            return self._call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - e.g. wrong argument types from a small model
            return (f"Error: {type(exc).__name__}: {exc}. Check the arguments and try again.", False,
                    {"errors": [f"{type(exc).__name__}: {exc}"]})

    def _loop(self, max_turns: int, follow_up: bool = False) -> dict:
        st = self.project.status()
        self._base = (st["circuit_attempts"], st["layout_attempts"])
        idle = 0
        acted = False         # a state-changing tool ran in this turn
        settled = 0           # model turns since the design reached a finished state
        stalled = 0           # consecutive tool batches that made no progress (nothing ran, nothing changed)
        pending_fix = False   # the last design change the model tried was rejected
        fix_nudges = 0
        for turn in range(max_turns):
            if self.cancel.is_set():
                return self._finish({"ok": False, "reason": "stopped by the user", "turns": turn})
            finished, why = self.done()
            closing = acted and finished and not why.startswith("stopped")
            t0 = time.time()
            self.emit("thinking", model=self.model)
            try:
                msg = self.chat()
            except ModelError as exc:
                if closing:
                    # The board is done; only the model's closing words are missing.
                    self.emit("error", text=f"The model could not write its summary ({exc}). The design itself "
                                            "is finished.")
                    return self._finish({"ok": True, "reason": why, "turns": turn})
                raise
            calls = msg.get("tool_calls") or []
            self.messages.append({"role": "assistant", "content": msg.get("content") or "",
                                  **({"tool_calls": calls} if calls else {})})
            self.save_transcript()
            if msg.get("content"):
                self.log(f"[model {time.time() - t0:.1f}s] {msg['content'].strip()[:300]}")
                self.emit("assistant", text=msg["content"].strip(), seconds=round(time.time() - t0, 1))
            if calls:
                idle = 0
                before = self._progress_key()
                batch_ok_action = False
                batch_failed_action = False
                for call in calls:
                    fn = call.get("function") or {}
                    name = str(fn.get("name") or "")
                    call_id = call.get("id", name)
                    if self.cancel.is_set():
                        out, ok, facts = "Skipped: the user stopped the run.", False, {}
                    else:
                        try:
                            args = json.loads(fn.get("arguments") or "{}")
                        except (TypeError, json.JSONDecodeError) as exc:
                            out, ok, facts = (f"Rejected: tool arguments are not valid JSON ({exc}). "
                                              "Send a JSON object."), False, {"errors": [str(exc)]}
                            self.emit("tool_call", id=call_id, name=name, summary="(unreadable arguments)")
                        else:
                            args = args if isinstance(args, dict) else {}
                            self.log(f"[tool] {name}")
                            self.emit("tool_call", id=call_id, name=name, summary=describe_call(name, args))
                            t1 = time.time()
                            out, ok, facts = self._run_tool(name, args)
                            facts["seconds"] = round(time.time() - t1, 2)
                            if name in ACTIONS:
                                batch_ok_action |= ok
                                batch_failed_action |= not ok
                        self.log("       " + out.replace("\n", "\n       ")[:1500])
                        self.emit("tool_result", id=call_id, name=name, ok=ok, text=out[:4000], facts=facts,
                                  phase=self.project.state()["phase"])
                    # Every tool call gets an answer, so the saved conversation stays valid for the next turn.
                    self.messages.append({"role": "tool", "tool_call_id": call_id, "name": name,
                                          "content": out[:8000]})
                self.save_transcript()
                progressed = batch_ok_action or self._progress_key() != before
                if progressed:
                    acted, stalled = True, 0
                else:
                    stalled += 1
                # A rejected change (invalid circuit, bad constraints, locked spec) leaves the design as it was.
                if batch_ok_action:
                    pending_fix = False
                elif batch_failed_action:
                    pending_fix = True
                if self.cancel.is_set():
                    return self._finish({"ok": False, "reason": "stopped by the user", "turns": turn + 1})
                finished, why = self.done()
                if finished and why.startswith("stopped"):
                    return self._finish({"ok": False, "reason": why, "turns": turn + 1})
                if finished and acted:
                    settled += 1
                    if settled > 3:   # the model keeps looking things up after finishing
                        return self._finish({"ok": True, "reason": why, "turns": turn + 1})
                if stalled >= STALL_STOP:
                    return self._finish({"ok": False, "reason": "the model kept calling tools without making progress",
                                         "turns": turn + 1})
                if stalled == STALL_NUDGE:
                    nudge = ("PCBAgent: you have looked things up several times in a row. Answer the user now, or "
                             "change the design with pcb_circuit or pcb_layout." if follow_up and not acted else
                             prompts.continue_prompt(self._relative_status(), self.max_circuit, self.max_layout))
                    self.emit("nudge", text=nudge.splitlines()[0])
                    self.messages.append({"role": "user", "content": nudge})
                # Otherwise let the model read the results: it writes its summary or acts again.
                continue
            # A reply without tool calls.
            if follow_up and pending_fix:
                if fix_nudges < 2:
                    # The model tried a change, it was rejected, and now it is talking instead of fixing it.
                    fix_nudges += 1
                    nudge = ("PCBAgent: your last design change was rejected (see the tool result above). Fix it and "
                             "call the tool again, or tell the user plainly why the change cannot be made.")
                    self.emit("nudge", text=nudge)
                    self.messages.append({"role": "user", "content": nudge})
                    continue
                return self._finish({"ok": False, "reason": "the requested change was rejected; the design is unchanged",
                                     "turns": turn + 1})
            if follow_up and not acted:
                # The user asked something and the model answered without changing the design.
                return self._finish({"ok": True, "reason": "replied", "turns": turn + 1})
            finished, why = self.done()
            if finished:
                return self._finish({"ok": not why.startswith("stopped"), "reason": why, "turns": turn + 1})
            idle += 1
            if idle > 3:
                return self._finish({"ok": False, "reason": "the model stopped calling tools", "turns": turn + 1})
            nudge = prompts.continue_prompt(self._relative_status(), self.max_circuit, self.max_layout)
            self.log("[pcbagent] " + nudge.splitlines()[0])
            self.emit("nudge", text=nudge.splitlines()[0])
            self.messages.append({"role": "user", "content": nudge})
        return self._finish({"ok": False, "reason": f"turn limit {max_turns} reached", "turns": max_turns})


    def _progress_key(self) -> tuple:
        """Changes when the design moves: a new phase, or a circuit or layout attempt spent."""
        st = self.project.state()
        return (st.get("phase"), st.get("circuit_attempts"), st.get("layout_attempts"), st.get("spec_hash"))

    def _relative_status(self) -> dict:
        """Design status with attempt counts for this turn, matching the per-turn budgets."""
        status = self.project.status()
        status["circuit_attempts"] -= self._base[0]
        status["layout_attempts"] -= self._base[1]
        return status


ACTIONS = {"pcb_spec", "pcb_circuit", "pcb_layout"}   # tools that change the design
STALL_NUDGE = 6     # consecutive tool batches without progress before PCBAgent says "act or answer"
STALL_STOP = 10     # ... and before it gives up on the turn (look-ups, rejected or unreadable calls, crashed tools)


class ModelError(OSError):
    """The model endpoint answered with an error."""


SYSTEM = ("You are PCBAgent, a careful electronics engineer. You design circuits by calling tools; "
          "you never invent measurements.")


def describe_call(name: str, args: dict) -> str:
    """One line saying what a tool call asks for, for the live view."""
    from ..cli import unwrap
    try:
        if name == "pcb_spec":
            a = unwrap(args, "spec")
            return f"{a.get('title') or 'spec'}: {len(a.get('requirements') or [])} requirements, " \
                   f"{len(a.get('conditions') or [])} test conditions"
        if name == "pcb_circuit":
            comps = unwrap(args, "circuit").get("components") or []
            parts = ", ".join(f"{c.get('ref')} {c.get('value') or c.get('part')}" for c in comps[:8]
                              if isinstance(c, dict))
            return f"{len(comps)} components: {parts}" + (" ..." if len(comps) > 8 else "")
        if name == "pcb_layout":
            cons = unwrap(args, "constraints").get("constraints") or []
            return f"{len(cons)} placement constraints"
        if name == "pcb_parts_search":
            return " ".join(f"{k}={v}" for k, v in args.items() if v) or "all parts"
        if name == "pcb_models":
            return f"kind={args['kind']}" if args.get("kind") else "all part models"
    except (AttributeError, TypeError):
        pass
    return ""


def _as_obj(args):
    if isinstance(args, str):
        try:
            return json.loads(args)
        except json.JSONDecodeError:
            return {}
    return args or {}


def _search(args: dict) -> str:
    from ..catalog import db
    from ..catalog.parse import unit_price
    from ..units import UnitError, parse_value
    conn = db.connect()
    where, params = [], []
    for key, col in (("kind", "kind"), ("package", "package_norm")):
        if args.get(key):
            where.append(f"{col} = ?")
            params.append(str(args[key]))
    if args.get("value"):
        try:
            v = parse_value(args["value"])
            where.append("value BETWEEN ? AND ?")
            params += [v * 0.9995, v * 1.0005]
        except UnitError:
            pass
    for word in str(args.get("query") or "").split()[:8]:
        where.append("(mpn LIKE ? OR description LIKE ?)")
        params += [f"%{word}%", f"%{word}%"]
    sql = "SELECT * FROM parts" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY stock DESC LIMIT ?"
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    params.append(max(1, min(limit, 50)))
    rows = [db.row_to_dict(r) for r in conn.execute(sql, params)]
    conn.close()
    if not rows:
        return "no matching parts in the local catalog"
    return "\n".join(f"{r['lcsc']} {r['mpn']} | {r['package']} | {r['library_type']} | stock {r['stock']} | "
                     f"${unit_price(r['price_breaks'], 10)} | {r['description']}" for r in rows)


def main(args) -> int:
    from ..project import Project, slugify
    host = args.host if args.host.startswith("http") else f"http://{args.host}"
    if not is_local_url(host) and os.environ.get("PCBAGENT_ALLOW_REMOTE_MODEL") != "1":
        print(f"refusing to send the design to a non-local model endpoint ({host}). "
              "Set PCBAGENT_ALLOW_REMOTE_MODEL=1 if you really mean it.", file=sys.stderr)
        return 2
    root = Path(args.dir) if args.dir else Path("designs") / slugify(args.request)
    if root.exists() and (root / "pcbagent.json").exists():
        n = 2
        while (root.parent / f"{root.name}_{n}").exists():
            n += 1
        root = root.parent / f"{root.name}_{n}"
    project = Project.create(root, args.request)
    agent = Agent(project, host, args.model, include_layout=not args.no_layout, max_circuit=args.max_attempts)
    ctx = f", context {agent.num_ctx} tokens" if agent.api == "ollama" else ""
    print(f"design: {project.root}\nmodel: {args.model} at {host} ({agent.api} API{ctx})")
    try:
        result = agent.run(args.request)
    except OSError as exc:
        print(f"model endpoint error: {exc}. Is Ollama running (ollama serve) with the model pulled "
              f"(ollama pull {args.model})?", file=sys.stderr)
        return 2
    print(f"\n{'DONE' if result['ok'] else 'STOPPED'}: {result['reason']} after {result['turns']} model turns")
    print(f"design files: {project.root}")
    return 0 if result["ok"] else 3
