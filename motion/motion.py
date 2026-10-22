#!/usr/bin/env python3
"""Video motion detector for the virtual ONVIF cameras.

Reads a tiny greyscale copy of a go2rtc stream (decoded on the Intel GPU), compares each
frame with a slowly-updated background, and tells the onvif-server motion API when motion
starts and stops. UniFi Protect then gets ONVIF motion events.

Usage: motion.py <go2rtc stream> <onvif camera name>     e.g. motion.py cam2 Camera2
Tuning via environment (see /etc/default/onvif-motion).
"""
import os
import subprocess
import sys
import time
import urllib.request

STREAM, CAMERA = sys.argv[1], sys.argv[2]
W, H, FPS = 160, 90, 4


def env(name, default):
    """Per-camera setting (e.g. ON_PERCENT_cam2) overrides the shared one (ON_PERCENT)."""
    return os.environ.get(f"{name}_{STREAM}", os.environ.get(name, default))


PIXEL_DIFF = int(env("PIXEL_DIFF", "25"))            # 0-255 change for a pixel to count
ON_PERCENT = float(env("ON_PERCENT", "1.0"))         # % of picture changed = motion
GLOBAL_PERCENT = float(env("GLOBAL_PERCENT", "45"))  # above this = lighting/IR switch, ignore
ON_FRAMES = int(env("ON_FRAMES", "2"))               # consecutive motion frames to start
HOLD_SECONDS = float(env("HOLD_SECONDS", "10"))      # quiet time before motion stops
ALPHA = float(env("BG_ALPHA", "0.08"))               # background adaptation speed
CELL = int(env("CELL", "8"))                         # picture is split into CELL x CELL pixel blocks
DENSE = float(env("DENSE", "0.30"))                  # block counts as solid change if this share changed
MIN_DENSE = int(env("MIN_DENSE", "1"))               # solid blocks needed for motion (light flicker is scattered)
# Areas to ignore, as fractions of the picture: "x1,y1,x2,y2;x1,y1,x2,y2" (0,0 = top left)
MASK = env("MASK", "")
SNAP_DIR = os.environ.get("STATE_DIRECTORY", "")      # where trigger snapshots go (systemd StateDirectory)
SNAP_KEEP = 40
API = os.environ.get("MOTION_API", "http://127.0.0.1:8099")
TOKEN = open(os.environ.get("TOKEN_FILE", "/opt/onvif-server/motion.token")).read().strip()

FRAME = W * H  # 8-bit greyscale
CMD = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
       "-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi",
       "-i", f"rtsp://127.0.0.1:8555/{STREAM}", "-an",
       "-vf", f"fps={FPS},hwdownload,format=nv12,scale={W}:{H},format=gray",
       "-f", "rawvideo", "pipe:1"]


def log(msg):
    print(f"[{CAMERA}] {msg}", flush=True)


def send(state):
    req = urllib.request.Request(f"{API}/motion/{CAMERA}?state={state}", method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=5).read()
            return True
        except Exception as e:
            log(f"motion API {state} failed ({e}), retry {attempt + 1}")
            time.sleep(1)
    return False


def watched_pixels():
    """Indexes of the pixels outside the masked areas."""
    boxes = []
    for part in filter(None, MASK.split(";")):
        x1, y1, x2, y2 = (float(v) for v in part.split(","))
        boxes.append((x1 * W, y1 * H, x2 * W, y2 * H))
    return [y * W + x for y in range(H) for x in range(W)
            if not any(bx1 <= x + 0.5 < bx2 and by1 <= y + 0.5 < by2 for bx1, by1, bx2, by2 in boxes)]


