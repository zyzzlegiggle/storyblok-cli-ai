import json
from typing import Any, Dict, List

def summarize_schema(schema: Dict[str, Any],
                     max_components: int = 20,
                     max_fields: int = 12,
                     max_chars: int = 3000) -> str:
    """
    Produce a concise textual summary of a Storyblok schema object.
    - schema: raw JSON from Storyblok (expected key 'components' or similar)
    - max_components: limit how many components to list
    - max_fields: limit how many fields per component to show
    - max_chars: final string length cap (to avoid huge LLM prompts)
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
    for comp in components:
        if n >= max_components:
            lines.append(f"...and {len(components)-max_components} more components (truncated)")
            break
        n += 1

        # Component name
        name = comp.get("name") or comp.get("display_name") or comp.get("component") or "Component"
        lines.append(f"- Component: {name}")

        # Schema fields: support multiple shapes
        comp_schema = comp.get("schema") or comp.get("fields") or {}
        if isinstance(comp_schema, dict):
            fields = list(comp_schema.keys())
        elif isinstance(comp_schema, list):
            # some exports use list of fields
            try:
                fields = [f.get("name", f.get("field", "unnamed")) for f in comp_schema]
            except Exception:
                fields = []
        else:
            fields = []

        if not fields:
            lines.append("    fields: (none detected)")
        else:
            # Show up to max_fields fields with their types if available
            shown = 0
            field_strs = []
            for field in fields:
                if shown >= max_fields:
                    break
                # try to get type info if present in schema dict
                ftype = None
                if isinstance(comp_schema, dict) and field in comp_schema and isinstance(comp_schema[field], dict):
                    ftype = comp_schema[field].get("type") or comp_schema[field].get("field_type")
                if ftype:
                    field_strs.append(f"{field}:{ftype}")
                else:
                    field_strs.append(f"{field}")
                shown += 1
            if len(fields) > max_fields:
                field_strs.append(f"...(+{len(fields)-max_fields} more)")
            lines.append("    fields: " + ", ".join(field_strs))

        # Keep output length bounded
        if sum(len(l) for l in lines) > max_chars:
            lines.append("...schema summary truncated due to length")
            break

    return "\n".join(lines)


def build_system_prompt() -> str:
    return (
        "You are an expert code generator. Generate a React + TypeScript + Tailwind project scaffold that "
        "maps Storyblok components to React components. Output MUST be valid JSON exactly as described:"
        '{"project_name": "...", "files": [{"path":"...","content":"..."}], "warnings":[...]}'
    )


def build_user_prompt(user_answers: Dict[str, Any], schema: Dict[str, Any], options: Dict[str, Any]) -> str:
    # Keep schema concise but include key parts (components with names + fields)
    schema_summary = summarize_schema(schema)
    # Add a JSON-encoded small user answers block for clarity
    try:
        user_json = json.dumps(user_answers, indent=2)
    except Exception:
        user_json = str(user_answers)
    opt_json = json.dumps(options or {}, indent=2)
    prompt = (
        f"User requirements:\n{user_json}\n\n"
        f"Storyblok schema summary:\n{schema_summary}\n\n"
        f"Options:\n{opt_json}\n\n"
        "Constraints and instructions:\n"
        "1) Produce a JSON object with keys: project_name, files (list of {path, content}), metadata (optional).\n"
        "2) Files.content must be string-escaped source code (TSX, JS, JSON, config files).\n"
        "3) Use TypeScript for React components if options.typescript is true.\n"
        "4) Map each Storyblok component to a React component that accepts a 'blok' prop.\n"
        "5) Provide a Tailwind setup, a storyblok service file for fetching content, and at least a Home page if include_pages is true.\n"
        "6) If you cannot generate all files due to size, still return whatever you can in the files array and mention warnings in metadata.\n\n"
        "Respond only with valid JSON (no extra commentary)."
    )
    return prompt
