# ai_backend_demo/app/core/validator.py
import os
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

from core.llm_client import call_structured_generation, GenerateResponseModel
from core.prompts import build_system_prompt

# Configuration (can be tuned via env)
LLM_RETRIES = int(os.environ.get("AI_RETRY_COUNT", 2))
VALIDATOR_TIMEOUT = int(os.environ.get("AI_VALIDATOR_TIMEOUT", 60))  # seconds per validator run
REPAIR_MAX_ATTEMPTS = int(os.environ.get("AI_REPAIR_ATTEMPTS", 1))
REPAIR_TIMEOUT = int(os.environ.get("AI_REPAIR_TIMEOUT", 180))  # LLM call timeout for repair

# ----------------------------
# Local validators
# ----------------------------
def _run_cmd(cmd: List[str], cwd: Optional[str] = None, timeout: int = VALIDATOR_TIMEOUT) -> Tuple[int, str]:
    """
    Run a shell command and return (returncode, combined_output).
    """
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        out = proc.stdout or ""
        return proc.returncode, out
    except subprocess.TimeoutExpired as e:
        return 124, f"validator timeout after {timeout}s: {e}"
    except Exception as e:
        return 1, f"validator execution failed: {e}"

def run_tsc_check(project_dir: str) -> Dict[str, Any]:
    """
    Reuse run_tsc_check semantics used elsewhere: returns {ok, skipped, output}
    """
    if shutil.which("npx"):
        cmd = ["npx", "tsc", "--noEmit"]
    elif shutil.which("tsc"):
        cmd = ["tsc", "--noEmit"]
    else:
        return {"ok": False, "skipped": True, "output": "tsc not found; skipping TypeScript validation"}
    code, out = _run_cmd(cmd, cwd=project_dir, timeout=VALIDATOR_TIMEOUT)
    return {"ok": code == 0, "skipped": False, "output": out}

def run_pytests(project_dir: str) -> Dict[str, Any]:
    """
    Run pytest if available. Returns same shape.
    """
    if not shutil.which("pytest"):
        return {"ok": False, "skipped": True, "output": "pytest not found; skipping Python tests"}
    code, out = _run_cmd(["pytest", "-q"], cwd=project_dir, timeout=VALIDATOR_TIMEOUT)
    return {"ok": code == 0, "skipped": False, "output": out}

def run_go_vet(project_dir: str) -> Dict[str, Any]:
    """
    Run 'go vet' if Go is installed.
    """
    if not shutil.which("go"):
        return {"ok": False, "skipped": True, "output": "go not found; skipping go vet"}
    code, out = _run_cmd(["go", "vet", "./..."], cwd=project_dir, timeout=VALIDATOR_TIMEOUT)
    return {"ok": code == 0, "skipped": False, "output": out}

