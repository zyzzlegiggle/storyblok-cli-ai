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
                     max_components: int = 20,
                     max_fields: int = 12,
                     max_chars: int = 3000) -> str:
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
    System prompt instructing the model about output format and policies.
    model_name is optional hint for model-specific behavior 
    """
    model_hint = f" Target model: {model_name}." if model_name else ""
    return (
        "You are an expert code generator that creates React frontends wired to Storyblok.\n"
        "CRITICAL:\n"
        " - Respond ONLY with a single valid JSON object (no surrounding text, no backticks).\n"
        " - Do NOT include any commentary, explanation, or extraneous keys.\n"
        " - When asked for dependencies return package *names only* (no versions, no URLs) in a separate key called 'dependencies'.\n"
        " - When generating follow-up questions return them in the 'followups' key as a list of objects with these fields:\n"
        "     {\"id\": \"string\", \"question\": \"string\", \"type\": \"string\", \"required\": bool, \"choices\": [..]?, \"default\": ..?}\n"
        "   Supported types: 'string', 'boolean', 'choice', 'multichoice'.\n"
        " - If follow-ups are required, set 'files' to an empty list and return the questions in 'followups'.\n"
        " - If you can generate code, return 'files' as a list of {path, content} objects where content is string-escaped source code.\n"
        " - If you cannot produce everything due to token limits, still return partial 'files' plus 'warnings'.\n"
        " - Always include a 'metadata' object with any notes, validations, or warnings.\n"
        f"{model_hint}\n"
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
    Build a prompt specifically for the diagnostic step where the LLM must
    return structured follow-up questions if more information is required.
    """
    schema_summary = summarize_schema(schema)
    try:
        user_json = json.dumps(user_answers, indent=2)
    except Exception:
        user_json = str(user_answers)
    opt_json = json.dumps(options or {}, indent=2)

    prompt = (
        "You will only output valid JSON matching the structure shown in the EXAMPLE below.\n\n"
        "Context:\n"
        f"User answers:\n{user_json}\n\n"
        f"Storyblok schema summary:\n{schema_summary}\n\n"
        f"Options:\n{opt_json}\n\n"
        "Task:\n"
        "  - Decide whether you need clarifying questions to generate a runnable scaffold.\n"
        "  - If clarifying questions are needed, output JSON with 'followups' (list) and empty 'files'.\n"
        "  - If no clarifying questions are needed, output 'followups': [] and proceed to include 'files'.\n\n"
        "Followup format requirements:\n"
        "  - Each followup must contain: id (string), question (string), type ('string'|'boolean'|'choice'|'multichoice'), required (bool).\n"
        "  - For 'choice' or 'multichoice' provide a 'choices' array of strings and optionally 'default'.\n"
        "  - Keep followups short and scoped to what is needed to produce the frontend (colors, layout, auth, pages, content mapping).\n\n"
        "Dependency policy:\n"
        "  - If referencing packages, use the 'dependencies' array to list package names ONLY (no versions, no commentary).\n\n"
        "Example output (must follow this exact JSON schema):\n"
        f"{_example_output_block()}\n\n"
        "Now produce the JSON output required for this diagnostic step. Do NOT add any explanation or non-JSON text."
    )
    return prompt


def build_user_prompt(user_answers: Dict[str, Any], schema: Dict[str, Any], options: Dict[str, Any]) -> str:
    """
    Build the main generation prompt used when the backend is ready to generate files.
    The model is expected to return a single JSON object with keys:
      - project_name
      - files: [{path, content}] (content must be string)
      - dependencies: [package-name-only]
      - metadata: { ... }
      - warnings: [ ... ]
      - followups: []   (should be empty when returning files)
    """
    schema_summary = summarize_schema(schema)
    try:
        user_json = json.dumps(user_answers, indent=2)
    except Exception:
        user_json = str(user_answers)
    opt_json = json.dumps(options or {}, indent=2)

    prompt = (
        "You will ONLY respond with a single valid JSON object (no surrounding text).\n\n"
        "Context:\n"
        f"User requirements:\n{user_json}\n\n"
        f"Storyblok schema summary:\n{schema_summary}\n\n"
        f"Options:\n{opt_json}\n\n"
        "Generation instructions and constraints:\n"
        "1) Output JSON keys: project_name, files, dependencies, metadata, warnings, followups.\n"
        "2) 'files' must be a list of objects: {\"path\": \"relative/path.ext\", \"content\": \"...source code...\"}.\n"
        "   - Content must be string-escaped. Use TypeScript (TSX) for React components if options.typescript is true.\n        Otherwise use JavaScript/JSX.\n"
        "3) 'dependencies' must be an array of package names only (e.g. ['next', 'react']). DO NOT include versions or comments.\n"
        "4) Map each Storyblok component to a React component that accepts a 'blok' prop and renders fields.\n"
        "5) Provide a minimal Tailwind setup and a storyblok client/service file that uses the Storyblok token if provided in user answers.\n"
        "6) If options.include_pages is true, include at least a Home page and mapping to Storyblok content fetching.\n"
        "7) If you cannot generate all files due to length, return as many complete files as possible and add explanatory warnings in 'metadata' and 'warnings'.\n"
        "8) 'followups' must be an empty list here (this is the generation step); followups belong to the diagnostic question step.\n\n"
        "Important: be conservative with large files — prefer smaller, readable files over huge single-file dumps.\n\n"
        "Example of expected top-level JSON shape (abbreviated):\n"
        '{"project_name":"my-app","files":[{"path":"pages/index.tsx","content":"..."}],"dependencies":["next","react"],'
        '"metadata":{},"warnings":[],"followups":[]}\n\n'
        "Now produce the JSON response. DO NOT include any commentary, only JSON."
    )
    return prompt
