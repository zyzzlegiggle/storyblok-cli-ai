# ai_backend_demo/app/core/chain.py
import os
import json
import time
import shutil
import subprocess
import tempfile
import traceback
import pickle
from pathlib import Path
from typing import Dict, Any, List, Optional

from core.llm_client import call_structured_generation, GenerateResponseModel
from core.prompts import build_system_prompt, build_user_prompt

# ----------------------------
# Configuration / env overrides
# ----------------------------
CHUNK_SIZE = int(os.environ.get("AI_CHUNK_SIZE", 10))
LLM_RETRIES = int(os.environ.get("AI_RETRY_COUNT", 2))
TIMEOUT = int(os.environ.get("AI_TIMEOUT", 180))
LOG_DIR = os.environ.get("AI_BACKEND_LOG_DIR", "./ai_backend_logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ----------------------------
# NPM registry helpers & curated map
# ----------------------------
import requests

NPM_REGISTRY = "https://registry.npmjs.org"
NPM_CACHE_FILE = os.path.join(LOG_DIR, "npm_cache.pkl")
NPM_CACHE_TTL = 24 * 3600  # seconds

# curated dependency map file path (one level up from this file: ai_backend_demo/dependency_map.json)
DEP_MAP_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dependency_map.json")
try:
    with open(DEP_MAP_PATH, "r", encoding="utf-8") as fh:
        CURATED_DEP_MAP = json.load(fh)
except Exception:
    CURATED_DEP_MAP = {}

def _load_npm_cache():
    try:
        if os.path.exists(NPM_CACHE_FILE):
            mtime = os.path.getmtime(NPM_CACHE_FILE)
            if time.time() - mtime < NPM_CACHE_TTL:
                with open(NPM_CACHE_FILE, "rb") as fh:
                    return pickle.load(fh)
    except Exception:
        pass
    return {}

def _save_npm_cache(cache):
    try:
        with open(NPM_CACHE_FILE, "wb") as fh:
            pickle.dump(cache, fh)
    except Exception:
        pass

def get_latest_npm_version(pkg_name: str) -> Optional[str]:
    """
    Query npm registry for dist-tags.latest. Returns version string or None.
    Caches results for NPM_CACHE_TTL.
    """
    try:
        cache = _load_npm_cache()
        key = pkg_name
        if key in cache:
            entry = cache[key]
            if time.time() - entry.get("ts", 0) < NPM_CACHE_TTL:
                return entry.get("ver")
        url = f"{NPM_REGISTRY}/{pkg_name}"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        dist_tags = data.get("dist-tags", {})
        latest = dist_tags.get("latest")
        if latest:
            cache[key] = {"ver": latest, "ts": time.time()}
            _save_npm_cache(cache)
            return latest
        if "version" in data:
            v = data.get("version")
            cache[key] = {"ver": v, "ts": time.time()}
            _save_npm_cache(cache)
            return v
        return None
    except Exception:
        return None

def _pin_dep_version(name: str, requested: Any) -> (str, str):
    """
    Return (name, pinned_version). Prefer curated map entries; otherwise query npm; otherwise fallback.
    """
    # prefer curated map (search across stacks)
    try:
        for stack, mapping in CURATED_DEP_MAP.items():
            deps = mapping.get("dependencies", {})
            devs = mapping.get("devDependencies", {})
            if name in deps:
                return name, deps[name]
            if name in devs:
                return name, devs[name]
    except Exception:
        pass

    # query npm
    latest = get_latest_npm_version(name)
    if latest:
        return name, latest

    # fallback: strip ^/~ if requested is str
    try:
        if isinstance(requested, str):
            stripped = requested.lstrip("^~")
            if stripped:
                return name, stripped
    except Exception:
        pass
    return name, "1.0.0"

def resolve_and_pin_dependencies(files: List[Dict[str, str]], options: Dict[str, Any]) -> (List[Dict[str, str]], Dict[str, Any]):
    """
    Find package.json in files, pin dependency versions using curated map + npm registry.
    Returns updated files and a metadata dict about dependency resolution.
    """
    meta = {"resolved": [], "warnings": [], "lockfile": {"skipped": True, "reason": "npm not available on backend"}}
    pkg_idx = None
    pkg_obj = None
    for i, f in enumerate(files):
        path = os.path.normpath(f.get("path", ""))
        if path == "package.json" or path.endswith("/package.json"):
            try:
                pkg_obj = json.loads(f.get("content", "") or "{}")
                pkg_idx = i
            except Exception as e:
                meta["warnings"].append(f"failed to parse package.json: {e}")
                pkg_obj = None
            break

    if pkg_obj is None:
        return files, meta

    for sec in ("dependencies", "devDependencies", "peerDependencies"):
        sec_map = pkg_obj.get(sec, {}) or {}
        pinned = {}
        for name, requested in sec_map.items():
            nm, pv = _pin_dep_version(name, requested)
            pinned[nm] = pv
            meta["resolved"].append({"name": nm, "version": pv, "origin": "curated_or_registry"})
        if pinned:
            pkg_obj[sec] = pinned

    try:
        files[pkg_idx]["content"] = json.dumps(pkg_obj, indent=2)
    except Exception:
        meta["warnings"].append("failed to re-serialize package.json after pinning")

    meta["lockfile"] = {"skipped": True, "reason": "npm not available on backend (no lockfile generated)"}
    return files, meta

# ----------------------------
# Utility & file helpers
# ----------------------------
def chunk_components(components: List[Dict[str, Any]], chunk_size: int = CHUNK_SIZE):
    for i in range(0, len(components), chunk_size):
        yield components[i:i + chunk_size]

def run_tsc_check(project_dir: str) -> Dict[str, Any]:
    """
    Best-effort TypeScript check. Uses npx tsc --noEmit or tsc --noEmit if available.
    """
    cmd = None
    if shutil.which("npx"):
        cmd = ["npx", "tsc", "--noEmit"]
    elif shutil.which("tsc"):
        cmd = ["tsc", "--noEmit"]
    else:
        return {"ok": False, "skipped": True, "output": "tsc not found; skipping TypeScript validation"}
    try:
        proc = subprocess.run(cmd, cwd=project_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        ok = proc.returncode == 0
        return {"ok": ok, "skipped": False, "output": proc.stdout}
    except Exception as e:
        return {"ok": False, "skipped": False, "output": f"tsc execution failed: {e}"}

# ----------------------------
# Robust parsing helpers for LLM outputs
# ----------------------------
def _ensure_parsed_dict(name: str, parsed) -> Dict[str, Any]:
    """
    Ensure parsed is a plain dict. If not, attempt conversions or write debug file and return {}.
    """
    try:
        if parsed is None:
            fname = f"{int(time.time())}_{name}_none.log"
            with open(os.path.join(LOG_DIR, fname), "w", encoding="utf-8") as fh:
                fh.write(f"{name} returned None\n")
            return {}
        if isinstance(parsed, dict):
            return parsed
        if hasattr(parsed, "dict"):
            try:
                return parsed.dict()
            except Exception:
                pass
        if hasattr(parsed, "__dict__"):
            try:
                return dict(parsed.__dict__)
            except Exception:
                pass
        s = str(parsed)
        try:
            return json.loads(s)
        except Exception:
            fname = f"{int(time.time())}_{name}_unparseable.json"
            with open(os.path.join(LOG_DIR, fname), "w", encoding="utf-8") as fh:
                fh.write("UNPARSEABLE DIAGNOSTIC RESULT\n\n")
                fh.write("repr(parsed):\n")
                fh.write(repr(parsed) + "\n\n")
                fh.write("str(parsed):\n")
                fh.write(s + "\n\n")
                fh.write("traceback:\n")
                fh.write(traceback.format_exc())
            return {}
    except Exception:
        fname = f"{int(time.time())}_{name}_ensure_exception.log"
        with open(os.path.join(LOG_DIR, fname), "w", encoding="utf-8") as fh:
            fh.write("Exception in _ensure_parsed_dict:\n")
            fh.write(traceback.format_exc())
        return {}

# ----------------------------
# Followup normalization
# ----------------------------
def _normalize_followup_item(raw) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, str):
        return {"id": f"q_{int(time.time()*1000)}", "question": raw, "type": "text", "default": ""}
    if isinstance(raw, dict):
        qid = raw.get("id") or raw.get("name") or f"q_{int(time.time()*1000)}"
        question = raw.get("question") or raw.get("prompt") or raw.get("text") or ""
        ftype = raw.get("type") or "text"
        default = raw.get("default") or ""
        return {"id": str(qid), "question": str(question), "type": str(ftype), "default": default}
    return None

STREAM_CHUNK_SZ = int(os.environ.get("AI_STREAM_CHUNK_SZ", 1024))

async def _yield_event(event_type: str, payload: Any):
    """
    Small helper to produce newline-delimited JSON events (string).
    Caller can 'yield' these strings from the async generator.
    """
    try:
        out = {"event": event_type, "payload": payload}
        return json.dumps(out, ensure_ascii=False) + "\n"
    except Exception:
        # best-effort fallback
        return json.dumps({"event": "error", "payload": f"failed to serialize {event_type}"}) + "\n"


async def stream_generate_project(payload: Dict[str, Any]):
    """
    Async generator that yields newline-delimited JSON events (as strings).
    This function mirrors generate_project_files but streams progress as events.
    """
    user_answers = payload.get("user_answers", {}) or {}
    schema = payload.get("storyblok_schema", {}) or {}
    options = payload.get("options", {}) or {}
    debug = bool(options.get("debug", False))
    followup_answers = user_answers.get("followup_answers", {}) or {}

    app_name = user_answers.get("app_name", user_answers.get("project_name", "storyblok-app"))

    system_prompt = build_system_prompt()
    user_for_prompt = dict(user_answers)
    user_for_prompt.update({"followup_answers": followup_answers})
    user_prompt = build_user_prompt(user_for_prompt, schema, options)

    # Diagnostic / question generation (same logic as generate_project_files)
    diag_parsed = None
    try:
        max_questions = int(options.get("max_questions", 3))
        request_questions = bool(options.get("request_questions", False))

        if request_questions:
            q_instruction = (
                system_prompt
                + "\n\n"
                + user_prompt
                + f"\n\nYou are asked to produce up to {max_questions} structured clarifying questions to gather requirements from the user. "
                  'Return JSON exactly like: {"followups":[{"id":"q1","question":"...","type":"text","default":"..."}, ...]} '
                  "If you think no clarifying questions are needed, return {\"followups\":[]}."
                  "Respond only with valid JSON of that shape."
            )
            diag_parsed = await call_structured_generation(q_instruction, GenerateResponseModel, max_retries=1, timeout=30, debug=debug)
        else:
            diag_instruction = (
                system_prompt
                + "\n\n"
                + user_prompt
                + "\n\nIf you require clarifying questions, respond with JSON exactly like: {\"followups\": [\"question1\", ...]} otherwise {\"followups\": []}. "
                "Respond only with valid JSON with that shape."
            )
            diag_parsed = await call_structured_generation(diag_instruction, GenerateResponseModel, max_retries=1, timeout=30, debug=debug)
    except Exception:
        diag_parsed = None

    diag_parsed = _ensure_parsed_dict("diag_parsed", diag_parsed)

    raw_followups = None
    if isinstance(diag_parsed, dict):
        if diag_parsed.get("followups"):
            raw_followups = diag_parsed.get("followups")
        elif diag_parsed.get("metadata") and isinstance(diag_parsed.get("metadata"), dict):
            raw_followups = diag_parsed.get("metadata", {}).get("followups")
        else:
            raw_followups = None

    normalized = []
    if isinstance(raw_followups, list) and len(raw_followups) > 0:
        for item in raw_followups:
            nf = _normalize_followup_item(item)
            if nf and nf.get("question"):
                normalized.append(nf)

    request_questions = bool(options.get("request_questions", False))
    if request_questions and not normalized:
        normalized = [{
            "id": f"q_{int(time.time()*1000)}",
            "question": "Please list the key requirements for the app (pages, main features, and visual style).",
            "type": "text",
            "default": ""
        }]

    # If followups are required, stream them and finish
    if normalized and (not followup_answers or len(followup_answers.keys()) == 0):
        yield await _yield_event("followups", normalized)
        return

    # Generation (streamed)
    components = schema.get("components", []) or []
    generated_files: List[Dict[str, str]] = []
    merged_warnings: List[str] = []
    llm_debug_all = []

    # helper to stream a list of files (will chunk file content into chunks)
    async def _stream_files_list(files_list: List[Dict[str, str]]):
        for f in files_list:
            path = os.path.normpath(f.get("path", "") or "")
            content = f.get("content", "") or ""
            # file_start
            await_event = await _yield_event("file_start", {"path": path})
            yield await_event
            # accumulate the file for later dependency pinning / validation
            try:
                accumulated_files.append({"path": path, "content": content})
            except Exception:
                pass
            # stream chunks
            if not isinstance(content, str):
                content = str(content)
            idx = 0
            for i in range(0, len(content), STREAM_CHUNK_SZ):
                chunk = content[i:i+STREAM_CHUNK_SZ]
                final = (i + STREAM_CHUNK_SZ) >= len(content)
                yield await _yield_event("file_chunk", {"path": path, "chunk": chunk, "index": idx, "final": final})
                idx += 1
            # file_complete
            yield await _yield_event("file_complete", {"path": path, "size": len(content)})

    # If there are no components, single-shot generation
    if not components:
        full_prompt = system_prompt + "\n" + user_prompt + "\n\nReturn JSON with project_name, files[], metadata."
        parsed = await call_structured_generation(full_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
        parsed = _ensure_parsed_dict("full_gen", parsed)
        files = parsed.get("files", []) or []
        # stream each file
        async for ev in _stream_files_list(files):
            yield ev
        if parsed.get("metadata", {}).get("warnings"):
            for w in parsed.get("metadata", {}).get("warnings"):
                yield await _yield_event("warning", w)
        if debug:
            llm_debug_all.append(parsed)
    else:
        # chunked component generation
        total_files_so_far = 0
        for idx, chunk in enumerate(chunk_components(components, CHUNK_SIZE)):
            chunk_schema = {"components": chunk}
            chunk_user_prompt = build_user_prompt(user_for_prompt, chunk_schema, options)
            chunk_prompt = system_prompt + "\n" + chunk_user_prompt + "\n\nReturn JSON with files[] (only files for these components)."
            parsed = await call_structured_generation(chunk_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
            parsed = _ensure_parsed_dict(f"chunk_{idx}_gen", parsed)
            files = parsed.get("files", []) or []
            # stream files for this chunk
            async for ev in _stream_files_list(files):
                yield ev
            if parsed.get("metadata", {}).get("warnings"):
                for w in parsed.get("metadata", {}).get("warnings"):
                    yield await _yield_event("warning", w)
            if debug:
                llm_debug_all.append(parsed)

        # scaffold
        scaffold_prompt = system_prompt + "\n" + user_prompt + "\n\nNow produce project-level scaffolding files (package.json, tsconfig, pages, services, env files). Return JSON with files[]."
        parsed_scaffold = await call_structured_generation(scaffold_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
        parsed_scaffold = _ensure_parsed_dict("scaffold_gen", parsed_scaffold)
        scaffold_files = parsed_scaffold.get("files", []) or []
        async for ev in _stream_files_list(scaffold_files):
            yield ev
        if parsed_scaffold.get("metadata", {}).get("warnings"):
            for w in parsed_scaffold.get("metadata", {}).get("warnings"):
                yield await _yield_event("warning", w)
        if debug:
            llm_debug_all.append(parsed_scaffold)

    # Dependency pinning + validation: run once against accumulated_files (no extra LLM calls)
    try:
        if accumulated_files:
            try:
                # resolve and pin dependency versions in package.json if present
                pinned_files, dep_meta = resolve_and_pin_dependencies(list(accumulated_files), options)
            except Exception as e:
                dep_meta = {"warnings": [f"dependency resolution failed: {e}"]}

            # emit any resolved dependency info if available
            try:
                resolved = dep_meta.get("resolved") if isinstance(dep_meta, dict) else None
                if isinstance(resolved, list):
                    for d in resolved:
                        # emit dependency event so CLI (if desired) can pick it up; else ignored by CLI per your request
                        yield await _yield_event("dependency", d)
            except Exception:
                pass

            # emit any warnings from dependency step
            try:
                if isinstance(dep_meta, dict) and dep_meta.get("warnings"):
                    for w in dep_meta.get("warnings", []):
                        yield await _yield_event("warning", w)
            except Exception:
                pass

            # optional TypeScript validation step (if requested)
            try:
                validate = bool(options.get("validate", False))
                if validate:
                    tmpdir = tempfile.mkdtemp(prefix="ai_gen_")
                    try:
                        for f in pinned_files:
                            target = Path(tmpdir) / f["path"]
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text(f.get("content", ""), encoding="utf-8")
                        res = run_tsc_check(tmpdir)
                        # emit validation output as a warning (CLI currently ignores validation events)
                        yield await _yield_event("validation", res)
                    finally:
                        shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
        else:
            # no files were streamed; nothing to validate/pin
            pass
    except Exception: 
        pass

    # done - report files_count if available
    try:
        final_resp = await generate_project_files(payload)
        files_count = len(final_resp.get("files", []) or [])
    except Exception:
        files_count = 0
    yield await _yield_event("done", {"files_count": files_count})
    return


# ----------------------------
# Main orchestration
# ----------------------------
async def generate_project_files(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Entry point for /generate/:
      - payload: { user_answers, storyblok_schema, options }
      - returns either early with followups: top-level "followups" + files: []
      - or full project: "files", "metadata", optional "llm_debug"
    """
    user_answers = payload.get("user_answers", {}) or {}
    schema = payload.get("storyblok_schema", {}) or {}
    options = payload.get("options", {}) or {}
    debug = bool(options.get("debug", False))
    followup_answers = user_answers.get("followup_answers", {}) or {}

    app_name = user_answers.get("app_name", user_answers.get("project_name", "storyblok-app"))

    system_prompt = build_system_prompt()
    user_for_prompt = dict(user_answers)
    user_for_prompt.update({"followup_answers": followup_answers})
    user_prompt = build_user_prompt(user_for_prompt, schema, options)

    # ----------------------------
    # Diagnostic / question generation
    # ----------------------------
    diag_parsed = None
    try:
        max_questions = int(options.get("max_questions", 3))
        request_questions = bool(options.get("request_questions", False))

        if request_questions:
            q_instruction = (
                system_prompt
                + "\n\n"
                + user_prompt
                + f"\n\nYou are asked to produce up to {max_questions} structured clarifying questions to gather requirements from the user. "
                  'Return JSON exactly like: {"followups":[{"id":"q1","question":"...","type":"text","default":"..."}, ...]} '
                  "If you think no clarifying questions are needed, return {\"followups\":[]}."
                  "Respond only with valid JSON of that shape."
            )
            diag_parsed = await call_structured_generation(q_instruction, GenerateResponseModel, max_retries=1, timeout=30, debug=debug)
        else:
            diag_instruction = (
                system_prompt
                + "\n\n"
                + user_prompt
                + "\n\nIf you require clarifying questions, respond with JSON exactly like: {\"followups\": [\"question1\", ...]} otherwise {\"followups\": []}. "
                "Respond only with valid JSON with that shape."
            )
            diag_parsed = await call_structured_generation(diag_instruction, GenerateResponseModel, max_retries=1, timeout=30, debug=debug)
    except Exception:
        diag_parsed = None

    diag_parsed = _ensure_parsed_dict("diag_parsed", diag_parsed)

    raw_followups = None
    if isinstance(diag_parsed, dict):
        if diag_parsed.get("followups"):
            raw_followups = diag_parsed.get("followups")
        elif diag_parsed.get("metadata") and isinstance(diag_parsed.get("metadata"), dict):
            raw_followups = diag_parsed.get("metadata", {}).get("followups")
        else:
            raw_followups = None

    normalized = []
    if isinstance(raw_followups, list) and len(raw_followups) > 0:
        for item in raw_followups:
            nf = _normalize_followup_item(item)
            if nf and nf.get("question"):
                normalized.append(nf)

    request_questions = bool(options.get("request_questions", False))
    if request_questions and not normalized:
        normalized = [{
            "id": f"q_{int(time.time()*1000)}",
            "question": "Please list the key requirements for the app (pages, main features, and visual style).",
            "type": "text",
            "default": ""
        }]

    if normalized and (not followup_answers or len(followup_answers.keys()) == 0):
        resp = {
            "project_name": app_name,
            "files": [],
            "followups": normalized
        }
        if debug and diag_parsed is not None:
            resp["llm_debug"] = diag_parsed
        return resp

    # ----------------------------
    # Generation
    # ----------------------------
    components = schema.get("components", []) or []
    generated_files: List[Dict[str, str]] = []
    merged_warnings: List[str] = []
    llm_debug_all = []
    accumulated_files: List[Dict[str, str]] = []

    if not components:
        full_prompt = system_prompt + "\n" + user_prompt + "\n\nReturn JSON with project_name, files[], metadata."
        parsed = await call_structured_generation(full_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
        parsed = _ensure_parsed_dict("full_gen", parsed)
        files = parsed.get("files", []) or []
        generated_files.extend(files)
        if parsed.get("metadata", {}).get("warnings"):
            merged_warnings.extend(parsed.get("metadata", {}).get("warnings"))
        if debug:
            llm_debug_all.append(parsed)
    else:
        for idx, chunk in enumerate(chunk_components(components, CHUNK_SIZE)):
            chunk_schema = {"components": chunk}
            chunk_user_prompt = build_user_prompt(user_for_prompt, chunk_schema, options)
            chunk_prompt = system_prompt + "\n" + chunk_user_prompt + "\n\nReturn JSON with files[] (only files for these components)."
            parsed = await call_structured_generation(chunk_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
            parsed = _ensure_parsed_dict(f"chunk_{idx}_gen", parsed)
            files = parsed.get("files", []) or []
            generated_files.extend(files)
            if parsed.get("metadata", {}).get("warnings"):
                merged_warnings.extend(parsed.get("metadata", {}).get("warnings"))
            if debug:
                llm_debug_all.append(parsed)

        scaffold_prompt = system_prompt + "\n" + user_prompt + "\n\nNow produce project-level scaffolding files (package.json, tsconfig, vite.config, pages, services, env files). Return JSON with files[]."
        parsed_scaffold = await call_structured_generation(scaffold_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
        parsed_scaffold = _ensure_parsed_dict("scaffold_gen", parsed_scaffold)
        scaffold_files = parsed_scaffold.get("files", []) or []
        generated_files.extend(scaffold_files)
        if parsed_scaffold.get("metadata", {}).get("warnings"):
            merged_warnings.extend(parsed_scaffold.get("metadata", {}).get("warnings"))
        if debug:
            llm_debug_all.append(parsed_scaffold)

    # ----------------------------
    # Deduplicate & sanitize
    # ----------------------------
    seen = set()
    sanitized_files: List[Dict[str, str]] = []
    for f in generated_files:
        p = f.get("path", "")
        if not p:
            continue
        clean_p = os.path.normpath(p).lstrip(os.sep)
        if clean_p in seen:
            for i, ex in enumerate(sanitized_files):
                if os.path.normpath(ex["path"]).lstrip(os.sep) == clean_p:
                    sanitized_files[i] = {"path": clean_p, "content": f.get("content", "")}
                    break
            continue
        seen.add(clean_p)
        sanitized_files.append({"path": clean_p, "content": f.get("content", "")})

    # ----------------------------
    # Dependency pinning (curated map + npm registry)
    # ----------------------------
    try:
        sanitized_files, dep_meta = resolve_and_pin_dependencies(sanitized_files, options)
    except Exception as e:
        dep_meta = {"warnings": [f"dependency resolution failed: {e}"]}

    # ----------------------------
    # Optional validation (best-effort)
    # ----------------------------
    validate = bool(options.get("validate", False))
    validation_report = {"checked": False, "ok": None, "output": "", "skipped": False}

    if validate:
        tmpdir = tempfile.mkdtemp(prefix="ai_gen_")
        try:
            for f in sanitized_files:
                target = Path(tmpdir) / f["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f["content"], encoding="utf-8")
            res = run_tsc_check(tmpdir)
            validation_report["checked"] = True
            validation_report["ok"] = res.get("ok", False)
            validation_report["output"] = res.get("output", "")
            validation_report["skipped"] = res.get("skipped", False)

            if not res.get("ok", False) and not res.get("skipped", False):
                retries_left = LLM_RETRIES
                repair_prompt = system_prompt + "\n" + user_prompt + "\n\nValidation output:\n" + res.get("output", "") + "\n\nPlease regenerate corrected files only in JSON {files: [...]} format."
                while retries_left > 0:
                    try:
                        repaired = await call_structured_generation(repair_prompt, GenerateResponseModel, max_retries=1, timeout=TIMEOUT, debug=debug)
                        repaired = _ensure_parsed_dict("repair_gen", repaired)
                        rep_files = repaired.get("files", []) or []
                        for rf in rep_files:
                            rp = os.path.normpath(rf.get("path", "")).lstrip(os.sep)
                            replaced = False
                            for i, ex in enumerate(sanitized_files):
                                if os.path.normpath(ex["path"]).lstrip(os.sep) == rp:
                                    sanitized_files[i] = {"path": rp, "content": rf.get("content", "")}
                                    replaced = True
                                    break
                            if not replaced:
                                sanitized_files.append({"path": rp, "content": rf.get("content", "")})
                        for f in sanitized_files:
                            target = Path(tmpdir) / f["path"]
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text(f["content"], encoding="utf-8")
                        res2 = run_tsc_check(tmpdir)
                        if res2.get("ok", False):
                            validation_report["ok"] = True
                            validation_report["output"] = res2.get("output", "")
                            break
                        else:
                            validation_report["output"] = res2.get("output", "")
                    except Exception:
                        pass
                    retries_left -= 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ----------------------------
    # Final response assembly
    # ----------------------------
    metadata = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "warnings": merged_warnings,
        "validation": validation_report
    }
    # Merge dependency metadata if present
    if 'dep_meta' in locals():
        metadata.setdefault("dependencies", {}).update(dep_meta)
    elif 'dep_meta' not in locals() and dep_meta:
        metadata.setdefault("dependencies", {}).update(dep_meta)

    resp: Dict[str, Any] = {
        "project_name": app_name,
        "files": sanitized_files,
        "metadata": metadata
    }

    if debug:
        resp["llm_debug"] = {"chunks": llm_debug_all, "diag": diag_parsed}

    return resp
