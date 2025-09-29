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
        print("npm path:", shutil.which("npm"))
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
    if language in ("js", "ts", "node", "tsx") and _npm_available():
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

        # Prefer npm lockfile resolution when possible (deterministic)
    pinned = {}
    warnings = []
    lockfile = {"type": "none", "content": None}

    try:
        if language in ("js", "ts", "node", "tsx") and _npm_available():
            # Attempt to run `npm install --package-lock-only` using the full package.json
            npm_res = resolve_with_npm_lockfile_fully(pkg_obj, list(collected.keys()))
            if npm_res.get("ok"):
                pinned = npm_res.get("pinned", {}) or {}
                lockfile = {"type": "package-lock", "content": npm_res.get("lockfile")}
                warnings.extend(npm_res.get("warnings", []) or [])
            else:
                # record npm error, will fallback to registry for all packages
                warnings.append(f"npm lockfile resolution failed: {npm_res.get('error', 'unknown')}")
        # For any missing packages (or if npm not available), use registry fallback (semver-aware if implemented)
        missing = [n for n in collected.keys() if n not in pinned]
        if missing:
            reg_res = _resolve_with_registry({n: collected.get(n) for n in missing})
            pinned.update(reg_res.get("pinned", {}) or {})
            warnings.extend(reg_res.get("warnings", []) or [])
            # if registry fallback used, mark lockfile as registry-fallback when no package-lock was produced
            if lockfile["type"] == "none":
                lockfile = {"type": "registry-fallback", "content": None}
    except Exception as e:
        # last resort: call resolve_and_pin (existing logic) to preserve behavior
        warnings.append(f"dependency resolution pipeline exception: {e}")
        try:
            fallback = resolve_and_pin(collected, language=language)
            pinned.update(fallback.get("pinned", {}) or {})
            warnings.extend(fallback.get("warnings", []) or [])
            if not lockfile or lockfile["type"] == "none":
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


# constants (tune as needed)
NPM_INSTALL_TIMEOUT = 120  # seconds
NPM_CMD = "npm"

def _run_npm_package_lock_only(pkg_json_obj: Dict[str, Any], timeout: int = NPM_INSTALL_TIMEOUT) -> Dict[str, Any]:
    """
    Given a package.json object, run `npm install --package-lock-only` in a tempdir
    and return parsed package-lock.json (or raise/return error info).
    The command uses --ignore-scripts to avoid running lifecycle scripts.
    """
    td = tempfile.mkdtemp(prefix="npm_resolve_")
    try:
        pj_path = Path(td) / "package.json"
        pj_path.write_text(json.dumps(pkg_json_obj), encoding="utf-8")

        # environment to reduce telemetry and avoid running scripts
        env = os.environ.copy()
        env["npm_config_audit"] = "false"
        env["npm_config_fund"] = "false"
        # ensure scripts are ignored
        # Note: --ignore-scripts flag passed to npm below is the key safety measure.
        cmd = [NPM_CMD, "install", "--package-lock-only", "--no-audit", "--no-fund", "--ignore-scripts"]

        proc = subprocess.run(cmd, cwd=td, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        out = proc.stdout or ""
        lock_path = Path(td) / "package-lock.json"
        if proc.returncode != 0:
            # still attempt to read lockfile if produced; otherwise raise informative error
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
            # pkg_path may be "" (root) or "node_modules/<name>" or deeper
            if not pkg_path.startswith("node_modules/"):
                continue
            nm = pkg_path.replace("node_modules/", "", 1)
            if nm in requested_names and isinstance(meta, dict):
                ver = meta.get("version")
                if ver:
                    pinned[nm] = ver

    return pinned

# Example wrapper you can call from resolve_and_pin_files
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
        # pass through error info
        return {"ok": False, "pinned": {}, "lockfile": None, "stdout": res.get("stdout", ""), "error": res.get("error"), "warnings": res.get("warnings", [])}

    lock_json = res.get("lockfile")
    pinned = _extract_pinned_from_lockfile(lock_json, requested_names)
    return {"ok": True, "pinned": pinned, "lockfile": lock_json, "stdout": res.get("stdout", ""), "warnings": res.get("warnings", [])}