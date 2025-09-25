# app/api/generate.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, Optional
from fastapi.responses import StreamingResponse

from core.chain import generate_project_files, stream_generate_project

router = APIRouter()

class GenerateRequest(BaseModel):
    user_answers: Dict[str, Any]
    storyblok_schema: Dict[str, Any]
    options: Optional[Dict[str, Any]] = {}

@router.post("/", response_model=Dict[str, Any])
async def generate(req: GenerateRequest):
    try:
        result = await generate_project_files(req.dict())
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
