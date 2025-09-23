from fastapi import APIRouter, HTTPException
from models import GenerateRequest, GenerateResponse
from core.chain import generate_project_files

router = APIRouter()

@router.post("/", response_model=GenerateResponse)
async def generate(req: GenerateRequest):
    try:
        result = await generate_project_files(req.dict())
        return result
    except Exception as e:
        # don't expose stack traces in prod; for hackathon it's okay to return message
        raise HTTPException(status_code=500, detail=str(e))