"""CoverBrand: put each vendor's name/logo/contact onto a container-cover concept video."""
import base64
import csv
import io
import os
import secrets
import shutil
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from . import finishing as fin
from . import render, store, tracking, vendors as vparse
from .decal import DEFAULT_STYLE

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
STATIC = Path(__file__).parent / "static"
pool = ThreadPoolExecutor(max_workers=int(os.environ.get("RENDER_WORKERS", "2")))
finish_pool = ThreadPoolExecutor(max_workers=6)  # mostly waiting on Shotstack
app = FastAPI(title="CoverBrand")


# ---------- auth: everything except public share pages needs the password ----------
@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    public = path.startswith(("/s/", "/media/", "/assets/")) or path == "/health"
    if APP_PASSWORD and not public:
        h = request.headers.get("authorization", "")
        ok = False
        if h.startswith("Basic "):
            try:
                _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
                ok = secrets.compare_digest(pw, APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="CoverBrand"'})
    return await call_next(request)


def base_url(request: Request) -> str:
    if PUBLIC_URL:
        return PUBLIC_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def get_project(pid: str) -> dict:
    if not store.exists(pid):
        raise HTTPException(404, "Project not found")
    return store.load(pid)


def find_vendor(p, vid):
    for v in p["vendors"]:
        if v["id"] == vid:
            return v
    raise HTTPException(404, "Vendor not found")


@app.on_event("startup")
def recover_interrupted_jobs():
    """Jobs run in-process; after a restart/redeploy mark unfinished ones so they can be re-run."""
    try:
        store.ping()
        for pr in store.list_projects():
            def fn(q):
                if q["track"].get("status") == "running":
                    q["track"].update(status="error", error="Interrupted by a restart - click Track cover again")
                for r in q["renders"].values():
                    if r.get("status") in ("queued", "rendering"):
                        r.update(status="error", error="Interrupted by a restart - click Re-render")
            store.update(pr["id"], fn)
            # resume Shotstack jobs: they keep rendering on Shotstack's side
            for vid, r in pr.get("renders", {}).items():
                todo = [ra for ra, st in (r.get("finish") or {}).items()
                        if st.get("status") in ("queued", "submitted", "rendering")]
                if todo and fin.configured():
                    finish_pool.submit(_run_finish, pr["id"], vid, todo)
    except Exception:
        traceback.print_exc()


@app.get("/health")
def health():
    try:
        store.ping()
        return {"ok": True, "storage": store.backend()}
    except Exception as e:
        return JSONResponse({"ok": False, "storage": store.backend(), "error": str(e)}, status_code=503)


def ensure_local(pid: str, vendor: dict | None = None) -> None:
    """Make sure the working copies FFmpeg/OpenCV need are on local disk."""
    store.local(pid, "source.mp4")
    if vendor and vendor.get("logo"):
        store.local(pid, f"logos/{vendor['logo']}")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


# ---------- projects ----------
@app.get("/api/projects")
def projects():
    out = []
    for p in store.list_projects():
        kfs = sorted(p.get("cover", {}).get("keyframes", []), key=lambda k: k["frame"])
        out.append({k: p[k] for k in ("id", "name", "frames", "fps", "width", "height", "created")} |
                   {"vendors": len(p["vendors"]), "thumb": kfs[0]["frame"] if kfs else p["frames"] // 3,
                    "cover_ready": p.get("track", {}).get("status") == "done",
                    "made": sum(1 for r in p.get("renders", {}).values() if r.get("status") == "done")})
    return out


@app.put("/api/projects/{pid}")
def rename_project(pid: str, body: dict = Body(...)):
    get_project(pid)
    name = str(body.get("name", "")).strip()[:120]
    if not name:
        raise HTTPException(400, "Name required")
    return store.update(pid, lambda p: p.update(name=name))


@app.post("/api/projects")
async def create_project(file: UploadFile = File(...), name: str = Form("")):
    import time
    pid = store.new_id()
    d = store.pdir(pid)
    (d / "logos").mkdir(parents=True, exist_ok=True)
    (d / "out").mkdir(exist_ok=True)
    ext = Path(file.filename or "upload.mp4").suffix.lower() or ".mp4"
    raw = d / f"original{ext}"
    with raw.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        info = render.ingest(raw, d / "source.mp4")
        store.put_file(pid, "source.mp4", d / "source.mp4")
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, str(e))
    finally:
        raw.unlink(missing_ok=True)
    p = {"id": pid, "name": name or Path(file.filename or "concept").stem, "created": time.time(),
         "original": raw.name, "is_gif": ext == ".gif", **info,
         "cover": {"mode": "colour", "keyframes": [], "aspect_override": None},
         "track": {"status": "none", "progress": 0, "error": ""},
         "vendors": [], "style": dict(DEFAULT_STYLE), "renders": {}, "finish": dict(fin.DEFAULT_FINISH)}
    store.save(pid, p)
    return p


