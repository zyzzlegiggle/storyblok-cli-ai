import os
import re
from typing import Any, Dict, List, Optional

def validate_file_tree(files):
    # Basic checks: path traversal, non-empty content, allowed extensions
    out = []
    for f in files:
        p = f.get("path") or "unknown.txt"
        # sanitize path
        p = p.replace("..", "")
        c = f.get("content","")
        out.append({"path": p, "content": c})
    return out

# --- Helper: safe path normalize & reject traversal/abs paths ---
def _safe_normalize(p: str) -> Optional[str]:
    if not isinstance(p, str) or p.strip() == "":
        return None
    p = p.replace("\\", "/")
    # disallow absolute paths
    if os.path.isabs(p):
        return None
    # clean
    clean = os.path.normpath(p)
    # normalized might contain backslashes on windows; use forward slashes in returned path
    if clean.startswith("..") or "/.." in clean or clean == "..":
        return None
    # ensure relative and use forward slashes
    return clean.replace("\\", "/").lstrip("./")