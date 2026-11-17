"""Planar tracking of the container cover.

The user marks the 4 corners of the cover (TL, TR, BR, BL) on one or more
keyframes. Each frame then gets a quad:
  - static : same quad on every frame (camera and cover don't move)
  - manual : linear interpolation between user keyframes
  - auto   : SIFT feature homography against the nearest keyframe, with
             optical-flow fallback, then light temporal smoothing.
"""
import cv2
import numpy as np

TRACK_MAX_W = 960  # tracking runs on downscaled frames for speed


def _read_gray(video_path: str, scale: float) -> list[np.ndarray]:
    cap = cv2.VideoCapture(video_path)
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        if scale != 1.0:
            g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        frames.append(g)
    cap.release()
    return frames


def _poly_mask(shape, quad, grow: float = 0.0) -> np.ndarray:
    q = np.asarray(quad, np.float32)
    if grow:
        c = q.mean(axis=0)
        q = c + (q - c) * (1 + grow)
    m = np.zeros(shape[:2], np.uint8)
    cv2.fillPoly(m, [q.astype(np.int32)], 255)
    return m


def _rect_mask(shape, quad, grow: float) -> np.ndarray:
    q = np.asarray(quad, np.float32)
    x0, y0 = q.min(axis=0)
    x1, y1 = q.max(axis=0)
    w, h = x1 - x0, y1 - y0
    x0, x1 = int(max(0, x0 - w * grow)), int(min(shape[1], x1 + w * grow))
    y0, y1 = int(max(0, y0 - h * grow)), int(min(shape[0], y1 + h * grow))
    m = np.zeros(shape[:2], np.uint8)
    m[y0:y1, x0:x1] = 255
    return m


def _area(q) -> float:
    return abs(cv2.contourArea(np.asarray(q, np.float32)))


def _sane(q, prev) -> bool:
    q = np.asarray(q, np.float32)
    if not np.all(np.isfinite(q)):
        return False
    if not cv2.isContourConvex(q.reshape(-1, 1, 2)):
        return False
    a, ap = _area(q), _area(prev)
    if ap <= 0 or a < 64:
        return False
    r = a / ap
    return 0.6 < r < 1.65


def _flow_step(prev_g, cur_g, prev_quad):
    """Estimate prev->cur homography with KLT on corners inside the cover."""
    mask = _poly_mask(prev_g.shape, prev_quad, grow=-0.03)
    pts = cv2.goodFeaturesToTrack(prev_g, 300, 0.01, 5, mask=mask)
    if pts is None or len(pts) < 8:
        return None
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev_g, cur_g, pts, None, winSize=(21, 21), maxLevel=3)
    good = st.reshape(-1) == 1
    if good.sum() < 8:
        return None
    H, _ = cv2.findHomography(pts[good], nxt[good], cv2.RANSAC, 3.0)
    return H


def _track_segment(frames, ref_idx, ref_quad, lo, hi, quads, conf):
    """Track from ref_idx forward to hi-1 and backward to lo."""
    sift = cv2.SIFT_create(nfeatures=2000)
    ref = frames[ref_idx]
    kp_r, des_r = sift.detectAndCompute(ref, _poly_mask(ref.shape, ref_quad, grow=-0.02))
    ref_pts = np.float32([k.pt for k in kp_r]) if kp_r else np.zeros((0, 2), np.float32)
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    ref_q = np.asarray(ref_quad, np.float32).reshape(-1, 1, 2)
    quads[ref_idx] = np.asarray(ref_quad, np.float32).tolist()
    conf[ref_idx] = 1.0

    for direction, rng in ((1, range(ref_idx + 1, hi)), (-1, range(ref_idx - 1, lo - 1, -1))):
        prev_quad = np.asarray(ref_quad, np.float32)
        prev_i = ref_idx
        for t in rng:
            cur = frames[t]
            new_quad, c = None, 0.0
            # 1) SIFT against the keyframe (no drift)
            if des_r is not None and len(kp_r) >= 8:
                kp, des = sift.detectAndCompute(cur, _rect_mask(cur.shape, prev_quad, 0.35))
                if des is not None and len(kp) >= 8:
                    good = []
                    for pair in matcher.knnMatch(des_r, des, k=2):
                        if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
                            good.append(pair[0])
                    if len(good) >= 10:
                        src = ref_pts[[m.queryIdx for m in good]]
                        dst = np.float32([kp[m.trainIdx].pt for m in good])
                        H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
                        if H is not None and inl is not None and inl.sum() >= 10:
                            q = cv2.perspectiveTransform(ref_q, H).reshape(4, 2)
                            if _sane(q, prev_quad):
                                new_quad = q
                                c = float(min(1.0, inl.sum() / 60.0))
            # 2) optical flow from the previous frame
            if new_quad is None:
                H = _flow_step(frames[prev_i], cur, prev_quad)
                if H is not None:
                    q = cv2.perspectiveTransform(prev_quad.reshape(-1, 1, 2), H).reshape(4, 2)
                    if _sane(q, prev_quad):
                        new_quad, c = q, 0.3
            # 3) hold last good position
            if new_quad is None:
                new_quad, c = prev_quad.copy(), 0.0
            quads[t] = new_quad.tolist()
            conf[t] = c
            prev_quad, prev_i = new_quad, t


