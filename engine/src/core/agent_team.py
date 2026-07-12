# -*- coding: utf-8 -*-
"""Agent Team 预分析引擎。

在主模型处理用户消息之前，依次运行 3 个 DeepSeek-v4-flash 轻量代理（非思考模式），
每个代理从前一个代理的结果中获取上下文，形成链式分析。

执行顺序:
  1. 意图解析 — 分析用户核心诉求，拆解子任务
  2. 风险评估 — 基于意图分析结果，评估潜在风险和边界条件
  3. 方案建议 — 综合意图和风险，推荐实现路径

每个代理使用非思考模式（最低深度），快速给出分析。
"""
import logging
import threading
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 预分析使用的轻量模型
AGENT_TEAM_MODEL = "deepseek-v4-flash"
# 单个代理超时（秒）
AGENT_TIMEOUT = 30


class AgentTeamResult:
    """单个代理的分析结果。"""

    def __init__(self, agent_id: str, label: str):
        self.agent_id = agent_id
        self.label = label
        self.content: str = ""
        self.status: str = "pending"  # pending / running / done / error / cancelled
        self.error: str = ""
        self.start_time: float = 0.0
        self.end_time: float = 0.0

    @property
    def elapsed(self) -> float:
        if self.end_time > 0:
            return self.end_time - self.start_time
        if self.start_time > 0:
            return time.monotonic() - self.start_time
        return 0.0

    def to_dict(self) -> Dict:
        return {
            "agent_id": self.agent_id,
            "label": self.label,
            "content": self.content,
            "status": self.status,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
        }


# 三个代理的系统提示 — 按执行顺序排列
_AGENT_PROMPTS = [
    {
        "id": "intent",
        "label": "意图解析",
        "system": (
            "你是一个需求意图分析专家。用户会给你一段描述，请用简洁的语言分析:\n"
            "1. 用户的核心诉求是什么（1-2句话概括）\n"
            "2. 拆解出具体要完成的子任务（用要点列出）\n"
            "3. 用户可能没有明说但隐含的期望\n"
            "请保持简洁，总共不超过 200 字。不要写代码。"
        ),
    },
    {
        "id": "risk",
        "label": "风险评估",
        "system": (
            "你是一个风险评估专家。你会收到用户的原始描述以及意图分析专家的分析结果。\n"
            "请基于这些信息分析:\n"
            "1. 可能遇到的技术风险或难点\n"
            "2. 需要注意的边界条件和异常场景\n"
            "3. 外部依赖或前置条件\n"
            "请保持简洁，总共不超过 200 字。不要写代码。"
        ),
    },
    {
        "id": "suggest",
        "label": "方案建议",
        "system": (
            "你是一个方案建议专家。你会收到用户的原始描述、意图分析专家和风险评估专家的分析结果。\n"
            "请综合这些信息建议:\n"
            "1. 推荐的实现路径（最佳实践）\n"
            "2. 需要特别注意的关键步骤\n"
            "3. 可能的优化方向\n"
            "请保持简洁，总共不超过 200 字。不要写代码。"
        ),
    },
]


