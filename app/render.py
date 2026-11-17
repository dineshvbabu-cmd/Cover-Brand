"""Compositing the vendor print onto the tracked cover and encoding video."""
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .decal import render_decal
from .tracking import colour_mask


def ingest(src: Path, out_mp4: Path, max_w: int = 1920, max_seconds: int = 120) -> dict:
    """Normalise any upload (mp4/mov/webm/gif) to H.264 MP4 with even dimensions."""
    vf = f"scale='min({max_w},iw)':-2,format=yuv420p"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-t", str(max_seconds),
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_mp4)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Could not read this video/GIF: " + r.stderr[-400:])
    cap = cv2.VideoCapture(str(out_mp4))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n = 0
    while cap.grab():
        n += 1
    cap.release()
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                            "stream=index", "-of", "csv=p=0", str(out_mp4)], capture_output=True, text=True)
    return {"fps": fps, "width": w, "height": h, "frames": n, "has_audio": bool(probe.stdout.strip())}


def read_frame(video: Path, idx: int) -> np.ndarray:
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ok, f = cap.read()
    if not ok:  # seek fallback
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        for _ in range(idx + 1):
            ok, f = cap.read()
    cap.release()
    if not ok:
        raise ValueError("frame not found")
    return f


def quad_aspect(q) -> float:
    q = np.asarray(q, np.float32)
    top = np.linalg.norm(q[1] - q[0]); bot = np.linalg.norm(q[2] - q[3])
    left = np.linalg.norm(q[3] - q[0]); right = np.linalg.norm(q[2] - q[1])
    return float((top + bot) / 2 / max(1e-3, (left + right) / 2))


def ref_quad(project) -> list:
    kfs = sorted(project["cover"]["keyframes"], key=lambda k: k["frame"])
    return kfs[0]["quad"]


