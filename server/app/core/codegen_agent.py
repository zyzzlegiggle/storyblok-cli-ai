# ai_backend_demo/app/core/codegen_agent.py
"""
Code Generation Agent
- Exposes:
    async def generate_project(payload) -> Dict[str,Any]
    async def stream_generate_project(payload) -> AsyncGenerator[str, None]
- Intended to be the single responsibility module that performs:
    - LLM orchestration for chunked component generation + scaffold
    - Dependency collection + pinning (curated map + npm registry)
    - Sanitization & deduplication of emitted files
    - Streaming-safe emission helpers (file_start/file_chunk/file_complete)
"""
import os
import json
import time
import shutil
import subprocess
import tempfile
import traceback
import pickle
from pathlib import Path
from typing import Dict, Any, List, Optional, AsyncGenerator
import requests

from core.llm_client import call_structured_generation, GenerateResponseModel
from core.prompts import build_system_prompt, build_user_prompt
from core.dep_resolver import resolve_and_pin_files
from core.validator import run_validations, attempt_repair
from core.followup_agent import generate_followup_questions  # localized import
from utils.file_helpers import _safe_normalize
from utils.config import AGENT_TEMPERATURES

# configuration
CHUNK_SIZE = int(os.environ.get("AI_CHUNK_SIZE", 10))
LLM_RETRIES = int(os.environ.get("AI_RETRY_COUNT", 2))
TIMEOUT = int(os.environ.get("AI_TIMEOUT", 180))
LOG_DIR = os.environ.get("AI_BACKEND_LOG_DIR", "./ai_backend_logs")
DIAGNOSTIC_MAX_QUESTIONS = int(os.environ.get("AI_DIAG_MAX_Q", 5))
STREAM_CHUNK_SZ = int(os.environ.get("AI_STREAM_CHUNK_SZ", 1024))
NPM_REGISTRY = "https://registry.npmjs.org"
NPM_CACHE_FILE = os.path.join(LOG_DIR, "npm_cache.pkl")
NPM_CACHE_TTL = 24 * 3600  # seconds
os.makedirs(LOG_DIR, exist_ok=True)



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
# Streaming helpers (events -> newline-delimited JSON)
# ----------------------------
async def _yield_event(event_type: str, payload: Any) -> str:
    try:
        out = {"event": event_type, "payload": payload}
        return json.dumps(out, ensure_ascii=False) + "\n"
    except Exception:
        return json.dumps({"event": "error", "payload": f"failed to serialize {event_type}"}) + "\n"

