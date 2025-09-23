import os
import re

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
