# ai_backend_demo/app/core/dep_resolver.py
"""
Dependency Resolver Agent

Responsibilities:
- Given dependencies (name -> requested range or ""), produce pinned versions.
- Prefer deterministic resolution using `npm --package-lock-only` (if npm available).
- Fallback to registry queries (npm registry) to obtain latest versions.
- Provide helper to update package.json inside a list of generated files.

Returns:
- pinned results: {"pinned": {"pkg": "1.2.3", ...}, "lockfile": {"type": "...", "content": ...}, "warnings": [...]}
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

import requests

LOG_DIR = os.environ.get("AI_BACKEND_LOG_DIR", "./ai_backend_logs")
os.makedirs(LOG_DIR, exist_ok=True)

NPM_REGISTRY = "https://registry.npmjs.org"
NPM_CACHE_FILE = os.path.join(LOG_DIR, "npm_cache.pkl")
NPM_CACHE_TTL = 24 * 3600

try:
    import pickle
except Exception:
    pickle = None

def _load_cache() -> Dict[str, Any]:
    if not pickle:
        return {}
    try:
        if os.path.exists(NPM_CACHE_FILE):
            mtime = os.path.getmtime(NPM_CACHE_FILE)
            if time.time() - mtime < NPM_CACHE_TTL:
                with open(NPM_CACHE_FILE, "rb") as fh:
                    return pickle.load(fh)
    except Exception:
        pass
    return {}

def _save_cache(cache: Dict[str, Any]):
    if not pickle:
        return
    try:
        with open(NPM_CACHE_FILE, "wb") as fh:
            pickle.dump(cache, fh)
    except Exception:
        pass

def _npm_available() -> bool:
    return shutil.which("npm") is not None

def _resolve_with_npm(deps: Dict[str, str]) -> Dict[str, Any]:
    """
    Use npm in a tempdir to create package-lock.json, then return pinned versions.
    """
    warnings: List[str] = []
    pinned: Dict[str, str] = {}
    lockfile_content: Optional[Dict[str, Any]] = None

    td = tempfile.mkdtemp(prefix="dep_resolve_")
    try:
        pkg = {"name": "ai-resolve-temp", "version": "0.0.0", "private": True, "dependencies": {}}
        for name, req in deps.items():
            # if req is falsy, use latest alias so npm resolves to latest
            pkg["dependencies"][name] = req if req else "latest"
        pj = Path(td) / "package.json"
        pj.write_text(json.dumps(pkg), encoding="utf-8")

        # run npm install --package-lock-only for deterministic lock generation
        cmd = ["npm", "install", "--package-lock-only", "--no-audit", "--no-fund"]
        proc = subprocess.run(cmd, cwd=td, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        out = proc.stdout or ""
        if proc.returncode != 0:
            warnings.append(f"npm install failed: exit {proc.returncode}; output: {out[:2000]}")
            # still try to read package-lock.json if exists
        lock_path = Path(td) / "package-lock.json"
        if lock_path.exists():
            try:
                lockfile_content = json.loads(lock_path.read_text(encoding="utf-8"))
                # package-lock v2: dependencies field
                deps_obj = lockfile_content.get("dependencies", {}) or {}
                for name in deps.keys():
                    meta = deps_obj.get(name) or {}
                    ver = meta.get("version")
                    if ver:
                        pinned[name] = ver
                # If pinned missing for some names, try to inspect packages object (npm <7)
                if not pinned:
                    # fallback: top-level packages?
                    packages = lockfile_content.get("packages", {})
                    for pkg_path, meta in packages.items():
                        # pkg_path like "node_modules/react"
                        if pkg_path.startswith("node_modules/"):
                            nm = pkg_path.replace("node_modules/", "")
                            ver = meta.get("version")
                            if nm and ver and nm in deps:
                                pinned[nm] = ver
            except Exception as e:
                warnings.append(f"failed to parse package-lock.json: {e}")
        else:
            warnings.append("npm did not write package-lock.json; cannot extract pinned versions")

    except Exception as e:
        warnings.append(f"npm resolution failed: {e}")
    finally:
        try:
            shutil.rmtree(td, ignore_errors=True)
        except Exception:
            pass

    result = {"pinned": pinned, "lockfile": {"type": "package-lock", "content": lockfile_content}, "warnings": warnings}
    return result

def _resolve_with_registry(deps: Dict[str, str]) -> Dict[str, Any]:
    """
    Fallback: query npm registry dist-tags.latest for each package.
    This picks the latest release (best-effort).
    """
    cache = _load_cache()
    pinned: Dict[str, str] = {}
    warnings: List[str] = []

    for name in deps.keys():
        try:
            if name in cache:
                entry = cache[name]
                if time.time() - entry.get("ts", 0) < NPM_CACHE_TTL:
                    pinned[name] = entry.get("ver")
                    continue
            url = f"{NPM_REGISTRY}/{name}"
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                warnings.append(f"npm registry returned {resp.status_code} for {name}")
                continue
            data = resp.json()
            ver = None
            dist = data.get("dist-tags", {})
            ver = dist.get("latest") or data.get("version")
            if not ver:
                # pick highest semver available (best-effort)
                versions = sorted(list(data.get("versions", {}).keys()))
                if versions:
                    ver = versions[-1]
            if ver:
                pinned[name] = ver
                cache[name] = {"ver": ver, "ts": time.time()}
        except Exception as e:
            warnings.append(f"failed to query registry for {name}: {e}")

    _save_cache(cache)
    return {"pinned": pinned, "lockfile": {"type": "registry-fallback", "content": None}, "warnings": warnings}

def resolve_and_pin(deps: Dict[str, str], language: str = "js") -> Dict[str, Any]:
    """
    Public function: given a mapping of dependency name -> requested (range or '').
    Returns:
      {
        "pinned": { "react": "18.2.0", ... },
        "lockfile": {"type": "package-lock"|"registry-fallback", "content": {...} or None},
        "warnings": [...]
      }
    """
    if not deps:
        return {"pinned": {}, "lockfile": {"type": "none", "content": None}, "warnings": []}

    # Prefer npm resolution for JS/TS projects if npm exists
    if language in ("js", "ts", "node") and _npm_available():
        res = _resolve_with_npm(deps)
        # if npm failed to produce pins for many packages, fallback to registry for missing entries
        missing = [n for n in deps.keys() if n not in res.get("pinned", {})]
        if missing:
            fallback = _resolve_with_registry({n: deps.get(n) for n in missing})
            res["pinned"].update(fallback.get("pinned", {}))
            res["warnings"].extend(fallback.get("warnings", []))
        return res
    else:
        # Non-JS projects or npm unavailable: use registry lookup
        res = _resolve_with_registry(deps)
        return res

# ----------------------------
# Helper: update package.json inside files list
# ----------------------------
def resolve_and_pin_files(files: List[Dict[str, str]], options: Dict[str, Any]) -> (List[Dict[str, str]], Dict[str, Any]):
    """
    Given files (list of {path, content}), find package.json and pin its dependencies using resolve_and_pin.
    Returns (updated_files_list, meta)
    meta contains 'pinned' list and warnings and lockfile info.
    """
    pkg_idx = None
    pkg_obj = None
    for i, f in enumerate(files):
        p = os.path.normpath(f.get("path", "") or "")
        if p == "package.json" or p.endswith("/package.json"):
            try:
                pkg_obj = json.loads(f.get("content", "") or "{}")
                pkg_idx = i
            except Exception as e:
                return files, {"warnings": [f"failed to parse package.json: {e}"]}

    if pkg_obj is None:
        return files, {"warnings": [], "pinned": {}, "lockfile": {"type": "none", "content": None}}

    # collect deps across sections
    collected = {}
    for sec in ("dependencies", "devDependencies", "peerDependencies"):
        sec_map = pkg_obj.get(sec, {}) or {}
        for name, req in sec_map.items():
            collected[name] = req if isinstance(req, str) else ""

    language = "js"
    if options and isinstance(options, dict) and options.get("language"):
        language = options.get("language")

    res = resolve_and_pin(collected, language=language)
    pinned = res.get("pinned", {}) or {}
    warnings = res.get("warnings", []) or []
    lockfile = res.get("lockfile", {})

    # rewrite package sections with pinned versions
    for sec in ("dependencies", "devDependencies", "peerDependencies"):
        sec_map = pkg_obj.get(sec, {}) or {}
        if sec_map:
            new_sec = {}
            for name in sec_map.keys():
                if name in pinned:
                    new_sec[name] = pinned[name]
                else:
                    # if pin missing, preserve original request string or fallback to '*'
                    val = sec_map.get(name)
                    new_sec[name] = val if isinstance(val, str) and val.strip() else "*"
            pkg_obj[sec] = new_sec

    # serialize back
    try:
        files[pkg_idx]["content"] = json.dumps(pkg_obj, indent=2)
    except Exception as e:
        warnings.append(f"failed to serialize updated package.json: {e}")

    meta = {"pinned": pinned, "warnings": warnings, "lockfile": lockfile}
    return files, meta
