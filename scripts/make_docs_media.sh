#!/usr/bin/env bash
# Regenerate docs/board.png and docs/replay.gif from a demo design.
# Needs: KiCad (pcbnew, kicad-cli), rsvg-convert, ImageMagick, Xvfb.
set -euo pipefail
DESIGN=${1:?usage: make_docs_media.sh <design-dir>}
OUT=$(cd "$(dirname "$0")/.." && pwd)/docs
BOARD=$(ls "$DESIGN"/layout/*.kicad_pcb | head -1)
TMP=$(mktemp -d)
mkdir -p "$OUT"

# Static render of the routed board without the ground pours, so tracks are visible.
KPY=$(python3 -c "from pcbagent.kicad.tools import find_kicad_python as f; print(f())")
"$KPY" - "$BOARD" "$TMP/nozones.kicad_pcb" <<'PY'
import sys, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
for z in [b.GetArea(i) for i in range(b.GetAreaCount())]:
    b.Delete(z)
pcbnew.SaveBoard(sys.argv[2], b)
PY
kicad-cli pcb export svg --layers F.Cu,B.Cu,F.Silkscreen,Edge.Cuts --page-size-mode 2 \
  --exclude-drawing-sheet -o "$TMP/board.svg" "$TMP/nozones.kicad_pcb" >/dev/null
rsvg-convert -z 8 -b '#0b1220' "$TMP/board.svg" -o "$OUT/board.png"

# Replay in the real PCB editor under a virtual display, captured as a GIF.
python3 -m pcbagent install-plugin >/dev/null
cat > "$TMP/capture.sh" <<EOS
#!/usr/bin/env bash
export PCBAGENT_REPLAY="$DESIGN/layout/layout_trace.json" PCBAGENT_REPLAY_FRAME_MS=120 PCBAGENT_REPLAY_NET_MS=700
pcbnew "$BOARD" >/dev/null 2>&1 &
PID=\$!
sleep 4
for i in \$(seq -w 0 59); do import -window root "$TMP/f_\$i.png" 2>/dev/null; sleep 0.25; done
kill \$PID; sleep 1; kill -9 \$PID 2>/dev/null || true
EOS
chmod +x "$TMP/capture.sh"
xvfb-run -a -s "-screen 0 1600x1000x24" "$TMP/capture.sh"
# Crop to the drawing canvas, shrink, and keep a long hold on the final frame.
convert -delay 12 -loop 0 "$TMP"/f_*.png -crop 1025x560+35+102 +repage -resize 760x \
  -layers Optimize -colors 128 "$OUT/replay.gif"
echo "wrote $OUT/board.png and $OUT/replay.gif"
