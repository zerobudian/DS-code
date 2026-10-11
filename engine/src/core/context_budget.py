"""Build bounded requests without splitting native tool exchanges or changing history."""

import json


def estimated_tokens(value):
    # A conservative heuristic, not a provider tokenizer. Images have a separate allowance.
    if isinstance(value, list):
        return sum(4096 if isinstance(part, dict) and part.get("type") == "image_url"
                   else estimated_tokens(part) for part in value)
    if isinstance(value, dict):
        if value.get("type") == "image_url": return 4096
        return 4 + sum(estimated_tokens(k) + estimated_tokens(v) for k, v in value.items())
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return max(1, (len(text.encode("utf-8")) + 1) // 2)


def complete_tool_exchanges(messages):
    result, pending = [], {}
    def finish_pending():
        for call_id in pending:
            result.append({"role": "tool", "tool_call_id": call_id,
                           "content": "工具调用未完成或已取消；没有可用执行结果。"})
        pending.clear()
    for source in messages:
        message = dict(source)
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id in pending:
                pending.pop(call_id)
            else:
                message = {"role": "user", "content": "[历史工具结果]\n" + str(message.get("content", ""))}
                finish_pending()
        else:
            finish_pending()
            for call in message.get("tool_calls", []):
                pending[call["id"]] = True
        result.append(message)
    finish_pending()
    return result


def fit_context(messages, limit, tools=None):
    prepared = complete_tool_exchanges(messages)
    prefix = prepared[:1] if prepared and prepared[0].get("role") == "system" else []
    groups = []
    for message in prepared[len(prefix):]:
        if not groups or message.get("role") == "user":
            groups.append([])
        groups[-1].append(message)
    reserve = min(4096, max(128, limit // 8))
    available = limit - reserve - estimated_tokens(tools or []) - estimated_tokens(prefix)
    if not groups:
        if available < 0: raise ValueError("系统提示与工具描述超过上下文预算")
        return prepared, 0
    costs = [estimated_tokens(group) for group in groups]
    if costs[-1] > available:
        raise ValueError("当前消息、系统提示与工具描述超过上下文预算；请减少附件或提高 context_tokens")
    selected = len(groups) - 1
    remaining = available - costs[-1]
    while selected > 0 and costs[selected - 1] <= remaining:
        selected -= 1
        remaining -= costs[selected]
    return prefix + [m for group in groups[selected:] for m in group], sum(len(g) for g in groups[:selected])
