from pydantic import BaseModel
from typing import Any, Dict, List, Optional

class GenerateRequest(BaseModel):
    user_answers: Dict[str, Any]
    options: Optional[Dict[str, Any]] = {}

class FileOut(BaseModel):
    path: str
    content: str

class GenerateResponse(BaseModel):
    project_name: str
    files: List[FileOut]
    metadata: Optional[Dict[str, Any]] = {}