WATCH = watched_pixels()
CELLS_X = (W + CELL - 1) // CELL
CELL_OF = [(i // W) // CELL * CELLS_X + (i % W) // CELL for i in range(W * H)]
CELL_SIZE = [0] * (CELLS_X * ((H + CELL - 1) // CELL))
for _i in WATCH:
    CELL_SIZE[CELL_OF[_i]] += 1


def snapshot(frame, mask):
    """Save the trigger frame and its change mask side by side as a PGM, keeping the newest SNAP_KEEP."""
    if not SNAP_DIR:
        return
    try:
        rows = b"".join(frame[r * W:(r + 1) * W] + mask[r * W:(r + 1) * W] for r in range(H))
        name = os.path.join(SNAP_DIR, time.strftime("%Y%m%d-%H%M%S") + ".pgm")
        with open(name, "wb") as f:
            f.write(b"P5 %d %d 255\n" % (W * 2, H) + rows)
        old = sorted(n for n in os.listdir(SNAP_DIR) if n.endswith(".pgm"))[:-SNAP_KEEP]
        for n in old:
            os.remove(os.path.join(SNAP_DIR, n))
    except OSError as e:
        log(f"snapshot failed: {e}")


def run():
    proc = subprocess.Popen(CMD, stdout=subprocess.PIPE, bufsize=FRAME * 4)
    bg = None
    active, streak, last_motion = False, 0, 0.0
    peak, peak_since, ignored = 0.0, time.time(), 0
    total = len(WATCH)
    try:
        while True:
            buf = proc.stdout.read(FRAME)
            if len(buf) < FRAME:
                raise RuntimeError("stream ended")
            y = buf
            if bg is None:
                bg = [float(v) for v in y]
                continue
            # Allow for the whole picture getting brighter or darker (clouds, camera exposure).
            shift = sum(bg[i] for i in WATCH) / total - sum(y[i] for i in WATCH) / total
            changed = 0
            cells = [0] * len(CELL_SIZE)
            hits = []
            for i in WATCH:
                v = y[i]
                b = bg[i]
                if abs(v + shift - b) > PIXEL_DIFF:
                    changed += 1
                    cells[CELL_OF[i]] += 1
                    hits.append(i)
                bg[i] = b + (v - b) * ALPHA
            pct = changed * 100.0 / total
            dense = sum(1 for c, n in enumerate(cells) if CELL_SIZE[c] >= 20 and n >= DENSE * CELL_SIZE[c])
            now = time.time()
            peak = max(peak, pct)
            if pct > GLOBAL_PERCENT:
                # Whole picture changed: lights, IR night-mode switch or camera adjusting. Re-baseline.
                bg = [float(v) for v in y]
                streak = 0
            elif pct >= ON_PERCENT and dense >= MIN_DENSE:
                streak += 1
                if streak >= ON_FRAMES:
                    last_motion = now
                    if not active:
                        active = True
                        log(f"motion START ({pct:.1f}% changed, {dense} solid blocks, brightness shift {shift:+.0f})")
                        mask = bytearray(FRAME)
                        for i in hits:
                            mask[i] = 255
                        snapshot(y, bytes(mask))
                        send("on")
            elif pct >= ON_PERCENT:
                ignored += 1  # enough change but only scattered speckle: light flicker, noise
                streak = 0
            else:
                streak = 0
            if active and now - last_motion > HOLD_SECONDS:
                active = False
                log("motion STOP")
                send("off")
            if now - peak_since >= 300:
                log(f"5-min peak change {peak:.1f}%, {ignored} scattered-change frames ignored")
                peak, peak_since, ignored = 0.0, now, 0
    finally:
        proc.kill()
        if active:
            send("off")


if __name__ == "__main__":
    log(f"starting on stream {STREAM}: diff>{PIXEL_DIFF}, on>={ON_PERCENT}%, ignore>{GLOBAL_PERCENT}%, hold {HOLD_SECONDS}s, "
        f"watching {len(WATCH) * 100 // (W * H)}% of picture")
    while True:
        try:
            run()
        except Exception as e:
            log(f"restarting in 10s: {e}")
            time.sleep(10)
