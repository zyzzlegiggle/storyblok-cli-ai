# app/core/llm_client.py
import os
import json
import time
import logging
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv


from  langchain_core.pydantic_v1 import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()

# Logging / debug directory
LOG_DIR = os.environ.get("AI_BACKEND_LOG_DIR", "./ai_backend_logs")
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# -------------------------
# Structured Pydantic models
# -------------------------
class MetadataValidationModel(BaseModel):
    checked: Optional[bool] = Field(None, description="Whether validation was run")
    ok: Optional[bool] = Field(None, description="Validation success")
    output: Optional[str] = Field(None, description="Validation output or errors")
    skipped: Optional[bool] = Field(None, description="If validation was skipped due to missing tools")

class FollowupItem(BaseModel):
    id: Optional[str] = Field(None, description="Followup id")
    question: Optional[str] = Field(None, description="Followup question text")
    type: Optional[str] = Field(None, description="Type, e.g. 'text'")

class MetadataModel(BaseModel):
    warnings: Optional[List[str]] = Field(default_factory=list, description="Generator warnings")
    validation: Optional[MetadataValidationModel] = Field(None, description="Validation results")
    followups: Optional[List[FollowupItem]] = Field(None, description="Follow-up questions, if any")

class LLMAttempt(BaseModel):
    attempt: Optional[int] = Field(None)
    duration_s: Optional[float] = Field(None)
    prompt: Optional[str] = Field(None)
    raw_result: Optional[str] = Field(None)

class LLMDebugModel(BaseModel):
    attempts: Optional[List[LLMAttempt]] = Field(None, description="Raw attempt info")
    raw: Optional[str] = Field(None, description="Raw model output string (if any)")

class FileOutModel(BaseModel):
    path: str = Field(..., description="Relative path for the file")
    content: str = Field(..., description="File content as a string")

class GenerateResponseModel(BaseModel):
    project_name: str = Field(..., description="Project name")
    files: List[FileOutModel] = Field(..., description="List of files")
    metadata: Optional[MetadataModel] = Field(None, description="Optional metadata")
    llm_debug: Optional[LLMDebugModel] = Field(None, description="Optional debug info")

# Simple model for followup-only responses: {"followups": ["q1","q2",...]}
class FollowupsListModel(BaseModel):
    followups: List[str] = Field(..., description="List of follow-up question strings")


# -------------------------
# LLM init + structured call
# -------------------------
def get_llm():
    # use the env var you specified
    api_key = os.getenv("GOOGLE_API_KEY_GEMINI")
    if not api_key:
        raise RuntimeError("Please set GOOGLE_API_KEY_GEMINI environment variable for Gemini access.")
    if "GOOGLE_API_KEY" not in os.environ:
        os.environ["GOOGLE_API_KEY"] = api_key
    # Instantiate the LangChain Google Gemini LLM wrapper
    # you can adjust model name to available ones in your account
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash-lite", temperature=0.5)
    return llm

def _save_debug_log(prefix: str, payload: Dict[str, Any]):
    fname = f"{int(time.time())}_{prefix}.json"
    path = os.path.join(LOG_DIR, fname)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
    except Exception:
        logger.exception("Failed to write debug log")

async def call_structured_generation(prompt: str,
                                     structured_model: BaseModel,
                                     max_retries: int = 2,
                                     timeout: int = 180,
                                     debug: bool = False) -> Dict[str, Any]:
    """
    Call Gemini via langchain_google_genai ChatGoogleGenerativeAI.with_structured_output.
    structured_model should be a Pydantic model class (like GenerateResponseModel).
    Returns a dict parsed from the model response.
    """
    llm = get_llm()

    # Build structured callable: pass the Pydantic class itself
    structured_callable = llm.with_structured_output(structured_model, method="json_mode")

    last_exc = None
    attempts_info = []

    # Total attempts = 1 initial + max_retries
    total_attempts = 1 + max_retries
    for attempt in range(1, total_attempts + 1):
        start_ts = time.time()
        try:
            # invoke may be synchronous in this wrapper
            result = structured_callable.invoke(prompt)
            duration = time.time() - start_ts

            raw_result_str = str(result)
            attempts_info.append({
                "attempt": attempt,
                "duration_s": duration,
                "prompt": (prompt[:2000] + "...") if len(prompt) > 2000 else prompt,
                "raw_result": raw_result_str if len(raw_result_str) <= 10000 else raw_result_str[:10000] + "..."
            })

            # Save debug logs
            if debug:
                _save_debug_log(f"llm_attempt_{attempt}", {"prompt": prompt, "raw_result": raw_result_str})

            # result may be a Pydantic object or a custom wrapper. Try few conversions:
            parsed: Dict[str, Any] = {}
            # if result is pydantic BaseModel instance:
            try:
                # Many LangChain wrappers return an object with .dict() or .__dict__
                if hasattr(result, "dict"):
                    parsed = result.dict()
                elif hasattr(result, "__dict__"):
                    parsed = result.__dict__
                else:
                    # try parse as JSON string
                    parsed = json.loads(str(result))
            except Exception:
                # final fallback: attach raw as text
                parsed = {"raw": str(result)}

            # attach debug attempts summary
            parsed.setdefault("metadata", {})
            if debug:
                parsed["llm_debug"] = {"attempts": attempts_info, "raw": parsed.get("llm_debug", None)}

            return parsed
        except Exception as e:
            last_exc = e
            duration = time.time() - start_ts
            attempts_info.append({"attempt": attempt, "duration_s": duration, "error": repr(e)})
            logger.exception("LLM attempt %d failed: %s", attempt, e)
            # save prompt+error
            _save_debug_log(f"llm_error_attempt_{attempt}", {"prompt": prompt, "error": repr(e)})
            # backoff
            if attempt < total_attempts:
                time.sleep(1 * attempt)
                continue
            else:
                break

    raise RuntimeError(f"LLM generation failed after {total_attempts} attempts. Last error: {last_exc}")
