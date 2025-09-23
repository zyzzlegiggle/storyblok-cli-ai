# app/core/chain.py
import os
import json
import time
import shutil
import subprocess
from typing import Dict, Any, List, Optional

from core.llm_client import call_structured_generation, GenerateResponseModel, FileOutModel
from core.prompts import build_system_prompt, build_user_prompt  # keep your prompts.py
from pathlib import Path

# Config defaults (adjustable)
CHUNK_SIZE = int(os.environ.get("AI_CHUNK_SIZE", 10))
LLM_RETRIES = int(os.environ.get("AI_RETRY_COUNT", 2))  # user's choice: 2 retries
TIMEOUT = int(os.environ.get("AI_TIMEOUT", 300))
LOG_DIR = os.environ.get("AI_BACKEND_LOG_DIR", "./ai_backend_logs")
os.makedirs(LOG_DIR, exist_ok=True)


def chunk_components(components: List[Dict[str, Any]], chunk_size: int = CHUNK_SIZE):
    """Yield successive chunks of components."""
    for i in range(0, len(components), chunk_size):
        yield components[i:i + chunk_size]


def run_tsc_check(project_dir: str) -> Dict[str, Any]:
    """
    Run a TypeScript check (best-effort). Uses `npx tsc --noEmit` if available,
    or `tsc --noEmit` if globally installed. Returns dict with {ok: bool, output: str}
    """
    # prefer npx if available
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


