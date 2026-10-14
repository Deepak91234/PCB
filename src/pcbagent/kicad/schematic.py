"""KiCad schematic (.kicad_sch) from the circuit, so the design opens as a normal KiCad project.

Symbols are copied from the installed KiCad libraries (derived symbols are
flattened, as KiCad itself does when it embeds them). Every pin gets a short
wire and a net label; KiCad joins equal labels into nets. No-connect pins get
a no-connect flag. Symbol UUIDs are deterministic per reference, and the PCB
footprints carry the same UUID path, so "Update PCB from Schematic" matches
parts one-to-one.

Correctness check used by the tests: ``kicad-cli sch export netlist`` on the
generated file must reproduce exactly the circuit's connectivity.
"""

from __future__ import annotations

import copy
import math
import uuid

from .. import sexpr
from ..circuit import Circuit
from ..sexpr import Quoted
from . import libs

GRID = 1.27
STUB = 2.54


def stable_uuid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "pcbagent:" + ":".join(parts)))


def _snap(v: float, g: float = GRID) -> float:
    return round(round(v / g) * g, 4)


def _find_symbol(lib_tree, name: str):
    for node in sexpr.children(lib_tree, "symbol"):
        if str(node[1]) == name:
            return node
    return None


_LIB_CACHE: dict[str, object] = {}


def _lib(lib_name: str):
    if lib_name not in _LIB_CACHE:
        path = libs.symbol_lib_path(f"{lib_name}:x")
        if path is None:
            raise libs.LibraryError(f"symbol library {lib_name} not found")
        _LIB_CACHE[lib_name] = sexpr.loads(path.read_text(errors="replace"))
    return _LIB_CACHE[lib_name]


def flattened_symbol(symbol_id: str) -> list:
    """The library symbol as KiCad embeds it in a schematic: named Lib:Name, no 'extends'."""
    lib_name, _, name = symbol_id.partition(":")
    tree = _lib(lib_name)
    node = _find_symbol(tree, name)
    if node is None:
        raise libs.LibraryError(f"symbol {symbol_id} not found")
    node = copy.deepcopy(node)
    ext = sexpr.child(node, "extends")
    if ext is not None:
        parent = flattened_symbol(f"{lib_name}:{ext[1]}")
        parent_name = str(ext[1])
        child_props = {str(p[1]): p for p in sexpr.children(node, "property")}
        merged = [Quoted(f"{lib_name}:{name}") if False else "symbol", Quoted(f"{lib_name}:{name}")]
        for item in parent[2:]:
            if isinstance(item, list) and sexpr.head(item) == "property" and str(item[1]) in child_props:
                merged.append(child_props.pop(str(item[1])))
            elif isinstance(item, list) and sexpr.head(item) == "symbol":
                sub = copy.deepcopy(item)
                sub[1] = Quoted(str(sub[1]).replace(f"{parent_name}_", f"{name}_", 1))
                merged.append(sub)
            else:
                merged.append(item)
        # child-only properties go after the inherited ones
        insert_at = max(i for i, it in enumerate(merged) if isinstance(it, list) and sexpr.head(it) == "property") + 1
        for p in child_props.values():
            merged.insert(insert_at, p)
            insert_at += 1
        return merged
    node[1] = Quoted(f"{lib_name}:{name}")
    return node


def symbol_pins(sym: list) -> dict[str, tuple[float, float, float]]:
    """pin number -> (x, y, angle) of the connection point in symbol (y-up) coordinates."""
    out: dict[str, tuple[float, float, float]] = {}
    for unit in sexpr.children(sym, "symbol"):
        for pin in sexpr.children(unit, "pin"):
            at = sexpr.floats(sexpr.child(pin, "at"))
            num = str(sexpr.value(pin, "number"))
            out.setdefault(num, (at[0], at[1], at[2] if len(at) > 2 else 0.0))
    return out


def _extent(pins: dict) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pins.values()] or [0.0]
    ys = [p[1] for p in pins.values()] or [0.0]
    return min(xs), min(ys), max(xs), max(ys)


def _effects(hide: bool = False, justify: str | None = None) -> list:
    eff = ["effects", ["font", ["size", 1.27, 1.27]]]
    if justify:
        eff.append(["justify", *justify.split()])
    if hide:
        eff.append("hide")
    return eff


def _prop(name: str, value: str, x: float, y: float, angle: float = 0, hide: bool = False,
          justify: str | None = None) -> list:
    return ["property", Quoted(name), Quoted(value), ["at", x, y, angle], _effects(hide, justify)]


