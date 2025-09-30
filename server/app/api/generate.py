# app/api/generate.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from fastapi.responses import StreamingResponse
from core.codegen_agent import generate_project, stream_generate_project
from core.followup_agent import generate_followup_questions
from core.llm_client import call_structured_generation
from core.prompts import summarize_schema
import json
import os
import traceback
import tempfile
from pathlib import Path
import shutil

from utils.file_helpers import _safe_normalize
from pydantic import BaseModel as PydanticBaseModel

router = APIRouter()

class GenerateRequest(BaseModel):
    user_answers: Dict[str, Any]
    storyblok_schema: Dict[str, Any]
    options: Optional[Dict[str, Any]] = {}
    base_files: Optional[List[Dict[str, Any]]] = None
    asset_files: Optional[List[str]] = None


@router.post("/", response_model=Dict[str, Any])
async def generate(req: GenerateRequest):
    try:
        result = await generate_project(req.dict())
        _log_incoming_request("generate", req)
        return result
    except Exception as e:
        # Save stacktrace to logs for debugging
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stream")
async def generate_stream(req: GenerateRequest):
    """
    Streaming version of /generate that yields newline-delimited JSON events.
    The client should read the response line-by-line and parse each JSON event.

    Note: If the model decides followups are required, a 'followups' event will be
    emitted and the stream will end. The client should then collect answers and
    re-call this endpoint with followup_answers included.
    """
    try:
        async def event_generator():
            async for line in stream_generate_project(req.dict()):
                # stream_generate_project yields strings (JSON lines)
                yield line.encode("utf-8")
        return StreamingResponse(event_generator(), media_type="application/x-ndjson")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/questions", response_model=Dict[str, Any])
async def generate_questions(req: GenerateRequest):
    """
    Dedicated followup-question endpoint.
    Returns JSON: { "followups": ["q1", "q2", ...] }
    """
    try:
        result = await generate_followup_questions(req.dict())
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




class FileOutModel(PydanticBaseModel):
    path: str
    content: str

class OverlayResponseModel(PydanticBaseModel):
    project_name: Optional[str] = None
    files: List[FileOutModel] = []
    new_dependencies: List[str] = []
    warnings: Optional[List[str]] = []

# --- The overlay endpoint ---
@router.post("/overlay", response_model=Dict[str, Any])
async def generate_overlay(request: Dict[str, Any]):
    """
    Overlay generation:
    Request JSON:
      {
        "user_answers": {...},
        "storyblok_schema": {...},   # optional
        "options": {...},
        "base_files": [ {"path": "src/...", "content": "..."}, ... ]
      }
    Response:
      {
        "files": [ {"path","content"}, ... ],   # only new/changed files (package.json excluded)
        "new_dependencies": ["pkg1","pkg2", ...],   # names only
        "warnings": [...]
      }
    """
    try:
        
        user_answers = request.get("user_answers", {}) or {}
        schema = request.get("storyblok_schema", {}) or {}
        options = request.get("options", {}) or {}