def _smooth(quads, fixed: set[int], win: int = 2):
    arr = np.asarray(quads, np.float32)
    out = arr.copy()
    n = len(arr)
    for i in range(n):
        if i in fixed:
            continue
        a, b = max(0, i - win), min(n, i + win + 1)
        out[i] = arr[a:b].mean(axis=0)
    return out.tolist()


def interpolate(keyframes, n_frames):
    kfs = sorted(keyframes, key=lambda k: k["frame"])
    out = []
    for t in range(n_frames):
        if t <= kfs[0]["frame"]:
            out.append(kfs[0]["quad"])
            continue
        if t >= kfs[-1]["frame"]:
            out.append(kfs[-1]["quad"])
            continue
        for a, b in zip(kfs, kfs[1:]):
            if a["frame"] <= t <= b["frame"]:
                w = (t - a["frame"]) / max(1, b["frame"] - a["frame"])
                qa, qb = np.asarray(a["quad"], np.float32), np.asarray(b["quad"], np.float32)
                out.append((qa * (1 - w) + qb * w).tolist())
                break
    return out


def track(video_path: str, width: int, n_frames: int, mode: str, keyframes: list[dict], progress=None):
    """Return (quads per frame in full-res pixel coords, confidence per frame)."""
    if not keyframes:
        raise ValueError("Mark the 4 cover corners on at least one frame first.")
    kfs = sorted(keyframes, key=lambda k: k["frame"])
    if mode == "static":
        return [kfs[0]["quad"]] * n_frames, [1.0] * n_frames
    if mode == "manual":
        return interpolate(kfs, n_frames), [1.0] * n_frames

    scale = min(1.0, TRACK_MAX_W / float(width))
    frames = _read_gray(video_path, scale)
    n = len(frames)
    quads = [None] * n
    conf = [0.0] * n
    # each keyframe owns the frames closest to it
    bounds = [0] + [(a["frame"] + b["frame"]) // 2 + 1 for a, b in zip(kfs, kfs[1:])] + [n]
    for i, kf in enumerate(kfs):
        f = min(max(0, kf["frame"]), n - 1)
        q = (np.asarray(kf["quad"], np.float32) * scale).tolist()
        _track_segment(frames, f, q, bounds[i], bounds[i + 1], quads, conf)
        if progress:
            progress((i + 1) / len(kfs))
    fixed = {min(max(0, k["frame"]), n - 1) for k in kfs}
    quads = _smooth(quads, fixed)
    quads = (np.asarray(quads, np.float32) / scale).tolist()
    # pad if container reported more frames than decoded
    while len(quads) < n_frames:
        quads.append(quads[-1])
        conf.append(conf[-1])
    return quads, conf


# ====================================================================== colour mode
# For coloured covers / curtain-siders: find the cover in every frame by its colour,
# print only on the fabric, and hide the print while the curtain is folded open.
COLOUR_W = 640


def _hue_diff(h, ref):
    d = np.abs(h.astype(np.int16) - int(ref))
    return np.minimum(d, 180 - d)


def colour_stats(frame_bgr, quad) -> dict:
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    m = _poly_mask(frame_bgr.shape, quad, grow=-0.08)
    px = hsv[m > 0]
    if len(px) < 50:
        raise ValueError("Cover area too small")
    # circular median of hue
    ang = px[:, 0].astype(np.float32) * (2 * np.pi / 180)
    h = (np.degrees(np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())) / 2) % 180
    return {"h": float(h), "s": float(np.median(px[:, 1])), "v": float(np.median(px[:, 2]))}


def colour_mask(hsv, cs, tol=12):
    return ((_hue_diff(hsv[..., 0], cs["h"]) < tol) & (hsv[..., 1] > max(40, cs["s"] * 0.55))
            & (hsv[..., 2] > cs["v"] * 0.62)).astype(np.uint8) * 255


def _order(q):
    q = np.asarray(q, np.float32)
    s, d = q.sum(1), np.diff(q, axis=1).ravel()
    return np.float32([q[np.argmin(s)], q[np.argmin(d)], q[np.argmax(s)], q[np.argmax(d)]])


