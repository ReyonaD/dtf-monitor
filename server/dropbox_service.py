"""
Dropbox access for the agent Print-Files feature. The token lives ONLY here on
the server (never on agents). PRODUCTION is a team folder, so every call targets
the team namespace via the Dropbox-API-Path-Root header. Folder listings are
cached briefly so many agents asking for the same folder cost one Dropbox call.
"""
import os
import json
import time
import threading
import logging
import requests

logger = logging.getLogger(__name__)

DBX_TOKEN = os.environ.get("DROPBOX_TOKEN", "")             # legacy short-lived token (fallback)
DBX_APP_KEY = os.environ.get("DROPBOX_APP_KEY", "")
DBX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET", "")
DBX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN", "")  # permanent — mints access tokens
DBX_ROOT_NS = os.environ.get("DROPBOX_ROOT_NS", "")          # team namespace id
DBX_ROOT_PATH = os.environ.get("DROPBOX_ROOT_PATH", "/PRODUCTION")
API = "https://api.dropboxapi.com/2"
TIMEOUT = 20


def _clean_root(p: str) -> str:
    """Recover a clean Dropbox path if the env var got Windows-mangled by MSYS
    (e.g. '/PRODUCTION' set from Git Bash becomes 'C:/Program Files/Git/PRODUCTION')."""
    p = (p or "/PRODUCTION").strip().replace("\\", "/")
    if ":" in p or "program files" in p.lower():
        seg = [s for s in p.split("/") if s]
        p = "/" + seg[-1] if seg else "/PRODUCTION"
    if not p.startswith("/"):
        p = "/" + p
    return p


DBX_ROOT_PATH = _clean_root(DBX_ROOT_PATH)

# path -> (timestamp, entries)
_cache: dict[str, tuple[float, list]] = {}
_lock = threading.Lock()
CACHE_TTL = 20  # seconds


def configured() -> bool:
    return bool((DBX_REFRESH_TOKEN and DBX_APP_KEY and DBX_APP_SECRET) or DBX_TOKEN)


# ── access token: minted from the refresh token (permanent), else the legacy env token ──
_tok_lock = threading.Lock()
_access_token = ""
_access_expiry = 0.0


