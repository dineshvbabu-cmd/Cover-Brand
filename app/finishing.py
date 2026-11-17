"""Designer finishing via the Shotstack Edit API.

CoverBrand renders the tracked, branded cover clip itself (Shotstack cannot corner-pin
track). This module wraps that clip in a designed template - intro, the clip with
lower-thirds, and a contact end card - with music, in 16:9 / 9:16 / 1:1.

Layouts are described as simple pixel boxes so the exact same description can be
(a) converted to Shotstack JSON and (b) drawn locally by mock_frame() for previews/tests.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = Path(__file__).parent / "static" / "fonts"
FONT_FILES = {"CB Display": "CBDisplay.ttf", "CB Semi": "CBSemi.ttf", "CB Text": "CBText.ttf"}

SIZES = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
SLUG = {"16:9": "16x9", "9:16": "9x16", "1:1": "1x1"}
RATIO_OF = {v: k for k, v in SLUG.items()}

# Royalty-free tracks (Unminus licence: commercial use, no attribution) hosted by Shotstack.
MUSIC = {
    "berlin": ("Berlin - modern electronic", "https://shotstack-assets.s3.amazonaws.com/music/unminus/berlin.mp3"),
    "lit": ("Lit - upbeat", "https://shotstack-assets.s3.amazonaws.com/music/unminus/lit.mp3"),
    "ambisax": ("Ambisax - smooth corporate", "https://shotstack-assets.s3.amazonaws.com/music/unminus/ambisax.mp3"),
    "palmtrees": ("Palm Trees - relaxed", "https://shotstack-assets.s3.amazonaws.com/music/unminus/palmtrees.mp3"),
    "none": ("No music (keep original audio)", None),
}

PRESETS = {"corporate": "Corporate reveal"}

DEFAULT_FINISH = {
    "enabled": False,          # run automatically after each vendor's clip is rendered
    "preset": "corporate",
    "ratios": ["16:9", "9:16", "1:1"],
    "music": "berlin",
    "music_volume": 0.6,
    "accent": "#e4572e",
    "dark": "#0d1b2a",
    "kicker": "PREPARED FOR",
    "headline": "Container Cover Concept",
    "caption": "Your brand on our container cover",
    "cta": "Let's talk",
    "maker": "",               # e.g. "Concept by Atlantas"
}

API_KEY = os.environ.get("SHOTSTACK_API_KEY", "").strip()
API_ENV = os.environ.get("SHOTSTACK_ENV", "stage").strip() or "stage"   # stage = free sandbox (watermark), v1 = production
API_BASE = os.environ.get("SHOTSTACK_API_BASE") or f"https://api.shotstack.io/edit/{API_ENV}"


def configured() -> bool:
    return bool(API_KEY)


def env_label() -> str:
    return "production" if API_ENV == "v1" else "sandbox (watermarked test renders)"


# ------------------------------------------------------------------ text measuring
_font_cache = {}


def _font(family, size):
    key = (family, int(size))
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(FONT_DIR / FONT_FILES[family]), int(size))
    return _font_cache[key]


_meas = ImageDraw.Draw(Image.new("RGB", (8, 8)))


def text_w(text, family, size, spacing=0):
    l, t, r, b = _meas.textbbox((0, 0), text, font=_font(family, size))
    return (r - l) + spacing * max(0, len(text) - 1)


def wrap(text, family, size, width, spacing=0):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if text_w(trial, family, size, spacing) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_size(text, family, width, height, max_size, max_lines=2, line_h=1.12, spacing=0, min_size=18):
    """Largest font size where text wraps into <= max_lines inside the box."""
    size = int(max_size)
    while size > min_size:
        lines = wrap(text, family, size, width * 0.97, spacing)
        if len(lines) <= max_lines and all(text_w(l, family, size, spacing) <= width * 0.97 for l in lines) \
                and len(lines) * size * line_h <= height:
            return size
        size -= 2
    return min_size


def initials(name):
    parts = [p for p in name.replace("&", " ").split() if p[:1].isalnum()]
    stop = {"pvt", "ltd", "llc", "inc", "co", "the", "and", "of", "limited", "private", "gmbh", "pte", "as"}
    parts = [p for p in parts if p.lower().strip(".") not in stop] or parts
    return "".join(p[0] for p in parts[:2]).upper() or "?"


# ------------------------------------------------------------------ layout model
def E(kind, x, y, w, h, start, length, **kw):
    return {"kind": kind, "x": x, "y": y, "w": w, "h": h, "start": round(start, 3), "length": round(length, 3), **kw}


def _text(text, x, y, w, h, start, length, family, size, color, align="left", valign="middle",
          spacing=0, upper=False, anim=None, fade_out=True, max_lines=2, line_h=1.12, slide=None):
    if upper:
        text = text.upper()
    return E("text", x, y, w, h, start, length, text=text, family=family, size=size, color=color,
             align=align, valign=valign, spacing=spacing, anim=anim, fade_out=fade_out,
             max_lines=max_lines, line_h=line_h, slide=slide)


def _rect(x, y, w, h, start, length, color, opacity=1.0, radius=0, slide=None, fade_in=None, fade_out=True):
    return E("rect", x, y, w, h, start, length, color=color, opacity=opacity, radius=radius,
             slide=slide, fade_in=fade_in, fade_out=fade_out)


def _logo_block(name, logo_url, x, y, w, h, start, length, f, fade_in=0.5, slide=None, pad=0.1):
    """White rounded card with the logo, or an accent monogram when there is no logo."""
    out = []
    if logo_url:
        out.append(_rect(x, y, w, h, start, length, "#ffffff", 1, radius=int(min(w, h) * 0.08),
                         fade_in=fade_in, slide=slide))
        px, py = int(w * pad), int(h * pad)
        out.append(E("image", x + px, y + py, w - 2 * px, h - 2 * py, start + 0.15, length - 0.15,
                     src=logo_url, fade_in=fade_in, slide=slide))
    else:
        d = int(min(w, h) * 0.9)
        cx, cy = x + (w - d) // 2, y + (h - d) // 2
        out.append(E("circle", cx, cy, d, d, start, length, color=f["accent"], fade_in=fade_in, slide=slide))
        ini = initials(name)
        out.append(_text(ini, cx, cy, d, d, start + 0.15, length - 0.15, "CB Display",
                         fit_size(ini, "CB Display", d * 0.7, d * 0.6, d * 0.45, 1), "#ffffff", "center"))
    return out


def contact_lines(v):
    return [s for s in (v.get("contact"), v.get("phone"), v.get("email"), v.get("website")) if s and str(s).strip()]


def corporate(vendor: dict, ratio: str, clip_dur: float, clip_aspect: float, f: dict, logo_url):
    """Return (elements, total_duration). Elements are drawn bottom-to-top in list order."""
    W, H = SIZES[ratio]
    name = (vendor.get("name") or "Vendor").strip()
    NAME = name.upper()
    acc, dark, panel = f["accent"], f["dark"], "#132a43"
    T1 = 3.2                       # clip starts
    D = max(2.0, float(clip_dur))  # clip length
    T2 = T1 + D - 0.4              # end card starts
    END = 4.8
    total = round(T2 + END, 2)
    intro_len = T1 + 0.5
    els = []
    lines = contact_lines(vendor)[:4]

    if ratio == "16:9":
        # ---- intro
        els.append(_rect(1210, 0, 710, 1080, 0, intro_len, panel, slide=("x", 500, 0.9)))
        els.append(_rect(140, 330, 10, 420, 0.2, intro_len - 0.2, acc, slide=("y", -200, 0.7)))
        els.append(_text(f["kicker"], 190, 330, 900, 50, 0.35, intro_len - 0.35, "CB Semi", 30, acc,
                         spacing=6, anim={"preset": "fadeIn", "duration": 0.6}))
        ns = fit_size(NAME, "CB Display", 960, 250, 124)
        els.append(_text(NAME, 190, 390, 960, 260, 0.55, intro_len - 0.55, "CB Display", ns, "#ffffff",
                         anim={"preset": "ascend", "duration": 0.9, "style": "word"}))
        els.append(_text(f["headline"], 190, 665, 960, 80, 1.0, intro_len - 1.0, "CB Text", 40, "#c9d3df",
                         anim={"preset": "fadeIn", "duration": 0.7}))
        els += _logo_block(name, logo_url, 1330, 330, 470, 420, 0.7, intro_len - 0.7, f, slide=("x", 260, 0.8))
        # ---- clip
        els.append(E("video", 0, 0, W, H, T1, D, fit="contain", transition_in="fade"))
        lt0, ltl = T1 + 0.7, max(1.0, D - 1.2)
        cap_w = max(text_w(NAME, "CB Display", 46), text_w(f["caption"], "CB Text", 28)) + 90
        cap_w = min(cap_w, 1300)
        els.append(_rect(0, 850, 14, 140, lt0, ltl, acc, slide=("x", -cap_w, 0.6)))
        els.append(_rect(14, 850, cap_w, 140, lt0, ltl, "#ffffff", 0.95, slide=("x", -cap_w, 0.6)))
        els.append(_text(NAME, 54, 866, cap_w - 70, 62, lt0 + 0.2, ltl - 0.2, "CB Display",
                         fit_size(NAME, "CB Display", cap_w - 70, 60, 46, 1), dark, max_lines=1,
                         slide=("x", -cap_w, 0.6)))
        els.append(_text(f["caption"], 54, 928, cap_w - 70, 46, lt0 + 0.35, ltl - 0.35, "CB Text", 28, "#415a77",
                         max_lines=1, slide=("x", -cap_w, 0.6)))
        if logo_url:
            els.append(_rect(1650, 50, 220, 120, T1 + 0.5, D - 0.9, "#ffffff", 0.95, radius=14, fade_in=0.5))
            els.append(E("image", 1665, 62, 190, 96, T1 + 0.55, D - 0.95, src=logo_url, fade_in=0.5))
        # ---- end card
        els.append(_rect(0, 0, W, H, T2, END, dark, fade_in=0.5, fade_out=False))
        els += _logo_block(name, logo_url, 180, 300, 440, 380, T2 + 0.3, END - 0.3, f, slide=("x", -200, 0.7))
        ns = fit_size(NAME, "CB Display", 1060, 160, 92)
        els.append(_text(NAME, 700, 290, 1060, 160, T2 + 0.45, END - 0.45, "CB Display", ns, "#ffffff",
                         valign="bottom", anim={"preset": "ascend", "duration": 0.7, "style": "word"}))
        y = 470
        for i, ln in enumerate(lines):
            els.append(_text(ln, 700, y, 1060, 50, T2 + 0.7 + 0.12 * i, END - 0.7 - 0.12 * i, "CB Text",
                             fit_size(ln, "CB Text", 1060, 48, 34, 1), "#dfe6ee", max_lines=1,
                             anim={"preset": "fadeIn", "duration": 0.5}))
            y += 54
        cta_w = int(text_w(f["cta"], "CB Semi", 32, 1) + 90)
        els.append(_rect(700, y + 26, cta_w, 76, T2 + 1.2, END - 1.2, acc, radius=38, fade_in=0.4))
        els.append(_text(f["cta"], 700, y + 26, cta_w, 76, T2 + 1.3, END - 1.3, "CB Semi", 32, "#ffffff",
                         "center", spacing=1, max_lines=1))
        if f.get("maker"):
            els.append(_text(f["maker"], 180, 960, 1500, 50, T2 + 1.4, END - 1.4, "CB Text", 26, "#778da9",
                             max_lines=1))
        els.append(_rect(0, 1064, W, 16, T2 + 0.2, END - 0.2, acc, slide=("x", -W, 0.9), fade_out=False))
    else:
        tall = ratio == "9:16"
        cx0 = 80
        tw = W - 2 * cx0
        # ---- intro
        if tall:
            els.append(_rect(0, 0, W, 760, 0, intro_len, panel, slide=("y", -500, 0.9)))
            logo_box, kick_y, name_box, head_y = (290, 230, 500, 420), 900, (960, 380), 1350
            bar = (440, 862, 200, 8)
        else:
            els.append(_rect(0, 0, W, 470, 0, intro_len, panel, slide=("y", -300, 0.9)))
            logo_box, kick_y, name_box, head_y = (340, 100, 400, 300), 505, (555, 250), 815
            bar = (460, 478, 160, 8)
        els += _logo_block(name, logo_url, *logo_box, 0.6, intro_len - 0.6, f, slide=("y", 160, 0.8))
        els.append(_rect(*bar, 0.3, intro_len - 0.3, acc, fade_in=0.5))
        els.append(_text(f["kicker"], cx0, kick_y, tw, 50, 0.4, intro_len - 0.4, "CB Semi", 30, acc, "center",
                         spacing=6, anim={"preset": "fadeIn", "duration": 0.6}))
        ns = fit_size(NAME, "CB Display", tw, name_box[1] - 20, 120 if tall else 96, max_lines=3 if tall else 2)
        els.append(_text(NAME, cx0, name_box[0], tw, name_box[1], 0.6, intro_len - 0.6, "CB Display", ns,
                         "#ffffff", "center", max_lines=3 if tall else 2,
                         anim={"preset": "ascend", "duration": 0.9, "style": "word"}))
        els.append(_text(f["headline"], cx0, head_y, tw, 90, 1.0, intro_len - 1.0, "CB Text", 40 if tall else 36,
                         "#c9d3df", "center", anim={"preset": "fadeIn", "duration": 0.7}))
        # ---- clip: blurred fill + sharp video in the middle
        els.append(E("video", 0, 0, W, H, T1, D, fit="crop", filter="blur", opacity=0.55, muted=True,
                     effect="zoomInSlow", transition_in="fade"))
        vh = int(W / max(0.3, clip_aspect))
        vy = (H - vh) // 2 if tall else max(170, (H - vh) // 2)
        els.append(E("video", 0, vy, W, vh, T1, D, fit="contain", transition_in="fade"))
        lt0, ltl = T1 + 0.5, max(1.0, D - 0.9)
        if tall:
            els.append(_text(f["kicker"], cx0, vy - 300, tw, 50, lt0, ltl, "CB Semi", 30, acc, "center", spacing=6))
            els.append(_text(NAME, cx0, vy - 245, tw, 200, lt0 + 0.15, ltl - 0.15, "CB Display",
                             fit_size(NAME, "CB Display", tw, 190, 84), "#ffffff", "center",
                             anim={"preset": "ascend", "duration": 0.7, "style": "word"}))
            els.append(_text(f["caption"], cx0, vy + vh + 50, tw, 110, lt0 + 0.4, ltl - 0.4, "CB Text", 38,
                             "#ffffff", "center", anim={"preset": "fadeIn", "duration": 0.6}))
            if logo_url:
                els.append(_rect(380, vy + vh + 220, 320, 170, lt0 + 0.6, ltl - 0.6, "#ffffff", 0.95, radius=16,
                                 fade_in=0.5))
                els.append(E("image", 400, vy + vh + 238, 280, 134, lt0 + 0.65, ltl - 0.65, src=logo_url,
                             fade_in=0.5))
        else:
            els.append(_rect(0, 0, W, vy, lt0 - 0.3, ltl + 0.3, dark, 0.55, fade_in=0.4))
            els.append(_text(NAME, cx0, 30, tw, vy - 50, lt0, ltl, "CB Display",
                             fit_size(NAME, "CB Display", tw, vy - 60, 64), "#ffffff", "center",
                             anim={"preset": "ascend", "duration": 0.7, "style": "word"}))
            els.append(_rect(0, vy + vh, W, H - vy - vh, lt0 - 0.3, ltl + 0.3, dark, 0.55, fade_in=0.4))
            els.append(_text(f["caption"], cx0, vy + vh + 20, tw, H - vy - vh - 40, lt0 + 0.3, ltl - 0.3,
                             "CB Text", 34, "#ffffff", "center", anim={"preset": "fadeIn", "duration": 0.6}))
        # ---- end card
        els.append(_rect(0, 0, W, H, T2, END, dark, fade_in=0.5, fade_out=False))
        if tall:
            lb, ny, nh, cy, cstep, csz = (290, 260, 500, 420), 760, 300, 1090, 62, 36
        else:
            lb, ny, nh, cy, cstep, csz = (390, 70, 300, 220), 320, 200, 545, 50, 30
        els += _logo_block(name, logo_url, *lb, T2 + 0.3, END - 0.3, f, slide=("y", 140, 0.7))
        els.append(_text(NAME, cx0, ny, tw, nh, T2 + 0.45, END - 0.45, "CB Display",
                         fit_size(NAME, "CB Display", tw, nh - 20, 96 if tall else 76, max_lines=3 if tall else 2),
                         "#ffffff", "center", max_lines=3 if tall else 2,
                         anim={"preset": "ascend", "duration": 0.7, "style": "word"}))
        y = cy
        for i, ln in enumerate(lines):
            els.append(_text(ln, cx0, y, tw, cstep - 6, T2 + 0.7 + 0.12 * i, END - 0.7 - 0.12 * i, "CB Text",
                             fit_size(ln, "CB Text", tw, cstep - 8, csz, 1), "#dfe6ee", "center", max_lines=1,
                             anim={"preset": "fadeIn", "duration": 0.5}))
            y += cstep
        cta_w = int(text_w(f["cta"], "CB Semi", 32, 1) + 90)
        els.append(_rect((W - cta_w) // 2, y + 30, cta_w, 76, T2 + 1.2, END - 1.2, acc, radius=38, fade_in=0.4))
        els.append(_text(f["cta"], (W - cta_w) // 2, y + 30, cta_w, 76, T2 + 1.3, END - 1.3, "CB Semi", 32,
                         "#ffffff", "center", spacing=1, max_lines=1))
        if f.get("maker"):
            els.append(_text(f["maker"], cx0, H - 120, tw, 50, T2 + 1.4, END - 1.4, "CB Text", 26, "#778da9",
                             "center", max_lines=1))
        els.append(_rect(0, H - 16, W, 16, T2 + 0.2, END - 0.2, acc, slide=("x", -W, 0.9), fade_out=False))
    return els, total


BUILDERS = {"corporate": corporate}


# ------------------------------------------------------------------ Shotstack JSON
def _offset(e, W, H):
    cx, cy = e["x"] + e["w"] / 2, e["y"] + e["h"] / 2
    return round((cx - W / 2) / W, 4), round((H / 2 - cy) / H, 4)


def _tween(frm, to, length, easing="easeOutCubic", start=0):
    return {"from": frm, "to": to, "start": start, "length": length, "interpolation": "bezier", "easing": easing}


def to_clip(e, W, H, video_url, music_on):
    k = e["kind"]
    clip = {"start": e["start"], "length": e["length"]}
    if k == "video":
        asset = {"type": "video", "src": video_url}
        if e.get("muted") or music_on:
            asset["volume"] = 0 if e.get("muted") else 0.35
        clip["asset"] = asset
        clip["fit"] = e.get("fit", "contain")
        if e["w"] < W or e["h"] < H:
            clip.update(width=int(e["w"]), height=int(e["h"]))
        for key in ("filter", "effect"):
            if e.get(key):
                clip[key] = e[key]
        if e.get("opacity") is not None:
            clip["opacity"] = e["opacity"]
    elif k == "image":
        clip["asset"] = {"type": "image", "src": e["src"]}
        clip.update(fit="contain", width=int(e["w"]), height=int(e["h"]))
    elif k in ("rect", "circle"):
        if k == "rect":
            shape = {"type": "shape", "shape": "rectangle", "fill": {"color": e["color"], "opacity": 1},
                     "rectangle": {"width": int(e["w"]), "height": int(e["h"]),
                                   "cornerRadius": int(e.get("radius") or 0)}}
        else:
            shape = {"type": "shape", "shape": "circle", "fill": {"color": e["color"], "opacity": 1},
                     "circle": {"radius": int(e["w"] // 2)}}
        clip["asset"] = shape
        clip.update(width=int(e["w"]) + 2, height=int(e["h"]) + 2)
        if e.get("opacity", 1) != 1 and not e.get("fade_in"):
            clip["opacity"] = e["opacity"]
    elif k == "text":
        asset = {"type": "rich-text", "text": e["text"],
                 "font": {"family": e["family"], "size": int(e["size"]), "color": e["color"]},
                 "style": {"letterSpacing": e.get("spacing", 0), "lineHeight": e.get("line_h", 1.12)},
                 "align": {"horizontal": e.get("align", "left"), "vertical": e.get("valign", "middle")}}
        if e.get("anim"):
            asset["animation"] = e["anim"]
        clip["asset"] = asset
        clip.update(width=int(e["w"]), height=int(e["h"]))
    ox, oy = _offset(e, W, H)
    clip["position"] = "center"
    clip["offset"] = {"x": ox, "y": oy}
    slide = e.get("slide")
    if slide:
        axis, delta, dur = slide
        if axis == "x":
            clip["offset"]["x"] = [_tween(round(ox + delta / W, 4), ox, dur)]
        else:
            clip["offset"]["y"] = [_tween(round(oy - delta / H, 4), oy, dur)]
    if e.get("fade_in"):
        clip["opacity"] = [_tween(0, e.get("opacity", 1), e["fade_in"], "easeOutQuad")]
    tr = {}
    if e.get("transition_in"):
        tr["in"] = e["transition_in"]
    if e.get("fade_out", k != "video") and k != "video":
        tr["out"] = "fade"
    if tr:
        clip["transition"] = tr
    return clip


def build_edit(project, vendor, ratio, urls, fin=None):
    f = {**DEFAULT_FINISH, **(fin or project.get("finish") or {})}
    W, H = SIZES[ratio]
    clip_dur = project["frames"] / float(project["fps"] or 25)
    els, total = BUILDERS.get(f["preset"], corporate)(vendor, ratio, clip_dur, project["width"] / project["height"],
                                                      f, urls.get("logo"))
    music = MUSIC.get(f["music"], MUSIC["none"])[1]
    clips = [to_clip(e, W, H, urls["video"], bool(music)) for e in els]
    timeline = {"background": f["dark"], "fonts": [{"src": urls["fonts"] + fn} for fn in FONT_FILES.values()],
                "tracks": [{"clips": [c]} for c in reversed(clips)]}  # first track = top layer
    if music:
        timeline["soundtrack"] = {"src": music, "effect": "fadeInFadeOut", "volume": float(f["music_volume"])}
    fps = min((24, 25, 30), key=lambda x: abs(x - float(project["fps"] or 25)))
    return {"timeline": timeline,
            "output": {"format": "mp4", "resolution": "1080", "aspectRatio": ratio, "fps": fps, "quality": "high"}}


# ------------------------------------------------------------------ Shotstack HTTP
def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API_BASE + path, data=data, method=method,
                               headers={"x-api-key": API_KEY, "Content-Type": "application/json",
                                        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="ignore")
        try:
            j = json.loads(msg)
            msg = j.get("response", {}).get("error") or j.get("message") or msg
            if isinstance(msg, dict):
                msg = json.dumps(msg)
        except Exception:
            pass
        raise RuntimeError(f"Shotstack {e.code}: {str(msg)[:400]}")


def submit(edit) -> str:
    return _req("POST", "/render", edit)["response"]["id"]


def status(render_id) -> dict:
    """{'status': queued|fetching|rendering|saving|done|failed, 'url':..., 'error':...}"""
    return _req("GET", f"/render/{render_id}?data=false")["response"]


def download(url, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".dl")
    with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    tmp.replace(dest)


def wait(render_id, timeout=1800, every=5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = status(render_id)
        if s.get("status") in ("done", "failed"):
            return s
        time.sleep(every)
    raise RuntimeError("Shotstack render timed out")


# ------------------------------------------------------------------ local mock preview
def _hex(c, a=255):
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), a)


def mock_frame(els, ratio, t, video_frame: Image.Image, logo: Image.Image | None, bg="#0d1b2a"):
    """Approximate still of the layout at time t (animations shown in their end state)."""
    W, H = SIZES[ratio]
    im = Image.new("RGBA", (W, H), _hex(bg))
    for e in els:
        if not (e["start"] <= t < e["start"] + e["length"]):
            continue
        k = e["kind"]
        x, y, w, h = int(e["x"]), int(e["y"]), int(e["w"]), int(e["h"])
        op = e.get("opacity", 1) if e.get("opacity") is not None else 1
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        if k == "rect":
            d.rounded_rectangle([x, y, x + w, y + h], radius=e.get("radius") or 0, fill=_hex(e["color"], int(255 * op)))
        elif k == "circle":
            d.ellipse([x, y, x + w, y + h], fill=_hex(e["color"]))
        elif k in ("video", "image"):
            src = video_frame if k == "video" else logo
            if src is None:
                continue
            s = src.convert("RGBA")
            if e.get("fit") == "crop":
                sc = max(w / s.width, h / s.height)
            else:
                sc = min(w / s.width, h / s.height)
            s = s.resize((max(1, int(s.width * sc)), max(1, int(s.height * sc))), Image.LANCZOS)
            if e.get("filter") == "blur":
                s = s.filter(ImageFilter.GaussianBlur(24))
            px, py = x + (w - s.width) // 2, y + (h - s.height) // 2
            if op < 1:
                s.putalpha(Image.eval(s.getchannel("A"), lambda a: int(a * op)))
            box = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            box.paste(s, (px - x, py - y), s)
            layer.paste(box, (x, y), box)
        elif k == "text":
            fnt = _font(e["family"], e["size"])
            lines = wrap(e["text"], e["family"], e["size"], w * 0.97, e.get("spacing", 0))
            lh = e["size"] * e.get("line_h", 1.12)
            th = lh * len(lines)
            ty = y + {"top": 0, "middle": (h - th) / 2, "bottom": h - th}[e.get("valign", "middle")]
            for ln in lines:
                lw = text_w(ln, e["family"], e["size"], e.get("spacing", 0))
                tx = x + {"left": 0, "center": (w - lw) / 2, "right": w - lw}[e.get("align", "left")]
                if e.get("spacing"):
                    cx = tx
                    for ch in ln:
                        d.text((cx, ty), ch, font=fnt, fill=_hex(e["color"]))
                        cx += text_w(ch, e["family"], e["size"]) + e["spacing"]
                else:
                    d.text((tx, ty), ln, font=fnt, fill=_hex(e["color"]))
                ty += lh
        im.alpha_composite(layer)
    return im.convert("RGB")