async def generate_project_files(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main entrypoint for /generate/:
     - Accepts payload with user_answers, storyblok_schema, options
     - If payload lacks followup_answers and LLM requires clarifying questions, backend returns followups
     - Otherwise, does chunked file generation via structured LLM calls and merges results
     - Performs best-effort validation (tsc) if requested and available; auto-retry on validation failure
     - Returns {"project_name", "files", "metadata"} and optionally llm_debug when options.debug==true
    """
    user_answers = payload.get("user_answers", {})
    schema = payload.get("storyblok_schema", {})
    options = payload.get("options", {}) or {}
    debug = bool(options.get("debug", False))
    followup_answers = user_answers.get("followup_answers", {}) or {}

    # Quick sanity
    app_name = user_answers.get("app_name", user_answers.get("project_name", "storyblok-app"))

    # Build prompts
    system_prompt = build_system_prompt()
    # Include any followup_answers into user_answers for the prompt
    user_for_prompt = dict(user_answers)
    user_for_prompt.update({"followup_answers": followup_answers})
    user_prompt = build_user_prompt(user_for_prompt, schema, options)

    # === Decide if follow-ups are needed before heavy generation ===
    # We can optionally ask the LLM a short "diagnostic" question: does it need clarifying input?
    # Simpler: ask the LLM to return a "followups" list if it needs more info.
    diagnostic_prompt = system_prompt + "\n" + user_prompt + "\n\nRespond with JSON: {\"followups\": [\"question1\", ...]} if you need any clarifying questions, otherwise return {\"followups\": []}."
    try:
        diag_result = await call_structured_generation(diagnostic_prompt, GenerateResponseModel, max_retries=1, timeout=30, debug=debug)
        # diag_result likely contains project_name/files; but we expect followups in metadata
        # to be robust, look in metadata or raw llm_debug; but for now, inspect 'metadata'->'followups'
        followups = diag_result.get("metadata", {}).get("followups", [])
        if isinstance(followups, list) and len(followups) > 0 and not followup_answers:
            # return followups to the CLI for synchronous prompts
            return {"project_name": app_name, "files": [], "metadata": {"followups": followups}, "llm_debug": diag_result if debug else None}
    except Exception:
        # If diagnostic failed, continue to generation (we don't block)
        pass

    # === Chunking approach ===
    components = schema.get("components", []) or []
    generated_files: List[Dict[str, str]] = []

    # If no components, we still want to run a single full generation
    if not components:
        # Single-shot generation using complete schema & user prompt
        full_prompt = system_prompt + "\n" + user_prompt + "\n\nReturn JSON with project_name, files[], metadata."
        parsed = await call_structured_generation(full_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
        # parsed is a dict mapping to GenerateResponseModel
        files = parsed.get("files", [])
        generated_files.extend(files or [])
        metadata = parsed.get("metadata", {})
        llm_debug = parsed if debug else None
    else:
        # Process components in chunks to avoid token limits
        component_chunks = list(chunk_components(components, CHUNK_SIZE))
        merged_warnings = []
        llm_debug_all = []
        for idx, chunk in enumerate(component_chunks):
            chunk_schema = {"components": chunk}
            chunk_user_prompt = build_user_prompt(user_for_prompt, chunk_schema, options)
            chunk_prompt = system_prompt + "\n" + chunk_user_prompt + "\n\nReturn JSON with files[] (only files for these components)."
            parsed = await call_structured_generation(chunk_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
            # parsed.files should be list of files for components
            files = parsed.get("files", []) or []
            generated_files.extend(files)
            if parsed.get("metadata", {}).get("warnings"):
                merged_warnings.extend(parsed.get("metadata", {}).get("warnings"))
            if debug:
                llm_debug_all.append(parsed)

        # After per-chunk components, ask for app-level scaffolding (index, pages, configs)
        scaffold_prompt = system_prompt + "\n" + user_prompt + "\n\nNow produce project-level scaffolding files (package.json, tsconfig, vite.config, pages, services, env files). Return JSON with files[]."
        parsed_scaffold = await call_structured_generation(scaffold_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
        scaffold_files = parsed_scaffold.get("files", []) or []
        generated_files.extend(scaffold_files)
        if parsed_scaffold.get("metadata", {}).get("warnings"):
            merged_warnings.extend(parsed_scaffold.get("metadata", {}).get("warnings"))
        if debug:
            llm_debug_all.append(parsed_scaffold)

        metadata = {"warnings": merged_warnings}
        llm_debug = {"chunks": llm_debug_all} if debug else None

    # === Basic File Deduplication & sanitization ===
    seen = set()
    sanitized_files = []
    for f in generated_files:
        p = f.get("path", "")
        if not p:
            continue
        clean_p = os.path.normpath(p).lstrip(os.sep)
        if clean_p in seen:
            # prefer the last occurrence (overwrite semantics)
            for i, ex in enumerate(sanitized_files):
                if os.path.normpath(ex["path"]).lstrip(os.sep) == clean_p:
                    sanitized_files[i] = {"path": clean_p, "content": f.get("content", "")}
                    break
            continue
        seen.add(clean_p)
        sanitized_files.append({"path": clean_p, "content": f.get("content", "")})

    # === Optional: validation step (best-effort) ===
    validate = bool(options.get("validate", False))
    validation_report = {"checked": False, "ok": None, "output": ""}

    if validate:
        # Write to a temp dir, run tsc --noEmit if available
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="ai_gen_")
        try:
            for f in sanitized_files:
                target = Path(tmpdir) / f["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f["content"], encoding="utf-8")
            # Run tsc check
            res = run_tsc_check(tmpdir)
            validation_report["checked"] = True
            validation_report["ok"] = res.get("ok", False)
            validation_report["output"] = res.get("output", "")
            # If validation failed, we do auto-retry generation up to LLM_RETRIES
            if not res.get("ok", False):
                # Auto-retry logic: attempt to re-call LLM to fix issues
                # Strategy: send validation output as instruction and re-run scaffold prompt (one attempt per retry)
                retries_left = LLM_RETRIES
                repair_prompt = system_prompt + "\n" + user_prompt + "\n\nValidation output:\\n" + res.get("output", "") + "\\n\\nPlease regenerate corrected files only."
                repaired = None
                while retries_left > 0:
                    try:
                        repaired = await call_structured_generation(repair_prompt, GenerateResponseModel, max_retries=1, timeout=TIMEOUT, debug=debug)
                        # merge repaired files (overwrite previous)
                        rep_files = repaired.get("files", []) or []
                        # merging: for each rep_file replace or append
                        for rf in rep_files:
                            rp = os.path.normpath(rf.get("path","")).lstrip(os.sep)
                            replaced = False
                            for i, ex in enumerate(sanitized_files):
                                if os.path.normpath(ex["path"]).lstrip(os.sep) == rp:
                                    sanitized_files[i] = {"path": rp, "content": rf.get("content","")}
                                    replaced = True
                                    break
                            if not replaced:
                                sanitized_files.append({"path": rp, "content": rf.get("content","")})
                        # re-run tsc check quickly
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
                            validation_report["output"] = res2.get("output","")
                    except Exception as e:
                        # log error and continue retry
                        retries_left -= 1
                        continue
                    retries_left -= 1
                # end retries
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # Build final response
    resp = {
        "project_name": app_name,
        "files": sanitized_files,
        "metadata": {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            **(metadata if isinstance(metadata, dict) else {}),
            "validation": validation_report
        }
    }
    if debug:
        resp["llm_debug"] = llm_debug
    return resp
