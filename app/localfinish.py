"""Built-in finisher: renders the designed template (same layout as the Shotstack version)
locally with animations, so finished videos are free and carry no watermark.

Each layout element becomes a pre-drawn sprite; per frame we only work out its position and
opacity (slides, fades, text entrances) and alpha-blend it. Music is mixed in with FFmpeg.
"""
import math
import os
import subprocess
import threading
import urllib.request
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from . import finishing as F

FPS = 30
TRANSITION = 0.45          # seconds for fade in/out transitions
MUSIC_DIR = Path(os.environ.get("MUSIC_CACHE", "/tmp/coverbrand-music"))
_music_lock = threading.Lock()


# ------------------------------------------------------------------ easing
def ease_out_cubic(x):
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


def ease_out_quad(x):
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 2


# ------------------------------------------------------------------ sprites
def _hex(c, a=255):
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), a)


def _to_bgra_float(im: Image.Image):
    a = np.asarray(im.convert("RGBA")).astype(np.float32) / 255.0
    rgb = a[..., :3][..., ::-1] * a[..., 3:4]          # premultiplied BGR
    return np.concatenate([rgb, a[..., 3:4]], axis=2)


def _text_sprite(e):
    w, h = int(e["w"]), int(e["h"])
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    fam, size, sp = e["family"], e["size"], e.get("spacing", 0)
    fnt = F._font(fam, size)
    lines = F.wrap(e["text"], fam, size, w * 0.97, sp)[: max(1, e.get("max_lines", 3))]
    lh = size * e.get("line_h", 1.12)
    th = lh * len(lines)
    y = {"top": 0, "middle": (h - th) / 2, "bottom": h - th}[e.get("valign", "middle")]
    asc = fnt.getbbox("Hg")[1]
    for ln in lines:
        lw = F.text_w(ln, fam, size, sp)
        x = {"left": 0, "center": (w - lw) / 2, "right": w - lw}[e.get("align", "left")]
        if sp:
            cx = x
            for ch in ln:
                d.text((cx, y - asc + (lh - size) / 2), ch, font=fnt, fill=_hex(e["color"]))
                cx += F.text_w(ch, fam, size) + sp
        else:
            d.text((x, y - asc + (lh - size) / 2), ln, font=fnt, fill=_hex(e["color"]))
        y += lh
    return im


