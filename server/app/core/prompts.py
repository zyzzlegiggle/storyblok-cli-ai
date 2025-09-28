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
        "You are an expert code generator producing React frontends wired to Storyblok with Tailwind styling.\n"
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
        " - Focus on actionable topics: pages, main features, auth, content mapping, component granularity,\n"
        "   visual style, i18n, preview/auth, and deployment target.\n"
        " - Prefer concrete, answerable prompts (e.g. 'Which pages do you need?') rather than developer-internal wording.\n\n"
        f"\n"
    )


def _example_output_block() -> str:
    """Short example that demonstrates the expected JSON shape for followups and files."""
    example = {
        "project_name": "my-app",
        "followups": [
            {
                "id": "primary_color",
                "question": "What is the primary brand color (hex or tailwind token)?",
                "type": "string",
                "required": True,
                "default": "#0ea5e9"
            },
            {
                "id": "include_auth",
                "question": "Should the scaffold include authentication (signup/login)?",
                "type": "boolean",
                "required": True,
                "default": False
            }
        ],
        "files": [],
        "dependencies": ["next", "react", "react-dom", "tailwindcss", "@storyblok/react"],
        "metadata": {"notes": "Returned followups because user asked for clarifications."},
        "warnings": []
    }
    return json.dumps(example, indent=2)


def build_question_generation_prompt(user_answers: Dict[str, Any],
                                     schema: Dict[str, Any],
                                     options: Dict[str, Any]) -> str:
    """
    Build the followup-generation body (context + short task).
    This is appended to the followup system prompt.
    """
    schema_summary = summarize_schema(schema)
    try:
        user_json = json.dumps(user_answers, indent=2)
    except Exception:
        user_json = str(user_answers)
    opt_json = json.dumps(options or {}, indent=2)

    prompt = (
        "Context:\n"
        f"User description / answers:\n{user_json}\n\n"
        f"Storyblok schema summary:\n{schema_summary}\n\n"
        f"Options:\n{opt_json}\n\n"
        "Task:\n"
        " - Based on the context above, decide what clarifying questions are required to produce a runnable frontend scaffold.\n"
        " - If required, return questions in the 'followups' array (caller will request the number of questions).\n\n"
        "Remember: output must be a single JSON object with key 'followups' (array of question strings)."
    )
    return prompt


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
