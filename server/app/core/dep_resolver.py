# ai_backend_demo/app/core/dep_resolver.py
"""
Dependency Resolver Agent

Responsibilities:
- Given dependencies (name -> requested range or ""), produce pinned versions.
- Prefer deterministic resolution using `npm --package-lock-only` (if npm available).
- Fallback to registry queries (npm registry) to obtain latest versions.
- Provide helper to update package.json inside a list of generated files.

Returns:
- pinned results: {
    "resolved": [ {name, version|null, source, url|null, confidence, candidates? }, ... ],
    "pinned": {"pkg": "1.2.3", ...},
    "lockfile": {"type": "...", "content": ...},
    "warnings": [...]
  }
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import urllib.parse

import requests

LOG_DIR = os.environ.get("AI_BACKEND_LOG_DIR", "./ai_backend_logs")
os.makedirs(LOG_DIR, exist_ok=True)

NPM_REGISTRY = "https://registry.npmjs.org"
NPM_SEARCH = "https://registry.npmjs.org/-/v1/search"
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
        # atomic write
        tmp = NPM_CACHE_FILE + ".tmp"
        with open(tmp, "wb") as fh:
            pickle.dump(cache, fh)
        os.replace(tmp, NPM_CACHE_FILE)
    except Exception:
        pass

def _npm_available() -> bool:
    return shutil.which("npm") is not None

def _resolve_with_npm(deps: Dict[str, str]) -> Dict[str, Any]:
    """
    Use npm in a tempdir to create package-lock.json, then return pinned versions.
    This function now calls the canonical _run_npm_package_lock_only helper to
    ensure --ignore-scripts is used consistently.
    """
    warnings: List[str] = []
    pinned: Dict[str, str] = {}
    lockfile_content: Optional[Dict[str, Any]] = None

    # build minimal package.json with requested deps
    pkg = {"name": "ai-resolve-temp", "version": "0.0.0", "private": True, "dependencies": {}}
    for name, req in deps.items():
        pkg["dependencies"][name] = req if req else "latest"

    try:
        npm_res = _run_npm_package_lock_only(pkg)
        if not npm_res.get("ok"):
            warnings.append(f"npm install failed: {npm_res.get('error') or 'unknown'}")
            # try to salvage lockfile if present
            lock_json = npm_res.get("lockfile")
            if lock_json:
                lockfile_content = lock_json
                pinned = _extract_pinned_from_lockfile(lock_json, list(deps.keys()))
        else:
            lockfile_content = npm_res.get("lockfile")
            pinned = _extract_pinned_from_lockfile(lockfile_content, list(deps.keys()))
    except Exception as e:
        warnings.append(f"npm resolution failed: {e}")

    result = {"pinned": pinned, "lockfile": {"type": "package-lock", "content": lockfile_content}, "warnings": warnings}
    return result

def _search_registry(name: str, size: int = 5) -> List[Dict[str, Any]]:
    """
    Use npm search endpoint to return candidate packages for a given query.
    Returns a list of candidate dicts: {name, version, description, links}.
    """
    try:
        params = {"text": name, "size": size}
        resp = requests.get(NPM_SEARCH, params=params, timeout=8)
        if resp.status_code != 200:
            return []
        data = resp.json()
        objects = data.get("objects", []) or []
        out = []
        for obj in objects:
            pkg = obj.get("package", {}) or {}
            out.append({
                "name": pkg.get("name"),
                "version": pkg.get("version"),
                "description": pkg.get("description"),
                "links": pkg.get("links", {})
            })
        return out
    except Exception:
        return []

def _resolve_with_registry(deps: Dict[str, str]) -> Dict[str, Any]:
    """
    Fallback: query npm registry dist-tags.latest for each package.
    This picks the latest release (best-effort). Adds candidate search when exact lookup fails.
    Returns 'pinned' dict and 'resolved' list with candidate suggestions for missing packages.
    """
    cache = _load_cache()
    pinned: Dict[str, str] = {}
    warnings: List[str] = []
    resolved_list: List[Dict[str, Any]] = []

    for name in deps.keys():
        try:
            # Check cache first
            if name in cache:
                entry = cache[name]
                if time.time() - entry.get("ts", 0) < NPM_CACHE_TTL:
                    ver = entry.get("ver")
                    pinned[name] = ver
                    resolved_list.append({
                        "name": name,
                        "version": ver,
                        "source": "npm-cache",
                        "url": f"{NPM_REGISTRY}/{urllib.parse.quote(name, safe='')}",
                        "confidence": 0.95
                    })
                    continue

            # exact name lookup: URL-encode the package name (handles @scope/pkg)
            encoded = urllib.parse.quote(name, safe='')
            url = f"{NPM_REGISTRY}/{encoded}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                ver = None
                dist = data.get("dist-tags", {}) or {}
                ver = dist.get("latest") or data.get("version")
                if not ver:
                    versions = sorted(list(data.get("versions", {}).keys()))
                    if versions:
                        ver = versions[-1]
                if ver:
                    pinned[name] = ver
                    resolved_list.append({
                        "name": name,
                        "version": ver,
                        "source": "npm",
                        "url": f"{NPM_REGISTRY}/{encoded}",
                        "confidence": 0.98
                    })
                    cache[name] = {"ver": ver, "ts": time.time()}
                else:
                    # add unresolved but with candidates
                    candidates = _search_registry(name)
                    resolved_list.append({
                        "name": name,
                        "version": None,
                        "source": None,
                        "url": None,
                        "confidence": 0.0,
                        "candidates": candidates
                    })
                    warnings.append(f"no version found for {name} in registry response")
            elif resp.status_code == 404:
                # package not found - attempt search fallback
                candidates = _search_registry(name)
                if candidates:
                    # choose top candidate as tentative but low confidence
                    top = candidates[0]
                    top_name = top.get("name")
                    top_ver = top.get("version")
                    pinned[top_name] = top_ver
                    cache[top_name] = {"ver": top_ver, "ts": time.time()}
                    resolved_list.append({
                        "name": name,
                        "version": None,
                        "source": None,
                        "url": None,
                        "confidence": 0.0,
                        "candidates": candidates
                    })
                    warnings.append(f"{name} not found; suggested candidates returned")
                else:
                    resolved_list.append({
                        "name": name,
                        "version": None,
                        "source": None,
                        "url": None,
                        "confidence": 0.0,
                        "candidates": []
                    })
                    warnings.append(f"npm registry returned 404 for {name}")
            else:
                warnings.append(f"npm registry returned {resp.status_code} for {name}")
        except Exception as e:
            warnings.append(f"failed to query registry for {name}: {e}")
            resolved_list.append({
                "name": name,
                "version": None,
                "source": None,
                "url": None,
                "confidence": 0.0,
                "candidates": []
            })

    _save_cache(cache)
    return {"pinned": pinned, "resolved": resolved_list, "lockfile": {"type": "registry-fallback", "content": None}, "warnings": warnings}

def resolve_and_pin(deps: Dict[str, str], language: str = "js") -> Dict[str, Any]:
    """
    Public function: given a mapping of dependency name -> requested (range or '').
    Returns:
      {
        "resolved": [ {name, version|null, source, url|null, confidence, candidates?}, ... ],
        "pinned": { "react": "18.2.0", ... },
        "lockfile": {"type": "package-lock"|"registry-fallback", "content": {...} or None},
        "warnings": [...]
      }
    """
    if not deps:
        return {"resolved": [], "pinned": {}, "lockfile": {"type": "none", "content": None}, "warnings": []}

    if language in ("js", "ts", "node", "tsx") and _npm_available():
        res = _resolve_with_npm(deps)
        # ensure we have 'resolved' entries for each requested package
        pinned = res.get("pinned", {}) or {}
        missing = [n for n in deps.keys() if n not in pinned]
        resolved_combined: List[Dict[str, Any]] = []
        # add pinned entries with high confidence
        for n, v in pinned.items():
            resolved_combined.append({"name": n, "version": v, "source": "npm", "url": f"{NPM_REGISTRY}/{urllib.parse.quote(n, safe='')}", "confidence": 0.98})
        if missing:
            fallback = _resolve_with_registry({n: deps.get(n) for n in missing})
            pinned.update(fallback.get("pinned", {}) or {})
            # append fallback resolved entries
            for entry in fallback.get("resolved", []) or []:
                resolved_combined.append(entry)
            res["pinned"] = pinned
            res["resolved"] = resolved_combined
            res["warnings"].extend(fallback.get("warnings", []) or [])
        else:
            res["resolved"] = resolved_combined
        return res
    else:
        res = _resolve_with_registry(deps)
        return res

# ----------------------------
# Helper: update package.json inside files list
# ----------------------------
def resolve_and_pin_files(files: List[Dict[str, str]], options: Dict[str, Any]) -> (List[Dict[str, str]], Dict[str, Any]):
    """
    Given files (list of {path, content}), find package.json and pin its dependencies using resolve_and_pin.
    Returns (updated_files_list, meta)
    meta contains 'pinned' list and warnings and lockfile info, and now also 'resolved'.
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
        return files, {"warnings": [], "pinned": {}, "resolved": [], "lockfile": {"type": "none", "content": None}}

    # collect deps across sections
    collected = {}
    for sec in ("dependencies", "devDependencies", "peerDependencies"):
        sec_map = pkg_obj.get(sec, {}) or {}
        for name, req in sec_map.items():
            # normalize: if user accidentally added version spec in name, split on @ (naively)
            if isinstance(name, str) and "@" in name and name.startswith("@") is False and not name.startswith("http"):
                # avoid splitting scoped names like @scope/pkg
                # only split if pattern looks like "name@version"
                parts = name.split("@")
                if len(parts) == 2 and parts[1] and parts[0]:
                    nm = parts[0]
                    collected[nm] = req if isinstance(req, str) else ""
                else:
                    collected[name] = req if isinstance(req, str) else ""
            else:
                collected[name] = req if isinstance(req, str) else ""

    language = "js"
    if options and isinstance(options, dict) and options.get("language"):
        language = options.get("language")

    pinned = {}
    warnings = []
    lockfile = {"type": "none", "content": None}
    resolved_entries: List[Dict[str, Any]] = []

    try:
        res = resolve_and_pin(collected, language=language)
        pinned = res.get("pinned", {}) or {}
        lockfile = res.get("lockfile", {"type": "registry-fallback", "content": None}) or {"type": "registry-fallback", "content": None}
        resolved_entries = res.get("resolved", []) or []
        warnings.extend(res.get("warnings", []) or [])
    except Exception as e:
        warnings.append(f"dependency resolution pipeline exception: {e}")
        try:
            fallback = _resolve_with_registry(collected)
            pinned.update(fallback.get("pinned", {}) or {})
            resolved_entries.extend(fallback.get("resolved", []) or [])
            warnings.extend(fallback.get("warnings", []) or [])
            if lockfile["type"] == "none":
                lockfile = fallback.get("lockfile", {"type": "registry-fallback", "content": None})
        except Exception:
            pass

    # rewrite package sections with pinned versions
    for sec in ("dependencies", "devDependencies", "peerDependencies"):
        sec_map = pkg_obj.get(sec, {}) or {}
        if sec_map:
            new_sec = {}
            for name in sec_map.keys():
                if name in pinned:
                    new_sec[name] = pinned[name]
                else:
                    # preserve original request string or fallback to '*'
                    val = sec_map.get(name)
                    new_sec[name] = val if isinstance(val, str) and val.strip() else "*"
            pkg_obj[sec] = new_sec

    # serialize back
    try:
        files[pkg_idx]["content"] = json.dumps(pkg_obj, indent=2)
    except Exception as e:
        warnings.append(f"failed to serialize updated package.json: {e}")

    meta = {"pinned": pinned, "resolved": resolved_entries, "warnings": warnings, "lockfile": lockfile}
    return files, meta


