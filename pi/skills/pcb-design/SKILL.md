---
name: pcb-design
description: Design verified, manufacturable PCBs locally with PCBAgent (spec -> ngspice-verified circuit -> JLCPCB parts -> routed KiCad board). Use when the user asks for a circuit, schematic, PCB, regulator, divider, LED driver or filter, or wants to inspect a PCBAgent design.
---

# PCB design with PCBAgent

Start a run with `/design <request>`; it keeps going until the board passes KiCad DRC.
`/layout` re-runs placement and routing; `/pcb-status`, `/pcb-replay` and `/pcb-fab` inspect,
replay in KiCad and export Gerbers/BOM/CPL.

During a run you only have the `pcb_*` tools. They are the source of truth:

| Tool | What it does |
|---|---|
| `pcb_models` | Parts you may use (ids, pins, ratings, datasheet rules) |
| `pcb_parts_search` | What the local JLCPCB catalog stocks (values, packages) |
| `pcb_spec` | Writes and **locks** the spec. Once only. |
| `pcb_circuit` | Validates, simulates every condition across tolerance corners, checks ratings and datasheet rules, sources parts |
| `pcb_layout` | Placement constraints in; placer + Freerouting + KiCad DRC out |
| `pcb_status` | Phase, last failures, next step |

## Writing a good spec

- Ports: every net that leaves the board, plus `GND` with role `ground`.
- Conditions are test benches, not board parts: ideal sources (`{"net":"VIN","volts":5}`) and loads
  (`{"net":"VOUT","ohms":100}` or `{"net":"VOUT","amps":0.5}`). Include the worst cases the user cares
  about: full load, low input, no load.
- Requirements need numbers: `target` + `tolerance` (`{"pct":2}` / `{"abs":0.05}`) or `min`/`max`.
- Measures: `voltage{net}`, `source_current{net}`, `source_power{net}`, `load_power{net}`, `efficiency`,
  `component_current{ref,pin}`, `component_power{ref}`, `voltage_change{net,from_condition}`,
  `gain_db{input,output,hz}`, `cutoff_hz{input,output}`.
- If a requirement measures a component (`component_current` of `D1`), the circuit must use that ref.

## Writing a circuit

```json
{"components": [
  {"ref": "J1", "part": "header_1x02", "pins": {"1": "VIN", "2": "GND"}},
  {"ref": "R1", "part": "resistor", "value": "10k", "pins": {"1": "VIN", "2": "VOUT"}},
  {"ref": "R2", "part": "resistor", "value": "19.6k", "pins": {"1": "VOUT", "2": "GND"}}
]}
```

- Passives need `value` (`10k`, `4.7u`, `100n`); `package` is optional (`0402`/`0603`/`0805`/`1206`).
- Every pin of every part must be connected (except pins named `NC`); every net needs two connections.
- Every spec port needs a connector or test point.
- Datasheet rules are enforced: e.g. AMS1117 needs >= 10 uF on IN and >= 22 uF on OUT; AP2112K's EN
  must be tied high.

## Fixing a failure

Read the measured numbers. They come from ngspice, at the worst tolerance corner.

- Divider low/high: Vout = Vin * R2 / (R1 + R2); remember the load is in parallel with R2.
- Regulator output sagging at low input: dropout. Pick a lower-dropout regulator or raise the input.
- Junction temperature too high: P = (Vin - Vout) * Iout; choose a bigger package or a different
  topology, or reduce the input voltage.
- "no orderable part": the value is not stocked; use one of the listed nearest in-stock values and re-check
  the math.

Never try to change the spec to pass. It is locked; only the user can unlock it with
`pcbagent spec-unlock <design>`.
