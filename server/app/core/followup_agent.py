# ai_backend_demo/app/core/followup_agent.py
import time
import json
from typing import Any, Dict, List, Optional

from core.prompts import build_question_generation_prompt, build_followup_system_prompt
from core.llm_client import call_structured_generation, FollowupsListModel
from utils.config import AGENT_TEMPERATURES

# in followup_agent.py - replace _parse_followups and generate_followup_questions

def _normalize_qtext(s: str) -> str:
    return " ".join(s.strip().lower().split())

def _normalize(s: str) -> str:
    return " ".join(s.strip().lower().split())

def _parse_followups(raw) -> List[Dict[str, Any]]:
    """
    Convert raw LLM output to list of followup objects:
      {id, question, urgency}
    Accept strings or objects.
    """
    out: List[Dict[str, Any]] = []
    if raw is None:
        return out
    try:
        if isinstance(raw, dict):
            f = raw.get("followups")
            if isinstance(f, list):
                for it in f:
                    if isinstance(it, dict):
                        q = it.get("question") or it.get("q") or ""
                        if q and isinstance(q, str):
                            try:
                                urgency = float(it.get("urgency")) if it.get("urgency") is not None else 0.5
                            except Exception:
                                urgency = 0.5
                            out.append({"id": it.get("id") or "", "question": q.strip(), "urgency": max(0.0, min(1.0, urgency))})
                    elif isinstance(it, str):
                        if it.strip():
                            out.append({"id": "", "question": it.strip(), "urgency": 0.5})
                return out
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return _parse_followups(parsed)
            except Exception:
                lines = [l.strip() for l in raw.splitlines() if l.strip()]
                for ln in lines:
                    out.append({"id": "", "question": ln, "urgency": 0.5})
                return out
    except Exception:
        pass
    return out

async def generate_followup_questions(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dedicated followup-question generator.
    Input payload: { user_answers, storyblok_schema, options }
    Behavior:
      - options may include:
          - max_questions (int)
          - round_number (int)
          - previous_questions (list of normalized question strings)   # optional, provided by client to avoid repetition
          - min_urgency (float between 0.0 and 1.0)                   # threshold for smart stopping
          - pad (bool)                                               # whether to pad with generic questions if model returns too few
    Returns: {"followups": [ {id, question, urgency}, ... ] }
    """
    user_answers = payload.get("user_answers", {}) or {}
    schema = payload.get("storyblok_schema", {}) or {}
    options = payload.get("options", {}) or {}
    debug = bool(options.get("debug", False))

    # Parameters
    try:
        max_questions = int(options.get("max_questions", 5))
    except Exception:
        max_questions = 5
    if max_questions < 1:
        max_questions = 1

    round_num = int(options.get("round_number", 1) or 1)

    # Build followup-focused prompt
    system_prompt = build_followup_system_prompt(max_questions=max_questions)
    body_prompt = build_question_generation_prompt(user_answers, schema, options)
    full_prompt = system_prompt + "\n\n" + body_prompt

    # Local normalizer
    def _normalize(s: str) -> str:
        return " ".join(s.strip().lower().split())

    # 1) Call the LLM (structured) and parse results (best-effort)
    parsed = None
    candidates: List[Dict[str, Any]] = []
    try:
        parsed = await call_structured_generation(full_prompt, FollowupsListModel, max_retries=1, timeout=30, debug=debug)
    except Exception:
        parsed = None

    # Use your existing forgiving parser to convert raw -> list of followup dicts
    try:
        candidates = _parse_followups(parsed)
    except Exception:
        candidates = []

    # 2) Build set of previously-asked question texts (client-supplied) and previously-answered content
    prev_questions_client = options.get("previous_questions", []) or []
    prev_q_norm = set(_normalize(q) for q in prev_questions_client if isinstance(q, str))

    prev_answers_map = {}
    if isinstance(user_answers, dict):
        prev_answers_map = user_answers.get("followup_answers", {}) or {}
    prev_answer_texts = set()
    for k, v in (prev_answers_map.items() if isinstance(prev_answers_map, dict) else []):
        if isinstance(v, str) and v.strip():
            prev_answer_texts.add(_normalize(v))

    # 3) Dedupe/filter candidates against previous Qs + previous answers
    filtered: List[Dict[str, Any]] = []
    seen_qs = set(prev_q_norm)  # start with already asked questions
    for cand in candidates:
        qtext = (cand.get("question") or "") if isinstance(cand, dict) else ""
        if not isinstance(qtext, str) or not qtext.strip():
            continue
        qnorm = _normalize(qtext)
        if qnorm in seen_qs:
            continue

        # Basic coverage heuristic: if the candidate question appears to be already answered
        # by any previous answer, skip it. This is a conservative substring check.
        already_answered = False
        for a in prev_answer_texts:
            if a and (a in qnorm or qnorm in a):
                already_answered = True
                break
        if already_answered:
            continue

        # Accept candidate
        seen_qs.add(qnorm)
        # Ensure structure and defaults
        out_item = {
            "id": cand.get("id", "") if isinstance(cand, dict) else "",
            "question": qtext.strip(),
            "urgency": float(cand.get("urgency", 0.5)) if isinstance(cand, dict) else 0.5
        }
        filtered.append(out_item)

    # 4) Apply urgency threshold
    try:
        min_urgency = float(options.get("min_urgency", 0.25))
    except Exception:
        min_urgency = 0.25
    filtered = [f for f in filtered if float(f.get("urgency", 0.5)) >= min_urgency]

    # 5) Truncate to desired count
    out = filtered[:max_questions]

    # Return the final followups list (structured)
    return {"followups": out}
