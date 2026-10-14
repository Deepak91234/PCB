"""Units, S-expressions, the schema validator and the part-model library."""

import math

import pytest

from conftest import needs_kicad
from pcbagent import models, schemas, sexpr
from pcbagent.units import UnitError, bom_value, format_value, in_series, nearest_in_series, parse_value
from pcbagent.validate import validate


@pytest.mark.parametrize("text,unit,expected", [
    ("10k", "ohm", 10e3), ("4k7", "ohm", 4.7e3), ("4R7", "ohm", 4.7), ("0R", "ohm", 0.0),
    ("100nF", "F", 100e-9), ("22u", "F", 22e-6), ("2.2µF", "F", 2.2e-6), ("1F", "F", 1.0),
    ("10f", None, 10e-15), ("1M", "ohm", 1e6), ("1meg", None, 1e6), ("500mA", "A", 0.5),
    ("100mW", "W", 0.1), ("3.3V", "V", 3.3), ("10kohm", "ohm", 10e3), (5, "V", 5.0),
])
def test_parse_value(text, unit, expected):
    assert math.isclose(parse_value(text, unit), expected, rel_tol=1e-12, abs_tol=1e-30)


@pytest.mark.parametrize("bad,unit", [("10V", "ohm"), ("abc", None), ("", None), ("1.2.3k", None)])
def test_parse_value_rejects(bad, unit):
    with pytest.raises(UnitError):
        parse_value(bad, unit)


def test_format_and_series():
    assert format_value(4700, "ohm") == "4.7kΩ"
    assert format_value(1e-7, "F") == "100nF"
    assert bom_value(4.7e-6, "F") == "4.7u"
    assert nearest_in_series(19400, "E96") == 19600
    assert in_series(19600, "E96") and not in_series(19400, "E96")


def test_sexpr_roundtrip():
    text = '(kicad (version 20221018) (pad "1" smd rect (at -0.8 0) (size 0.8 0.95) (layers "F.Cu")))'
    node = sexpr.loads(text)
    pad = sexpr.child(node, "pad")
    assert pad[1] == "1" and isinstance(pad[1], sexpr.Quoted)
    assert sexpr.floats(sexpr.child(pad, "at")) == [-0.8, 0.0]
    again = sexpr.loads(sexpr.dumps(node))
    assert again == node


def test_validator_messages():
    schema = {"type": "object", "required": ["a"], "additionalProperties": False,
              "properties": {"a": {"type": "string", "pattern": "[A-Z]+", "patternHint": "must be uppercase"},
                             "n": {"type": "number", "minimum": 0}}}
    errs = validate({"a": "low", "n": -1, "x": 1}, schema, "obj")
    assert any("must be uppercase" in e for e in errs)
    assert any("below the minimum" in e for e in errs)
    assert any("unknown field 'x'" in e for e in errs)
    assert validate({}, schema, "obj") == ["obj: missing required field 'a'"]


def test_models_are_consistent():
    for m in models.all_models().values():
        sp = m["spice"]
        assert sp["type"] in {"R", "C", "L", "led", "D", "Q", "M", "subckt", "none"}, m["id"]
        assert set(m["kicad"]["pins"]) == set(m["pins"]), m["id"]
        if sp["type"] == "subckt":
            assert sp["name"] in models.SPICE_LIBS
            assert set(sp["nodes"]) <= set(m["pins"])
        if sp["type"] in ("Q", "M"):
            assert set(sp["nodes"]) == set(m["pins"])
        assert m.get("source"), f"{m['id']} must say where its numbers come from"


@needs_kicad
def test_models_reference_installed_kicad_libraries():
    from pcbagent.kicad import libs
    for m in models.all_models().values():
        k = m["kicad"]
        assert libs.symbol_exists(k["symbol"]), f"{m['id']}: symbol {k['symbol']}"
        fps = [k["footprint"]] if "footprint" in k else list(k["footprints"].values())
        for fp in fps:
            assert libs.footprint_exists(fp), f"{m['id']}: footprint {fp}"


def test_schema_exports_are_current():
    """schemas/*.json is what the Pi extension loads; it must match the Python source."""
    import json
    from pathlib import Path
    from pcbagent.agent import prompts
    root = Path(__file__).resolve().parents[1] / "schemas"
    for name in schemas.ALL:
        on_disk = json.loads((root / f"{name}.schema.json").read_text())
        assert on_disk == schemas.tool_schema(name), f"run `pcbagent schema --write schemas` ({name})"
    assert json.loads((root / "prompts.json").read_text()) == prompts.export()
