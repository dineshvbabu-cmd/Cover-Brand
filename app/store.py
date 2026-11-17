"""Storage layer.

With MONGO_URL set (Railway: ${{MongoDB.MONGO_URL}}):
  - projects, tracks and share links live in MongoDB collections
  - videos, GIFs, thumbnails and logos live in MongoDB GridFS
  - CACHE_DIR is only a local working copy (FFmpeg/OpenCV need real files);
    it can be wiped at any time and is refilled from GridFS on demand.
Without MONGO_URL everything is kept as files under DATA_DIR (local development).
"""
import json
import os
import secrets
import shutil
import threading
from pathlib import Path

MONGO_URL = os.environ.get("MONGO_URL", "").strip()
MONGO_DB = os.environ.get("MONGO_DB", "coverbrand")
_default_dir = Path(__file__).resolve().parent.parent / "data"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/coverbrand" if MONGO_URL else _default_dir))
CACHE = DATA_DIR / "projects"
CACHE.mkdir(parents=True, exist_ok=True)
SHARES_FILE = DATA_DIR / "shares.json"

_locks: dict[str, threading.RLock] = {}
_global = threading.RLock()

# ---------------------------------------------------------------- backend
_db = None
_fs = None


def _mongo():
    global _db, _fs
    if _db is None:
        import gridfs
        import pymongo
        client = pymongo.MongoClient(MONGO_URL, serverSelectionTimeoutMS=10000)
        _db = client[MONGO_DB]
        _fs = gridfs.GridFS(_db, collection="files")
        _db.projects.create_index("created")
        _db.shares.create_index("pid")
        _db["files.files"].create_index("filename")
    return _db, _fs


def use_mongo(db, fs) -> None:
    """For tests: inject a database / GridFS pair."""
    global _db, _fs, MONGO_URL
    _db, _fs, MONGO_URL = db, fs, MONGO_URL or "injected"


def backend() -> str:
    return "mongodb" if MONGO_URL else "files"


def ping() -> bool:
    if not MONGO_URL:
        return True
    db, _ = _mongo()
    db.command("ping")
    return True


# ---------------------------------------------------------------- helpers
def lock(pid: str) -> threading.RLock:
    with _global:
        return _locks.setdefault(pid, threading.RLock())


