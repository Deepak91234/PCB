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
import time
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
    def __init__(self, project, host: str, model: str, include_layout: bool = True,
                 max_circuit: int = 12, max_layout: int = 6, log=print):
        self.project = project
        self.host = host.rstrip("/")
        self.model = model
        self.include_layout = include_layout
        self.max_circuit = max_circuit
        self.max_layout = max_layout
        self.log = log
        self.messages: list[dict] = []

    # --- model --------------------------------------------------------------
    def chat(self) -> dict:
        body = {"model": self.model, "messages": self.messages, "stream": False, "temperature": 0.2,
                "tools": [{"type": "function", "function": t} for t in TOOLS]}
        req = urllib.request.Request(f"{self.host}/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"})
        with urllib.request.urlopen(req, timeout=float(os.environ.get("PCBAGENT_MODEL_TIMEOUT", "900"))) as resp:
            data = json.load(resp)
        return data["choices"][0]["message"]

    # --- tools --------------------------------------------------------------
    def call_tool(self, name: str, args: dict) -> str:
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
                return "\n".join(json.dumps(models_mod.summary(m)) for m in models_mod.all_models().values()
                                 if not kind or m["kind"] == kind)
            if name == "pcb_parts_search":
                return _search(args)
            if name == "pcb_spec":
                spec = p.write_spec(args)
                return (f"spec locked ({len(spec['requirements'])} requirements). "
                        "Next: call pcb_circuit with the full circuit.")
            if name == "pcb_circuit":
                res = p.submit_circuit(args)
                return res["summary"] + f"\nNext: {p.next_step()}"
            if name == "pcb_layout":
                res = pipeline.run(p, args)
                return pipeline.summarize(res)
            if name == "pcb_status":
                return json.dumps(p.status(), indent=1, default=str)
            return f"unknown tool {name}"
        except (SpecError, CircuitError, ConstraintError) as exc:
            return "Rejected:\n" + "\n".join(f"- {e}" for e in exc.errors)
        except (ProjectError, CatalogMissing) as exc:
            return f"Error: {exc}"

    # --- loop -----------------------------------------------------------------
    def done(self) -> tuple[bool, str]:
        st = self.project.status()
        if st["phase"] == "done":
            return True, "board routed, DRC clean"
        if st["phase"] == "layout" and not self.include_layout:
            return True, "circuit verified and parts sourced"
        if st["phase"] != "layout" and st["circuit_attempts"] >= self.max_circuit:
            return True, f"stopped after {st['circuit_attempts']} circuit attempts"
        if st["phase"] == "layout" and st["layout_attempts"] >= self.max_layout:
            return True, f"stopped after {st['layout_attempts']} layout attempts"
        return False, ""

    def run(self, request: str, max_turns: int = 80) -> dict:
        self.messages = [
            {"role": "system", "content": "You are PCBAgent, a careful electronics engineer. You design circuits "
                                          "by calling tools; you never invent measurements."},
            {"role": "user", "content": prompts.kickoff(request, str(self.project.root), self.include_layout)},
        ]
        idle = 0
        for turn in range(max_turns):
            t0 = time.time()
            msg = self.chat()
            calls = msg.get("tool_calls") or []
            self.messages.append({"role": "assistant", "content": msg.get("content") or "",
                                  **({"tool_calls": calls} if calls else {})})
            if msg.get("content"):
                self.log(f"[model {time.time() - t0:.1f}s] {msg['content'].strip()[:300]}")
            if calls:
                idle = 0
                for call in calls:
                    fn = call["function"]
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError as exc:
                        out = f"Rejected: tool arguments are not valid JSON ({exc}). Send a JSON object."
                    else:
                        self.log(f"[tool] {fn['name']}")
                        out = self.call_tool(fn["name"], args if isinstance(args, dict) else {})
                    self.log("       " + out.replace("\n", "\n       ")[:1500])
                    self.messages.append({"role": "tool", "tool_call_id": call.get("id", fn["name"]),
                                          "name": fn["name"], "content": out[:8000]})
                finished, why = self.done()
                if finished and not why.startswith("stopped"):
                    # Let the model write its closing summary, then stop.
                    final = self.chat()
                    if final.get("content"):
                        self.log(f"[model] {final['content'].strip()[:500]}")
                    return {"ok": True, "reason": why, "turns": turn + 1}
                if finished:
                    return {"ok": False, "reason": why, "turns": turn + 1}
                continue
            finished, why = self.done()
            if finished:
                return {"ok": not why.startswith("stopped"), "reason": why, "turns": turn + 1}
            idle += 1
            if idle > 3:
                return {"ok": False, "reason": "the model stopped calling tools", "turns": turn + 1}
            nudge = prompts.continue_prompt(self.project.status(), self.max_circuit, self.max_layout)
            self.log("[pcbagent] " + nudge.splitlines()[0])
            self.messages.append({"role": "user", "content": nudge})
        return {"ok": False, "reason": f"turn limit {max_turns} reached", "turns": max_turns}


def _search(args: dict) -> str:
    from ..catalog import db
    from ..catalog.parse import unit_price
    from ..units import UnitError, parse_value
    conn = db.connect()
    where, params = [], []
    for key, col in (("kind", "kind"), ("package", "package_norm")):
        if args.get(key):
            where.append(f"{col} = ?")
            params.append(args[key])
    if args.get("value"):
        try:
            v = parse_value(args["value"])
            where.append("value BETWEEN ? AND ?")
            params += [v * 0.9995, v * 1.0005]
        except UnitError:
            pass
    for word in str(args.get("query") or "").split():
        where.append("(mpn LIKE ? OR description LIKE ?)")
        params += [f"%{word}%", f"%{word}%"]
    sql = "SELECT * FROM parts" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY stock DESC LIMIT ?"
    params.append(int(args.get("limit") or 10))
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
    print(f"design: {project.root}\nmodel: {args.model} at {host}")
    agent = Agent(project, host, args.model, include_layout=not args.no_layout, max_circuit=args.max_attempts)
    try:
        result = agent.run(args.request)
    except OSError as exc:
        print(f"model endpoint error: {exc}. Is Ollama running (ollama serve) with the model pulled "
              f"(ollama pull {args.model})?", file=sys.stderr)
        return 2
    print(f"\n{'DONE' if result['ok'] else 'STOPPED'}: {result['reason']} after {result['turns']} model turns")
    print(f"design files: {project.root}")
    return 0 if result["ok"] else 3