class AgentTeam:
    """Agent Team 预分析引擎 — 顺序执行 3 个轻量代理，链式传递上下文。

    执行流程:
      intent → risk → suggest
    每个代理的 messages 中包含前序所有代理的分析结果。
    """

    def __init__(self):
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._results: Dict[str, AgentTeamResult] = {}

        # 回调 — 由 GUI/Controller 设置
        self.on_agent_start: Optional[Callable[[str, str], None]] = None  # (agent_id, label)
        self.on_agent_delta: Optional[Callable[[str, str], None]] = None  # (agent_id, delta_text)
        self.on_agent_done: Optional[Callable[[str, str, str], None]] = None  # (agent_id, status, content)
        self.on_all_done: Optional[Callable[[List[Dict]], None]] = None  # (results_list)

    def stop(self):
        """取消所有代理。"""
        self._stop_event.set()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def _run_single_agent(
        self,
        agent_id: str,
        label: str,
        system_prompt: str,
        messages: List[Dict],
        api_instance,
        timeout: float = AGENT_TIMEOUT,
    ) -> AgentTeamResult:
        """运行单个代理（顺序执行中的一个步骤），带超时保护。"""
        result = AgentTeamResult(agent_id, label)
        result.status = "running"
        result.start_time = time.monotonic()

        if self.on_agent_start:
            try:
                self.on_agent_start(agent_id, label)
            except Exception as _exc:
                logger.debug("on_agent_start 回调异常: %s", _exc, exc_info=True)

        try:
            def _on_text(delta: str):
                if self._stop_event.is_set():
                    return
                result.content += delta
                if self.on_agent_delta:
                    try:
                        self.on_agent_delta(agent_id, delta)
                    except Exception as _exc:
                        logger.debug("on_agent_delta 回调异常: %s", _exc, exc_info=True)

            def _on_cancel():
                return self._stop_event.is_set()

            # 使用线程+超时机制防止单个代理无限挂起
            _err: List[Exception] = []
            _done = threading.Event()

            def _run():
                try:
                    # 调用轻量模型 — 非思考模式（最低深度，最快响应）
                    api_instance.chat_stream(
                        messages,
                        model=AGENT_TEAM_MODEL,
                        on_text=_on_text,
                        on_reasoning=None,
                        on_cancel_check=_on_cancel,
                        enable_thinking=False,
                    )
                except Exception as _e:
                    _err.append(_e)
                finally:
                    _done.set()

            _t = threading.Thread(target=_run, daemon=True)
            _t.start()
            _done.wait(timeout=timeout)

            if not _done.is_set():
                # 超时：设置停止事件让流式调用自行取消
                self._stop_event.set()
                _done.wait(timeout=2.0)
                result.status = "cancelled"
                result.error = f"代理超时 ({timeout}s)"
            elif _err:
                raise _err[0]
            elif self._stop_event.is_set():
                result.status = "cancelled"
            else:
                result.status = "done"

        except Exception as e:
            if self._stop_event.is_set():
                result.status = "cancelled"
            else:
                result.status = "error"
                result.error = str(e)
            logger.warning("[AgentTeam] 代理 %s 失败: %s", agent_id, e, exc_info=True)

        result.end_time = time.monotonic()

        if self.on_agent_done:
            try:
                self.on_agent_done(agent_id, result.status, result.content)
            except Exception as _exc:
                logger.debug("on_agent_done 回调异常: %s", _exc, exc_info=True)

        return result

    def analyze(
        self,
        user_text: str,
        api_instance,
        timeout: float = AGENT_TIMEOUT,
    ) -> List[Dict]:
        """顺序运行 3 个代理分析用户需求，每个代理获取前序代理的上下文。

        Args:
            user_text: 用户输入的原始文本
            api_instance: DeepSeekAPI 实例（用于调用 chat_stream）
            timeout: 单个代理超时秒数

        Returns:
            3 个代理结果的列表 [{agent_id, label, content, status, error, elapsed}, ...]
        """
        self._stop_event.clear()
        self._results.clear()

        # 累积前序代理的分析结果，用于构建后续代理的上下文
        prior_context = ""

        for agent_config in _AGENT_PROMPTS:
            if self._stop_event.is_set():
                break

            agent_id = agent_config["id"]
            label = agent_config["label"]
            system_prompt = agent_config["system"]

            # 构建消息：system + 用户原始输入 + 前序分析上下文
            messages = [{"role": "system", "content": system_prompt}]

            if prior_context:
                # 将前序代理的分析结果作为上下文注入
                messages.append({
                    "role": "user",
                    "content": (
                        f"用户原始描述:\n{user_text}\n\n"
                        f"---\n前序代理分析结果:\n{prior_context}\n---\n"
                        f"请基于以上信息进行你的分析。"
                    ),
                })
            else:
                messages.append({"role": "user", "content": user_text})

            result = self._run_single_agent(
                agent_id, label, system_prompt, messages, api_instance, timeout=timeout
            )

            with self._lock:
                self._results[agent_id] = result

            # 累积上下文供下一个代理使用
            if result.content and result.status == "done":
                prior_context += f"\n## {label}\n{result.content}\n"
            elif result.status in ("error", "cancelled"):
                # 出错或取消时也记录，但不阻塞后续代理
                prior_context += f"\n## {label}\n(分析未完成: {result.status})\n"

        # 按固定顺序输出结果
        ordered = []
        for agent_config in _AGENT_PROMPTS:
            agent_id = agent_config["id"]
            r = self._results.get(agent_id)
            if r:
                ordered.append(r.to_dict())
            else:
                ordered.append({
                    "agent_id": agent_id,
                    "label": agent_config["label"],
                    "content": "",
                    "status": "skipped",
                    "error": "未执行",
                    "elapsed": 0,
                })

        if self.on_all_done:
            try:
                self.on_all_done(ordered)
            except Exception as _exc:
                logger.debug("on_all_done 回调异常: %s", _exc, exc_info=True)

        return ordered

    @staticmethod
    def format_analysis_for_context(results: List[Dict]) -> str:
        """将预分析结果格式化为可注入主模型上下文的文本。"""
        lines = ["[Agent Team 预分析结果]", ""]
        for r in results:
            label = r.get("label", r.get("agent_id", "?"))
            content = r.get("content", "").strip()
            status = r.get("status", "")
            if content and status == "done":
                lines.append(f"## {label}")
                lines.append(content)
                lines.append("")
        if len(lines) <= 2:
            return ""
        return "\n".join(lines)
