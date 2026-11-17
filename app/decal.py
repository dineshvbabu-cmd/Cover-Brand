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


def logo_needs_badge(logo: Image.Image, cover_rgb) -> bool:
    """True when the logo's main colour is too close to the cover colour to read."""
    if cover_rgb is None:
        return False
    a = np.asarray(logo.convert("RGBA")).reshape(-1, 4)
    a = a[a[:, 3] > 128][:, :3]
    if len(a) < 20:
        return False
    cov = _lab(cover_rgb)
    labs = np.asarray([_lab(c) for c in a[:: max(1, len(a) // 400)]])
    dist = np.linalg.norm(labs - cov, axis=1)
    return float(np.median(dist)) < 45


def contact_lines(v):
    parts = [v.get("contact"), v.get("phone"), v.get("email"), v.get("website")]
    return [p.strip() for p in parts if p and str(p).strip()]


def render_decal(vendor: dict, style: dict, aspect: float, width: int, logo_path=None, cover_rgb=None) -> Image.Image:
    s = {**DEFAULT_STYLE, **(style or {})}
    W = int(width)
    H = max(10, int(round(W / max(0.05, aspect))))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if s["plate_opacity"] > 0:
        d.rectangle([0, 0, W, H], fill=_hex(s["plate_color"], int(255 * s["plate_opacity"])))

    pad = int(s["padding"] * min(W, H))
    x0, y0, x1, y1 = pad, pad, W - pad, H - pad
    iw, ih = x1 - x0, y1 - y0
    color = _hex(s["text_color"])
    name = (vendor.get("name") or "").strip() or "Vendor"
    if s["uppercase"]:
        name = name.upper()
    lines = contact_lines(vendor) if s["show_contact"] else []
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

    layout = s["layout"]
    if layout == "auto":
        layout = "row" if (iw / max(1, ih)) >= 1.7 and logo else "stack"
    if not logo:
        layout = "stack" if layout != "text" else "text"

    fam = s["font"]

    def text_block(bx0, by0, bw, bh, align):
        """name + tagline + contact lines inside a box."""
        rows = []
        if tagline:
            rows.append(("tag", tagline))
        rows.append(("name", name))
        if lines:
            if len(lines) >= 3:
                rows.append(("c", "  |  ".join(lines[:2])))
                rows.append(("c", "  |  ".join(lines[2:])))
            else:
                rows += [("c", ln) for ln in lines]
        # height budget: name gets the lion's share
        n_small = sum(1 for r in rows if r[0] != "name")
        name_h = bh * (0.9 if n_small == 0 else 0.6 if n_small <= 2 else 0.5)
        small_h = (bh - name_h) / max(1, n_small) * 0.8
        fonts = []
        for kind, text in rows:
            if kind == "name":
                fonts.append(_fit(d, text, fam, True, bw, name_h))
            else:
                fonts.append(_fit(d, text, fam, kind == "tag", bw, small_h))
        # keep small lines a consistent size
        small = [f.size for (k, _), f in zip(rows, fonts) if k == "c"]
        if small:
            name_size = next(f.size for (k, _), f in zip(rows, fonts) if k == "name")
            sz = max(6, min(min(small), int(name_size * 0.6)))
            fonts = [(_font(fam, False, sz) if k == "c" else f) for (k, _), f in zip(rows, fonts)]
        heights = []
        for (k, t), f in zip(rows, fonts):
            l, tp, r, b = d.textbbox((0, 0), t, font=f)
            heights.append((tp, b - tp, r - l))
        gap = bh * 0.04
        total = sum(h for _, h, _ in heights) + gap * (len(rows) - 1)
        y = by0 + (bh - total) / 2
        for (k, t), f, (tp, h, w) in zip(rows, fonts, heights):
            x = bx0 + (bw - w) / 2 if align == "center" else bx0
            d.text((x, y - tp), t, font=f, fill=color)
            y += h + gap

    if layout == "row":
        lw = int(min(ih, iw * 0.32))
        lg = logo.copy()
        lg.thumbnail((lw, ih), Image.LANCZOS)
        img.alpha_composite(lg, (x0 + (lw - lg.width) // 2, y0 + (ih - lg.height) // 2))
        gap = int(iw * 0.04)
        text_block(x0 + lw + gap, y0, iw - lw - gap, ih, "left")
    elif layout == "stack" and logo:
        lh = int(ih * 0.42)
        lg = logo.copy()
        lg.thumbnail((iw, lh), Image.LANCZOS)
        img.alpha_composite(lg, (x0 + (iw - lg.width) // 2, y0 + (lh - lg.height) // 2))
        text_block(x0, y0 + lh + int(ih * 0.04), iw, ih - lh - int(ih * 0.04), "center")
    else:
        text_block(x0, y0, iw, ih, "center")
    return img