@app.get("/api/projects/{pid}")
def project(pid: str):
    p = get_project(pid)
    p["finish"] = {**fin.DEFAULT_FINISH, **(p.get("finish") or {})}
    return p


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    get_project(pid)
    store.delete_project(pid)
    return {"ok": True}


@app.get("/api/projects/{pid}/frame/{idx}.jpg")
def frame(pid: str, idx: int, w: int = 0):
    p = get_project(pid)
    import cv2
    f = render.read_frame(store.local(pid, "source.mp4"), max(0, min(idx, p["frames"] - 1)))
    if w and w < f.shape[1]:
        f = cv2.resize(f, (w, int(f.shape[0] * w / f.shape[1])), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return Response(buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


# ---------- cover marking + tracking ----------
@app.get("/api/projects/{pid}/detect/{idx}")
def detect_cover(pid: str, idx: int):
    """Find a coloured cover automatically on this frame (or another frame if not visible here)."""
    p = get_project(pid)
    src = store.local(pid, "source.mp4")
    idx = max(0, min(idx, p["frames"] - 1))
    q = tracking.find_cover(render.read_frame(src, idx))
    if not q:
        for j in range(0, p["frames"], max(1, p["frames"] // 8)):
            q = tracking.find_cover(render.read_frame(src, j))
            if q:
                idx = j
                break
    if not q:
        raise HTTPException(404, "No clearly coloured cover found - drag the dots by hand")
    return {"frame": idx, "quad": q}


@app.put("/api/projects/{pid}/cover")
def set_cover(pid: str, body: dict = Body(...)):
    get_project(pid)
    kfs = body.get("keyframes", [])
    for k in kfs:
        if len(k.get("quad", [])) != 4:
            raise HTTPException(400, "Each keyframe needs 4 corners")

    def fn(p):
        p["cover"] = {"mode": body.get("mode", "auto"), "keyframes": kfs,
                      "aspect_override": body.get("aspect_override")}
        p["track"] = {"status": "stale" if kfs else "none", "progress": 0, "error": ""}
    return store.update(pid, fn)


def _run_track(pid):
    try:
        p = store.load(pid)

        def prog(x):
            store.update(pid, lambda q: q["track"].update(progress=round(x, 3)))
        src = str(store.local(pid, "source.mp4"))
        if p["cover"]["mode"] == "colour":
            quads, conf, vis, cs = tracking.track_colour(src, p["width"], p["frames"], p["cover"]["keyframes"], prog)
            store.save_tracks(pid, {"quads": quads, "conf": conf, "vis": vis, "cs": cs})
        else:
            quads, conf = tracking.track(src, p["width"], p["frames"], p["cover"]["mode"], p["cover"]["keyframes"], prog)
            store.save_tracks(pid, {"quads": quads, "conf": conf})
        low = sum(1 for c in conf if c < 0.15)
        store.update(pid, lambda q: q.update(track={"status": "done", "progress": 1, "error": "",
                                                    "low_confidence_frames": low}))
    except Exception as e:
        traceback.print_exc()
        store.update(pid, lambda q: q.update(track={"status": "error", "progress": 0, "error": str(e)}))


@app.post("/api/projects/{pid}/track")
def start_track(pid: str):
    p = get_project(pid)
    if not p["cover"]["keyframes"]:
        raise HTTPException(400, "Mark the 4 corners of the cover first")
    store.update(pid, lambda q: q.update(track={"status": "running", "progress": 0, "error": ""}))
    pool.submit(_run_track, pid)
    return {"ok": True}


@app.get("/api/projects/{pid}/tracks")
def tracks(pid: str):
    get_project(pid)
    return store.load_tracks(pid) or {"quads": [], "conf": []}


def _quads_or_400(pid, p):
    t = store.load_tracks(pid)
    if not t or p["track"]["status"] != "done":
        raise HTTPException(400, "Save the cover position first (step 2)")
    return t if t.get("cs") else t["quads"]


# ---------- vendors ----------
def _save_logo(pid, vid, filename, blob) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in vparse.IMG_EXT:
        ext = ".png"
    name = f"{vid}{ext}"
    path = store.pdir(pid) / "logos" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    store.put_file(pid, f"logos/{name}", path)
    return name


@app.post("/api/projects/{pid}/vendors/import")
async def import_vendors(pid: str, files: list[UploadFile] = File(default=[]), text: str = Form("")):
    get_project(pid)
    found, logos, errors = [], [], []
    for f in files:
        data = await f.read()
        try:
            v, lg = vparse.parse_file(f.filename or "file", data)
            found += v
            logos += lg
        except Exception as e:
            errors.append(f"{f.filename}: {e}")
    if text.strip():
        found += vparse.from_text(text, "pasted", freeform=False)

    added, matched = [], 0

    def fn(p):
        nonlocal added, matched
        new = vparse.dedupe(p["vendors"], found)
        for v in new:
            v["id"] = store.new_id()
            v["logo"] = None
            want = v.pop("_logo_file", None)
            if want:
                blob = next((b for n, b in logos if n == want), None)
                if blob:
                    v["logo"] = _save_logo(pid, v["id"], want, blob)
                    matched += 1
        p["vendors"] += new
        for fn_, blob in logos:  # loose images: match by filename
            v = vparse.match_logo(fn_, p["vendors"])
            if v and not v.get("logo"):
                v["logo"] = _save_logo(pid, v["id"], fn_, blob)
                matched += 1
        added = new
    p = store.update(pid, fn)
    return {"added": len(added), "logos_matched": matched, "errors": errors, "vendors": p["vendors"]}


@app.post("/api/projects/{pid}/vendors/copy")
def copy_vendors(pid: str, body: dict = Body(...)):
    """Re-use the vendor list (with logos) of another sample."""
    get_project(pid)
    src = get_project(str(body.get("from", "")))
    new = vparse.dedupe(store.load(pid)["vendors"], [dict(v) for v in src["vendors"]])
    copied = []
    for v in new:
        old_id, v["id"] = v["id"], store.new_id()
        if v.get("logo"):
            f = store.local(src["id"], f"logos/{v['logo']}")
            if f.exists():
                v["logo"] = _save_logo(pid, v["id"], f.name, f.read_bytes())
            else:
                v["logo"] = None
        copied.append(v)
    p = store.update(pid, lambda q: q["vendors"].extend(copied))
    return {"added": len(copied), "vendors": p["vendors"]}


@app.put("/api/projects/{pid}/vendors")
def put_vendors(pid: str, body: list[dict] = Body(...)):
    get_project(pid)

    def fn(p):
        old = {v["id"]: v for v in p["vendors"]}
        out = []
        for v in body:
            vid = v.get("id") or store.new_id()
            base = old.get(vid, {"logo": None, "source": "manual"})
            out.append({**base, "id": vid, **{k: str(v.get(k, "") or "") for k in
                                              ("name", "contact", "email", "phone", "website")}})
        p["vendors"] = out
    return store.update(pid, fn)["vendors"]


@app.post("/api/projects/{pid}/vendors/{vid}/logo")
async def vendor_logo(pid: str, vid: str, file: UploadFile = File(...)):
    find_vendor(get_project(pid), vid)
    blob = await file.read()
    name = _save_logo(pid, vid, file.filename or "logo.png", blob)

    def fn(p):
        next(v for v in p["vendors"] if v["id"] == vid)["logo"] = name
    store.update(pid, fn)
    return {"logo": name}


@app.delete("/api/projects/{pid}/vendors/{vid}/logo")
def del_logo(pid: str, vid: str):
    def fn(p):
        next(v for v in p["vendors"] if v["id"] == vid)["logo"] = None
    store.update(pid, fn)
    return {"ok": True}


@app.get("/api/projects/{pid}/logo/{vid}")
def get_logo(pid: str, vid: str):
    v = find_vendor(get_project(pid), vid)
    if not v.get("logo"):
        raise HTTPException(404)
    f = store.local(pid, f"logos/{v['logo']}")
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f)


# ---------- style + previews ----------
@app.put("/api/projects/{pid}/style")
def set_style(pid: str, body: dict = Body(...)):
    get_project(pid)
    clean = {k: body[k] for k in DEFAULT_STYLE if k in body}
    return store.update(pid, lambda p: p["style"].update(clean))["style"]


@app.get("/api/projects/{pid}/decal/{vid}.png")
def decal(pid: str, vid: str):
    p = get_project(pid)
    if not p["cover"]["keyframes"]:
        raise HTTPException(400, "Mark the cover first")
    ensure_local(pid, find_vendor(p, vid))
    t = store.load_tracks(pid) or {}
    return Response(render.decal_png(p, find_vendor(p, vid), store.pdir(pid), t.get("cs")), media_type="image/png")


@app.get("/api/projects/{pid}/preview/{vid}/{idx}.jpg")
def preview(pid: str, vid: str, idx: int):
    p = get_project(pid)
    quads = _quads_or_400(pid, p)
    ensure_local(pid, find_vendor(p, vid))
    jpg = render.preview(p, find_vendor(p, vid), store.pdir(pid), quads, max(0, min(idx, p["frames"] - 1)))
    return Response(jpg, media_type="image/jpeg")


# ---------- rendering + sharing ----------
def _run_render(pid, vid):
    def setr(**kw):
        store.update(pid, lambda q: q["renders"].setdefault(vid, {}).update(kw))
    try:
        p = store.load(pid)
        v = find_vendor(p, vid)
        t = store.load_tracks(pid)
        quads = t if t.get("cs") else t["quads"]
        ensure_local(pid, v)
        out = store.pdir(pid) / "out" / f"{vid}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        setr(status="rendering", progress=0, finish={})
        render.render_vendor(p, v, store.pdir(pid), quads, out, p.get("is_gif", False),
                             lambda x: setr(progress=round(x, 3)))
        for ext in ("mp4", "gif", "jpg"):
            f = out.with_suffix("." + ext)
            if f.exists():
                store.put_file(pid, f"out/{vid}.{ext}", f)
        import time
        setr(status="done", progress=1, error="", rendered_at=time.time(), has_gif=p.get("is_gif", False))
        f = {**fin.DEFAULT_FINISH, **(p.get("finish") or {})}
        if f.get("enabled") and fin.configured() and f.get("ratios"):
            _queue_finish(pid, [vid], f["ratios"])
    except Exception as e:
        traceback.print_exc()
        setr(status="error", error=str(e))


@app.post("/api/projects/{pid}/render")
def start_render(pid: str, request: Request, body: dict = Body(default={})):
    p = get_project(pid)
    store.update(pid, lambda q: q.update(base_url=base_url(request)))
    _quads_or_400(pid, p)
    ids = body.get("vendor_ids") or [v["id"] for v in p["vendors"]]
    if not ids:
        raise HTTPException(400, "Add vendors first")

    def fn(q):
        for vid in ids:
            r = q["renders"].get(vid) or {}
            if not r.get("token"):
                r["token"] = secrets.token_urlsafe(9)
                store.add_share(r["token"], pid, vid)
            r.update(status="queued", progress=0, error="")
            q["renders"][vid] = r
    store.update(pid, fn)
    for vid in ids:
        pool.submit(_run_render, pid, vid)
    return {"queued": len(ids)}


@app.get("/api/projects/{pid}/links")
def links(pid: str, request: Request):
    p = get_project(pid)
    b = base_url(request)
    out = []
    for v in p["vendors"]:
        r = p["renders"].get(v["id"], {})
        out.append({"vendor_id": v["id"], "name": v["name"], "email": v["email"],
                    "phone": v.get("phone", ""), "contact": v.get("contact", ""),
                    "status": r.get("status", "not rendered"), "progress": r.get("progress", 0),
                    "error": r.get("error", ""),
                    "share_url": f"{b}/s/{r['token']}" if r.get("token") else "",
                    "video_url": f"{b}/media/{r['token']}.mp4" if r.get("status") == "done" else "",
                    "gif_url": f"{b}/media/{r['token']}.gif" if r.get("status") == "done" and r.get("has_gif") else "",
                    "finish": {ra: {"status": st.get("status"), "error": st.get("error", ""),
                                    "url": f"{b}/media/{r['token']}/{fin.SLUG[ra]}.mp4" if st.get("status") == "done" else ""}
                               for ra, st in (r.get("finish") or {}).items()}})
    return out


@app.get("/api/projects/{pid}/zip")
def zip_all(pid: str, request: Request):
    p = get_project(pid)
    rows = [l for l in links(pid, request) if l["status"] == "done"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(["Vendor", "Email", "Share link", "Video file"])
        for l in rows:
            safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in l["name"]).strip() or l["vendor_id"]
            fn = f"{safe}.mp4"
            z.write(store.local(pid, f"out/{l['vendor_id']}.mp4"), fn)
            gif = store.local(pid, f"out/{l['vendor_id']}.gif")
            if gif.exists():
                z.write(gif, f"{safe}.gif")
            for ra, st in l["finish"].items():
                if st["status"] == "done":
                    ff = store.local(pid, f"out/{l['vendor_id']}_{fin.SLUG[ra]}.mp4")
                    if ff.exists():
                        z.write(ff, f"{safe} - {fin.SLUG[ra]}.mp4")
            w.writerow([l["name"], l["email"], l["share_url"], fn])
        z.writestr("share_links.csv", s.getvalue())
    name = "".join(c if c.isalnum() else "_" for c in p["name"])
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}_vendor_videos.zip"'})


@app.get("/api/projects/{pid}/out/{vid}.jpg")
def out_thumb(pid: str, vid: str):
    f = store.local(pid, f"out/{vid}.jpg")
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f)


# ---------- designer finishing (Shotstack) ----------
@app.get("/api/finishing/options")
def finishing_options():
    return {"configured": fin.configured(), "env": fin.env_label(), "presets": fin.PRESETS,
            "music": {k: {"label": v[0], "url": v[1]} for k, v in fin.MUSIC.items()}, "ratios": list(fin.SIZES), "defaults": fin.DEFAULT_FINISH}


@app.put("/api/projects/{pid}/finish")
def set_finish(pid: str, body: dict = Body(...)):
    get_project(pid)
    clean = {k: body[k] for k in fin.DEFAULT_FINISH if k in body}
    if "ratios" in clean:
        clean["ratios"] = [r for r in clean["ratios"] if r in fin.SIZES]
    if "music" in clean and clean["music"] not in fin.MUSIC:
        clean["music"] = "none"

    def fn(p):
        p["finish"] = {**fin.DEFAULT_FINISH, **(p.get("finish") or {}), **clean}
    return store.update(pid, fn)["finish"]


@app.get("/api/projects/{pid}/finish/preview/{vid}/{ratio}.jpg")
def finish_preview(pid: str, vid: str, ratio: str, scene: str = "intro"):
    """Local still of the template (no Shotstack credits used)."""
    from PIL import Image
    p = get_project(pid)
    v = find_vendor(p, vid)
    ra = fin.RATIO_OF.get(ratio, ratio)
    if ra not in fin.SIZES:
        raise HTTPException(404)
    ensure_local(pid, v)
    f = {**fin.DEFAULT_FINISH, **(p.get("finish") or {})}
    out = store.local(pid, f"out/{vid}.jpg")
    if out.exists():
        frame = Image.open(out)
    else:
        import cv2
        fr = render.read_frame(store.local(pid, "source.mp4"), p["frames"] // 2)
        frame = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
    logo = None
    if v.get("logo"):
        lp = store.local(pid, f"logos/{v['logo']}")
        if lp.exists():
            logo = Image.open(lp)
    els, total = fin.BUILDERS.get(f["preset"], fin.corporate)(
        v, ra, p["frames"] / float(p["fps"] or 25), p["width"] / p["height"], f, "logo" if logo else None)
    t = {"intro": 2.6, "clip": 5.0, "end": total - 0.6}.get(scene, 2.6)
    im = fin.mock_frame(els, ra, t, frame, logo, f["dark"])
    im.thumbnail((960, 960))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return Response(buf.getvalue(), media_type="image/jpeg")


def _queue_finish(pid, vids, ratios):
    def fn(q):
        for vid in vids:
            r = q["renders"].setdefault(vid, {})
            fs = r.setdefault("finish", {})
            for ra in ratios:
                fs[ra] = {"status": "queued", "error": ""}
    store.update(pid, fn)
    for vid in vids:
        finish_pool.submit(_run_finish, pid, vid, list(ratios))


def _run_finish(pid, vid, ratios):
    def setf(ra, **kw):
        store.update(pid, lambda q: q["renders"][vid].setdefault("finish", {}).setdefault(ra, {}).update(kw))
    try:
        p = store.load(pid)
        v = find_vendor(p, vid)
        r = p["renders"].get(vid, {})
        b = PUBLIC_URL or p.get("base_url", "")
        tok = r["token"]
        urls = {"video": f"{b}/media/{tok}.mp4", "fonts": f"{b}/assets/fonts/",
                "logo": f"{b}/media/{tok}/logo.png" if v.get("logo") else None}
        ids = {}
        for ra in ratios:
            st = (r.get("finish") or {}).get(ra, {})
            if st.get("id") and st.get("status") in ("submitted", "rendering"):
                ids[ra] = st["id"]  # resuming after a restart
                continue
            try:
                rid = fin.submit(fin.build_edit(p, v, ra, urls))
                ids[ra] = rid
                setf(ra, status="submitted", id=rid, error="")
            except Exception as e:
                setf(ra, status="error", error=str(e))
        for ra, rid in ids.items():
            try:
                res = fin.wait(rid)
                if res.get("status") != "done":
                    setf(ra, status="error", error=res.get("error") or "Shotstack render failed")
                    continue
                setf(ra, status="rendering")
                dest = store.pdir(pid) / "out" / f"{vid}_{fin.SLUG[ra]}.mp4"
                fin.download(res["url"], dest)
                store.put_file(pid, f"out/{vid}_{fin.SLUG[ra]}.mp4", dest)
                setf(ra, status="done", error="")
            except Exception as e:
                setf(ra, status="error", error=str(e))
    except Exception as e:
        traceback.print_exc()
        for ra in ratios:
            setf(ra, status="error", error=str(e))


@app.post("/api/projects/{pid}/finish/render")
def start_finish(pid: str, request: Request, body: dict = Body(default={})):
    if not fin.configured():
        raise HTTPException(400, "Add SHOTSTACK_API_KEY in Railway to enable designer finishing")
    p = get_project(pid)
    store.update(pid, lambda q: q.update(base_url=base_url(request)))
    f = {**fin.DEFAULT_FINISH, **(p.get("finish") or {})}
    ratios = [r for r in (body.get("ratios") or f["ratios"]) if r in fin.SIZES]
    ids = body.get("vendor_ids") or [v["id"] for v in p["vendors"]]
    ready = [vid for vid in ids if p["renders"].get(vid, {}).get("status") == "done"]
    if not ready:
        raise HTTPException(400, "Render the cover clips first (step 5)")
    if not ratios:
        raise HTTPException(400, "Pick at least one size")
    _queue_finish(pid, ready, ratios)
    return {"queued": len(ready) * len(ratios), "skipped": len(ids) - len(ready)}


# ---------- public share pages ----------
def _share(token):
    s = store.get_share(token)
    if not s:
        raise HTTPException(404, "Link not found")
    p = get_project(s["pid"])
    v = find_vendor(p, s["vid"])
    r = p["renders"].get(v["id"], {})
    return p, v, r


FONT_NAMES = set(fin.FONT_FILES.values())


@app.get("/assets/fonts/{name}")
def font_file(name: str):
    if name not in FONT_NAMES:
        raise HTTPException(404)
    return FileResponse(fin.FONT_DIR / name, media_type="font/ttf",
                        headers={"Cache-Control": "public, max-age=86400", "Access-Control-Allow-Origin": "*"})


@app.get("/media/{token}/logo.png")
def media_logo(token: str):
    """Vendor logo as PNG, used by Shotstack when it builds the finished videos."""
    from PIL import Image
    p, v, r = _share(token)
    if not v.get("logo"):
        raise HTTPException(404)
    src = store.local(p["id"], f"logos/{v['logo']}")
    if not src.exists():
        raise HTTPException(404)
    im = Image.open(src).convert("RGBA")
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    im.thumbnail((1200, 1200))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "public, max-age=600"})