def _quad_from_region(reg):
    cnts, _ = cv2.findContours(reg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None, 0.0, 0.0
    c = max(cnts, key=cv2.contourArea)
    hull = cv2.convexHull(c)
    ap = None
    for eps in np.linspace(0.005, 0.12, 40):
        ap = cv2.approxPolyDP(hull, eps * cv2.arcLength(hull, True), True)
        if len(ap) <= 4:
            break
    if ap is None or len(ap) != 4:
        rect = cv2.boxPoints(cv2.minAreaRect(c))
        ap = rect.reshape(-1, 1, 2)
    q = _order(ap.reshape(4, 2))
    qa = cv2.contourArea(q)
    return q, (cv2.contourArea(c) / qa if qa else 0.0), float(cv2.contourArea(c))


def _measure(frame_small, cs):
    """Largest cover-coloured region -> (quad, solidity, area, fold score)."""
    hsv = cv2.cvtColor(frame_small, cv2.COLOR_BGR2HSV)
    m = cv2.morphologyEx(colour_mask(hsv, cs), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m)
    if n <= 1:
        return None, 0.0, 0.0, 99.0
    k = 1 + int(np.argmax(st[1:, 4]))
    reg = (lab == k).astype(np.uint8) * 255
    q, sol, area = _quad_from_region(reg)
    inner = cv2.erode(reg, np.ones((9, 9), np.uint8))
    if q is None or inner.sum() == 0:
        return None, 0.0, 0.0, 99.0
    gx = np.abs(cv2.Sobel(cv2.GaussianBlur(hsv[..., 2], (5, 5), 0), cv2.CV_32F, 1, 0, ksize=3))
    return q, sol, area, float(gx[inner > 0].mean())


def find_cover(frame_bgr):
    """Auto-detect a coloured cover: the largest strongly-coloured region, as 4 corners."""
    h, w = frame_bgr.shape[:2]
    sc = min(1.0, COLOUR_W / float(w))
    small = cv2.resize(frame_bgr, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA) if sc < 1 else frame_bgr
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    sat = hsv[..., 1]
    best = None
    for thr in (90, 70, 55):
        m = ((sat > thr) & (hsv[..., 2] > 60)).astype(np.uint8) * 255
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        n, lab, st, _ = cv2.connectedComponentsWithStats(m)
        if n <= 1:
            continue
        k = 1 + int(np.argmax(st[1:, 4]))
        if st[k, 4] < 0.01 * small.shape[0] * small.shape[1]:
            continue
        q, sol, area = _quad_from_region((lab == k).astype(np.uint8) * 255)
        if q is not None and sol > 0.8:
            best = q
            break
    if best is None:
        return None
    return (best / sc).tolist()


def track_colour(video_path, width, n_frames, keyframes, progress=None):
    kf = sorted(keyframes, key=lambda k: k["frame"])[0]
    cap = cv2.VideoCapture(video_path)
    sc = min(1.0, COLOUR_W / float(width))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(cv2.resize(f, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA) if sc < 1 else f)
    cap.release()
    n = len(frames)
    if n == 0:
        raise ValueError("Could not read the video")
    k0 = min(max(0, kf["frame"]), n - 1)
    ref_q = np.asarray(kf["quad"], np.float32) * sc
    cs = colour_stats(frames[k0], ref_q)
    q0, sol0, area0, g0 = _measure(frames[k0], cs)
    if q0 is None:
        raise ValueError("Couldn't find the cover colour on the saved frame - try 'Find cover automatically'")
    fold_lim = g0 * 1.6 + 1.0
    raw, ok = [], []
    for i, f in enumerate(frames):
        q, sol, area, g = _measure(f, cs)
        good = (q is not None and 0.92 < sol < 1.09 and 0.6 < area / max(1.0, area0) < 1.6 and g < fold_lim)
        raw.append(q if q is not None else None)
        ok.append(bool(good))
        if progress and i % 25 == 0:
            progress(i / n)
    # remove 1-2 frame flickers, then fade in/out over ~0.3 s
    okv = np.array(ok, np.float32)
    okv = np.array([okv[max(0, i - 2):i + 3].mean() >= 0.6 for i in range(n)], np.float32)
    fade = 8
    hidden = np.where(okv == 0)[0]
    vis = np.zeros(n, np.float32)
    for i in range(n):
        if okv[i]:
            d = np.min(np.abs(hidden - i)) if len(hidden) else 10 ** 6
            vis[i] = min(1.0, d / fade)
    # smooth corners inside visible runs; hold last good quad elsewhere
    quads, last = [], ref_q
    good_idx = [i for i in range(n) if ok[i] and raw[i] is not None]
    arr = {i: raw[i] for i in good_idx}
    for i in range(n):
        if i in arr:
            win = [arr[j] for j in range(i - 3, i + 4) if j in arr]
            last = np.mean(win, axis=0)
        quads.append((last / sc).tolist())
    conf = [1.0 if v > 0 else 0.0 for v in vis]
    while len(quads) < n_frames:
        quads.append(quads[-1]); conf.append(conf[-1]); vis = np.append(vis, vis[-1])
    return quads, conf, [round(float(v), 3) for v in vis[:max(n, n_frames)]], cs
