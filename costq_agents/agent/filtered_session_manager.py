"""过滤工具返回结果的 SessionManager。

在消息持久化到 AgentCore Memory 之前，清除 toolResult 的返回内容，
仅保留 toolUseId 和 status 以便追踪。
运行时上下文（agent.messages）不受影响。
"""

import copy
import json
import logging
from typing import TYPE_CHECKING, Any

from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from strands.types.content import Message
from typing_extensions import override

if TYPE_CHECKING:
    from strands.agent.agent import Agent

logger = logging.getLogger(__name__)

# toolResult 返回内容占位文本（语言无关）
TOOL_RESULT_STRIPPED = "TOOL_RESULT_STRIPPED"


class FilteredMemorySessionManager(AgentCoreMemorySessionManager):
    """在持久化前清除工具返回结果的 SessionManager。"""

    @override
    def append_message(self, message: Message, agent: "Agent", **kwargs: Any) -> None:
        """追加消息到会话，持久化前清除工具返回结果。"""
        filtered = self._strip_tool_results(message)
        super().append_message(filtered, agent, **kwargs)

    @staticmethod
    def _strip_tool_results(message: Message) -> Message:
        """清除消息中 toolResult 的返回内容。

        如果消息不包含 toolResult，直接返回原始 message（零开销）。
        遇到非法结构时静默跳过，不抛异常。
        """
        content = message.get("content", [])
        if not content:
            return message

        has_tool_result = any(
            isinstance(block, dict) and "toolResult" in block
            for block in content
        )
        if not has_tool_result:
            return message

        filtered = copy.deepcopy(message)
        for block in filtered.get("content", []):
            if not isinstance(block, dict) or "toolResult" not in block:
                continue

            tool_result = block.get("toolResult")
            if not isinstance(tool_result, dict):
                continue

            tool_use_id = tool_result.get("toolUseId", "unknown")

            original_content = tool_result.get("content", [])
            try:
                original_size = len(json.dumps(original_content, ensure_ascii=False))
            except (TypeError, ValueError):
                original_size = -1

            # 每次构造新 list，避免可变对象共享
            tool_result["content"] = [{"text": TOOL_RESULT_STRIPPED}]

            logger.debug(
                "清除 toolResult 返回内容",
                extra={
                    "tool_use_id": tool_use_id,
                    "original_size": original_size,
                    "status": tool_result.get("status", "unknown"),
                },
            )

        return filtered