# constants (tune as needed)
NPM_INSTALL_TIMEOUT = 120  # seconds
NPM_CMD = "npm"

def _run_npm_package_lock_only(pkg_json_obj: Dict[str, Any], timeout: int = NPM_INSTALL_TIMEOUT) -> Dict[str, Any]:
    """
    Given a package.json object, run `npm install --package-lock-only` in a tempdir
    and return parsed package-lock.json (or raise/return error info).
    Uses --ignore-scripts for safety.
    """
    td = tempfile.mkdtemp(prefix="npm_resolve_")
    try:
        pj_path = Path(td) / "package.json"
        pj_path.write_text(json.dumps(pkg_json_obj), encoding="utf-8")

        env = os.environ.copy()
        env["npm_config_audit"] = "false"
        env["npm_config_fund"] = "false"
        cmd = [NPM_CMD, "install", "--package-lock-only", "--no-audit", "--no-fund", "--ignore-scripts"]

        proc = subprocess.run(cmd, cwd=td, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        out = proc.stdout or ""
        lock_path = Path(td) / "package-lock.json"
        if proc.returncode != 0:
            if lock_path.exists():
                try:
                    lock_json = json.loads(lock_path.read_text(encoding="utf-8"))
                    return {"ok": True, "lockfile": lock_json, "stdout": out, "warnings": [f"npm exited {proc.returncode}, but lockfile present"]}
                except Exception as e:
                    return {"ok": False, "error": f"npm exited {proc.returncode}; failed to read lockfile: {e}", "stdout": out}
            return {"ok": False, "error": f"npm exited {proc.returncode}", "stdout": out}

        if not lock_path.exists():
            return {"ok": False, "error": "npm finished but package-lock.json missing", "stdout": out}

        try:
            lock_json = json.loads(lock_path.read_text(encoding="utf-8"))
            return {"ok": True, "lockfile": lock_json, "stdout": out}
        except Exception as e:
            return {"ok": False, "error": f"failed to parse package-lock.json: {e}", "stdout": out}
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "error": f"npm timed out after {timeout}s: {e}", "stdout": getattr(e, "output", "")}
    finally:
        try:
            shutil.rmtree(td, ignore_errors=True)
        except Exception:
            pass

