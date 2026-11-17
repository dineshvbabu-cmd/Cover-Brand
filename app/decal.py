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
    "logo_knockout_white": True,
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
    im = Image.open(path).convert("RGBA")
    if max(im.size) > 1600:
        im.thumbnail((1600, 1600), Image.LANCZOS)
    if knockout:
        a = np.asarray(im).astype(np.float32)
        m = a[..., :3].min(axis=2)
        fade = np.clip((255 - m) / 20.0, 0, 1)  # near-white -> transparent, soft edge
        a[..., 3] = np.where(m > 235, a[..., 3] * fade, a[..., 3])
        im = Image.fromarray(a.astype(np.uint8), "RGBA")
    bbox = im.getbbox()
    return im.crop(bbox) if bbox else im


def contact_lines(v):
    parts = [v.get("contact"), v.get("phone"), v.get("email"), v.get("website")]
    return [p.strip() for p in parts if p and str(p).strip()]


def render_decal(vendor: dict, style: dict, aspect: float, width: int, logo_path=None) -> Image.Image:
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
