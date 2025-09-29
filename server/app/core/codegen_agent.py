# ai_backend_demo/app/core/codegen_agent.py
"""
Code Generation Agent
- Exposes:
    async def generate_project(payload) -> Dict[str,Any]
    async def stream_generate_project(payload) -> AsyncGenerator[str, None]
- Intended to be the single responsibility module that performs:
    - LLM orchestration for chunked component generation + scaffold
    - Dependency collection + pinning (curated map + npm registry)
    - Optional TypeScript validation + bounded repair loop
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
from core.prompts import build_system_prompt, build_user_prompt, summarize_schema
from core.dep_resolver import resolve_and_pin_files
from core.validator import run_validations, attempt_repair
from core.followup_agent import generate_followup_questions  # localized import
from app.utils.config import AGENT_TEMPERATURES

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

# ----------------------------
# Utilities & validation
# ----------------------------
def chunk_components(components: List[Dict[str, Any]], chunk_size: int = CHUNK_SIZE):
    for i in range(0, len(components), chunk_size):
        yield components[i:i + chunk_size]


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
    schema = payload.get("storyblok_schema", {}) or {}
    options = payload.get("options", {}) or {}
    debug = bool(options.get("debug", False))
    followup_answers = user_answers.get("followup_answers", {}) or {}

    app_name = user_answers.get("app_name", user_answers.get("project_name", "storyblok-app"))

    system_prompt = build_system_prompt()
    user_for_prompt = dict(user_answers)
    user_for_prompt.update({"followup_answers": followup_answers})
    user_prompt = build_user_prompt(user_for_prompt, schema, options)

    # Diagnostic / question generation - if caller requested, we early-return followups (NOT used in stream path here)
    request_questions = bool(options.get("request_questions", False))
    if request_questions:
        # Ideally followup agent handles this; but leave compatibility: call followup agent if available
        
        qres = await generate_followup_questions(payload)
        followups = qres.get("followups", []) if isinstance(qres, dict) else []
        if followups:
            yield await _yield_event("followups", followups)
            return

    # Generation: handle components or single-shot
    components = schema.get("components", []) or []
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

    if not components:
        full_prompt = system_prompt + "\n" + user_prompt + "\n\nReturn JSON with project_name, files[], metadata."
        parsed = await call_structured_generation(full_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug, temperature=AGENT_TEMPERATURES["codegen"])
        parsed = _ensure_parsed_dict("full_gen", parsed)
        files = parsed.get("files", []) or []
        async for ev in _stream_files_list(files):
            yield ev
        if parsed.get("metadata", {}).get("warnings"):
            for w in parsed.get("metadata", {}).get("warnings"):
                yield await _yield_event("warning", w)
        if debug:
            llm_debug_all.append(parsed)
    else:
        # chunked generation
        total_files_so_far = 0
        for idx, chunk in enumerate(chunk_components(components, CHUNK_SIZE)):
            chunk_schema = {"components": chunk}
            chunk_user_prompt = build_user_prompt(user_for_prompt, chunk_schema, options)
            chunk_prompt = system_prompt + "\n" + chunk_user_prompt + "\n\nReturn JSON with files[] (only files for these components)."
            parsed = await call_structured_generation(chunk_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
            parsed = _ensure_parsed_dict(f"chunk_{idx}_gen", parsed)
            files = parsed.get("files", []) or []
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

            # optional TypeScript validation
            # optional TypeScript validation using validator agent (with bounded repair)
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
    schema = payload.get("storyblok_schema", {}) or {}
    options = payload.get("options", {}) or {}
    debug = bool(options.get("debug", False))
    followup_answers = user_answers.get("followup_answers", {}) or {}

    app_name = user_answers.get("app_name", user_answers.get("project_name", "storyblok-app"))

    system_prompt = build_system_prompt()
    user_for_prompt = dict(user_answers)
    user_for_prompt.update({"followup_answers": followup_answers})
    user_prompt = build_user_prompt(user_for_prompt, schema, options)

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

    components = schema.get("components", []) or []
    generated_files: List[Dict[str, str]] = []
    merged_warnings: List[str] = []
    llm_debug_all = []

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