# ----------------------------
# Main: streaming generator (used by CLI)
# ----------------------------
async def stream_generate_project(payload: Dict[str, Any]) -> AsyncGenerator[str, None]:
    """
    Async generator that yields newline-delimited JSON events (strings).
    Mirrors prior streaming behavior but lives in this agent.
    """
    user_answers = payload.get("user_answers", {}) or {}
    options = payload.get("options", {}) or {}
    debug = bool(options.get("debug", False))
    followup_answers = user_answers.get("followup_answers", {}) or {}

    app_name = user_answers.get("app_name", user_answers.get("project_name", "storyblok-app"))

    system_prompt = build_system_prompt()
    user_for_prompt = dict(user_answers)
    user_for_prompt.update({"followup_answers": followup_answers})
    user_prompt = build_user_prompt(user_for_prompt, options)

    # Diagnostic / question generation - if caller requested, we early-return followups (NOT used in stream path here)
    request_questions = bool(options.get("request_questions", False))
    if request_questions:
        # Ideally followup agent handles this; but leave compatibility: call followup agent if available
        
        qres = await generate_followup_questions(payload)
        followups = qres.get("followups", []) if isinstance(qres, dict) else []
        if followups:
            yield await _yield_event("followups", followups)
            return

    accumulated_files: List[Dict[str, str]] = []
    llm_debug_all = []
    merged_warnings = []

    async def _stream_files_list(files_list: List[Dict[str, str]]):
        for f in files_list:
            path = os.path.normpath(f.get("path", "") or "")
            content = f.get("content", "") or ""
            # file_start
            yield await _yield_event("file_start", {"path": path})
            # accumulate file
            try:
                accumulated_files.append({"path": path, "content": content})
            except Exception:
                pass
            # chunks
            if not isinstance(content, str):
                content = str(content)
            for i in range(0, len(content), STREAM_CHUNK_SZ):
                chunk = content[i:i+STREAM_CHUNK_SZ]
                final = (i + STREAM_CHUNK_SZ) >= len(content)
                yield await _yield_event("file_chunk", {"path": path, "chunk": chunk, "index": i//STREAM_CHUNK_SZ, "final": final})
            yield await _yield_event("file_complete", {"path": path, "size": len(content)})

    # build base_files_map if provided in payload
    base_files_map = {}
    normalized_assets = set()
    if isinstance(payload.get("asset_files"), list):
        for a in payload.get("asset_files", []):
            na = _safe_normalize(a) if callable(globals().get("_safe_normalize", None)) else None
            if na:
                normalized_assets.add(na)
    if isinstance(payload.get("base_files"), list):
        for bf in payload.get("base_files", []):
            if isinstance(bf, dict):
                p = bf.get("path") or ""
                # skip if path is provided as asset
                np = _normalize_path(p)
                if np in normalized_assets:
                    # explicitly treat as asset: set empty content or skip entirely
                    base_files_map[np] = ""  # indicate asset placeholder
                    continue
                c = bf.get("content") or ""
                base_files_map[np] = c


        if base_files_map:
            overlay_user_prompt = _build_overlay_user_prompt(user_for_prompt, options, base_files_map)
            full_prompt = system_prompt + "\n" + overlay_user_prompt + "\n\nReturn JSON with project_name, files[], new_dependencies, metadata."
        else:
            full_prompt = system_prompt + "\n" + user_prompt + "\n\nReturn JSON with project_name, files[], metadata."

        parsed = await call_structured_generation(full_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
        parsed = _ensure_parsed_dict("full_gen", parsed)
        files = parsed.get("files", []) or []
        if base_files_map:
            files = _compute_delta_files(files, base_files_map)
        async for ev in _stream_files_list(files):
            yield ev
        # emit new_dependencies event so CLI can print/apply them
        if base_files_map:
            nd = parsed.get("new_dependencies") or parsed.get("metadata", {}).get("new_dependencies")
            if isinstance(nd, list) and nd:
                try:
                    yield await _yield_event("new_dependencies", nd)
                except Exception:
                    pass

        if parsed.get("metadata", {}).get("warnings"):
            for w in parsed.get("metadata", {}).get("warnings"):
                yield await _yield_event("warning", w)
        if debug:
            llm_debug_all.append(parsed)
    else:
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

    # Dependency pinning + validation: run once against accumulated_files
    try:
        if accumulated_files:
            try:
                pinned_files, dep_meta = resolve_and_pin_files(list(accumulated_files), options)
            except Exception as e:
                dep_meta = {"warnings": [f"dependency resolution failed: {e}"], "pinned": {}, "resolved": []}

            # Emit resolved dependency details if available (so CLI can show found/missing/candidates)
            try:
                resolved_list = dep_meta.get("resolved") if isinstance(dep_meta, dict) else None
                if isinstance(resolved_list, list):
                    for d in resolved_list:
                        # Emit each resolved candidate as a structured dependency event
                        # Each 'd' is expected to be a dict with keys: name, version, source, url, confidence, candidates?
                        yield await _yield_event("dependency", d)
            except Exception:
                pass

            # emit any warnings from dep step
            try:
                if isinstance(dep_meta, dict) and dep_meta.get("warnings"):
                    for w in dep_meta.get("warnings", []):
                        yield await _yield_event("warning", w)
            except Exception:
                pass


            # emit resolved deps
            try:
                resolved = dep_meta.get("resolved") if isinstance(dep_meta, dict) else None
                if isinstance(resolved, list):
                    for d in resolved:
                        yield await _yield_event("dependency", d)
            except Exception:
                pass

            # emit any warnings from dep step
            try:
                if isinstance(dep_meta, dict) and dep_meta.get("warnings"):
                    for w in dep_meta.get("warnings", []):
                        yield await _yield_event("warning", w)
            except Exception:
                pass

            try:
                validate = bool(options.get("validate", False))
                if validate:
                    tmpdir = tempfile.mkdtemp(prefix="ai_gen_")
                    try:
                        # write pinned files to temp workspace
                        for f in pinned_files:
                            target = Path(tmpdir) / f["path"]
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text(f.get("content", ""), encoding="utf-8")

                        # run configured validators (tsc)
                        val_opts = {"validate_tsc": True}
                        val_res = run_validations(tmpdir, val_opts)
                        # emit validation result
                        yield await _yield_event("validation", val_res)

                        # if validation failed (and not skipped), attempt a single repair
                        if val_res.get("checked") and val_res.get("ok") is False:
                            # attempt one bounded repair via LLM
                            repair_opts = {"user_answers": user_for_prompt, "debug": debug, "repair_attempts": 1}
                            repair_res = await attempt_repair(tmpdir, val_res.get("output", ""), pinned_files, repair_opts)
                            # emit repair event
                            yield await _yield_event("repair", repair_res)

                            # if repair applied, re-run validators
                            if repair_res.get("ok"):
                                val_res_after = run_validations(tmpdir, val_opts)
                                yield await _yield_event("validation", val_res_after)
                    finally:
                        shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception as e:
                # emit warning if validation pipeline had an error
                try:
                    yield await _yield_event("warning", f"validation/repair pipeline error: {e}")
                except Exception:
                    pass

    except Exception:
        pass

    # done - return done event with files_count
    try:
        files_count = len(accumulated_files)
    except Exception:
        files_count = 0
    yield await _yield_event("done", {"files_count": files_count})
    return

# ----------------------------
# Main: non-streaming generation (returns full JSON)
# ----------------------------
async def generate_project(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Entrypoint for non-streaming generation. Mirrors the old generate_project_files
    shape: returns {"project_name":..., "files":[{"path","content"}], "metadata": {...}}
    """
    user_answers = payload.get("user_answers", {}) or {}
    options = payload.get("options", {}) or {}
    debug = bool(options.get("debug", False))
    followup_answers = user_answers.get("followup_answers", {}) or {}

    app_name = user_answers.get("app_name", user_answers.get("project_name", "storyblok-app"))

    system_prompt = build_system_prompt()
    user_for_prompt = dict(user_answers)
    user_for_prompt.update({"followup_answers": followup_answers})
    user_prompt = build_user_prompt(user_for_prompt, options)

    # Diagnostic / question generation (canonical path: use followup agent if requested)
    try:
        request_questions = bool(options.get("request_questions", False))
        if request_questions:
            from core.followup_agent import generate_followup_questions
            qres = await generate_followup_questions(payload)
            followups = qres.get("followups", []) if isinstance(qres, dict) else []
            if followups:
                return {"project_name": app_name, "files": [], "followups": followups, "metadata": {"notes": "followups required"}}
    except Exception:
        pass

    generated_files: List[Dict[str, str]] = []
    merged_warnings: List[str] = []
    llm_debug_all = []

    # detect base_files passed in payload (overlay scenario)
    base_files_map = {}
    if isinstance(payload.get("base_files"), list):
        for bf in payload.get("base_files", []):
            if isinstance(bf, dict):
                p = bf.get("path") or ""
                c = bf.get("content") or ""
                np = _normalize_path(p)
                base_files_map[np] = c

    # if we have a base scaffold, ask model to act as overlay
    if base_files_map:
        overlay_user_prompt = _build_overlay_user_prompt(user_for_prompt, options, base_files_map)
        full_prompt = system_prompt + "\n" + overlay_user_prompt + "\n\nReturn JSON with project_name, files[], new_dependencies, metadata."
    else:
        full_prompt = system_prompt + "\n" + user_prompt + "\n\nReturn JSON with project_name, files[], metadata."

    parsed = await call_structured_generation(full_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
    parsed = _ensure_parsed_dict("full_gen", parsed)
    _log_raw_llm_output("generate_project_full", parsed, debug)

    files = parsed.get("files", []) or []
    # compute delta if overlay context provided
    if base_files_map:
        files = _compute_delta_files(files, base_files_map)
    generated_files.extend(files)
    # attach any new_dependencies into metadata for CLI to pick up
    if base_files_map:
        nd = parsed.get("new_dependencies") or parsed.get("metadata", {}).get("new_dependencies")
        if nd:
            if isinstance(nd, list):
                dep_list = [d for d in nd if isinstance(d, str)]
                # record compactly in metadata for later usage
                merged_warnings.extend(parsed.get("metadata", {}).get("warnings", []) or [])
                # stash dep meta for the response
                dep_meta_for_resp = {"new_dependencies": dep_list}
                # attach into parsed metadata so outer code can pick it up
                parsed.setdefault("metadata", {}).setdefault("dependencies", {}).update(dep_meta_for_resp)

    if parsed.get("metadata", {}).get("warnings"):
        merged_warnings.extend(parsed.get("metadata", {}).get("warnings"))
    if debug:
        llm_debug_all.append(parsed)

    # sanitize & dedupe
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

    # dependency pinning
    try:
        sanitized_files, dep_meta = resolve_and_pin_files(sanitized_files, options)
    except Exception as e:
        dep_meta = {"warnings": [f"dependency resolution failed: {e}"]}

        # optional validation (tsc) using validator + repair agent
    validate = bool(options.get("validate", False))
    validation_report = {"checked": False, "ok": None, "output": "", "skipped": False}

    if validate:
        tmpdir = tempfile.mkdtemp(prefix="ai_gen_")
        try:
            # write current sanitized files to tempdir
            for f in sanitized_files:
                target = Path(tmpdir) / f["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f["content"], encoding="utf-8")

            # run validator agent
            val_opts = {"validate_tsc": True}
            val_res = run_validations(tmpdir, val_opts)
            validation_report["checked"] = val_res.get("checked", False)
            validation_report["ok"] = val_res.get("ok", None)
            validation_report["output"] = val_res.get("output", "")
            validation_report["skipped"] = val_res.get("skipped", False)

            # if validation failed (and not skipped), attempt bounded LLM repair
            if val_res.get("checked") and val_res.get("ok") is False:
                repair_opts = {"user_answers": user_for_prompt, "debug": debug, "repair_attempts": 1}
                repair_res = await attempt_repair(tmpdir, val_res.get("output", ""), sanitized_files, repair_opts)

                # If repair provided files, merge them into sanitized_files (replace or append)
                rep_files = repair_res.get("repaired_files", []) or []
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

                # re-run validation after repair
                for f in sanitized_files:
                    target = Path(tmpdir) / f["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(f["content"], encoding="utf-8")
                val_res_after = run_validations(tmpdir, val_opts)
                validation_report["checked"] = val_res_after.get("checked", False)
                validation_report["ok"] = val_res_after.get("ok", None)
                validation_report["output"] = val_res_after.get("output", "")
                validation_report["skipped"] = val_res_after.get("skipped", False)

                # optionally attach repair summary in metadata.warnings if repair didn't succeed
                if not validation_report.get("ok", False):
                    validation_report.setdefault("repair", {}).update({"attempted": True, "report": repair_res})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


    metadata = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "warnings": merged_warnings,
        "validation": validation_report
    }

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
        resp["llm_debug"] = {"chunks": llm_debug_all}

    return resp


# ----------------------------
# Overlay helpers
# ----------------------------
def _normalize_path(p: str) -> str:
    # make paths consistent for comparison
    return os.path.normpath(p).replace("\\", "/").lstrip("/")

def _build_overlay_user_prompt(
    user_for_prompt: Dict[str, Any],
    options: Dict[str, Any],
    base_files_map: Dict[str, str],
    asset_files: Optional[List[str]] = None  # optional list of asset paths
) -> str:
    """
    Build a user prompt that instructs the LLM to act as an overlay when base_files_map is provided.
    Includes asset placeholders so the LLM knows these exist but does not get their contents.
    """
    try:
        user_json = json.dumps(user_for_prompt, indent=2, ensure_ascii=False)
    except Exception:
        user_json = str(user_for_prompt)

    normalized_assets = set(asset_files or [])
    print(base_files_map)

    # small manifest: path + snippet (first ~600 chars) for context
    manifest = []
    for p, c in list(base_files_map.items())[:200]:
        if p in normalized_assets:
            manifest.append({"path": p, "asset": True})
        else:
            snippet = (c or "")[:800].replace("\n", "\\n")
            manifest.append({"path": p, "snippet": snippet})

    prompt = (
        "Context:\n"
        f"User requirements:\n{user_json}\n\n"
        "Existing scaffold manifest (path + snippet):\n"
        f"{json.dumps(manifest, ensure_ascii=False)}\n\n"
        "Task:\n"
        "- The project scaffold already exists (paths in the manifest). DO NOT regenerate the whole project.\n"
        "- Return ONLY files you need to ADD or CHANGE to implement the user's requests.\n"
        "- Do NOT modify 'package.json'. Instead, list any additional NPM packages (NAMES ONLY) in 'new_dependencies'.\n"
        "- For changed files, return the full file content (not a diff). For new files, return full content.\n"
        "- Keep changes minimal and avoid reformatting or renaming files unless required.\n\n"
        "- Follow the file format: e.g., if it's a Next.js project mainly using JavaScript, use JS format for new components, not TSX.\n"
        "- Do not add another Storyblok package.\n"
        "- Do NOT return binary assets; reference their paths only.\n"
        "Output: produce a single JSON object with keys: project_name (optional), files (array of {path,content}), new_dependencies (array of package NAMES), warnings (optional).\n"
        "Return only JSON and nothing else.\n"
    )
    return prompt

def _compute_delta_files(emitted_files: List[Dict[str, str]], base_files_map: Dict[str, str]) -> List[Dict[str, str]]:
    """
    Compare emitted_files (list of {path,content}) against base_files_map (path -> content).
    Return only files that are new or whose content differs. Always ignore any package.json emitted by model.
    Paths are normalized for comparison.
    """
    delta = []
    for f in emitted_files:
        path = f.get("path") or ""
        content = f.get("content") or ""
        npath = _normalize_path(path)
        if os.path.basename(npath) == "package.json":
            # explicitly skip package.json modifications
            continue
        base_content = base_files_map.get(npath)
        if base_content is None:
            # new file
            delta.append({"path": npath, "content": content})
        else:
            # differ? use exact string comparison
            if base_content != content:
                delta.append({"path": npath, "content": content})
            else:
                # identical; skip
                continue
    return delta


import time

def _log_raw_llm_output(tag: str, data: Any, debug: bool = False):
    """
    Write raw parsed/unparsed LLM output to ai_backend_logs for debugging.
    Only logs when debug=True in options.
    """
    if not debug:
        return
    try:
        os.makedirs("ai_backend_logs", exist_ok=True)
        fname = os.path.join("ai_backend_logs", f"{int(time.time())}_{tag}.json")
        with open(fname, "w", encoding="utf-8") as f:
            if isinstance(data, (dict, list)):
                json.dump(data, f, indent=2, ensure_ascii=False)
            else:
                f.write(str(data))
    except Exception as e:
        print(f"[debug-log-failed] {e}")
