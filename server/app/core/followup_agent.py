# ai_backend_demo/app/core/followup_agent.py
import time
import json
from typing import Any, Dict, List, Optional

from core.prompts import build_question_generation_prompt, build_followup_system_prompt
from core.llm_client import call_structured_generation, FollowupsListModel

# Small, forgiving parser for structured LLM outputs
def _parse_followups(raw) -> List[str]:
    """
    Ensure raw is a dict with 'followups' as a list of strings.
    Accepts variations and tries best-effort parsing.
    """
    out: List[str] = []
    if raw is None:
        return out
    # if it's already a dict-like with followups
    try:
        if isinstance(raw, dict):
            f = raw.get("followups")
            if isinstance(f, list):
                for it in f:
                    if isinstance(it, str) and it.strip():
                        out.append(it.strip())
                    else:
                        # try to coerce
                        try:
                            s = str(it).strip()
                            if s:
                                out.append(s)
                        except Exception:
                            pass
                return out
        # if it's a JSON string, attempt to parse
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return _parse_followups(parsed)
            except Exception:
                # attempt to treat raw as newline-separated questions
                lines = [l.strip() for l in raw.splitlines() if l.strip()]
                if lines:
                    return lines
    except Exception:
        pass
    return out

async def generate_followup_questions(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dedicated followup-question generator.
    Input payload: { user_answers, storyblok_schema, options }
    Returns: {"followups": ["q1", "q2", ...]}
    """
    user_answers = payload.get("user_answers", {}) or {}
    schema = payload.get("storyblok_schema", {}) or {}
    options = payload.get("options", {}) or {}
    debug = bool(options.get("debug", False))

    # Determine requested number of questions; enforce minimum 5
    try:
        max_questions = int(options.get("max_questions", 5))
    except Exception:
        max_questions = 5
    if max_questions < 5:
        max_questions = 5

    # Build followup-focused prompt
    system_prompt = build_followup_system_prompt(max_questions=max_questions)
    body_prompt = build_question_generation_prompt(user_answers, schema, options)
    full_prompt = system_prompt + "\n\n" + body_prompt

    parsed = None
    try:
        parsed = await call_structured_generation(full_prompt, FollowupsListModel, max_retries=1, timeout=30, debug=debug)
    except Exception:
        parsed = None

    followups = _parse_followups(parsed)

    # If LLM returned fewer than requested, pad with safe natural questions
    desired = max_questions
    if len(followups) < desired:
        pads = [
            "Which pages should the app include (e.g., home, about, blog, contact)?",
            "List the core features required (search, auth, forms, ecommerce, CMS editing).",
            "Do you want user authentication? If yes, what type (email, OAuth, SSO)?",
            "Describe the visual style briefly (minimal, corporate, colorful, design system).",
            "What is the target audience?"
        ]
        for p in pads:
            if len(followups) >= desired:
                break
            if p not in followups:
                followups.append(p)

    # Truncate to desired count and return
    return {"followups": followups[:desired]}