@app.get("/media/{token}/{slug}.mp4")
def media_finished(token: str, slug: str):
    p, v, r = _share(token)
    ra = fin.RATIO_OF.get(slug)
    if not ra or (r.get("finish") or {}).get(ra, {}).get("status") != "done":
        raise HTTPException(404)
    f = store.local(p["id"], f"out/{v['id']}_{slug}.mp4")
    if not f.exists():
        raise HTTPException(404)
    safe = "".join(c if c.isalnum() else "_" for c in v["name"])
    return FileResponse(f, media_type="video/mp4",
                        headers={"Content-Disposition": f'inline; filename="{safe}_{slug}.mp4"'})


@app.get("/media/{token}.{ext}")
def media(token: str, ext: str):
    p, v, r = _share(token)
    if ext not in ("mp4", "gif", "jpg") or r.get("status") != "done":
        raise HTTPException(404)
    f = store.local(p["id"], f"out/{v['id']}.{ext}")
    if not f.exists():
        raise HTTPException(404)
    safe = "".join(c if c.isalnum() else "_" for c in v["name"])
    headers = {"Content-Disposition": f'inline; filename="{safe}_concept.{ext}"'}
    return FileResponse(f, headers=headers)


@app.get("/s/{token}", response_class=HTMLResponse)
def share_page(token: str, request: Request):
    import html
    p, v, r = _share(token)
    tpl = (STATIC / "share.html").read_text()
    ready = r.get("status") == "done"
    b = base_url(request)
    done = [ra for ra in ("16:9", "1:1", "9:16") if (r.get("finish") or {}).get(ra, {}).get("status") == "done"]
    main = f"{b}/media/{token}/{fin.SLUG[done[0]]}.mp4" if done else f"{b}/media/{token}.mp4"
    labels = {"16:9": "Landscape 16:9", "9:16": "Vertical 9:16", "1:1": "Square 1:1"}
    btns = [f'<a class="btn {"primary" if i == 0 else "ghost"}" href="{b}/media/{token}/{fin.SLUG[ra]}.mp4" download>'
            f'Download {labels[ra]}</a>' for i, ra in enumerate(done)]
    btns.append(f'<a class="btn {"ghost" if done else "primary"}" href="{b}/media/{token}.mp4" download>'
                f'{"Cover clip only" if done else "Download MP4"}</a>')
    if r.get("has_gif"):
        btns.append(f'<a class="btn ghost" href="{b}/media/{token}.gif" download>Download GIF</a>')
    return (tpl.replace("{{VENDOR}}", html.escape(v["name"]))
               .replace("{{PROJECT}}", html.escape(p["name"]))
               .replace("{{VIDEO}}", main if ready else "")
               .replace("{{POSTER}}", f"{b}/media/{token}.jpg" if ready else "")
               .replace("{{DOWNLOADS}}", "\n    ".join(btns) if ready else "")
               .replace("{{READY}}", "1" if ready else "0")
               .replace("{{URL}}", f"{b}/s/{token}"))
