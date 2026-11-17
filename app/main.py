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

from . import render, store, tracking, vendors as vparse
from .decal import DEFAULT_STYLE

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
STATIC = Path(__file__).parent / "static"
pool = ThreadPoolExecutor(max_workers=int(os.environ.get("RENDER_WORKERS", "2")))
app = FastAPI(title="CoverBrand")


# ---------- auth: everything except public share pages needs the password ----------
@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    public = path.startswith("/s/") or path.startswith("/media/") or path == "/health"
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
    return [{k: p[k] for k in ("id", "name", "frames", "fps", "width", "height", "created")} |
            {"vendors": len(p["vendors"])} for p in store.list_projects()]


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
         "cover": {"mode": "auto", "keyframes": [], "aspect_override": None},
         "track": {"status": "none", "progress": 0, "error": ""},
         "vendors": [], "style": dict(DEFAULT_STYLE), "renders": {}}
    store.save(pid, p)
    return p


@app.get("/api/projects/{pid}")
def project(pid: str):
    return get_project(pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    get_project(pid)
    store.delete_project(pid)
    return {"ok": True}


@app.get("/api/projects/{pid}/frame/{idx}.jpg")
def frame(pid: str, idx: int):
    p = get_project(pid)
    import cv2
    f = render.read_frame(store.local(pid, "source.mp4"), max(0, min(idx, p["frames"] - 1)))
    ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return Response(buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


# ---------- cover marking + tracking ----------
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
        quads, conf = tracking.track(str(store.local(pid, "source.mp4")), p["width"], p["frames"],
                                     p["cover"]["mode"], p["cover"]["keyframes"], prog)
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
        raise HTTPException(400, "Track the cover first (step 2)")
    return t["quads"]


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
    return Response(render.decal_png(p, find_vendor(p, vid), store.pdir(pid)), media_type="image/png")


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
        quads = store.load_tracks(pid)["quads"]
        ensure_local(pid, v)
        out = store.pdir(pid) / "out" / f"{vid}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        setr(status="rendering", progress=0)
        render.render_vendor(p, v, store.pdir(pid), quads, out, p.get("is_gif", False),
                             lambda x: setr(progress=round(x, 3)))
        for ext in ("mp4", "gif", "jpg"):
            f = out.with_suffix("." + ext)
            if f.exists():
                store.put_file(pid, f"out/{vid}.{ext}", f)
        import time
        setr(status="done", progress=1, error="", rendered_at=time.time(), has_gif=p.get("is_gif", False))
    except Exception as e:
        traceback.print_exc()
        setr(status="error", error=str(e))


@app.post("/api/projects/{pid}/render")
def start_render(pid: str, body: dict = Body(default={})):
    p = get_project(pid)
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
                    "status": r.get("status", "not rendered"), "progress": r.get("progress", 0),
                    "error": r.get("error", ""),
                    "share_url": f"{b}/s/{r['token']}" if r.get("token") else "",
                    "video_url": f"{b}/media/{r['token']}.mp4" if r.get("status") == "done" else "",
                    "gif_url": f"{b}/media/{r['token']}.gif" if r.get("status") == "done" and r.get("has_gif") else ""})
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


# ---------- public share pages ----------
def _share(token):
    s = store.get_share(token)
    if not s:
        raise HTTPException(404, "Link not found")
    p = get_project(s["pid"])
    v = find_vendor(p, s["vid"])
    r = p["renders"].get(v["id"], {})
    return p, v, r


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
    return (tpl.replace("{{VENDOR}}", html.escape(v["name"]))
               .replace("{{PROJECT}}", html.escape(p["name"]))
               .replace("{{VIDEO}}", f"{b}/media/{token}.mp4" if ready else "")
               .replace("{{POSTER}}", f"{b}/media/{token}.jpg" if ready else "")
               .replace("{{GIF}}", f"{b}/media/{token}.gif" if ready and r.get("has_gif") else "")
               .replace("{{READY}}", "1" if ready else "0")
               .replace("{{URL}}", f"{b}/s/{token}"))
