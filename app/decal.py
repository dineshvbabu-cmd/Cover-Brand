"""Builds the vendor 'print' (logo + name + contact) as a transparent image."""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_DIRS = [Path("/usr/share/fonts/truetype/dejavu"), Path(__file__).parent / "fonts"]
FONTS = {
    "sans": ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"),
    "condensed": ("DejaVuSansCondensed-Bold.ttf", "DejaVuSansCondensed.ttf"),
    "serif": ("DejaVuSerif-Bold.ttf", "DejaVuSerif.ttf"),
    "mono": ("DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf"),
}

DEFAULT_STYLE = {
    "layout": "auto",          # auto | row | stack | text
    "font": "condensed",
    "uppercase": True,
    "text_color": "#ffffff",
    "plate_color": "#0b2f55",
    "plate_opacity": 0.0,      # 0 = print straight on the cover
    "padding": 0.08,           # fraction of the smaller side
    "show_contact": True,
    "show_logo": True,
    "logo_knockout_white": True,   # remove the logo's plain / grey / gradient background
    "logo_badge": "auto",          # white badge behind the logo: auto (when it blends with the cover) | always | never
    "tagline": "",             # optional extra line for every vendor, e.g. "Concept for"
    "text_direction": "auto",  # auto | horizontal | vertical (auto turns text sideways on tall covers)
    "opacity": 0.95,           # how solid the print is
    "realism": 0.8,            # 0 = flat sticker, 1 = follows cover lighting fully
}


def _font_path(family: str, bold: bool):
    names = FONTS.get(family, FONTS["sans"])
    name = names[0] if bold else names[1]
    for d in FONT_DIRS:
        if (d / name).exists():
            return str(d / name)
    return None


def _font(family, bold, size):
    p = _font_path(family, bold)
    if p:
        return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def _fit(draw, text, family, bold, max_w, max_h, max_size=600):
    lo, hi, best = 6, max(7, int(max_size)), 6
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _font(family, bold, mid)
        l, t, r, b = draw.textbbox((0, 0), text, font=f)
        if r - l <= max_w and b - t <= max_h:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return _font(family, bold, best)


def _hex(c, alpha=255):
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha)


def _prep_logo(path, knockout):
    """Load a logo; strip a plain/grey/gradient background by flood-filling in from the edges."""
    import cv2
    im = Image.open(path).convert("RGBA")
    if max(im.size) > 1600:
        im.thumbnail((1600, 1600), Image.LANCZOS)
    a = np.asarray(im).copy()
    border_alpha = np.concatenate([a[0, :, 3], a[-1, :, 3], a[:, 0, 3], a[:, -1, 3]])
    if knockout and border_alpha.mean() > 250:          # no transparency yet
        rgb = np.ascontiguousarray(a[..., :3])
        h, w = rgb.shape[:2]
        mask = np.zeros((h + 2, w + 2), np.uint8)
        flags = 4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8)
        step = max(1, min(h, w) // 40)
        seeds = [(x, 0) for x in range(0, w, step)] + [(x, h - 1) for x in range(0, w, step)] + \
                [(0, y) for y in range(0, h, step)] + [(w - 1, y) for y in range(0, h, step)]
        for (x, y) in seeds:
            if mask[y + 1, x + 1] == 0:
                cv2.floodFill(rgb, mask, (x, y), (0, 0, 0), (7, 7, 7), (7, 7, 7), flags)
        bg = mask[1:-1, 1:-1] > 0
        if 0.05 < bg.mean() < 0.97:
            # enclosed holes (e.g. inside an 'O' or a ring) that have the background's colour
            hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
            lum = hsv[..., 2][bg]
            if np.median(hsv[..., 1][bg]) < 30:          # grey/white background
                lo, hi = np.percentile(lum, 2) - 6, np.percentile(lum, 98) + 6
                cand = (~bg) & (hsv[..., 1] < 30) & (hsv[..., 2] >= lo) & (hsv[..., 2] <= hi)
                n, lab, st, _ = cv2.connectedComponentsWithStats(cand.astype(np.uint8), connectivity=4)
                for k in range(1, n):
                    if st[k, 4] > 0.002 * h * w:
                        bg |= lab == k
        if 0.05 < bg.mean() < 0.97:                      # sensible background found
            soft = cv2.GaussianBlur((~bg).astype(np.float32), (0, 0), 0.8)
            a[..., 3] = (a[..., 3].astype(np.float32) * soft).astype(np.uint8)
    im = Image.fromarray(a, "RGBA")
    bbox = im.getchannel("A").point(lambda v: 255 if v > 20 else 0).getbbox()
    return im.crop(bbox) if bbox else im


def _lab(rgb):
    import cv2
    px = np.uint8([[list(rgb)]])
    return cv2.cvtColor(px, cv2.COLOR_RGB2LAB)[0, 0].astype(np.float32)


def _rel_lum(rgb):
    c = np.asarray(rgb, np.float32) / 255.0
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return float(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2])