def write_schematic(circuit: Circuit, parts: dict | None, project: str, default_package: str = "0603") -> str:
    from .netlist import component_rows
    rows = component_rows(circuit, parts, default_package)
    root_uuid = stable_uuid(project, "root")
    lib_symbols: dict[str, list] = {}
    order = {"connector": 0, "ldo": 1, "npn": 2, "nmos": 2, "diode": 3, "led": 3,
             "capacitor": 4, "resistor": 5, "inductor": 5, "testpoint": 6}
    comps = sorted(circuit.components, key=lambda c: (order.get(c.kind, 9), c.ref))
    cols = max(2, math.ceil(math.sqrt(len(comps))))
    cell_w, cell_h = 38.1, 27.94
    items: list = []
    by_ref = {r["ref"]: r for r in rows}
    for idx, comp in enumerate(comps):
        row = by_ref[comp.ref]
        sym_id = row["symbol"]
        if sym_id not in lib_symbols:
            lib_symbols[sym_id] = flattened_symbol(sym_id)
        sym = lib_symbols[sym_id]
        pins = symbol_pins(sym)
        x0, y0, x1, y1 = _extent(pins)
        cx = _snap(25.4 + (idx % cols) * cell_w + 10.16, 2.54)
        cy = _snap(30.48 + (idx // cols) * cell_h, 2.54)
        sym_uuid = stable_uuid(comp.ref)
        node = ["symbol", ["lib_id", Quoted(sym_id)], ["at", cx, cy, 0], ["unit", 1],
                ["in_bom", "yes"], ["on_board", "yes"], ["dnp", "no"], ["uuid", Quoted(sym_uuid)]]
        top = cy - y1 - 2.54
        bottom = cy - y0 + 2.54
        angles = {round(p[2]) % 360 for p in pins.values()}
        if angles <= {90, 270}:
            # vertical two-terminal part: labels sit above/below, so put the texts beside it
            node.append(_prop("Reference", comp.ref, _snap(cx + 2.54), _snap(cy - 1.27), 0, justify="left"))
            node.append(_prop("Value", row["value"], _snap(cx + 2.54), _snap(cy + 1.27), 0, justify="left"))
        elif 90 in angles:
            # pins pointing up into the body sit at the bottom: keep both texts on top
            node.append(_prop("Reference", comp.ref, cx, _snap(top - 1.27), 0))
            node.append(_prop("Value", row["value"], cx, _snap(top + 1.27), 0))
        else:
            node.append(_prop("Reference", comp.ref, cx, _snap(top), 0))
            node.append(_prop("Value", row["value"], cx, _snap(bottom), 0))
        node.append(_prop("Footprint", row["footprint"], cx, cy, 0, hide=True))
        node.append(_prop("Datasheet", "~", cx, cy, 0, hide=True))
        for k, v in row["fields"].items():
            node.append(_prop(k, v, cx, cy, 0, hide=True))
        for num in pins:
            node.append(["pin", Quoted(num), ["uuid", Quoted(stable_uuid(comp.ref, "pin", num))]])
        node.append(["instances", ["project", Quoted(project),
                                   ["path", Quoted(f"/{root_uuid}"), ["reference", Quoted(comp.ref)], ["unit", 1]]]])
        items.append(node)
        pad_to_net = {row["pads"][pin]: net for pin, net in row["nets"].items()}
        for num, (px, py, ang) in pins.items():
            # schematic coordinates: y flipped relative to the symbol library
            sx, sy = _snap(cx + px), _snap(cy - py)
            net = pad_to_net.get(num)
            if net is None:
                items.append(["no_connect", ["at", sx, sy], ["uuid", Quoted(stable_uuid(comp.ref, "nc", num))]])
                continue
            # outward = opposite of the pin's direction (which points into the body)
            a = math.radians(ang)
            ox, oy = -round(math.cos(a)), round(math.sin(a))
            ex, ey = _snap(sx + ox * STUB), _snap(sy + oy * STUB)
            items.append(["wire", ["pts", ["xy", sx, sy], ["xy", ex, ey]],
                          ["stroke", ["width", 0], ["type", "default"]],
                          ["uuid", Quoted(stable_uuid(comp.ref, "wire", num))]])
            if ox > 0:
                rot, just = 0, "left bottom"
            elif ox < 0:
                rot, just = 180, "right bottom"
            elif oy < 0:
                rot, just = 90, "left bottom"
            else:
                rot, just = 270, "right bottom"
            items.append(["label", Quoted(net), ["at", ex, ey, rot], ["fields_autoplaced"],
                          _effects(False, just), ["uuid", Quoted(stable_uuid(comp.ref, "label", num))]])
    tree = ["kicad_sch", ["version", 20230121], ["generator", "pcbagent"], ["uuid", Quoted(root_uuid)],
            ["paper", Quoted("A3")],
            ["title_block", ["title", Quoted(circuit.name or project)], ["comment", 1, Quoted("Generated by PCBAgent")]],
            ["lib_symbols", *lib_symbols.values()],
            *items,
            ["sheet_instances", ["path", Quoted("/"), ["page", Quoted("1")]]]]
    return sexpr.dumps(tree) + "\n"


def footprint_paths(circuit: Circuit) -> dict[str, str]:
    """ref -> KIID path the PCB footprint should carry to link to its schematic symbol."""
    return {c.ref: f"/{stable_uuid(c.ref)}" for c in circuit.components}
