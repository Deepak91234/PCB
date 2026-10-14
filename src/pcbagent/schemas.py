"""JSON Schemas for everything the model writes: spec, circuit, layout constraints.

These are the single source of truth. ``pcbagent schema --write`` exports them
to ``schemas/*.json`` where the Pi extension loads them as tool parameter
schemas, so the TypeScript side never drifts from the Python validator.
"""

from __future__ import annotations

NET = {"type": "string", "pattern": r"[A-Z][A-Z0-9_]{0,31}",
       "patternHint": "must be an uppercase net name like VIN, VOUT_3V3 or GND"}
ID = {"type": "string", "pattern": r"[a-z][a-z0-9_]{0,39}",
      "patternHint": "must be a lowercase identifier like vout_nominal"}
REF = {"type": "string", "pattern": r"[A-Z]{1,3}[0-9]{1,3}",
       "patternHint": "must be a reference designator like R1, C12, U1 or TP3"}
POS = {"type": "number", "exclusiveMinimum": 0}

MEASURES = {
    "voltage": "Voltage of `net` relative to ground (V).",
    "source_current": "Current delivered by the test source on `net` (A).",
    "source_power": "Power delivered by the test source on `net` (W).",
    "load_power": "Power absorbed by the test load on `net` (W).",
    "efficiency": "Total load power / total source power (%).",
    "component_current": "Current flowing into pin `pin` of component `ref` (A).",
    "component_power": "Power dissipated in component `ref` (W).",
    "voltage_change": "V(`net`) in this condition minus V(`net`) in `from_condition` (V).",
    "gain_db": "AC gain from `input` to `output` at `hz` (dB).",
    "cutoff_hz": "-3 dB frequency of the AC response from `input` to `output` (Hz).",
}

MEASURE_UNITS = {
    "voltage": "V", "source_current": "A", "source_power": "W", "load_power": "W",
    "efficiency": "%", "component_current": "A", "component_power": "W",
    "voltage_change": "V", "gain_db": "dB", "cutoff_hz": "Hz",
}

MEASURE_SCHEMA = {
    "type": "object",
    "required": ["type"],
    "additionalProperties": False,
    "properties": {
        "type": {"type": "string", "enum": sorted(MEASURES)},
        "net": NET,
        "ref": REF,
        "pin": {"type": "string", "minLength": 1},
        "from_condition": ID,
        "input": NET,
        "output": NET,
        "hz": POS,
    },
}

SOURCE_SCHEMA = {
    "type": "object",
    "required": ["net"],
    "additionalProperties": False,
    "description": "A test source: {\"net\": \"VIN\", \"volts\": 5} or {\"net\": \"IIN\", \"amps\": 0.01}",
    "properties": {
        "net": NET,
        "volts": {"type": "number"},
        "amps": {"type": "number"},
        "series_ohms": {"type": "number", "minimum": 0},
    },
}

LOAD_SCHEMA = {
    "type": "object",
    "required": ["net"],
    "additionalProperties": False,
    "description": "A test load to ground: {\"net\": \"VOUT\", \"ohms\": 100} or {\"net\": \"VOUT\", \"amps\": 0.5}",
    "properties": {"net": NET, "ohms": POS, "amps": {"type": "number", "minimum": 0}},
}

SPEC_SCHEMA = {
    "type": "object",
    "title": "PCBAgent circuit specification",
    "required": ["title", "ports", "conditions", "requirements"],
    "additionalProperties": False,
    "properties": {
        "schema": {"type": "string", "enum": ["pcbagent/spec@1"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 120},
        "summary": {"type": "string", "maxLength": 2000},
        "ground": NET,
        "ports": {
            "type": "array", "minItems": 2,
            "description": "External connections of the board. Every port needs a connector or test point.",
            "items": {
                "type": "object", "required": ["net", "role"], "additionalProperties": False,
                "properties": {
                    "net": NET,
                    "role": {"type": "string", "enum": [
                        "power_in", "power_out", "signal_in", "signal_out", "bidirectional", "ground"]},
                    "description": {"type": "string", "maxLength": 300},
                },
            },
        },
        "conditions": {
            "type": "array", "minItems": 1,
            "description": "Test benches: ideal sources and loads applied to port nets. They are not part of the board.",
            "items": {
                "type": "object", "required": ["id", "sources"], "additionalProperties": False,
                "properties": {
                    "id": ID,
                    "description": {"type": "string", "maxLength": 300},
                    "sources": {"type": "array", "minItems": 1, "items": SOURCE_SCHEMA},
                    "loads": {"type": "array", "items": LOAD_SCHEMA},
                    "ambient_c": {"type": "number", "minimum": -55, "maximum": 125},
                },
            },
        },
        "requirements": {
            "type": "array", "minItems": 1,
            "description": "Acceptance checks. Each needs target+tolerance or min and/or max.",
            "items": {
                "type": "object", "required": ["id", "condition", "measure"], "additionalProperties": False,
                "properties": {
                    "id": ID,
                    "description": {"type": "string", "maxLength": 300},
                    "condition": ID,
                    "measure": MEASURE_SCHEMA,
                    "target": {"type": "number"},
                    "tolerance": {
                        "type": "object", "additionalProperties": False,
                        "description": "{\"pct\": 2} for +/-2 % or {\"abs\": 0.05} for +/-0.05 units",
                        "properties": {"pct": POS, "abs": POS},
                    },
                    "min": {"type": "number"},
                    "max": {"type": "number"},
                },
            },
        },
        "board": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "max_width_mm": POS, "max_height_mm": POS,
                "layers": {"type": "integer", "enum": [2]},
            },
        },
        "preferences": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "passive_package": {"type": "string", "enum": ["0402", "0603", "0805", "1206"]},
                "quantity": {"type": "integer", "minimum": 1, "maximum": 100000},
                "basic_parts_only": {"type": "boolean"},
                "ambient_c": {"type": "number", "minimum": -55, "maximum": 125},
            },
        },
    },
}

