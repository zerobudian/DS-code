"""Parse complete JSON tool objects without guessing parameters from malformed output."""

import json


def extract_calls(text, names, max_chars=1024 * 1024, max_calls=50):
    if not isinstance(text, str):
        return []
    text, calls, index = text[:max_chars], [], 0
    while len(calls) < max_calls:
        start = text.find("{", index)
        if start < 0:
            break
        depth, quoted, escaped, end = 0, False, False, None
        for position in range(start, len(text)):
            char = text[position]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = position + 1
                    break
        if end is None:
            break
        raw = text[start:end]
        try:
            value = json.loads(raw, strict=False)
        except (ValueError, RecursionError):
            value = None
        if isinstance(value, dict) and isinstance(value.get("tool"), str) and value["tool"] in names:
            calls.append(value)
        index = end
    return calls
