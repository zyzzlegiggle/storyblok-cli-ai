# app/api/generate.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from fastapi.responses import StreamingResponse
from core.codegen_agent import generate_project, stream_generate_project
from core.followup_agent import generate_followup_questions
from core.llm_client import call_structured_generation
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
