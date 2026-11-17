"""Simple JSON-on-disk storage. One folder per project under DATA_DIR."""
import json
import os
import secrets
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
PROJECTS = DATA_DIR / "projects"
PROJECTS.mkdir(parents=True, exist_ok=True)
SHARES_FILE = DATA_DIR / "shares.json"

_locks: dict[str, threading.RLock] = {}
_global = threading.RLock()


def lock(pid: str) -> threading.RLock:
    with _global:
        return _locks.setdefault(pid, threading.RLock())


def new_id(n: int = 8) -> str:
    return secrets.token_hex(n // 2)


def pdir(pid: str) -> Path:
    safe = "".join(c for c in pid if c.isalnum())
    return PROJECTS / safe


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def load(pid: str) -> dict:
    with lock(pid):
        return json.loads((pdir(pid) / "project.json").read_text())


def save(pid: str, data: dict) -> None:
    with lock(pid):
        _write_json(pdir(pid) / "project.json", data)


def update(pid: str, fn) -> dict:
    """Read-modify-write a project atomically under its lock."""
    with lock(pid):
        data = load(pid)
        fn(data)
        save(pid, data)
        return data


def list_projects() -> list[dict]:
    out = []
    for d in sorted(PROJECTS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        f = d / "project.json"
        if f.exists():
            try:
                out.append(json.loads(f.read_text()))
            except Exception:
                pass
    return out


def load_tracks(pid: str):
    f = pdir(pid) / "tracks.json"
    return json.loads(f.read_text()) if f.exists() else None


def save_tracks(pid: str, tracks) -> None:
    _write_json(pdir(pid) / "tracks.json", tracks)


# ---- share tokens (public links) ----
def shares() -> dict:
    with _global:
        return json.loads(SHARES_FILE.read_text()) if SHARES_FILE.exists() else {}


def add_share(token: str, pid: str, vid: str) -> None:
    with _global:
        s = shares()
        s[token] = {"pid": pid, "vid": vid}
        _write_json(SHARES_FILE, s)