# --- inside generate_overlay(...) near the top after reading request dict ---
        base_files = request.get("base_files", []) or []
        asset_files = request.get("asset_files", []) or []  # NEW: list of relative paths (strings)
        _log_incoming_request("overlay", request)
        # validate base_files shape
        if not isinstance(base_files, list):
            raise HTTPException(status_code=400, detail="base_files must be an array of {path,content}")
        if not isinstance(asset_files, list):
            raise HTTPException(status_code=400, detail="asset_files must be an array of file path strings")

        # create temp workspace and write base files (skip package.json if present)
        tmpdir = tempfile.mkdtemp(prefix="overlay_ws_")
        try:
            # normalize and validate asset list
            normalized_assets = set()
            for a in asset_files:
                if not isinstance(a, str):
                    continue
                sp = _safe_normalize(a)
                if sp:
                    normalized_assets.add(sp)

            base_map: Dict[str,str] = {}
            for f in base_files:
                if not isinstance(f, dict):
                    continue
                p = f.get("path")
                c = f.get("content", "")
                sp = _safe_normalize(p or "")
                if sp is None:
                    # skip unsafe or empty paths
                    continue
                # never write assets to workspace: if path is listed as asset, skip writing content
                if sp in normalized_assets:
                    # record but do not write binary content to disk
                    base_map[sp] = ""  # indicate presence but no content
                    continue

                # skip package.json intentionally
                if os.path.basename(sp) == "package.json":
                    continue
                target = Path(tmpdir) / sp
                target.parent.mkdir(parents=True, exist_ok=True)
                # write text content (safe, empty content allowed for placeholders)
                target.write_text(c or "", encoding="utf-8")
                base_map[sp] = c or ""


            # Build overlay-specific system + user prompt
            schema_summary = summarize_schema(schema)
            # system prompt (concise rules)
            system_prompt = (
                "You are an expert code generator that modifies an existing Storyblok demo scaffold.\n"
                "OUTPUT RULES:\n"
                " - Return EXACTLY one valid JSON object and nothing else.\n"
                " - Top-level keys: project_name (optional), files (array), new_dependencies (array of package NAMES only), warnings (optional).\n"
                " - 'files' must contain only files that are NEW or CHANGED relative to the provided base scaffold. Do NOT return package.json or modify it.\n"
                " - For each file return {\"path\":\"relative/path\",\"content\":\"full file content\"}.\n"
                " - 'new_dependencies' is an ARRAY OF PACKAGE NAMES ONLY (no versions, no URLs). The CLI will merge and pin them.\n"
                " - Do NOT include secrets or tokens.\n"
                " - If you cannot produce changes, return files:[] and explain in warnings.\n"
            )

            # user prompt: include short manifest and user instructions
            # include the first ~400 chars of each base file as context to help the model understand project layout
            manifest = []
            for p, c in list(base_map.items())[:200]:
                snippet = (c or "")[:800].replace("\n", "\\n")
                manifest.append({"path": p, "snippet": snippet})
            user_prompt = (
                "Context:\n"
                f"User answers: {json.dumps(user_answers, ensure_ascii=False)}\n\n"
                f"Storyblok schema summary:\n{schema_summary}\n\n"
                f"Scaffold manifest (path + snippet):\n{json.dumps(manifest, ensure_ascii=False)}\n\n"
                "Task:\n"
                "- Make the smallest set of file changes required to implement the user's requests.\n"
                "- Return only files you add or change. Do NOT return package.json. Instead list any new packages you require in 'new_dependencies'.\n"
                "- Keep file contents minimal and idiomatic. Avoid unrelated refactors.\n\n"
                "Now return the single JSON object as specified in the system prompt."
            )

            full_prompt = system_prompt + "\n\n" + user_prompt

            # Call the LLM with a structured model
            try:
                parsed = await call_structured_generation(full_prompt, OverlayResponseModel, max_retries=1, timeout=180, debug=bool(options.get("debug", False)))
            except Exception as e:
                # fallback: return no changes with a warning
                return {"files": [], "new_dependencies": [], "warnings": [f"llm_call_failed: {e}"]}

            # parsed should be a dict-like; ensure types
            if not isinstance(parsed, dict):
                # Some wrappers return Pydantic models — convert to dict safely
                try:
                    parsed = json.loads(json.dumps(parsed))
                except Exception:
                    parsed = {}

            # Extract files (list of {path,content})
            files_out_raw = parsed.get("files", []) or []
            candidate_files: List[Dict[str,str]] = []
            binary_exts = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".avif"}
            for it in files_out_raw:
                if not isinstance(it, dict):
                    continue
                p = it.get("path") or ""
                c = it.get("content") or ""
                sp = _safe_normalize(p)
                if sp is None: continue
                if os.path.splitext(sp)[1].lower() in binary_exts:
                    # warn and skip binary files returned by model
                    warnings.append(f"model attempted to return binary asset as text: {sp}; skipped")
                    continue
                # never accept package.json modifications
                if os.path.basename(sp) == "package.json":
                    # ignore but record warning
                    continue
                candidate_files.append({"path": sp, "content": c})

            # Compute delta: only include files that are new or whose content differs
            delta_files: List[Dict[str,str]] = []
            for cf in candidate_files:
                p = cf["path"]
                new_c = cf["content"] or ""
                old_c = base_map.get(p)
                if old_c is None or old_c != new_c:
                    delta_files.append({"path": p, "content": new_c})

            # extract new_dependencies robustly from model output
            new_deps_raw = parsed.get("new_dependencies", []) or []
            new_deps: List[str] = []
            if isinstance(new_deps_raw, list):
                for it in new_deps_raw:
                    if isinstance(it, str) and it.strip():
                        new_deps.append(it.strip())

            # Also accept dependencies listed under parsed.get("metadata", {}).get("dependencies") for backwards compatibility
            if not new_deps:
                meta = parsed.get("metadata") or {}
                if isinstance(meta, dict):
                    deps_meta = meta.get("dependencies") or meta.get("new_dependencies")
                    if isinstance(deps_meta, list):
                        for d in deps_meta:
                            if isinstance(d, str) and d.strip():
                                new_deps.append(d.strip())

            warnings = parsed.get("warnings") or []
            if not isinstance(warnings, list):
                warnings = [str(warnings)]

            return {"files": delta_files, "new_dependencies": new_deps, "warnings": warnings}
        finally:
            # cleanup workspace
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass

    except HTTPException:
        # re-raise FastAPI HTTP errors
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    
import os, time, json

def _log_incoming_request(tag: str, payload: dict):
    try:
        os.makedirs("ai_backend_logs", exist_ok=True)
        fname = os.path.join(
            "ai_backend_logs", f"{int(time.time())}_{tag}_incoming.json"
        )
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[warn] failed to log incoming request: {e}")