def new_id(n: int = 8) -> str:
    return secrets.token_hex(n // 2)


def _safe(pid: str) -> str:
    return "".join(c for c in pid if c.isalnum())


def pdir(pid: str) -> Path:
    """Local working folder for a project (cache when using MongoDB)."""
    return CACHE / _safe(pid)


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def exists(pid: str) -> bool:
    if MONGO_URL:
        db, _ = _mongo()
        return db.projects.count_documents({"_id": _safe(pid)}, limit=1) > 0
    return (pdir(pid) / "project.json").exists()


# ---------------------------------------------------------------- projects
def load(pid: str) -> dict:
    with lock(pid):
        if MONGO_URL:
            db, _ = _mongo()
            doc = db.projects.find_one({"_id": _safe(pid)})
            if not doc:
                raise FileNotFoundError(pid)
            doc.pop("_id", None)
            return doc
        return json.loads((pdir(pid) / "project.json").read_text())


def save(pid: str, data: dict) -> None:
    with lock(pid):
        if MONGO_URL:
            db, _ = _mongo()
            db.projects.replace_one({"_id": _safe(pid)}, {**data, "_id": _safe(pid)}, upsert=True)
        else:
            pdir(pid).mkdir(parents=True, exist_ok=True)
            _write_json(pdir(pid) / "project.json", data)


def update(pid: str, fn) -> dict:
    """Read-modify-write a project under its lock (single app process)."""
    with lock(pid):
        data = load(pid)
        fn(data)
        save(pid, data)
        return data


def list_projects() -> list[dict]:
    if MONGO_URL:
        db, _ = _mongo()
        out = []
        for d in db.projects.find({}, {"tracks": 0}).sort("created", -1):
            d.pop("_id", None)
            out.append(d)
        return out
    out = []
    for d in sorted(CACHE.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        f = d / "project.json"
        if f.exists():
            try:
                out.append(json.loads(f.read_text()))
            except Exception:
                pass
    return out


def delete_project(pid: str) -> None:
    with lock(pid):
        if MONGO_URL:
            db, fs = _mongo()
            sp = _safe(pid)
            db.projects.delete_one({"_id": sp})
            db.tracks.delete_one({"_id": sp})
            db.shares.delete_many({"pid": sp})
            for f in db["files.files"].find({"filename": {"$regex": f"^{sp}/"}}, {"_id": 1}):
                fs.delete(f["_id"])
        else:
            s = shares()
            s = {k: v for k, v in s.items() if v["pid"] != pid}
            with _global:
                _write_json(SHARES_FILE, s)
        shutil.rmtree(pdir(pid), ignore_errors=True)


# ---------------------------------------------------------------- tracks
def load_tracks(pid: str):
    if MONGO_URL:
        db, _ = _mongo()
        d = db.tracks.find_one({"_id": _safe(pid)})
        return {"quads": d["quads"], "conf": d["conf"]} if d else None
    f = pdir(pid) / "tracks.json"
    return json.loads(f.read_text()) if f.exists() else None


def save_tracks(pid: str, tracks) -> None:
    if MONGO_URL:
        db, _ = _mongo()
        db.tracks.replace_one({"_id": _safe(pid)}, {"_id": _safe(pid), **tracks}, upsert=True)
    else:
        _write_json(pdir(pid) / "tracks.json", tracks)


# ---------------------------------------------------------------- files
def put_file(pid: str, rel: str, local: Path) -> None:
    """Persist a local working file (e.g. 'source.mp4', 'logos/ab12.png', 'out/ab12.mp4')."""
    if not MONGO_URL:
        return  # already in DATA_DIR
    db, fs = _mongo()
    name = f"{_safe(pid)}/{rel}"
    old = [f["_id"] for f in db["files.files"].find({"filename": name}, {"_id": 1})]
    with open(local, "rb") as fh:
        fs.put(fh, filename=name)
    for oid in old:
        fs.delete(oid)


def local(pid: str, rel: str) -> Path:
    """Path to a working copy of a stored file, downloading it from GridFS if needed."""
    p = pdir(pid) / rel
    if p.exists() or not MONGO_URL:
        return p
    db, fs = _mongo()
    name = f"{_safe(pid)}/{rel}"
    with lock(f"file:{name}"):
        if p.exists():
            return p
        doc = db["files.files"].find_one({"filename": name}, sort=[("uploadDate", -1)])
        if not doc:
            return p  # caller checks .exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".dl")
        with open(tmp, "wb") as out:
            out.write(fs.get(doc["_id"]).read())
        os.replace(tmp, p)
    return p


def has_file(pid: str, rel: str) -> bool:
    return local(pid, rel).exists()


# ---------------------------------------------------------------- share tokens (public links)
def shares() -> dict:
    if MONGO_URL:
        db, _ = _mongo()
        return {d["_id"]: {"pid": d["pid"], "vid": d["vid"]} for d in db.shares.find()}
    with _global:
        return json.loads(SHARES_FILE.read_text()) if SHARES_FILE.exists() else {}


def get_share(token: str):
    if MONGO_URL:
        db, _ = _mongo()
        d = db.shares.find_one({"_id": token})
        return {"pid": d["pid"], "vid": d["vid"]} if d else None
    return shares().get(token)


def add_share(token: str, pid: str, vid: str) -> None:
    if MONGO_URL:
        db, _ = _mongo()
        db.shares.replace_one({"_id": token}, {"_id": token, "pid": _safe(pid), "vid": vid}, upsert=True)
        return
    with _global:
        s = shares()
        s[token] = {"pid": pid, "vid": vid}
        _write_json(SHARES_FILE, s)