COMPONENT_SCHEMA = {
    "type": "object",
    "required": ["ref", "part", "pins"],
    "additionalProperties": False,
    "properties": {
        "ref": REF,
        "part": {"type": "string", "minLength": 1,
                 "description": "Part model id from pcb_models, e.g. resistor, capacitor, AMS1117-3.3, header_1x02"},
        "value": {"type": ["string", "number"],
                  "description": "Required for resistor/capacitor/inductor, e.g. \"10k\", \"100n\", \"4.7u\""},
        "package": {"type": "string", "description": "Optional package for passives: 0402, 0603, 0805, 1206"},
        "pins": {"type": "object", "additionalProperties": NET,
                 "description": "Map of part pin name to net name, e.g. {\"IN\": \"VIN\", \"OUT\": \"VOUT\", \"GND\": \"GND\"}"},
        "note": {"type": "string", "maxLength": 300},
    },
}

CIRCUIT_SCHEMA = {
    "type": "object",
    "title": "PCBAgent circuit",
    "required": ["components"],
    "additionalProperties": False,
    "properties": {
        "schema": {"type": "string", "enum": ["pcbagent/circuit@1"]},
        "name": {"type": "string", "maxLength": 80},
        "description": {"type": "string", "maxLength": 2000},
        "components": {"type": "array", "minItems": 1, "maxItems": 200, "items": COMPONENT_SCHEMA},
    },
}

CONSTRAINT_TYPES = {
    "near": "Keep `ref` within `max_mm` of `to` (optionally of pin `pin` of `to`). Use for decoupling caps.",
    "edge": "Put `ref` on the board edge `side` (left/right/top/bottom/any). Use for connectors.",
    "group": "Keep `refs` together within `max_mm` of each other.",
    "flow": "Order `refs` along `axis` (x: left to right, y: top to bottom) to follow signal/power flow.",
    "align": "Line `refs` up along `axis` (x: same row, y: same column).",
    "apart": "Keep `ref` at least `min_mm` away from `to` (e.g. heat sources from sensitive parts).",
}

CONSTRAINTS_SCHEMA = {
    "type": "object",
    "title": "PCBAgent placement constraints",
    "required": ["constraints"],
    "additionalProperties": False,
    "properties": {
        "schema": {"type": "string", "enum": ["pcbagent/constraints@1"]},
        "board": {
            "type": "object", "additionalProperties": False,
            "description": "Optional board size limits. The placer picks the actual size.",
            "properties": {"max_width_mm": POS, "max_height_mm": POS, "margin_mm": POS},
        },
        "constraints": {
            "type": "array",
            "items": {
                "type": "object", "required": ["type"], "additionalProperties": False,
                "properties": {
                    "type": {"type": "string", "enum": sorted(CONSTRAINT_TYPES)},
                    "ref": REF,
                    "to": REF,
                    "pin": {"type": "string"},
                    "refs": {"type": "array", "items": REF, "minItems": 2},
                    "max_mm": POS,
                    "min_mm": POS,
                    "side": {"type": "string", "enum": ["left", "right", "top", "bottom", "any"]},
                    "axis": {"type": "string", "enum": ["x", "y"]},
                    "reason": {"type": "string", "maxLength": 200},
                },
            },
        },
        "pour_ground": {"type": "boolean", "description": "Add ground pours on both layers (default true)."},
    },
}

ALL = {"spec": SPEC_SCHEMA, "circuit": CIRCUIT_SCHEMA, "constraints": CONSTRAINTS_SCHEMA}


def tool_schema(name: str) -> dict:
    """Schema stripped of validator-only keywords (patternHint) for LLM tool definitions."""
    def clean(node):
        if isinstance(node, dict):
            return {k: clean(v) for k, v in node.items() if k not in ("patternHint",)}
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node
    return clean(ALL[name])
