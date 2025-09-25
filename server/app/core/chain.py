# ai_backend_demo/app/core/chain.py
import os
import json
import time
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional

from core.llm_client import call_structured_generation, GenerateResponseModel
from core.prompts import build_system_prompt, build_user_prompt

# Config (env override)
CHUNK_SIZE = int(os.environ.get("AI_CHUNK_SIZE", 10))
LLM_RETRIES = int(os.environ.get("AI_RETRY_COUNT", 2))  # number of retries (per user: 2)
TIMEOUT = int(os.environ.get("AI_TIMEOUT", 180))
LOG_DIR = os.environ.get("AI_BACKEND_LOG_DIR", "./ai_backend_logs")
os.makedirs(LOG_DIR, exist_ok=True)


def chunk_components(components: List[Dict[str, Any]], chunk_size: int = CHUNK_SIZE):
    for i in range(0, len(components), chunk_size):
        yield components[i:i + chunk_size]


def run_tsc_check(project_dir: str) -> Dict[str, Any]:
    """
    Best-effort TypeScript check. Uses npx tsc --noEmit or tsc --noEmit if available.
    Returns dict: {ok: bool, skipped: bool, output: str}
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


def _normalize_followup_item(raw) -> Optional[Dict[str, Any]]:
    """Coerce LLM-provided followup into expected shape {id, question, type, default}"""
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


def _ensure_parsed_dict(name, parsed, log_dir=LOG_DIR):
    """
    Ensure parsed is a plain dict. If it's None or an unexpected object,
    try to convert to dict, else write debug file and return {}.
    """
    try:
        if parsed is None:
            # nothing returned
            fname = f"{int(time.time())}_diag_none.log"
            with open(os.path.join(log_dir, fname), "w", encoding="utf-8") as fh:
                fh.write(f"{name} returned None\n")
            return {}
        if isinstance(parsed, dict):
            return parsed
        # try pydantic/dot->dict
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
        # try parsing JSON-ish string
        s = str(parsed)
        try:
            return json.loads(s)
        except Exception:
            # failed to parse
            fname = f"{int(time.time())}_diag_unparseable.json"
            with open(os.path.join(log_dir, fname), "w", encoding="utf-8") as fh:
                fh.write("UNPARSEABLE DIAGNOSTIC RESULT\n\n")
                fh.write("repr(parsed):\n")
                fh.write(repr(parsed) + "\n\n")
                fh.write("str(parsed):\n")
                fh.write(s + "\n\n")
                fh.write("exception traceback:\n")
                fh.write(traceback.format_exc())
            return {}
    except Exception as e:
        # In case of any unexpected error, log and return empty dict
        fname = f"{int(time.time())}_diag_exception.log"
        with open(os.path.join(log_dir, fname), "w", encoding="utf-8") as fh:
            fh.write("Exception normalizing diag_parsed:\n")
            fh.write(str(e) + "\n")
            fh.write(traceback.format_exc())
        return {}


async def generate_project_files(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main entrypoint:
      - payload: {user_answers: {...}, storyblok_schema: {...}, options: {...}}
      - may return early with:
          {"project_name": "...", "files": [], "followups": [...], "llm_debug": {...}}
      - or return final:
          {"project_name": "...", "files": [{"path","content"},...], "metadata": {...}, "llm_debug": {...}}
    """
    user_answers = payload.get("user_answers", {}) or {}
    schema = payload.get("storyblok_schema", {}) or {}
    options = payload.get("options", {}) or {}
    debug = bool(options.get("debug", False))
    followup_answers = user_answers.get("followup_answers", {}) or {}

    app_name = user_answers.get("app_name", user_answers.get("project_name", "storyblok-app"))

    # Build prompts
    system_prompt = build_system_prompt()
    # include any provided followup_answers for context
    user_for_prompt = dict(user_answers)
    user_for_prompt.update({"followup_answers": followup_answers})
    user_prompt = build_user_prompt(user_for_prompt, schema, options)

    # ----------------------------
    # Diagnostic: ask LLM if followups required
    # ----------------------------
    diag_instruction = (
        system_prompt
        + "\n\n"
        + user_prompt
        + "\n\n"
        "If you require additional clarifying questions from the user to produce a better scaffold, "
        "respond with JSON exactly like: {\"followups\": ["
        "{\"id\":\"q1\",\"question\":\"...\",\"type\":\"text\",\"default\":\"...\"}, ...] } "
        "If no followups are required, respond with: {\"followups\": []}.\n"
        "Respond only with valid JSON with that shape, no extra commentary."
    )

    # --- diagnostic / question-generation block (replace existing diag code) ---
    diag_parsed = None
    try:
        # If the caller explicitly requests questions, instruct the LLM to produce up to N questions
        max_questions = int(options.get("max_questions", 3))
        request_questions = bool(options.get("request_questions", False))

        if request_questions:
            # Ask the model to generate up to max_questions structured questions (type=text)
            q_instruction = (
                system_prompt
                + "\n\n"
                + user_prompt
                + f"\n\nYou are asked to produce up to {max_questions} structured clarifying questions "
                "to gather requirements from the user. Return JSON exactly like: "
                '{"followups":[{"id":"q1","question":"...","type":"text","default":"..."}, ...]} '
                "If you think no clarifying questions are needed, return {\"followups\":[]}."
                "Respond only with valid JSON of that shape."
            )
            diag_parsed = await call_structured_generation(q_instruction, GenerateResponseModel, max_retries=1, timeout=30, debug=debug)
            diag_parsed = _ensure_parsed_dict("diag_parsed", diag_parsed)
        else:
            # Regular lightweight diagnostic: ask whether clarifying questions are required
            diag_instruction = (
                system_prompt
                + "\n\n"
                + user_prompt
                + "\n\nIf you require clarifying questions, respond with JSON exactly like: {\"followups\": [\"question1\", ...]} otherwise {\"followups\": []}."
                "Respond only with valid JSON with that shape."
            )
            diag_parsed = await call_structured_generation(diag_instruction, GenerateResponseModel, max_retries=1, timeout=30, debug=debug)
            diag_parsed = _ensure_parsed_dict("diag_parsed", diag_parsed)
    except Exception:
        # diagnostic failed — continue to generation rather than block
        diag_parsed = None

    # normalize followups if present
    if isinstance(diag_parsed, dict):
        raw_followups = None
        # prefer top-level 'followups', then metadata.followups
        if diag_parsed.get("followups"):
            raw_followups = diag_parsed.get("followups")
        elif diag_parsed.get("metadata") and isinstance(diag_parsed.get("metadata"), dict):
            raw_followups = diag_parsed.get("metadata", {}).get("followups")
        else:
            raw_followups = None

        normalized = []
        if isinstance(raw_followups, list) and len(raw_followups) > 0:
            for item in raw_followups:
                # keep the same _normalize_followup_item logic you already have
                nf = _normalize_followup_item(item)
                if nf and nf.get("question"):
                    normalized.append(nf)

        # If request_questions was true, guarantee at least one fallback question if model returned none
        if request_questions and not normalized:
            normalized = [{
                "id": f"q_{int(time.time()*1000)}",
                "question": "Please list the key requirements for the app (pages, main features, and visual style).",
                "type": "text",
                "default": ""
            }]

        # If followups found and there are no followup answers yet, return them as top-level followups
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
    # Generation: chunked by components (or single shot if no components)
    # ----------------------------
    components = schema.get("components", []) or []
    generated_files: List[Dict[str, str]] = []
    merged_warnings: List[str] = []
    llm_debug_all = []

    if not components:
        # Single-shot: generate entire project
        full_prompt = system_prompt + "\n" + user_prompt + "\n\nReturn JSON with project_name, files[], metadata."
        parsed = await call_structured_generation(full_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
        files = parsed.get("files", []) or []
        generated_files.extend(files)
        if parsed.get("metadata", {}).get("warnings"):
            merged_warnings.extend(parsed.get("metadata", {}).get("warnings"))
        if debug:
            llm_debug_all.append(parsed)
    else:
        # Process components in chunks to limit token usage
        for idx, chunk in enumerate(chunk_components(components, CHUNK_SIZE)):
            chunk_schema = {"components": chunk}
            chunk_user_prompt = build_user_prompt(user_for_prompt, chunk_schema, options)
            chunk_prompt = system_prompt + "\n" + chunk_user_prompt + "\n\nReturn JSON with files[] (only files for these components)."
            parsed = await call_structured_generation(chunk_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
            files = parsed.get("files", []) or []
            generated_files.extend(files)
            if parsed.get("metadata", {}).get("warnings"):
                merged_warnings.extend(parsed.get("metadata", {}).get("warnings"))
            if debug:
                llm_debug_all.append(parsed)

        # Final scaffold step (configs, pages, env, storyblok service)
        scaffold_prompt = system_prompt + "\n" + user_prompt + "\n\nNow produce project-level scaffolding files (package.json, tsconfig, vite.config, pages, services, env files). Return JSON with files[]."
        parsed_scaffold = await call_structured_generation(scaffold_prompt, GenerateResponseModel, max_retries=LLM_RETRIES, timeout=TIMEOUT, debug=debug)
        scaffold_files = parsed_scaffold.get("files", []) or []
        generated_files.extend(scaffold_files)
        if parsed_scaffold.get("metadata", {}).get("warnings"):
            merged_warnings.extend(parsed_scaffold.get("metadata", {}).get("warnings"))
        if debug:
            llm_debug_all.append(parsed_scaffold)

    # ----------------------------
    # Deduplicate & sanitize file list
    # ----------------------------
    seen = set()
    sanitized_files: List[Dict[str, str]] = []
    for f in generated_files:
        p = f.get("path", "")
        if not p:
            continue
        clean_p = os.path.normpath(p).lstrip(os.sep)
        if clean_p in seen:
            # replace previous occurrence with later one
            for i, ex in enumerate(sanitized_files):
                if os.path.normpath(ex["path"]).lstrip(os.sep) == clean_p:
                    sanitized_files[i] = {"path": clean_p, "content": f.get("content", "")}
                    break
            continue
        seen.add(clean_p)
        sanitized_files.append({"path": clean_p, "content": f.get("content", "")})

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

            # Auto-retry on validation failure
            if not res.get("ok", False) and not res.get("skipped", False):
                retries_left = LLM_RETRIES
                repair_prompt = system_prompt + "\n" + user_prompt + "\n\nValidation output:\n" + res.get("output", "") + "\n\nPlease regenerate corrected files only in JSON {files: [...]} format."
                while retries_left > 0:
                    try:
                        repaired = await call_structured_generation(repair_prompt, GenerateResponseModel, max_retries=1, timeout=TIMEOUT, debug=debug)
                        rep_files = repaired.get("files", []) or []
                        # merge repaired files (overwrite)
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
                        # rewrite tmpdir and re-run tsc
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
                        # swallow and continue retry loop
                        pass
                    retries_left -= 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ----------------------------
    # Final response
    # ----------------------------
    metadata = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "warnings": merged_warnings,
        "validation": validation_report
    }

    resp: Dict[str, Any] = {
        "project_name": app_name,
        "files": sanitized_files,
        "metadata": metadata
    }

    if debug:
        resp["llm_debug"] = {"chunks": llm_debug_all}

    return resp
