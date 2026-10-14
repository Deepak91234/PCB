"""Parser for ngspice ASCII raw files (``set filetype=ascii``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Plot:
    title: str
    name: str
    complex: bool
    variables: list[tuple[str, str]]  # (name, type), names lowercased
    points: list[list] = field(default_factory=list)  # each point: one value per variable

    def index(self, name: str) -> int:
        name = name.lower()
        for i, (n, _t) in enumerate(self.variables):
            if n == name:
                return i
        raise KeyError(name)

    def has(self, name: str) -> bool:
        try:
            self.index(name)
            return True
        except KeyError:
            return False

    def vector(self, name: str) -> list:
        i = self.index(name)
        return [p[i] for p in self.points]

    def scalar(self, name: str) -> float:
        """Operating-point value (first point, real part)."""
        v = self.vector(name)[0]
        return v.real if isinstance(v, complex) else float(v)


class RawError(ValueError):
    pass


def parse(text: str) -> list[Plot]:
    lines = text.splitlines()
    plots: list[Plot] = []
    i = 0
    n = len(lines)
    while i < n:
        if not lines[i].startswith("Title:"):
            i += 1
            continue
        header: dict[str, str] = {}
        while i < n and not lines[i].startswith("Variables:"):
            if ":" in lines[i]:
                k, v = lines[i].split(":", 1)
                header[k.strip()] = v.strip()
            i += 1
        if i >= n:
            raise RawError("truncated header")
        nvars = int(header["No. Variables"])
        npoints = int(header["No. Points"])
        is_complex = "complex" in header.get("Flags", "")
        i += 1
        variables = []
        for _ in range(nvars):
            parts = lines[i].split()
            variables.append((parts[1].lower(), parts[2] if len(parts) > 2 else ""))
            i += 1
        while i < n and not lines[i].startswith("Values:"):
            i += 1
        i += 1
        points = []
        for _ in range(npoints):
            values = []
            first = lines[i].split()
            values.append(_num(first[1], is_complex))
            i += 1
            for _ in range(nvars - 1):
                values.append(_num(lines[i].strip(), is_complex))
                i += 1
            points.append(values)
            while i < n and not lines[i].strip():
                i += 1
        plots.append(Plot(header.get("Title", ""), header.get("Plotname", ""), is_complex, variables, points))
    return plots


def _num(token: str, is_complex: bool):
    if is_complex:
        re_, im = token.split(",")
        return complex(float(re_), float(im))
    return float(token)


def read(path: str | Path) -> list[Plot]:
    return parse(Path(path).read_text(errors="replace"))
