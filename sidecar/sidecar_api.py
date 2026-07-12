# -*- coding: utf-8 -*-
"""DScode V2 Sidecar — FastAPI 路由 (SSE 流式对话 + 配置 + 项目管理)。"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

SIDECAR_DIR = Path(__file__).resolve().parent
DSCODE_V2_ROOT = SIDECAR_DIR.parent
_OLD_DSC_ROOT = DSCODE_V2_ROOT.parent / "dscode"
for _p in (DSCODE_V2_ROOT, _OLD_DSC_ROOT):
    _ps = str(_p)
    if _ps not in sys.path and _p.exists():
        sys.path.insert(0, _ps)

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

from sidecar.sidecar_engine import get_engine, get_sse_events
from src.core.config import Config

logger = logging.getLogger("dscode-sidecar")

app = FastAPI(title="DScode Sidecar", version="2.0.1-0712-NEXT")

# 允许 Electron renderer 跨域（localhost:5173 for dev, file:// for prod）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 健康检查 ─────────────────────────────────────────
@app.get("/api/health")
async def health():
    cfg = Config()
    return {
        "status": "ok",
        "model": cfg.model,
        "onboarding_completed": cfg.data.get("onboarding_completed", False),
        "version": "2.0.1-0712-NEXT",
    }


# ── 配置 ─────────────────────────────────────────────
@app.get("/api/config")
async def get_config():
    cfg = Config()
    return {
        "model": cfg.model,
        "base_url": cfg.base_url,
        "reasoning_effort": cfg.reasoning_effort,
        "permission_mode": cfg.permission_mode,
        "chat_mode": cfg.data.get("chat_mode", "normal"),
        "language": cfg.language,
        "theme": cfg.data.get("theme", "light"),
        "nickname": cfg.nickname,
        "balance_cny": cfg.data.get("balance_cny", 0.0),
        "cache_hit_rate": cfg.data.get("cache_hit_rate", 0.0),
        "total_tokens_used": cfg.data.get("total_tokens_used", 0),
        "available_models": cfg.get_available_models() or [
            {"name": "deepseek-v4-pro", "label": "V4 Pro", "primary": True},
            {"name": "deepseek-v4-flash", "label": "V4 Flash", "primary": False},
        ],
        "api_key_exists": bool(cfg.api_key),
    }


@app.put("/api/config")
async def update_config(request: Request):
    """更新配置项。只更新请求中包含的字段。"""
    cfg = Config()
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON object")

    updatable = {"model", "reasoning_effort", "permission_mode", "chat_mode",
                  "language", "theme", "nickname", "base_url", "api_key",
                  "onboarding_completed"}
    for key, val in body.items():
        if key not in updatable:
            continue
        if key == "model":
            cfg.model = str(val)
        elif key == "reasoning_effort":
            cfg.data["reasoning_effort"] = str(val)
        elif key == "permission_mode":
            cfg.permission_mode = str(val)
        elif key == "chat_mode":
            cfg.data["chat_mode"] = str(val)
        elif key == "language":
            cfg.data["language"] = str(val)
        elif key == "theme":
            cfg.data["theme"] = str(val)
        elif key == "nickname":
            cfg.nickname = str(val)
        elif key == "base_url":
            cfg.base_url = str(val)
        elif key == "api_key":
            cfg.api_key = str(val)
        elif key == "onboarding_completed":
            cfg.data["onboarding_completed"] = bool(val)

    cfg.save()
    # 通知引擎刷新客户端
    eng = get_engine()
    try:
        eng._init_engine()
    except Exception:
        pass

    return await get_config()


# ── 项目管理 ─────────────────────────────────────────
@app.get("/api/projects")
async def list_projects():
    eng = get_engine()
    names = eng.projects.list_projects()
    result = []
    for name in names:
        try:
            sb = eng.projects.get(name)
            if sb:
                result.append({
                    "name": name,
                    "display_name": sb.get_display_name(),
                    "created_at": sb.created_at if hasattr(sb, "created_at") else "",
                })
        except Exception:
            result.append({"name": name, "display_name": name})
    return {"projects": result}


@app.post("/api/projects")
async def create_project(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    display_name = (body.get("display_name") or name).strip()
    if not name:
        raise HTTPException(400, "项目名不能为空")
    eng = get_engine()
    sb = eng.projects.create(name, display_name=display_name)
    if sb is None:
        raise HTTPException(500, "创建项目失败")
    return {"name": name, "display_name": display_name}


@app.delete("/api/projects/{name}")
async def delete_project(name: str):
    eng = get_engine()
    eng.projects.delete(name)
    return {"deleted": name}


# ── 余额查询 ─────────────────────────────────────────
@app.get("/api/balance")
async def get_balance():
    eng = get_engine()
    try:
        info = eng.engine.api.check_balance()
        return info
    except Exception as e:
        return {"available_balance": 0, "ok": False, "error": str(e)}


# ── 流式对话 (SSE) ───────────────────────────────────
async def _sse_generator(project: str, text: str):
    """SSE 事件生成器 — 从引擎事件队列轮询并写入 SSE 格式。

    引擎在后台线程运行，通过 queue.Queue 推送事件；
    本协程在主事件循环中非阻塞轮询，转写为 SSE。
    """
    eng = get_engine()

    # 启动引擎发送
    eng.send_message(text, project=project)

    done = False
    deadline = time.monotonic() + 300  # 5 分钟超时
    poll_interval = 0.02  # 20ms 轮询

    while not done:
        if time.monotonic() > deadline:
            yield f"event: error\ndata: {json.dumps({'code': 504, 'message': '请求超时'})}\n\n"
            break

        event = get_sse_events()
        if event is None:
            await asyncio.sleep(poll_interval)
            continue

        etype = event.get("type", "")
        data = event.get("data", "")

        if etype == "done":
            done = True
            yield f"event: done\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        elif etype == "error":
            yield f"event: error\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            done = True  # error 也终止
        elif etype == "confirm_command":
            yield f"event: confirm_command\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
        elif isinstance(data, str):
            safe = data.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "")
            yield f"event: {etype}\ndata: {safe}\n\n"
        else:
            yield f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@app.get("/api/chat/stream")
async def chat_stream(project: str = Query("demo"), text: str = Query(...)):
    """SSE 流式对话端点。

    用法:
      GET /api/chat/stream?project=demo&text=你好

    事件类型:
      new_turn  → 开始新轮次，前端新建 AI 气泡
      content   → 流式文本增量，追加到当前 AI 气泡
      reasoning → 思考链增量，追加到推理区域
      status    → 引擎状态文本
      tool_call → 工具调用开始
      tool_result → 工具执行结果
      confirm_command → 需要用户确认的工具
      plan_ready → 计划生成完毕
      done      → 流结束，包含 cost/tokens 信息
      error     → 错误
    """
    if not text:
        raise HTTPException(400, "text 参数不能为空")

    return StreamingResponse(
        _sse_generator(project, text),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat/stop")
async def chat_stop():
    """停止当前对话。"""
    get_engine().stop()
    return {"stopped": True}


# ── 启动 ─────────────────────────────────────────────
def main(host: str = "127.0.0.1", port: int = 19876):
    print(f"[DScode Sidecar] 启动中... http://{host}:{port}")
    print(f"[DScode Sidecar] SS 端点: http://{host}:{port}/api/chat/stream?project=demo&text=hello")
    uvicorn.run(
        "sidecar.sidecar_api:app",
        host=host,
        port=port,
        log_level="warning",
        reload=False,
        lifespan="off",
    )