def _sprite(e, logo: Image.Image | None):
    k = e["kind"]
    w, h = max(1, int(e["w"])), max(1, int(e["h"]))
    if k == "rect":
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(im).rounded_rectangle([0, 0, w - 1, h - 1], radius=int(e.get("radius") or 0),
                                             fill=_hex(e["color"]))
        return im
    if k == "circle":
        im = Image.new("RGBA", (w * 2, h * 2), (0, 0, 0, 0))   # 2x for smooth edges
        ImageDraw.Draw(im).ellipse([0, 0, w * 2 - 1, h * 2 - 1], fill=_hex(e["color"]))
        return im.resize((w, h), Image.LANCZOS)
    if k == "image":
        if logo is None:
            return None
        lg = logo.convert("RGBA").copy()
        lg.thumbnail((w, h), Image.LANCZOS)
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        im.alpha_composite(lg, ((w - lg.width) // 2, (h - lg.height) // 2))
        return im
    if k == "text":
        return _text_sprite(e)
    return None


# ------------------------------------------------------------------ animation state
def _state(e, t):
    """(dx, dy, opacity) of element e at time t, or None when not visible."""
    u = t - e["start"]
    if u < 0 or u >= e["length"]:
        return None
    op = e.get("opacity", 1.0) if e.get("opacity") is not None else 1.0
    dx = dy = 0.0
    if e.get("fade_in"):
        op *= ease_out_quad(u / e["fade_in"])
    if e.get("transition_in") == "fade":
        op *= min(1.0, u / TRANSITION)
    if e.get("fade_out", e["kind"] != "video") and e["kind"] != "video":
        op *= min(1.0, (e["length"] - u) / TRANSITION)
    sl = e.get("slide")
    if sl:
        axis, delta, dur = sl
        rem = (1 - ease_out_cubic(u / dur)) * delta
        if axis == "x":
            dx = rem
        else:
            dy = rem
    an = e.get("anim")
    if an:
        dur = an.get("duration", 0.6)
        p = ease_out_cubic(u / dur)
        op *= min(1.0, u / dur) if an.get("preset") == "fadeIn" else p
        if an.get("preset") in ("ascend", "slideIn", "shift"):
            dy += (1 - p) * 0.35 * e.get("size", 40)
    return dx, dy, max(0.0, min(1.0, op))


def _blend(canvas, spr, x, y, op):
    """canvas: float32 BGR (0..1); spr: premultiplied BGRA float."""
    H, W = canvas.shape[:2]
    h, w = spr.shape[:2]
    x, y = int(round(x)), int(round(y))
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0 or op <= 0.003:
        return
    s = spr[y0 - y:y1 - y, x0 - x:x1 - x]
    a = s[..., 3:4] * op
    canvas[y0:y1, x0:x1] = canvas[y0:y1, x0:x1] * (1 - a) + s[..., :3] * op


# ------------------------------------------------------------------ video element
class ClipReader:
    """Sequential reader of the branded cover clip, mapped to output time."""

    def __init__(self, path):
        self.cap = cv2.VideoCapture(str(path))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.n = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        self.idx = -1
        self.frame = None

    def at(self, u):
        target = min(self.n - 1, max(0, int(u * self.fps)))
        while self.idx < target:
            ok, f = self.cap.read()
            if not ok:
                break
            self.frame, self.idx = f, self.idx + 1
        return self.frame

    def close(self):
        self.cap.release()


def _video_layer(frame, e, u, W, H):
    """Return (premultiplied BGRA float sprite, x, y) for a video element."""
    if frame is None:
        return None
    w, h = int(e["w"]), int(e["h"])
    fh, fw = frame.shape[:2]
    if e.get("fit") == "crop":
        z = 1.0
        if e.get("effect") == "zoomInSlow":
            z = 1.0 + 0.12 * min(1.0, u / max(0.1, e["length"]))
        sc = max(w / fw, h / fh) * z
    else:
        sc = min(w / fw, h / fh)
    if e.get("filter") == "blur":
        small = cv2.resize(frame, (max(8, fw // 6), max(8, fh // 6)), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (0, 0), 3.5)
        frame = cv2.resize(small, (fw, fh), interpolation=cv2.INTER_LINEAR)
    nw, nh = max(1, int(fw * sc)), max(1, int(fh * sc))
    img = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_LINEAR)
    x = e["x"] + (w - nw) / 2
    y = e["y"] + (h - nh) / 2
    if e.get("fit") == "crop":   # clip to the element box
        cx0, cy0 = int(max(0, e["x"] - x)), int(max(0, e["y"] - y))
        img = img[cy0:cy0 + h, cx0:cx0 + w]
        x, y = max(x, e["x"]), max(y, e["y"])
    spr = np.concatenate([img.astype(np.float32) / 255.0, np.ones(img.shape[:2] + (1,), np.float32)], axis=2)
    return spr, x, y


# ------------------------------------------------------------------ music
def music_file(key):
    url = F.MUSIC.get(key, (None, None))[1]
    if not url:
        return None
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    dest = MUSIC_DIR / f"{key}.mp3"
    with _music_lock:
        if not dest.exists():
            try:
                tmp = dest.with_suffix(".dl")
                with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as out:
                    out.write(r.read())
                tmp.replace(dest)
            except Exception:
                return None
    return dest


# ------------------------------------------------------------------ render
def render(project, vendor, ratio, clip_path: Path, logo_path, out_path: Path, fin=None, progress=None,
           clip_has_audio=True):
    f = {**F.DEFAULT_FINISH, **(fin or project.get("finish") or {})}
    W, H = F.SIZES[ratio]
    clip_dur = project["frames"] / float(project["fps"] or 25)
    logo = None
    if logo_path and Path(logo_path).exists():
        try:
            from .decal import _prep_logo
            logo = _prep_logo(logo_path, True)
        except Exception:
            logo = None
    els, total = F.BUILDERS.get(f["preset"], F.corporate)(
        vendor, ratio, clip_dur, project["width"] / project["height"], f, "logo" if logo else None)
    sprites = {}
    for i, e in enumerate(els):
        if e["kind"] != "video":
            im = _sprite(e, logo)
            if im is not None:
                sprites[i] = _to_bgra_float(im)
    bg = np.empty((H, W, 3), np.float32)
    bg[:] = np.array(_hex(f["dark"])[:3][::-1], np.float32) / 255.0
    readers = {i: ClipReader(clip_path) for i, e in enumerate(els) if e["kind"] == "video"}

    music = music_file(f["music"])
    tmp = out_path.with_suffix(".part.mp4")
    t1 = next((e["start"] for e in els if e["kind"] == "video"), 3.2)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-"]
    filters, n_in = [], 1
    if clip_has_audio:
        cmd += ["-i", str(clip_path)]
        d = int(t1 * 1000)
        filters.append(f"[{n_in}:a]volume={0.35 if music else 1.0},adelay={d}|{d}[ca]")
        n_in += 1
    if music:
        cmd += ["-i", str(music)]
        mv = float(f.get("music_volume", 0.6))
        filters.append(f"[{n_in}:a]atrim=0:{total:.2f},volume={mv},afade=t=in:d=1.2,"
                       f"afade=t=out:st={max(0.0, total - 2.0):.2f}:d=2.0[ma]")
    if clip_has_audio and music:
        filters.append("[ca][ma]amix=inputs=2:duration=longest:normalize=0[a]")
    elif clip_has_audio:
        filters.append("[ca]anull[a]")
    elif music:
        filters.append("[ma]anull[a]")
    cmd += ["-map", "0:v"]
    if filters:
        cmd += ["-filter_complex", ";".join(filters), "-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-t", f"{total:.3f}", "-movflags", "+faststart", str(tmp)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    nfr = int(math.ceil(total * FPS))
    try:
        for fi in range(nfr):
            t = fi / FPS
            canvas = bg.copy()
            for i, e in enumerate(els):
                st = _state(e, t)
                if st is None:
                    continue
                dx, dy, op = st
                if e["kind"] == "video":
                    u = t - e["start"]
                    lay = _video_layer(readers[i].at(u), e, u, W, H)
                    if lay:
                        spr, x, y = lay
                        _blend(canvas, spr, x + dx, y + dy, op)
                elif i in sprites:
                    _blend(canvas, sprites[i], e["x"] + dx, e["y"] + dy, op)
            p.stdin.write((np.clip(canvas, 0, 1) * 255).astype(np.uint8).tobytes())
            if progress and fi % 15 == 0:
                progress(fi / nfr)
    finally:
        for r in readers.values():
            r.close()
        p.stdin.close()
        err = p.stderr.read().decode(errors="ignore")
        p.wait()
    if p.returncode != 0:
        raise RuntimeError("Encoding failed: " + err[-400:])
    tmp.replace(out_path)
    return out_path
