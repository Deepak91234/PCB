"""Minimal S-expression reader/writer for KiCad files and Specctra SES/DSN.

Atoms stay strings; callers convert numbers. Quoted strings are returned as
:class:`Quoted` so a writer can round-trip them faithfully.

This module must stay stdlib-only and Python 3.10 compatible because the KiCad
worker imports it under KiCad's bundled interpreter.
"""

from __future__ import annotations

from typing import Iterator, List, Union


class Quoted(str):
    """A string atom that was written with double quotes."""


Node = Union[str, "List[Node]"]


class SexprError(ValueError):
    pass


def _tokens(text: str, string_quote: str = '"') -> Iterator[str]:
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(" or c == ")":
            yield c
            i += 1
        elif c == string_quote:
            j = i + 1
            buf = []
            while j < n:
                d = text[j]
                if d == "\\" and j + 1 < n:
                    nxt = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, nxt))
                    j += 2
                    continue
                if d == string_quote:
                    break
                buf.append(d)
                j += 1
            else:
                raise SexprError("unterminated string")
            yield Quoted("".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in " \t\r\n()":
                j += 1
            yield text[i:j]
            i = j


def loads(text: str, string_quote: str = '"') -> Node:
    """Parse one S-expression (the first complete one in ``text``)."""
    stack: list[list] = []
    root = None
    for tok in _tokens(text, string_quote):
        if tok == "(" and not isinstance(tok, Quoted):
            stack.append([])
        elif tok == ")" and not isinstance(tok, Quoted):
            if not stack:
                raise SexprError("unbalanced ')'")
            done = stack.pop()
            if stack:
                stack[-1].append(done)
            else:
                root = done
                break
        else:
            if not stack:
                raise SexprError(f"atom outside list: {tok!r}")
            stack[-1].append(tok)
    if root is None:
        raise SexprError("no complete expression")
    return root


def load_specctra(text: str) -> Node:
    """Specctra files declare their quote character with ``(string_quote ")``."""
    return loads(text, '"')


def _atom(value) -> str:
    if isinstance(value, Quoted):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text if text not in ("", "-0") else "0"
    s = str(value)
    if s == "" or any(c in s for c in ' ()"\t\n'):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def dumps(node: Node, indent: int = 0, width: int = 100) -> str:
    """Serialise with KiCad-like layout: short lists on one line, long ones broken."""
    if not isinstance(node, list):
        return _atom(node)
    flat = "(" + " ".join(dumps(child, 0, 10**9) for child in node) + ")"
    if len(flat) + indent <= width or not any(isinstance(c, list) for c in node):
        return flat
    pad = "  " * (indent + 1)
    head = []
    rest = []
    for child in node:
        if not rest and not isinstance(child, list):
            head.append(_atom(child))
        else:
            rest.append(child)
    lines = ["(" + " ".join(head)]
    for child in rest:
        lines.append(pad + dumps(child, indent + 1, width))
    return "\n".join(lines) + ")"


# --- query helpers ----------------------------------------------------------

def head(node: Node) -> str | None:
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return None


def children(node: Node, name: str | None = None) -> list:
    if not isinstance(node, list):
        return []
    out = [c for c in node[1:] if isinstance(c, list)]
    if name is not None:
        out = [c for c in out if head(c) == name]
    return out


def child(node: Node, name: str):
    for c in children(node, name):
        return c
    return None


def value(node: Node, name: str, default=None, index: int = 1):
    c = child(node, name)
    if c is None or len(c) <= index:
        return default
    return c[index]


def floats(node: list, start: int = 1) -> list[float]:
    out = []
    for item in node[start:]:
        if isinstance(item, list):
            break
        try:
            out.append(float(item))
        except ValueError:
            break
    return out


def walk(node: Node) -> Iterator[list]:
    if isinstance(node, list):
        yield node
        for c in node:
            yield from walk(c)
