"""Deterministic simulated-annealing placer.

Minimises weighted half-perimeter wirelength plus penalties for courtyard
overlap, leaving the board, and violated constraints. Moves: translate,
rotate by 90 degrees, swap. Cost updates are incremental, so a 30-part board
anneals in a second or two of pure Python.

Coordinates are millimetres in board space: origin at the board's top-left
corner, x right, y down, rotation counter-clockwise in degrees (KiCad's
convention).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from ..kicad.libs import Footprint


def rot_point(x: float, y: float, quarter: int) -> tuple[float, float]:
    q = quarter % 4
    if q == 0:
        return x, y
    if q == 1:
        return y, -x
    if q == 2:
        return -x, -y
    return -y, x


@dataclass
class Item:
    ref: str
    fp: Footprint
    pad_nets: dict[str, str]          # pad number -> net
    rects: list = field(default_factory=list)   # per rotation: (x0, y0, x1, y1) inflated courtyard offsets
    pads: list = field(default_factory=list)    # per rotation: list of (net, dx, dy, pad_number)
    area: float = 0.0


@dataclass
class PlaceResult:
    width: float
    height: float
    positions: dict[str, tuple[float, float, int]]   # ref -> (x, y, rotation degrees)
    frames: list[dict]
    cost: dict
    legal: bool
    issues: list[str]


W_OVERLAP = 60.0
W_OUT = 60.0
W_NEAR = 12.0
W_EDGE = 6.0
W_FLOW = 4.0
W_ALIGN = 4.0
W_GROUP = 4.0
W_APART = 10.0
CON_WEIGHTS = {"near": W_NEAR, "edge": W_EDGE, "flow": W_FLOW, "align": W_ALIGN, "group": W_GROUP,
               "apart": W_APART}
MET_MM = 0.05   # a constraint within this many millimetres counts as met


class Placer:
    def __init__(self, items: list[Item], constraints: list[dict], width: float, height: float,
                 margin: float = 0.6, spacing: float = 0.5, net_weights: dict[str, float] | None = None,
                 seed: int = 1):
        self.items = items
        self.n = len(items)
        self.idx = {it.ref: i for i, it in enumerate(items)}
        self.width = width
        self.height = height
        self.margin = margin
        self.spacing = spacing
        self.rng = random.Random(seed)
        self.net_weights = net_weights or {}
        for it in items:
            x0, y0, x1, y1 = it.fp.courtyard
            h = spacing / 2
            it.rects = []
            it.pads = []
            for q in range(4):
                corners = [rot_point(x, y, q) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
                xs = [c[0] for c in corners]
                ys = [c[1] for c in corners]
                it.rects.append((min(xs) - h, min(ys) - h, max(xs) + h, max(ys) + h))
                pl = []
                for pad in it.fp.pads:
                    net = it.pad_nets.get(pad.number)
                    if net:
                        dx, dy = rot_point(pad.x, pad.y, q)
                        pl.append((net, dx, dy, pad.number))
                it.pads.append(pl)
            it.area = (x1 - x0 + spacing) * (y1 - y0 + spacing)
        # nets -> list of item indices
        self.nets: dict[str, list[int]] = {}
        for i, it in enumerate(items):
            for net in set(it.pad_nets.values()):
                self.nets.setdefault(net, []).append(i)
        self.nets = {n: v for n, v in self.nets.items()
                     if sum(1 for i in v for p in items[i].pads[0] if p[0] == n) >= 2}
        self.item_nets = [[n for n in self.nets if i in self.nets[n]] for i in range(self.n)]
        self.constraints = constraints
        self.item_cons = [[] for _ in range(self.n)]
        for k, c in enumerate(constraints):
            for r in self._con_refs(c):
                if r in self.idx:
                    self.item_cons[self.idx[r]].append(k)
        self.x = [0.0] * self.n
        self.y = [0.0] * self.n
        self.q = [0] * self.n

    @staticmethod
    def _con_refs(c: dict) -> list[str]:
        refs = list(c.get("refs", []))
        for key in ("ref", "to"):
            if key in c:
                refs.append(c[key])
        return refs

    # --- geometry -------------------------------------------------------------
    def rect(self, i: int):
        r = self.items[i].rects[self.q[i]]
        return (self.x[i] + r[0], self.y[i] + r[1], self.x[i] + r[2], self.y[i] + r[3])

    def center(self, i: int):
        x0, y0, x1, y1 = self.rect(i)
        return (x0 + x1) / 2, (y0 + y1) / 2

    def pad_positions(self, i: int, net: str | None = None, pin: str | None = None):
        out = []
        for n, dx, dy, num in self.items[i].pads[self.q[i]]:
            if net is not None and n != net:
                continue
            if pin is not None and num != pin:
                continue
            out.append((self.x[i] + dx, self.y[i] + dy))
        return out

    # --- cost terms -------------------------------------------------------------
    def net_cost(self, net: str) -> float:
        xs, ys = [], []
        for i in self.nets[net]:
            for n, dx, dy, _ in self.items[i].pads[self.q[i]]:
                if n == net:
                    xs.append(self.x[i] + dx)
                    ys.append(self.y[i] + dy)
        return self.net_weights.get(net, 1.0) * ((max(xs) - min(xs)) + (max(ys) - min(ys)))

    def overlap(self, i: int, j: int) -> float:
        a = self.rect(i)
        b = self.rect(j)
        w = min(a[2], b[2]) - max(a[0], b[0])
        h = min(a[3], b[3]) - max(a[1], b[1])
        if w <= 0 or h <= 0:
            return 0.0
        return w * h + 0.5 * min(w, h)

    def outside(self, i: int) -> float:
        x0, y0, x1, y1 = self.rect(i)
        m = self.margin - self.spacing / 2
        d = max(0.0, m - x0) + max(0.0, m - y0) + max(0.0, x1 - (self.width - m)) + \
            max(0.0, y1 - (self.height - m))
        return d * (1 + d)

    def violation(self, k: int) -> float:
        """How far constraint k is from being met, in millimetres (0 when met)."""
        c = self.constraints[k]
        t = c["type"]
        idx = self.idx
        if t == "near":
            a, b = idx.get(c["ref"]), idx.get(c["to"])
            if a is None or b is None:
                return 0.0
            pad = c.get("pad")
            target = self.pad_positions(b, pin=pad) if pad else self.pad_positions(b)
            pin_net = self.items[b].pad_nets.get(pad) if pad else None
            mine = self.pad_positions(a, net=pin_net) if pin_net else self.pad_positions(a)
            if not target or not mine:
                target = target or [self.center(b)]
                mine = mine or [self.center(a)]
            d = min(math.hypot(p[0] - q[0], p[1] - q[1]) for p in mine for q in target)
            return max(0.0, d - c.get("max_mm", 3.0))
        if t == "edge":
            a = idx.get(c["ref"])
            if a is None:
                return 0.0
            x0, y0, x1, y1 = self.rect(a)
            m = self.margin - self.spacing / 2
            dists = {"left": x0 - m, "right": (self.width - m) - x1, "top": y0 - m,
                     "bottom": (self.height - m) - y1}
            side = c.get("side", "any")
            d = min(dists.values()) if side == "any" else dists[side]
            return max(0.0, d - 0.3)  # within 0.3 mm of the keep-in line counts as "on the edge"
        if t in ("flow", "align", "group"):
            ids = [idx[r] for r in c["refs"] if r in idx]
            cs = [self.center(i) for i in ids]
            if len(cs) < 2:
                return 0.0
            axis = 0 if c.get("axis", "x") == "x" else 1
            if t == "flow":
                return sum(max(0.0, 1.0 - (q[axis] - p[axis])) for p, q in zip(cs, cs[1:]))
            if t == "align":
                other = 1 - axis
                mean = sum(p[other] for p in cs) / len(cs)
                return max(0.0, sum(abs(p[other] - mean) for p in cs) - 0.05)
            spread = max(math.hypot(p[0] - q[0], p[1] - q[1]) for p in cs for q in cs)
            return max(0.0, spread - c.get("max_mm", 10.0))
        if t == "apart":
            a, b = idx.get(c["ref"]), idx.get(c["to"])
            if a is None or b is None:
                return 0.0
            pa, pb = self.center(a), self.center(b)
            return max(0.0, c["min_mm"] - math.hypot(pa[0] - pb[0], pa[1] - pb[1]))
        return 0.0

    def con_cost(self, k: int) -> float:
        # Linear plus quadratic: a purely quadratic penalty has no pull at the boundary, so
        # wirelength always wins by a hair and parts settle just short of where they belong.
        v = self.violation(k)
        return CON_WEIGHTS.get(self.constraints[k]["type"], 1.0) * v * (1.0 + v)

    def local_cost(self, items: tuple[int, ...]) -> float:
        nets = set()
        cons = set()
        for i in items:
            nets.update(self.item_nets[i])
            cons.update(self.item_cons[i])
        cost = sum(self.net_cost(n) for n in nets)
        cost += sum(self.con_cost(k) for k in cons)
        counted = set()
        for i in items:
            cost += W_OUT * self.outside(i)
            for j in range(self.n):
                if j == i or (j, i) in counted:
                    continue
                counted.add((i, j))
                cost += W_OVERLAP * self.overlap(i, j)
        return cost

    def total_cost(self) -> dict:
        wl = sum(self.net_cost(n) for n in self.nets)
        ov = sum(self.overlap(i, j) for i in range(self.n) for j in range(i + 1, self.n))
        out = sum(self.outside(i) for i in range(self.n))
        cons = {k: self.con_cost(k) for k in range(len(self.constraints))}
        viol = {k: self.violation(k) for k in range(len(self.constraints))}
        return {"wirelength": wl, "overlap": ov, "outside": out, "constraints": sum(cons.values()),
                "total": wl + W_OVERLAP * ov + W_OUT * out + sum(cons.values()), "per_constraint": cons,
                "violation_mm": viol, "unmet": sum(1 for v in viol.values() if v > MET_MM)}

    # --- annealing ----------------------------------------------------------------
    def initial(self) -> None:
        order = sorted(range(self.n), key=lambda i: -self.items[i].area)
        cols = max(1, int(math.ceil(math.sqrt(self.n * self.width / max(self.height, 1)))))
        rows = max(1, int(math.ceil(self.n / cols)))
        for k, i in enumerate(order):
            cx = (k % cols + 0.5) * self.width / cols
            cy = (k // cols + 0.5) * self.height / rows
            self.x[i] = cx + self.rng.uniform(-0.5, 0.5)
            self.y[i] = cy + self.rng.uniform(-0.5, 0.5)
            self.q[i] = 0

    def snapshot(self) -> dict:
        return {it.ref: [round(self.x[i], 3), round(self.y[i], 3), self.q[i] * 90]
                for i, it in enumerate(self.items)}

    def anneal(self, iterations: int | None = None, frames: int = 48) -> list[dict]:
        n = self.n
        if n == 0:
            return []
        iterations = iterations or max(6000, min(60000, 2500 * n))
        diag = math.hypot(self.width, self.height)
        # Estimate starting temperature from random perturbations.
        deltas = []
        for _ in range(min(200, 20 * n)):
            i = self.rng.randrange(n)
            before = self.local_cost((i,))
            ox, oy = self.x[i], self.y[i]
            self.x[i] += self.rng.uniform(-diag / 4, diag / 4)
            self.y[i] += self.rng.uniform(-diag / 4, diag / 4)
            deltas.append(abs(self.local_cost((i,)) - before))
            self.x[i], self.y[i] = ox, oy
        t0 = max(1e-3, (sum(deltas) / len(deltas)) * 0.8)
        t_end = t0 * 2e-4
        alpha = (t_end / t0) ** (1.0 / iterations)
        t = t0
        recorded = [self.snapshot()]
        every = max(1, iterations // frames)
        for step in range(iterations):
            r = self.rng.random()
            frac = t / t0
            radius = max(0.25, diag * 0.5 * math.sqrt(frac))
            if r < 0.70 or n == 1:
                i = self.rng.randrange(n)
                before = self.local_cost((i,))
                ox, oy = self.x[i], self.y[i]
                self.x[i] += self.rng.uniform(-radius, radius)
                self.y[i] += self.rng.uniform(-radius, radius)
                delta = self.local_cost((i,)) - before
                if delta > 0 and self.rng.random() >= math.exp(-delta / t):
                    self.x[i], self.y[i] = ox, oy
            elif r < 0.85:
                i = self.rng.randrange(n)
                before = self.local_cost((i,))
                oq = self.q[i]
                self.q[i] = (oq + self.rng.choice((1, 2, 3))) % 4
                delta = self.local_cost((i,)) - before
                if delta > 0 and self.rng.random() >= math.exp(-delta / t):
                    self.q[i] = oq
            else:
                i, j = self.rng.sample(range(n), 2)
                before = self.local_cost((i, j))
                ci, cj = self.center(i), self.center(j)
                oxi, oyi, oxj, oyj = self.x[i], self.y[i], self.x[j], self.y[j]
                self.x[i] += cj[0] - ci[0]
                self.y[i] += cj[1] - ci[1]
                self.x[j] += ci[0] - cj[0]
                self.y[j] += ci[1] - cj[1]
                delta = self.local_cost((i, j)) - before
                if delta > 0 and self.rng.random() >= math.exp(-delta / t):
                    self.x[i], self.y[i], self.x[j], self.y[j] = oxi, oyi, oxj, oyj
            t *= alpha
            if step % every == every - 1:
                recorded.append(self.snapshot())
        return recorded

    def legalize(self, grid: float = 0.05, rounds: int = 400) -> list[str]:
        """Snap to grid and push apart any remaining overlaps; return remaining issues."""
        for i in range(self.n):
            self.x[i] = round(self.x[i] / grid) * grid
            self.y[i] = round(self.y[i] / grid) * grid
        for _ in range(rounds):
            moved = False
            for i in range(self.n):
                # clamp inside the board
                x0, y0, x1, y1 = self.rect(i)
                m = self.margin - self.spacing / 2
                dx = max(0.0, m - x0) - max(0.0, x1 - (self.width - m))
                dy = max(0.0, m - y0) - max(0.0, y1 - (self.height - m))
                if abs(dx) > 1e-9 or abs(dy) > 1e-9:
                    self.x[i] += dx
                    self.y[i] += dy
                    moved = True
                for j in range(i + 1, self.n):
                    a, b = self.rect(i), self.rect(j)
                    w = min(a[2], b[2]) - max(a[0], b[0])
                    h = min(a[3], b[3]) - max(a[1], b[1])
                    if w > 1e-6 and h > 1e-6:
                        ca, cb = self.center(i), self.center(j)
                        if w < h:
                            s = (w / 2 + grid) * (1 if ca[0] >= cb[0] else -1)
                            self.x[i] += s
                            self.x[j] -= s
                        else:
                            s = (h / 2 + grid) * (1 if ca[1] >= cb[1] else -1)
                            self.y[i] += s
                            self.y[j] -= s
                        moved = True
            if not moved:
                break
        for i in range(self.n):
            self.x[i] = round(self.x[i] / grid) * grid
            self.y[i] = round(self.y[i] / grid) * grid
        issues = []
        for i in range(self.n):
            if self.outside(i) > 1e-6:
                issues.append(f"{self.items[i].ref} does not fit inside the board")
            for j in range(i + 1, self.n):
                if self.overlap(i, j) > 1e-6:
                    issues.append(f"{self.items[i].ref} overlaps {self.items[j].ref}")
        return issues


def estimate_board(items: list[Item], spacing: float, margin: float, max_w: float | None,
                   max_h: float | None, utilization: float = 0.38) -> tuple[float, float]:
    area = sum((it.fp.width + spacing) * (it.fp.height + spacing) for it in items)
    need = area / utilization
    aspect = 1.4
    w = math.sqrt(need * aspect)
    h = need / w
    biggest = max((max(it.fp.width, it.fp.height) for it in items), default=5.0)
    w = max(w, biggest + 2 * margin + 1, 12.0)
    h = max(h, biggest + 2 * margin + 1, 10.0)
    if max_w:
        w = min(w, max_w)
    if max_h:
        h = min(h, max_h)
    return math.ceil(w), math.ceil(h)


def place(items: list[Item], constraints: list[dict], board: dict | None = None, seed: int = 1,
          spacing: float = 0.5, margin: float = 0.6, net_weights: dict[str, float] | None = None,
          attempts: int = 6, starts: int = 3) -> PlaceResult:
    """Anneal `starts` times per board size and keep the best legal result.

    Best means fewest unmet constraints, then lowest cost. If no start is legal
    the board grows by 15% (within the limits) and it tries again.
    """
    board = board or {}
    max_w, max_h = board.get("max_width_mm"), board.get("max_height_mm")
    margin = board.get("margin_mm", margin)
    w, h = estimate_board(items, spacing, margin, max_w, max_h)
    best = None
    for attempt in range(attempts):
        for s in range(starts):
            p = Placer(items, constraints, w, h, margin, spacing, net_weights, seed + 1000 * attempt + s)
            p.initial()
            frames = p.anneal()
            issues = p.legalize()
            frames.append(p.snapshot())
            cost = p.total_cost()
            result = PlaceResult(w, h, {it.ref: (p.x[i], p.y[i], p.q[i] * 90) for i, it in enumerate(items)},
                                 [{"board": [w, h], "positions": f} for f in frames], cost, not issues, issues)
            key = (not result.legal, cost["unmet"], cost["total"])
            if best is None or key < best[0]:
                best = (key, result)
        if best[1].legal:
            return best[1]
        grow_w = not max_w or w * 1.15 <= max_w
        grow_h = not max_h or h * 1.15 <= max_h
        if not grow_w and not grow_h:
            break
        w = math.ceil(w * 1.15) if grow_w else w
        h = math.ceil(h * 1.15) if grow_h else h
        best = None
    return best[1] if best else None
