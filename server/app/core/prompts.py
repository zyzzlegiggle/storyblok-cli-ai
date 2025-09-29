# ai_backend_demo/app/core/prompts.py
"""
Prompts used by the generation pipeline.

Goals:
- Force JSON-only structured outputs the backend can parse.
- Provide a clear dependency policy: return dependency *names only* (no versions).
- Provide a structured followup-question format so the CLI can render them and prefill answers.
- Encourage concise schema summaries and conservative file output (returns partial files + warnings).
"""

import json
from typing import Any, Dict, List, Optional


def summarize_schema(schema: Dict[str, Any],
                     max_components: int = 12,
                     max_fields: int = 8,
                     max_chars: int = 1500) -> str:
    """
    Produce a concise textual summary of a Storyblok schema object.
    - schema: raw JSON from Storyblok (expected key 'components' or similar)
    - returned string is human-friendly and intended for LLM prompts
    """
    if not schema:
        return "(no schema provided)"

    lines: List[str] = []
    components = []

    # Common Storyblok export uses "components" as top-level
    if isinstance(schema, dict) and "components" in schema:
        components = schema.get("components") or []
    else:
        # Heuristic: find any list of dicts that looks like components
        for k, v in schema.items():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "name" in v[0]:
                components = v
                break
        if not components:
            # Last resort: if schema is a mapping of component-name -> schema
            comps = []
            for k, v in schema.items():
                if isinstance(v, dict):
                    comps.append({"name": k, "schema": v})
            components = comps

    if not isinstance(components, list) or not components:
        return "(schema provided but no recognizable components found)"

    n = 0
    total = len(components)
    for comp in components:
        if n >= max_components:
            lines.append(f"...and {total - max_components} more components (truncated)")
            break
        n += 1

        name = comp.get("name") or comp.get("display_name") or comp.get("component") or "Component"
        lines.append(f"- Component: {name}")

        comp_schema = comp.get("schema") or comp.get("fields") or {}
        if isinstance(comp_schema, dict):
            fields = list(comp_schema.keys())
        elif isinstance(comp_schema, list):
            try:
                fields = [f.get("name", f.get("field", "unnamed")) for f in comp_schema]
            except Exception:
                fields = []
        else:
            fields = []

        if not fields:
            lines.append("    fields: (none detected)")
        else:
            shown = 0
            field_strs = []
            for field in fields:
                if shown >= max_fields:
                    break
                ftype = None
                if isinstance(comp_schema, dict) and field in comp_schema and isinstance(comp_schema[field], dict):
                    ftype = comp_schema[field].get("type") or comp_schema[field].get("field_type")
                if ftype:
                    field_strs.append(f"{field}:{ftype}")
                else:
                    field_strs.append(str(field))
                shown += 1
            if len(fields) > max_fields:
                field_strs.append(f"...(+{len(fields)-max_fields} more)")
            lines.append("    fields: " + ", ".join(field_strs))

        if sum(len(l) for l in lines) > max_chars:
            lines.append("...schema summary truncated due to length")
            break

    return "\n".join(lines)


def build_system_prompt(model_name: Optional[str] = None) -> str:
    """
    System prompt for the main code-generation agent.
    Clear, short rules to reduce accidental extra text.
    """
    return (
        "You are an expert and creative code generator producing NextJS React frontends wired to Storyblok with Tailwind styling.\n"
        "OUTPUT RULES:\n"
        " - Return EXACTLY one valid JSON object and nothing else.\n"
        " - Top-level keys must include: project_name, files, dependencies, metadata, warnings, followups.\n"
        " - 'files' is a list of {\"path\":\"relative/path\",\"content\":\"...\"}. Content must be a string.\n"
        " - 'dependencies' is an ARRAY OF PACKAGE NAMES ONLY (no versions, no URLs).\n"
        " - Keep files small and modular; prefer multiple small files over one huge file.\n"
        " - Do NOT include secrets or tokens in files; reference env vars instead.\n"
        f"\n"
    )

