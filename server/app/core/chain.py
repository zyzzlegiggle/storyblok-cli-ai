from core.llm_client import call_gemini
from core.prompts import build_system_prompt, build_user_prompt
from utils.file_helpers import validate_file_tree
import json
import time

async def generate_project_files(payload: dict) -> dict:
    user_answers = payload["user_answers"]
    schema = payload["storyblok_schema"]
    options = payload.get("options", {})

    # 1) Construct initial prompt: include instructions, project rules, constraints, and Storyblok schema.
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(user_answers, schema, options)

    # 2) Call LLM (Gemini) via llm_client (LangChain-style chain can be implemented here).
    # We ask the model to:
    # - Propose project structure (folders & files)
    # - For each Storyblok component, produce a React component (TSX)
    # - Produce service file to fetch from Storyblok
    # - Produce tailwind config, package.json, types
    llm_response = await call_gemini(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        timeout=120  # generous for code gen
    )

    # 3) Parse LLM output into file list
    # Expect a JSON blob from the LLM formatted like:
    # {"project_name": "...", "files": [{"path":"...","content":"..."}], "warnings": [...]}
    try:
        parsed = json.loads(llm_response)
    except Exception as e:
        # If LLM didn't return valid JSON, return raw output as a single file to inspect
        parsed = {
            "project_name": user_answers.get("app_name", "storyblok-app"),
            "files": [{"path": "LLM_OUTPUT.txt", "content": llm_response}],
            "metadata": {"parse_error": str(e)}
        }

    # 4) Validate / sanitize file list
    parsed["files"] = validate_file_tree(parsed.get("files", []))

    # 5) Attach metadata and return
    parsed.setdefault("metadata", {})
    parsed["metadata"].setdefault("generated_at", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return parsed