def _mint_access_token() -> tuple[str, float]:
    """Exchange the permanent refresh token for a fresh ~4h access token."""
    r = requests.post("https://api.dropboxapi.com/oauth2/token",
                      data={"grant_type": "refresh_token", "refresh_token": DBX_REFRESH_TOKEN},
                      auth=(DBX_APP_KEY, DBX_APP_SECRET), timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Dropbox token refresh {r.status_code}: {r.text[:300]}")
    d = r.json()
    return d["access_token"], time.time() + int(d.get("expires_in", 14400))


def _bearer() -> str:
    if not (DBX_REFRESH_TOKEN and DBX_APP_KEY and DBX_APP_SECRET):
        return DBX_TOKEN  # fallback to the legacy short-lived token
    global _access_token, _access_expiry
    with _tok_lock:
        if not _access_token or time.time() > _access_expiry - 120:
            _access_token, _access_expiry = _mint_access_token()
        return _access_token


def _headers(json_body: bool = True) -> dict:
    h = {"Authorization": f"Bearer {_bearer()}"}
    if DBX_ROOT_NS:
        h["Dropbox-API-Path-Root"] = json.dumps({".tag": "root", "root": DBX_ROOT_NS})
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _post(endpoint: str, payload: dict):
    r = requests.post(f"{API}/{endpoint}", headers=_headers(), json=payload, timeout=TIMEOUT)
    if r.status_code != 200:
        logger.warning("Dropbox %s -> %s: %s", endpoint, r.status_code, r.text[:400])
        raise RuntimeError(f"Dropbox {endpoint} {r.status_code}: {r.text[:300]}")
    return r.json()


def list_folder(path: str, use_cache: bool = True) -> list[dict]:
    """List a folder's immediate children (folders + files), normalized."""
    now = time.time()
    if use_cache:
        with _lock:
            c = _cache.get(path)
            if c and now - c[0] < CACHE_TTL:
                return c[1]

    entries = []
    data = _post("files/list_folder", {"path": path, "recursive": False, "limit": 2000})
    entries.extend(data.get("entries", []))
    while data.get("has_more"):
        r = requests.post(f"{API}/files/list_folder/continue", headers=_headers(),
                          json={"cursor": data["cursor"]}, timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"Dropbox list_folder/continue {r.status_code}: {r.text[:300]}")
        data = r.json()
        entries.extend(data.get("entries", []))

    out = [{
        "name": e.get("name"),
        "type": e.get(".tag"),               # "folder" | "file"
        "path": e.get("path_display"),
        "size": e.get("size", 0),
        "modified": e.get("server_modified") or e.get("client_modified"),
    } for e in entries]

    with _lock:
        _cache[path] = (now, out)
    return out


def temp_link(path: str) -> str:
    """A short-lived direct-download URL (served from Dropbox's CDN, not rate-limited)."""
    return _post("files/get_temporary_link", {"path": path}).get("link")


def move(from_path: str, to_path: str) -> dict:
    """Move a (printed) file to another folder. autorename avoids name clashes.
    Drops the cached listings of the source and target folders so agents see the
    change on their next poll instead of up to CACHE_TTL later."""
    out = _post("files/move_v2", {"from_path": from_path, "to_path": to_path, "autorename": True})
    with _lock:
        for p in (from_path.rsplit("/", 1)[0], to_path.rsplit("/", 1)[0]):
            for k in [k for k in _cache if k.lower() == p.lower()]:
                _cache.pop(k, None)
    return out


def ensure_folder(path: str):
    """Create a folder if it doesn't exist yet (e.g. a store's first BASILDI folder).
    Dropbox answers 409 path/conflict when it already exists — that's fine."""
    try:
        _post("files/create_folder_v2", {"path": path, "autorename": False})
    except RuntimeError as e:
        if "conflict" not in str(e):
            raise


def _search_extract(data: dict) -> list[dict]:
    out = []
    for m in data.get("matches", []):
        md = (m.get("metadata") or {}).get("metadata") or {}
        if md.get(".tag") == "file":
            out.append({
                "name": md.get("name"),
                "type": "file",
                "path": md.get("path_display"),
                "size": md.get("size", 0),
                "modified": md.get("server_modified") or md.get("client_modified"),
            })
    return out


def search(query: str, max_results: int = 1000) -> list[dict]:
    """Search filenames under PRODUCTION. search_v2 rejects an options.path in the
    team root namespace, so we search the whole (root) namespace and keep only the
    hits under PRODUCTION."""
    payload = {"query": query, "options": {
        "max_results": min(max_results, 1000),
        "file_status": "active",
        "filename_only": True,
    }}
    data = _post("files/search_v2", payload)
    out = _search_extract(data)
    guard = 0
    while data.get("has_more") and data.get("cursor") and guard < 5:
        data = _post("files/search/continue_v2", {"cursor": data["cursor"]})
        out.extend(_search_extract(data))
        guard += 1
    # keep only hits under PRODUCTION; Dropbox is case-insensitive so compare lowercased.
    prefix = ((DBX_ROOT_PATH or "").rstrip("/") + "/").lower()
    return [e for e in out if (e.get("path") or "").lower().startswith(prefix)]


# ── print claims — stop two machines grabbing the same file ──
# A file is "claimed" the instant an operator presses Print; other agents then
# see it as locked and can't take it. The claim clears when the file is moved to
# PRINTED on completion, or after CLAIM_TTL if the print never finishes.
_claims: dict[str, dict] = {}      # path -> {machine, operator, ts}
_claims_lock = threading.Lock()
CLAIM_TTL = 45 * 60  # seconds


def claim(path: str, machine: str, operator: str, force: bool = False):
    """Lock a file for `machine`. First machine wins: if another machine holds a live
    claim, return it (and do NOT take over) unless force=True. Returns None on success."""
    with _claims_lock:
        cur = _claims.get(path)
        if cur and cur.get("machine") != machine and time.time() - cur["ts"] <= CLAIM_TTL and not force:
            return dict(cur)
        _claims[path] = {"machine": machine, "operator": operator, "ts": time.time()}
        return None


def release(path: str):
    with _claims_lock:
        _claims.pop(path, None)


def active_claims() -> dict:
    now = time.time()
    with _claims_lock:
        for p in list(_claims):
            if now - _claims[p]["ts"] > CLAIM_TTL:
                _claims.pop(p, None)
        return {p: dict(v) for p, v in _claims.items()}
