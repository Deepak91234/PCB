"""Drive Pi's interactive TUI through a pseudo-terminal (end-to-end test helper).

Types a command into the real TUI, waits until a condition file appears or a
timeout passes, then quits. Usage:

    python3 tui_driver.py <done-file> <timeout-s> -- pi [args...]

stdin text to type is taken from $TUI_INPUT. Exit code 0 if <done-file>
appeared in time.
"""

import os
import pty
import select
import signal
import struct
import sys
import time
import fcntl
import termios


def main() -> int:
    done_file, timeout = sys.argv[1], float(sys.argv[2])
    cmd = sys.argv[sys.argv.index("--") + 1:]
    text = os.environ.get("TUI_INPUT", "")
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.execvp(cmd[0], cmd)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
    out = bytearray()
    start = time.time()
    typed = False
    ok = False
    while time.time() - start < timeout:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if not typed and time.time() - start > 4:
            for ch in text:
                os.write(fd, ch.encode())
                time.sleep(0.01)
            time.sleep(0.3)
            os.write(fd, b"\r")
            typed = True
        if typed and os.path.exists(done_file):
            ok = True
            time.sleep(2)
            break
    for seq in (b"\x03", b"\x03", b"\x04"):
        try:
            os.write(fd, seq)
        except OSError:
            break
        time.sleep(0.3)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            break
        for _ in range(30):
            if os.waitpid(pid, os.WNOHANG) != (0, 0):
                break
            time.sleep(0.1)
        else:
            continue
        break
    sys.stdout.buffer.write(bytes(out[-4000:]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