def build_followup_system_prompt(max_questions: int = 5, model_name: Optional[str] = None) -> str:
    """
    System prompt specialized for generating follow-up questions only.
    Returns a short instruction the followup-only agent should follow.
    """
    return (
        "You are a concise requirements elicitor for frontend projects wired to Storyblok.\n"
        "OUTPUT RULES:\n"
        " - Return ONLY a single JSON object with one key: 'followups', value is an ARRAY OF STRINGS.\n"
        " - Do NOT return ids, types, files, dependencies, code, or any commentary.\n"
        " - JSON must look exactly like: {\"followups\": [\"question 1\", \"question 2\", ...]}\n\n"
        "QUESTION GUIDELINES:\n"
        f" - Produce {max_questions} short, natural-language, user-facing clarifying questions (each <= 120 chars).\n"
        " - Focus on actionable topics: pages, main features, content mapping, component granularity,\n"
        "   visual style, theme, colors\n"
        " - Prefer concrete, answerable prompts (e.g. 'Which pages do you need?') rather than developer-internal wording.\n\n"
        f"\n"
    )


def build_question_generation_prompt(user_answers: Dict[str, Any],
                                     schema: Dict[str, Any],
                                     options: Dict[str, Any]) -> str:
    """
    Build the followup-generation prompt. Now explicitly includes previous followups
    and their answers (id->question->answer), and instructs the model to base next
    questions on those answers and NOT to repeat the same questions.
    """
    schema_summary = summarize_schema(schema)
    try:
        user_json = json.dumps(user_answers, indent=2)
    except Exception:
        user_json = str(user_answers)
    opt_json = json.dumps(options or {}, indent=2)

    round_num = (options.get("round_number") if options and isinstance(options, dict) else None) or 1

    # Extract previous followup answers mapping if present
    prev_fanswers = {}
    if isinstance(user_answers, dict):
        # expected shape: user_answers["followup_answers"] is map[id] = value
        prev_fanswers = user_answers.get("followup_answers", {}) or {}

    try:
        prev_json = json.dumps(prev_fanswers, indent=2)
    except Exception:
        prev_json = str(prev_fanswers)

    prompt_lines = [
        "Context:",
        f"User description / answers:\n{user_json}",
        "",
        f"Storyblok schema summary:\n{schema_summary}",
        "",
        f"Previous followup answers (round {round_num}):\n{prev_json}",
        "",
        f"Options:\n{opt_json}",
        "",
        "Task:",
        "- You will propose additional clarifying follow-up questions that *build on the user's previous answers*.",
        "- DO NOT repeat prior questions. Prior questions and their answers are provided above (id -> answer).",
        "- Where possible, reference prior answers to drill down. E.g. if the user answered 'auth: email', ask 'Do you want email+password or magic links?'.",
        "- Return only new, actionable questions that are necessary to produce a runnable scaffold.",
        "- For each followup, return an OBJECT with keys: 'id' (short identifier — optional, but prefer stable ids),",
        "  'question' (the user-facing question string), and optional 'urgency' (0.0-1.0).",
        "- If there are no further clarifications needed, return an empty 'followups' array.",
        "",
        "OUTPUT RULES:",
        "Return a single JSON object exactly like: {\"followups\":[{\"id\":\"...\",\"question\":\"...\",\"urgency\":0.8}, ...]}",
        "Followups may also be simple strings (for compatibility), but prefer the object form.",
    ]

    return "\n".join(prompt_lines)


def build_user_prompt(user_answers: Dict[str, Any], schema: Dict[str, Any], options: Dict[str, Any]) -> str:
    """
    Prompt body for the main generation step. Combined with build_system_prompt above.
    """
    schema_summary = summarize_schema(schema)
    try:
        user_json = json.dumps(user_answers, indent=2)
    except Exception:
        user_json = str(user_answers)
    opt_json = json.dumps(options or {}, indent=2)

    prompt = (
        "Context:\n"
        f"User requirements:\n{user_json}\n\n"
        f"Storyblok schema summary:\n{schema_summary}\n\n"
        f"Options:\n{opt_json}\n\n"
        "Generation instructions:\n"
        " 1) Produce a runnable frontend scaffold (React + Storyblok) matching the user's requirements.\n"
        " 2) Include minimal scaffolding files: package.json, build config, basic pages, Storyblok client, and representative components.\n"
        " 3) Only list dependency NAMES in 'dependencies' (no versions).\n"
        " 4) If any required information is missing, set 'followups' to a non-empty array (strings) and leave 'files' empty.\n"
        " 5) If you cannot generate everything, return partial files and include a clear note in metadata.warnings.\n\n"
        "Output: produce the single JSON object described by the system prompt. No extra text."
    )
    return prompt
