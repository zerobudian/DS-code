# -*- coding: utf-8 -*-
"""Tool descriptions data — loaded from JSON for maintainability.

The tool description schema is pure data (name, description, fields, returns),
so it lives in ``tool_descriptions.json`` and is loaded once at import time.
This keeps the Python module small and focused on the formatter logic.
"""
import json
import os
from typing import Dict, List, Any, Optional

_MAX_TOOL_DESC = 400        # max length of a single tool description

_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_descriptions.json")


def _clean_text(raw: Optional[str], max_len: int) -> str:
    """Strip non-printable characters and truncate by length."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        try:
            raw = str(raw)
        except Exception:
            return ""
    out: List[str] = []
    for ch in raw:
        o = ord(ch)
        if o == 0:
            continue
        if o < 32 and ch not in ("\n", "\r", "\t"):
            continue
        if o == 0x7f:
            continue
        out.append(ch)
    s = "".join(out)
    data = s.encode("utf-8", errors="replace")
    if len(data) > max_len:
        data = data[:max_len]
        s = data.decode("utf-8", errors="ignore")
    return s


def _load_tool_descriptions() -> List[Dict[str, Any]]:
    """Load tool descriptions from the JSON data file."""
    with open(_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# Load once at import time
TOOL_DESCRIPTIONS: List[Dict[str, Any]] = _load_tool_descriptions()

# Build a fast lookup index keyed by tool name
TOOL_DESCRIPTIONS_BY_NAME: Dict[str, Dict[str, Any]] = {
    t["name"]: t for t in TOOL_DESCRIPTIONS
}


def _format_tool_descriptions() -> str:
    """Format TOOL_DESCRIPTIONS into a single block for the system prompt."""
    lines: List[str] = []
    lines.append("----- BEGIN TOOL DESCRIPTIONS -----")
    lines.append("You may invoke the following tools via JSON tool call (multiple in parallel in one turn):")
    for t in TOOL_DESCRIPTIONS:
        name = t["name"]
        desc = _clean_text(t["description"], _MAX_TOOL_DESC)
        field_lines = []
        for f in t.get("fields", []):
            key = f.get("key", "")
            typ = f.get("type", "string")
            req = "required" if f.get("required") else "optional"
            example = f.get("example", "")
            note = f.get("note", "")
            field_lines.append(
                f"    - {key} ({typ}, {req}) example: {example}"
                + (f"  note: {note}" if note else "")
            )
        lines.append(f"- [{name}] {desc}")
        if field_lines:
            lines.append("  params:")
            lines.extend(field_lines)
        lines.append(f"  returns: {_clean_text(t.get('returns',''), 300)}")
        has_fields = bool(t.get("fields"))
        tool_sample = '{"tool": "%s"%s}' % (name, ', "arg": "..."' if has_fields else '')
        lines.append(f"  call example: {tool_sample}")
        lines.append("")
    lines.append("----- END TOOL DESCRIPTIONS -----")
    return "\n".join(lines)
