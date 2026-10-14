"""PCBAgent replay: watch placement and routing happen in KiCad's PCB editor.

Install with ``pcbagent install-plugin`` (or copy this file into KiCad's
scripting/plugins folder). Open a PCBAgent board and click
"PCBAgent: replay layout", or start KiCad with PCBAGENT_REPLAY=<trace.json>
(``pcbagent replay <design>`` does this) and the replay starts by itself once
the board is loaded.

The replay reads ``layout_trace.json`` next to the board: annealing frames
move the footprints, then tracks and vias appear net by net, then ground
pours are refilled. The final state equals the saved board.
"""

import json
import os

import pcbnew

try:
    import wx
except ImportError:  # pragma: no cover - always present inside KiCad
    wx = None

ENV = "PCBAGENT_REPLAY"
FRAME_MS = int(os.environ.get("PCBAGENT_REPLAY_FRAME_MS", "80"))
NET_MS = int(os.environ.get("PCBAGENT_REPLAY_NET_MS", "350"))


def _mm(v):
    return pcbnew.FromMM(float(v))


def _vec(x, y):
    return pcbnew.VECTOR2I(_mm(x), _mm(y))


def find_trace(board):
    env = os.environ.get(ENV)
    if env and os.path.exists(env):
        return env
    fn = board.GetFileName() if board else ""
    if fn:
        cand = os.path.join(os.path.dirname(fn), "layout_trace.json")
        if os.path.exists(cand):
            return cand
    return None


class Replayer:
    def __init__(self, board, trace, on_done=None):
        self.board = board
        self.trace = trace
        self.on_done = on_done
        self.footprints = {fp.GetReference(): fp for fp in board.GetFootprints()}
        self.steps = []
        self._build_steps()

    def _build_steps(self):
        self.steps.append((0, self._clear))
        for frame in self.trace.get("placement_frames", []):
            self.steps.append((FRAME_MS, lambda f=frame: self._apply_frame(f)))
        for group in self.trace.get("routing", []):
            self.steps.append((NET_MS, lambda g=group: self._add_net(g)))
        self.steps.append((NET_MS, self._finish))

    def _zones(self):
        return [self.board.GetArea(i) for i in range(self.board.GetAreaCount())]

    def _clear(self):
        for t in list(self.board.GetTracks()):
            try:
                self.board.Delete(t)
            except Exception:  # noqa: BLE001 - KiCad version differences
                self.board.Remove(t)
        for z in self._zones():
            try:
                z.UnFill()
            except Exception:  # noqa: BLE001
                pass

    def _apply_frame(self, frame):
        for ref, (x, y, rot) in frame.items():
            fp = self.footprints.get(ref)
            if fp is None:
                continue
            fp.SetPosition(_vec(x, y))
            fp.SetOrientationDegrees(float(rot))

    def _add_net(self, group):
        net = self.board.FindNet(group["net"])
        if net is None:
            return
        for x1, y1, x2, y2, w, layer in group.get("tracks", []):
            t = pcbnew.PCB_TRACK(self.board)
            t.SetStart(_vec(x1, y1))
            t.SetEnd(_vec(x2, y2))
            t.SetWidth(_mm(w))
            t.SetLayer(self.board.GetLayerID(layer))
            t.SetNet(net)
            self.board.Add(t)
        for x, y, d, drill in group.get("vias", []):
            v = pcbnew.PCB_VIA(self.board)
            v.SetPosition(_vec(x, y))
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            v.SetDrill(_mm(drill))
            try:
                v.SetWidth(_mm(d))
            except Exception:  # noqa: BLE001
                v.SetWidth(_mm(d), pcbnew.F_Cu)
            v.SetNet(net)
            self.board.Add(v)

    def _finish(self):
        try:
            pcbnew.ZONE_FILLER(self.board).Fill(self.board.Zones())
        except Exception:  # noqa: BLE001
            pass
        if self.on_done:
            self.on_done()

    def run_blocking(self):
        """Apply every step at once (used by tests and headless runs)."""
        for _delay, fn in self.steps:
            fn()

    def run_animated(self):
        self._next(0)

    def _next(self, i):
        if i >= len(self.steps):
            return
        delay, fn = self.steps[i]
        fn()
        pcbnew.Refresh()
        nxt = self.steps[i + 1][0] if i + 1 < len(self.steps) else 0
        wx.CallLater(max(1, nxt), self._next, i + 1)


def start(board=None, quiet=False):
    board = board or pcbnew.GetBoard()
    path = find_trace(board)
    if not path:
        if not quiet and wx:
            wx.MessageBox("No layout_trace.json next to this board.\nRun `pcbagent layout` first.",
                          "PCBAgent replay")
        return False
    with open(path) as fh:
        trace = json.load(fh)
    Replayer(board, trace).run_animated()
    return True


class PCBAgentReplay(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "PCBAgent: replay layout"
        self.category = "PCBAgent"
        self.description = "Replay PCBAgent's placement and routing on this board"
        self.show_toolbar_button = True
        icon = os.path.join(os.path.dirname(__file__), "pcbagent_replay.png")
        if os.path.exists(icon):
            self.icon_file_name = icon

    def Run(self):
        start()


def _auto_start(tries=[0]):  # noqa: B006 - deliberate mutable counter
    board = pcbnew.GetBoard()
    if board is not None and len(board.GetFootprints()) > 0:
        start(board, quiet=True)
        return
    tries[0] += 1
    if tries[0] < 120:
        wx.CallLater(500, _auto_start)


if not os.environ.get("PCBAGENT_REPLAY_NO_REGISTER"):
    PCBAgentReplay().register()

if os.environ.get(ENV) and wx is not None and not os.environ.get("PCBAGENT_REPLAY_STARTED"):
    os.environ["PCBAGENT_REPLAY_STARTED"] = "1"
    wx.CallLater(2000, _auto_start)
