"""Prompt text shared by the Pi extension and the standalone loop.

``pcbagent schema --write schemas`` exports these to ``schemas/prompts.json``,
which the Pi extension loads, so both front-ends say exactly the same thing.
"""

RULES = """Rules:
- You only describe intent as JSON through the pcb_* tools. The tools decide what is true: ngspice measures, the catalog sources parts, the placer and router draw the board, KiCad DRC scores it.
- The spec is locked after pcb_spec. Never try to change requirements to make a design pass; change the circuit.
- A number counts only if the simulator produced it. Do not claim results the tools did not report.
- Only use parts listed by pcb_models. Every spec port needs a connector or test point in the circuit.
- When a check fails, read the measured value, reason about the circuit (Ohm's law, divider ratios, dropout, current, power), change component values or topology, and submit the full revised circuit with pcb_circuit.
- Keep going without asking the user until the board passes or a tool says the budget is exhausted."""

# Small local models copy structure far more reliably than they follow prose.
EXAMPLE = """Example (5 V -> 3.3 V, 500 mA LDO):
pcb_spec {"title":"5 V to 3.3 V 500 mA regulator","ports":[{"net":"VIN","role":"power_in"},{"net":"VOUT","role":"power_out"},{"net":"GND","role":"ground"}],
 "conditions":[{"id":"no_load","sources":[{"net":"VIN","volts":5}]},{"id":"full_load","sources":[{"net":"VIN","volts":5}],"loads":[{"net":"VOUT","amps":0.5}]}],
 "requirements":[{"id":"vout_full_load","condition":"full_load","measure":{"type":"voltage","net":"VOUT"},"target":3.3,"tolerance":{"pct":3}},
  {"id":"idle_current","condition":"no_load","measure":{"type":"source_current","net":"VIN"},"max":0.02}]}
pcb_circuit {"components":[{"ref":"J1","part":"header_1x02","pins":{"1":"VIN","2":"GND"}},{"ref":"J2","part":"header_1x02","pins":{"1":"VOUT","2":"GND"}},
 {"ref":"U1","part":"AMS1117-3.3","pins":{"IN":"VIN","OUT":"VOUT","GND":"GND"}},{"ref":"C1","part":"capacitor","value":"10u","package":"0805","pins":{"1":"VIN","2":"GND"}},
 {"ref":"C2","part":"capacitor","value":"22u","package":"0805","pins":{"1":"VOUT","2":"GND"}}]}
pcb_layout {"constraints":[{"type":"edge","ref":"J1","side":"left"},{"type":"edge","ref":"J2","side":"right"},{"type":"flow","refs":["J1","U1","J2"],"axis":"x"},
 {"type":"near","ref":"C1","to":"U1","pin":"IN","max_mm":3},{"type":"near","ref":"C2","to":"U1","pin":"OUT","max_mm":3}]}"""

STEPS = """Steps:
1. Call pcb_models to see the parts you may use (and pcb_parts_search if you need to know which values are in stock).
2. Call pcb_spec with a specification: ports (every external net, plus GND with role ground), test conditions (ideal sources/loads on port nets, e.g. nominal, full load, low input) and requirements with numeric bounds (target + tolerance, or min/max). Write requirements that capture what the user asked for; include the load the circuit must drive.
3. Call pcb_circuit with the full circuit. It validates, simulates every condition across tolerance corners, checks part ratings and datasheet rules, and sources every part from the local JLCPCB catalog. Fix and resubmit until it passes."""

STEP_LAYOUT = ("4. Call pcb_layout with placement constraints (decoupling capacitors near their IC pins, connectors on "
               "edges, signal flow). Fix constraints and retry until DRC reports 0 errors and 0 unconnected items.")
STEP_NO_LAYOUT = "4. Stop after the circuit passes and parts are sourced."


def kickoff(request: str, design_dir: str, include_layout: bool = True) -> str:
    return (f'Design a PCB for this request:\n\n"{request}"\n\nDesign directory: {design_dir}\n\n'
            f"{STEPS}\n{STEP_LAYOUT if include_layout else STEP_NO_LAYOUT}\n\n{RULES}\n\n{EXAMPLE}")


def continue_prompt(status: dict, max_circuit: int, max_layout: int) -> str:
    lines = ["PCBAgent: the design is not finished, so the run continues."]
    phase = status.get("phase")
    if phase == "spec":
        lines.append("No spec is locked yet. Call pcb_spec now.")
    elif phase == "circuit":
        failed = (status.get("verification") or {}).get("failed") or []
        if failed:
            lines.append("Failing checks from the last simulation:")
            lines += [f"- {f}" for f in failed[:8]]
        lines.append(f"Next: {status.get('next')}. Call pcb_circuit with the complete revised circuit.")
        lines.append(f"Circuit attempts so far: {status.get('circuit_attempts')} of {max_circuit}.")
    elif phase == "layout":
        lay = status.get("layout") or {}
        if lay.get("message"):
            lines.append(f"Last layout: {lay['message']}")
        lines.append("Next: call pcb_layout with placement constraints.")
        lines.append(f"Layout attempts so far: {status.get('layout_attempts')} of {max_layout}.")
    lines.append("Do not ask the user; act with the tools.")
    return "\n".join(lines)


def export() -> dict:
    return {"rules": RULES, "example": EXAMPLE, "steps": STEPS, "step_layout": STEP_LAYOUT,
            "step_no_layout": STEP_NO_LAYOUT}
