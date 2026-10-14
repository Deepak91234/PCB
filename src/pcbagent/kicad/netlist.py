"""KiCad netlist (.net, S-expression format "E") from the circuit + chosen parts.

The same connections that were simulated become the netlist; footprints and
LCSC numbers come from part selection. KiCad imports it with
File > Import > Netlist in the PCB editor.
"""

from __future__ import annotations

import time
import uuid

from .. import __version__
from ..circuit import Circuit
from ..sexpr import Quoted, dumps


def _uuid(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pcbagent:{seed}"))


def component_rows(circuit: Circuit, parts: dict | None, default_package: str = "0603") -> list[dict]:
    """Per component: ref, value, footprint, symbol, pin->pad map, fields."""
    rows = []
    for c in circuit.components:
        sel = (parts or {}).get(c.ref, {})
        footprint = sel.get("footprint") or c.footprint(default_package)
        fields = {}
        if sel.get("lcsc"):
            fields["LCSC"] = sel["lcsc"]
        if sel.get("mpn"):
            fields["MPN"] = sel["mpn"]
        if sel.get("manufacturer"):
            fields["Manufacturer"] = sel["manufacturer"]
        if sel.get("status") in ("hand_solder", "not_purchased"):
            fields["JLCPCB"] = "not assembled"
        rows.append({
            "ref": c.ref,
            "value": c.value_text() if c.value is not None else c.part,
            "footprint": footprint,
            "symbol": c.model["kicad"]["symbol"],
            "pads": {pin: c.model["kicad"]["pins"][pin] for pin in c.pins},
            "nets": dict(c.pins),
            "fields": fields,
            "description": c.model["title"],
        })
    return rows


def write_netlist(circuit: Circuit, parts: dict | None, source_name: str, default_package: str = "0603") -> str:
    rows = component_rows(circuit, parts, default_package)
    comps = ["components"]
    for r in rows:
        lib, _, part = r["symbol"].partition(":")
        comp = ["comp", ["ref", Quoted(r["ref"])], ["value", Quoted(r["value"])],
                ["footprint", Quoted(r["footprint"])]]
        if r["fields"]:
            comp.append(["fields"] + [["field", ["name", Quoted(k)], Quoted(v)] for k, v in r["fields"].items()])
        comp.append(["libsource", ["lib", Quoted(lib)], ["part", Quoted(part)],
                     ["description", Quoted(r["description"])]])
        comp.append(["sheetpath", ["names", Quoted("/")], ["tstamps", Quoted("/")]])
        comp.append(["tstamps", Quoted(_uuid(r["ref"]))])
        comps.append(comp)
    nets_map: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        for pin, net in r["nets"].items():
            nets_map.setdefault(net, []).append((r["ref"], r["pads"][pin]))
    nets = ["nets"]
    for code, net in enumerate(sorted(nets_map), start=1):
        node = ["net", ["code", Quoted(str(code))], ["name", Quoted(net)]]
        for ref, pad in nets_map[net]:
            node.append(["node", ["ref", Quoted(ref)], ["pin", Quoted(pad)]])
        nets.append(node)
    tree = ["export", ["version", Quoted("E")],
            ["design", ["source", Quoted(source_name)],
             ["date", Quoted(time.strftime("%Y-%m-%d %H:%M:%S"))],
             ["tool", Quoted(f"pcbagent {__version__}")]],
            comps, nets]
    return dumps(tree) + "\n"