def _extract_pinned_from_lockfile(lock_json: Dict[str, Any], requested_names: List[str]) -> Dict[str, str]:
    """
    Parse lockfile to obtain pinned versions for requested top-level packages.
    Handles both npm v1/v2 shapes:
      - lock_json.get("dependencies", {...})
      - lock_json.get("packages", {...}) where keys like "node_modules/<pkg>"
    Returns dict name -> version (only for names present in lockfile)
    """
    pinned: Dict[str, str] = {}

    # 1) try "dependencies" top-level (common)
    deps = lock_json.get("dependencies", {}) or {}
    for name in requested_names:
        info = deps.get(name)
        if isinstance(info, dict):
            ver = info.get("version")
            if ver:
                pinned[name] = ver

    # 2) fallback: "packages" object where keys include node_modules/<name>
    if len(pinned) < len(requested_names):
        packages = lock_json.get("packages", {}) or {}
        for pkg_path, meta in packages.items():
            if not isinstance(pkg_path, str):
                continue
            if not pkg_path.startswith("node_modules/"):
                continue
            nm = pkg_path.replace("node_modules/", "", 1)
            if nm in requested_names and isinstance(meta, dict):
                ver = meta.get("version")
                if ver:
                    pinned[nm] = ver

    return pinned

def resolve_with_npm_lockfile_fully(pkg_obj: Dict[str, Any], requested_names: List[str], timeout: int = NPM_INSTALL_TIMEOUT) -> Dict[str, Any]:
    """
    Given a package.json dict (pkg_obj) and a list of top-level dependency names, attempt to
    run npm --package-lock-only and extract pinned versions. Returns:
      {
        "ok": True/False,
        "pinned": {name: version, ...},
        "lockfile": {...} or None,
        "stdout": "npm output",
        "error": "optional error message",
        "warnings": [...]
      }
    """
    res = _run_npm_package_lock_only(pkg_obj, timeout=timeout)
    if not res.get("ok"):
        return {"ok": False, "pinned": {}, "lockfile": None, "stdout": res.get("stdout", ""), "error": res.get("error"), "warnings": res.get("warnings", [])}

    lock_json = res.get("lockfile")
    pinned = _extract_pinned_from_lockfile(lock_json, requested_names)
    return {"ok": True, "pinned": pinned, "lockfile": lock_json, "stdout": res.get("stdout", ""), "warnings": res.get("warnings", [])}