def contrast_ratio(a, b):
    la, lb = sorted([_rel_lum(a), _rel_lum(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


def logo_needs_badge(logo: Image.Image, cover_rgb) -> bool:
    """True when the logo would not stand out on the cover (similar colour or low contrast)."""
    if cover_rgb is None:
        return False
    a = np.asarray(logo.convert("RGBA")).reshape(-1, 4)
    a = a[a[:, 3] > 128][:, :3]
    if len(a) < 20:
        return False
    main = np.median(a, axis=0)
    cov = _lab(cover_rgb)
    labs = np.asarray([_lab(c) for c in a[:: max(1, len(a) // 400)]])
    close = float(np.median(np.linalg.norm(labs - cov, axis=1))) < 45
    return close or contrast_ratio(main, cover_rgb) < 3.2


def contact_lines(v):
    parts = [v.get("contact"), v.get("phone"), v.get("email"), v.get("website")]
    return [p.strip() for p in parts if p and str(p).strip()]


DETAIL_DROP_ORDER = ["website", "contact", "email", "phone"]   # least important first


def _balanced_split(words, k):
    """Split words into k lines with similar lengths."""
    if k <= 1 or len(words) < 2:
        return [" ".join(words)]
    k = min(k, len(words))
    best, best_cost = None, 1e9
    import itertools
    for cuts in itertools.combinations(range(1, len(words)), k - 1):
        parts = [" ".join(words[a:b]) for a, b in zip((0,) + cuts, cuts + (len(words),))]
        cost = max(len(x) for x in parts)
        if cost < best_cost:
            best, best_cost = parts, cost
    return best


class _Meas:
    def __init__(self, fam):
        self.fam = fam
        self.d = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def w(self, text, size, bold):
        l, t, r, b = self.d.textbbox((0, 0), text, font=_font(self.fam, bold, max(6, int(size))))
        return r - l

    def fit(self, lines, bold, max_w, max_size):
        """Largest size (<= max_size) at which every line fits max_w."""
        if not lines:
            return 0
        ref = 100
        widest = max(self.w(ln, ref, bold) for ln in lines) or 1
        return max(6, min(max_size, int(ref * max_w / widest)))


def _text_plan(m, name, details, w, h, tagline):
    """Pick name line count, detail packing and which details to keep to maximise legibility."""
    words = name.split()
    best = None
    subsets = [details]
    keep = list(details)
    for key in DETAIL_DROP_ORDER:          # progressively drop the least important details
        if any(k == key for k, _ in keep) and len(keep) > 1:
            keep = [x for x in keep if x[0] != key]
            subsets.append(list(keep))
    for k in (1, 2, 3):
        if k > max(1, len(words)):
            break
        nlines = _balanced_split(words, k)
        for sub in subsets:
            texts = [t for _, t in sub]
            packings = [texts]
            if len(texts) >= 2:
                packings.append(["  |  ".join(texts[i:i + 2]) for i in range(0, len(texts), 2)])
            if len(texts) >= 3:
                packings.append(["  |  ".join(texts)])
            for pack in (packings if texts else [[]]):
                for frac in ((1.0,) if not pack and not tagline else (0.5, 0.58, 0.66, 0.74)):
                    nh = h * frac
                    tag_h = 0.0
                    if tagline:
                        tag_h = h * 0.1
                    ns = m.fit(nlines, True, w, (nh - tag_h) / (len(nlines) * 1.2))
                    ds = 0
                    if pack:
                        avail = h - nh - h * 0.04
                        ds = m.fit(pack, False, w, avail / (len(pack) * 1.32))
                        ds = min(ds, ns * 0.55)
                        if ds < ns * 0.2:
                            continue
                    coverage = (len(sub) / len(details)) if details else 1.0
                    score = ns + 1.7 * ds * (coverage ** 0.6)
                    if best is None or score > best[0]:
                        best = (score, nlines, ns, pack, ds, tag_h)
    return best


def _layout(m, name, details, logo, iw, ih, tagline):
    """Best arrangement in a box: 'row' (logo left), 'stack' (logo top) or 'text'."""
    cands = []
    t = _text_plan(m, name, details, iw, ih, tagline)
    if t:
        cands.append((t[0], "text", None, t))
    if logo is not None:
        la = logo.width / max(1, logo.height)
        gap = iw * 0.04
        # row: logo at the left, full height-ish
        for share in (0.28, 0.36, 0.45, 0.52):
            lh = ih * 0.86
            lw = min(la * lh, iw * share)
            lh = lw / la
            tw = iw - lw - gap
            if tw > iw * 0.4:
                t = _text_plan(m, name, details, tw, ih, tagline)
                if t:
                    cands.append((t[0] + min(0.5 * (lw * lh) ** 0.5, 0.8 * t[2]), "row", (lw, lh), t))
        # stack: logo on top
        for share in (0.28, 0.36, 0.45):
            lh = ih * share
            lw = min(la * lh, iw * 0.85)
            lh = lw / la
            th = ih - lh - ih * 0.05
            if th > ih * 0.35:
                t = _text_plan(m, name, details, iw, th, tagline)
                if t:
                    cands.append((t[0] + min(0.5 * (lw * lh) ** 0.5, 0.8 * t[2]), "stack", (lw, lh), t))
    return max(cands, key=lambda c: c[0])


def _draw_text(d, m, plan, x0, y0, w, h, align, color, tagline, fam):
    score, nlines, ns, pack, ds, tag_h = plan
    rows = []
    if tagline:
        ts = max(8, min(ns * 0.4, m.fit([tagline], True, w, tag_h)))
        rows.append((tagline, ts, True, 0.25))
    rows += [(ln, ns, True, 0.2) for ln in nlines]
    rows += [(ln, ds, False, 0.32) for ln in pack]
    heights = []
    for text, size, bold, _ in rows:
        f = _font(fam, bold, int(size))
        l, t, r, b = d.textbbox((0, 0), text, font=f)
        heights.append((t, b - t, r - l, f))
    gaps = [rows[i][1] * rows[i][3] for i in range(len(rows) - 1)]
    if pack and len(nlines):
        gaps[len(nlines) - 1 + (1 if tagline else 0)] = max(ds * 0.55, ns * 0.18)
    total = sum(hh for _, hh, _, _ in heights) + sum(gaps)
    y = y0 + (h - total) / 2
    for i, ((text, size, bold, _), (tp, hh, ww, f)) in enumerate(zip(rows, heights)):
        x = x0 + (w - ww) / 2 if align == "center" else x0
        d.text((x, y - tp), text, font=f, fill=color)
        y += hh + (gaps[i] if i < len(gaps) else 0)


def render_decal(vendor: dict, style: dict, aspect: float, width: int, logo_path=None, cover_rgb=None) -> Image.Image:
    """Print artwork that adapts to the cover shape, the logo shape and the amount of text."""
    s = {**DEFAULT_STYLE, **(style or {})}
    W = int(width)
    H = max(10, int(round(W / max(0.05, aspect))))
    name = (vendor.get("name") or "").strip() or "Vendor"
    if s["uppercase"]:
        name = name.upper()
    details = []
    if s["show_contact"]:
        for key in ("contact", "phone", "email", "website"):
            val = str(vendor.get(key) or "").strip()
            if val:
                details.append((key, val))
    tagline = (s.get("tagline") or "").strip()
    logo = None
    if s["show_logo"] and logo_path and Path(logo_path).exists():
        try:
            logo = _prep_logo(logo_path, s["logo_knockout_white"])
            badge = s.get("logo_badge", "auto")
            if badge == "always" or (badge == "auto" and logo_needs_badge(logo, cover_rgb)):
                pad = int(max(logo.size) * 0.12)
                b = Image.new("RGBA", (logo.width + 2 * pad, logo.height + 2 * pad), (0, 0, 0, 0))
                ImageDraw.Draw(b).rounded_rectangle([0, 0, b.width - 1, b.height - 1],
                                                    radius=int(min(b.size) * 0.18), fill=(255, 255, 255, 245))
                b.alpha_composite(logo, (pad, pad))
                logo = b
        except Exception:
            logo = None
    if s["layout"] == "text":
        logo = None
    m = _Meas(s["font"])

    def compose(cw, ch):
        pad = int(s["padding"] * min(cw, ch))
        iw, ih = cw - 2 * pad, ch - 2 * pad
        best = _layout(m, name, details, logo, iw, ih, tagline)
        if s["layout"] in ("row", "stack") and logo is not None:   # forced layout
            forced = [c for c in [_layout(m, name, details, logo, iw, ih, tagline)] if c[1] == s["layout"]]
            best = forced[0] if forced else best
        return best, pad, iw, ih

    def paint(cw, ch, plan):
        best, pad, iw, ih = plan
        img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if s["plate_opacity"] > 0:
            d.rectangle([0, 0, cw, ch], fill=_hex(s["plate_color"], int(255 * s["plate_opacity"])))
        color = _hex(s["text_color"])
        kind, lsize, tplan = best[1], best[2], best[3]
        if kind == "row":
            lw, lh = lsize
            lg = logo.resize((max(1, int(lw)), max(1, int(lh))), Image.LANCZOS)
            img.alpha_composite(lg, (pad, pad + int((ih - lh) / 2)))
            gap = iw * 0.04
            _draw_text(d, m, tplan, pad + lw + gap, pad, iw - lw - gap, ih, "left", color, tagline, s["font"])
        elif kind == "stack":
            lw, lh = lsize
            lg = logo.resize((max(1, int(lw)), max(1, int(lh))), Image.LANCZOS)
            img.alpha_composite(lg, (pad + int((iw - lw) / 2), pad))
            g = ih * 0.05
            _draw_text(d, m, tplan, pad, pad + lh + g, iw, ih - lh - g, "center", color, tagline, s["font"])
        else:
            _draw_text(d, m, tplan, pad, pad, iw, ih, "center", color, tagline, s["font"])
        return img

    direction = s.get("text_direction", "auto")
    horiz = compose(W, H)
    if direction == "vertical" or (direction == "auto" and aspect < 0.75):
        vert = compose(H, W)     # lay out on the rotated box, read bottom-to-top
        if direction == "vertical" or vert[0][0] > horiz[0][0] * 1.25:
            return paint(H, W, vert).rotate(90, expand=True)
    return paint(W, H, horiz)
