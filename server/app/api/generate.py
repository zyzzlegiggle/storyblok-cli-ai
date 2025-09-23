# app/api/generate.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, Optional
from core.chain import generate_project_files

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