def make_decal(project, vendor, pdir: Path, quads) -> np.ndarray:
    quads, _, _ = _tracks(quads)
    """BGRA numpy decal, sized to the cover's largest on-screen width."""
    rq = ref_quad(project)
    aspect = project["cover"].get("aspect_override") or quad_aspect(rq)
    widths = [np.linalg.norm(np.asarray(q[1]) - np.asarray(q[0])) for q in quads[:: max(1, len(quads) // 30)]]
    width = int(np.clip(max(widths) * 1.5, 400, 2400))
    logo = pdir / "logos" / vendor["logo"] if vendor.get("logo") else None
    im = render_decal(vendor, project.get("style", {}), aspect, width, logo)
    return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGBA2BGRA)


def reference_luma(project, video: Path) -> float:
    kf = sorted(project["cover"]["keyframes"], key=lambda k: k["frame"])[0]
    f = read_frame(video, kf["frame"])
    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
    m = np.zeros(g.shape, np.uint8)
    cv2.fillPoly(m, [np.asarray(kf["quad"], np.int32)], 255)
    return float(np.median(g[m > 0])) / 255.0 if m.any() else 0.5


def _tracks(tr):
    """Accept either a plain list of quads or the tracks dict {quads, vis, cs}."""
    if isinstance(tr, dict):
        return tr["quads"], tr.get("vis"), tr.get("cs")
    return tr, None, None


def composite(frame: np.ndarray, decal: np.ndarray, quad, style: dict, lref: float,
              vis: float = 1.0, cs: dict | None = None) -> np.ndarray:
    if vis is not None and vis <= 0.01:
        return frame
    h, w = frame.shape[:2]
    q = np.asarray(quad, np.float32)
    x0, y0 = np.floor(q.min(axis=0)).astype(int) - 2
    x1, y1 = np.ceil(q.max(axis=0)).astype(int) + 2
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return frame
    dh, dw = decal.shape[:2]
    src = np.float32([[0, 0], [dw, 0], [dw, dh], [0, dh]])
    M = cv2.getPerspectiveTransform(src, q - np.float32([x0, y0]))
    warped = cv2.warpPerspective(decal, M, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    roi = frame[y0:y1, x0:x1].astype(np.float32)
    alpha = warped[..., 3:4].astype(np.float32) / 255.0 * float(style.get("opacity", 0.95)) * float(vis or 1.0)
    if cs:  # print only on the cover fabric (not on posts, cargo, background)
        cm = colour_mask(cv2.cvtColor(roi.astype(np.uint8), cv2.COLOR_BGR2HSV), cs).astype(np.float32) / 255.0
        cm = cv2.GaussianBlur(cv2.morphologyEx(cm, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)), (0, 0), 1.5)
        alpha = alpha * cm[..., None]
    rgb = warped[..., :3].astype(np.float32)
    realism = float(style.get("realism", 0.8))
    if realism > 0:
        luma = cv2.cvtColor(roi.astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        luma = cv2.GaussianBlur(luma, (0, 0), 1.2)
        shade = luma / max(0.05, lref)
        shade = np.clip(1.0 + (shade - 1.0) * realism, 0.15, 1.6)[..., None]
        rgb = np.clip(rgb * shade, 0, 255)
    out = roi * (1 - alpha) + rgb * alpha
    frame = frame.copy()
    frame[y0:y1, x0:x1] = out.astype(np.uint8)
    return frame


def render_vendor(project, vendor, pdir: Path, quads, out_mp4: Path, make_gif: bool, progress=None):
    src = pdir / "source.mp4"
    style = project.get("style", {})
    decal = make_decal(project, vendor, pdir, quads)
    lref = reference_luma(project, src)
    quads, vis, cs = _tracks(quads)
    w, h, fps = project["width"], project["height"], project["fps"]
    tmp = out_mp4.with_suffix(".part.mp4")
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-",
           "-i", str(src), "-map", "0:v:0", "-map", "1:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(tmp)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    cap = cv2.VideoCapture(str(src))
    i, n = 0, len(quads)
    try:
        while True:
            ok, f = cap.read()
            if not ok:
                break
            q = quads[min(i, n - 1)]
            f = composite(f, decal, q, style, lref, vis[min(i, len(vis) - 1)] if vis else 1.0, cs)
            p.stdin.write(f.tobytes())
            i += 1
            if progress and i % 10 == 0:
                progress(i / max(1, project["frames"]))
    finally:
        cap.release()
        p.stdin.close()
        err = p.stderr.read().decode(errors="ignore")
        p.wait()
    if p.returncode != 0:
        raise RuntimeError("Encoding failed: " + err[-400:])
    tmp.replace(out_mp4)
    if make_gif:
        gif = out_mp4.with_suffix(".gif")
        gw = min(640, w)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(out_mp4), "-vf",
                        f"fps={min(15, round(fps))},scale={gw}:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
                        str(gif)], check=True)
    thumb = out_mp4.with_suffix(".jpg")
    mid = read_frame(out_mp4, max(0, min(project["frames"] - 1, project["frames"] // 2)))
    cv2.imwrite(str(thumb), mid, [cv2.IMWRITE_JPEG_QUALITY, 85])


def preview(project, vendor, pdir: Path, quads, idx: int) -> bytes:
    src = pdir / "source.mp4"
    f = read_frame(src, idx)
    decal = make_decal(project, vendor, pdir, quads)
    qs, vis, cs = _tracks(quads)
    j = min(idx, len(qs) - 1)
    f = composite(f, decal, qs[j], project.get("style", {}), reference_luma(project, src),
                  vis[min(j, len(vis) - 1)] if vis else 1.0, cs)
    ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return buf.tobytes()


def decal_png(project, vendor, pdir: Path) -> bytes:
    rq = ref_quad(project)
    aspect = project["cover"].get("aspect_override") or quad_aspect(rq)
    logo = pdir / "logos" / vendor["logo"] if vendor.get("logo") else None
    im = render_decal(vendor, project.get("style", {}), aspect, 900, logo)
    import io
    b = io.BytesIO()
    bg = Image.new("RGBA", im.size, (90, 98, 110, 255))
    bg.alpha_composite(im)
    bg.convert("RGB").save(b, "PNG")
    return b.getvalue()