# ----------------------------
# Public: run_validations
# ----------------------------
def run_validations(workdir: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """
    Runs configured validators in the given workspace.
    options may include:
      - "validate_tsc": bool
      - "validate_pytest": bool
      - "validate_go": bool

    Returns a dict:
    {
      "checked": True/False,
      "ok": True/False,
      "output": "<combined outputs>",
      "details": { "tsc": {...}, "pytest": {...} },
      "skipped": bool
    }
    """
    results = {}
    overall_ok = True
    any_checked = False
    outputs = []

    # TypeScript
    if options.get("validate_tsc", False):
        any_checked = True
        r = run_tsc_check(workdir)
        results["tsc"] = r
        outputs.append("=== tsc ===\n" + r.get("output", ""))
        if not r.get("ok", False) and not r.get("skipped", False):
            overall_ok = False

    # Pytest
    if options.get("validate_pytest", False):
        any_checked = True
        r = run_pytests(workdir)
        results["pytest"] = r
        outputs.append("=== pytest ===\n" + r.get("output", ""))
        if not r.get("ok", False) and not r.get("skipped", False):
            overall_ok = False

    # Go vet
    if options.get("validate_go", False):
        any_checked = True
        r = run_go_vet(workdir)
        results["go_vet"] = r
        outputs.append("=== go vet ===\n" + r.get("output", ""))
        if not r.get("ok", False) and not r.get("skipped", False):
            overall_ok = False

    combined_output = "\n".join(outputs).strip()
    resp = {
        "checked": any_checked,
        "ok": overall_ok if any_checked else None,
        "output": combined_output,
        "details": results,
        "skipped": not any_checked
    }
    return resp

# ----------------------------
# Repair prompt builder
# ----------------------------
def _build_repair_prompt(user_answers: Dict[str, Any], failing_output: str, files: List[Dict[str, str]], options: Dict[str, Any]) -> str:
    """
    Build a concise repair prompt asking the LLM to return corrected files in JSON {files:[{path,content},...]} format.
    We intentionally request only the files that need repair.
    """
    # short files preview (path + first N chars) to keep prompt small
    preview = []
    for f in files:
        p = f.get("path", "")
        c = f.get("content", "")
        snippet = c[:800].replace("\n", "\\n")
        preview.append({"path": p, "snippet": snippet})

    try:
        ua = json.dumps(user_answers, indent=2)
    except Exception:
        ua = str(user_answers)

    prompt = (
        "You are an assistant that repairs source files to fix the failures shown.\n"
        "OUTPUT RULE: Return a single JSON object with key 'files' whose value is a list of {\"path\":\"...\",\"content\":\"...\"}.\n"
        "Return only JSON and nothing else.\n\n"
        "Context:\n"
        f"User answers: {ua}\n\n"
        "Failure/validation output:\n"
        f"{failing_output}\n\n"
        "Files (path + snippet):\n"
        f"{json.dumps(preview, indent=2)}\n\n"
        "Task:\n"
        "- Provide corrected file contents for files that are likely causing the failures above.\n"
        "- Only include files you change. Do not return files that are already correct.\n"
        "- Ensure returned 'content' is the full file content (not a diff). Keep files minimal and idiomatic.\n"
        "- Avoid adding commentary, tests, or unrelated scaffolding.\n\n"
        "Now return the JSON object with 'files'.\n"
    )
    return prompt

# ----------------------------
# Public: attempt_repair
# ----------------------------
async def attempt_repair(workdir: str, failing_output: str, files: List[Dict[str, str]], options: Dict[str, Any]) -> Dict[str, Any]:
    """
    Attempts bounded LLM repair.
    - workdir: path where files can be written if needed (not required)
    - failing_output: combined validator output
    - files: list of {"path": "...", "content": "..."} representing current project files
    - options: may include 'user_answers', 'repair_attempts', 'debug'

    Returns:
      {
        "attempts": n,
        "repaired_files": [ {"path","content"} ... ],
        "applied": n_applied,
        "ok": True/False if repaired_files applied,
        "report": "<LLM parsed output or error>"
      }
    """
    debug = bool(options.get("debug", False))
    attempts_allowed = int(options.get("repair_attempts", REPAIR_MAX_ATTEMPTS))

    user_answers = options.get("user_answers", {})

    attempts = 0
    repaired_files = []
    parsed_resp = None

    for att in range(attempts_allowed):
        attempts += 1
        prompt = _build_repair_prompt(user_answers, failing_output, files, options)
        try:
            # Use GenerateResponseModel because it matches files:list[{path,content}]
            parsed = await call_structured_generation(prompt, GenerateResponseModel, max_retries=1, timeout=REPAIR_TIMEOUT, debug=debug)
            parsed = parsed or {}
        except Exception as e:
            # LLM call failed; record and break
            return {"attempts": attempts, "repaired_files": [], "applied": 0, "ok": False, "report": f"llm_call_failed: {e}"}

        parsed_resp = parsed
        # normalize parsed -> files list
        candidate_files = []
        try:
            files_out = parsed.get("files") if isinstance(parsed, dict) else None
            if isinstance(files_out, list):
                for it in files_out:
                    if isinstance(it, dict):
                        p = it.get("path") or ""
                        c = it.get("content") or ""
                        if p and isinstance(c, str):
                            candidate_files.append({"path": p, "content": c})
        except Exception:
            # fallthrough: parsed not as expected
            candidate_files = []

        if not candidate_files:
            # nothing to apply; stop early
            return {"attempts": attempts, "repaired_files": [], "applied": 0, "ok": False, "report": f"llm_returned_no_files: {parsed_resp}"}

        # Optionally write candidate files to workdir (caller may prefer to re-run validation)
        applied = 0
        for cf in candidate_files:
            try:
                target = Path(workdir) / cf["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(cf["content"], encoding="utf-8")
                applied += 1
            except Exception as e:
                # if writing fails, keep going but note it in report
                pass

        repaired_files = candidate_files
        return {"attempts": attempts, "repaired_files": repaired_files, "applied": applied, "ok": applied > 0, "report": parsed_resp}

    # if loop exhausted
    return {"attempts": attempts, "repaired_files": repaired_files, "applied": len(repaired_files), "ok": False, "report": parsed_resp or "no_repair_attempts_succeeded"}
