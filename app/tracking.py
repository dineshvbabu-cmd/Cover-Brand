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
