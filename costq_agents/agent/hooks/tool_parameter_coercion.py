"""基于 Schema 的工具参数类型自动转换 Hook

核心原理：
- 从 tool_spec.inputSchema 读取每个参数的期望类型
- 自动将 LLM 生成的错误类型参数转换为正确类型
- 无需穷举参数，完全基于 JSON Schema 动态处理

解决问题：
- Gateway MCP 严格 JSON Schema 验证导致的类型不匹配
- LLM 将数组序列化为逗号分隔字符串
- LLM 将数字序列化为字符串
- LLM 将布尔值序列化为字符串
- LLM 将对象序列化为 JSON 字符串

支持的类型转换：
- string → array（逗号分隔或 JSON 解析）
- string → integer
- string → number
- string → boolean
- string → object（JSON 解析）

参考文档：
- docs/20250128_参数转换问题/01_需求文档.md
- docs/20250128_参数转换问题/03_核心机制说明.md
- docs/20250128_参数转换问题/04_类型穷举说明.md
"""

import json
import logging
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent

logger = logging.getLogger(__name__)


class ToolParameterCoercionHook(HookProvider):
    """基于 Schema 的工具参数类型强制转换 Hook

    在工具调用前自动将 LLM 生成的错误类型参数转换为正确类型。
    完全基于 tool_spec.inputSchema 动态检测，无需维护参数清单。

    工作原理：
    1. 监听 BeforeToolCallEvent（工具调用前）
    2. 从 tool.tool_spec.inputSchema 读取参数类型定义
    3. 遍历 tool_use.input 中的每个参数
    4. 对比实际类型和期望类型
    5. 执行类型转换（基于 JSON Schema 类型）

    支持的转换：
    - string → array（逗号分隔或 JSON 解析）
    - string → integer
    - string → number
    - string → boolean
    - string → object（JSON 解析）

    不转换的情况：
    - 参数已经是正确类型
    - 非 string 类型的错误（只处理 LLM 最常见的 string → other）
    - inputSchema 不存在或格式错误
    """

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """注册 Hook 回调到 BeforeToolCallEvent"""
        registry.add_callback(BeforeToolCallEvent, self._coerce_parameters)

    def _coerce_parameters(self, event: BeforeToolCallEvent) -> None:
        """在工具调用前基于 schema 转换参数类型

        Args:
            event: BeforeToolCallEvent 包含 selected_tool 和 tool_use
        """
        # 获取工具和参数
        tool = event.selected_tool
        tool_use = event.tool_use

        if tool is None:
            return

        tool_name = tool_use.get("name", "")
        tool_input = tool_use.get("input", {})

        if not isinstance(tool_input, dict):
            return

        # 获取 inputSchema
        try:
            tool_spec = tool.tool_spec
            input_schema = tool_spec.get("inputSchema", {})

            # inputSchema 可能在 "json" 键下（Bedrock 格式）
            if "json" in input_schema:
                input_schema = input_schema["json"]

            properties = input_schema.get("properties", {})
        except Exception as e:
            logger.debug(f"无法获取 {tool_name} 的 inputSchema: {e}")
            return

        if not properties:
            return

        # 遍历所有参数，检查类型并转换
        modified = False
        for param_name, param_schema in properties.items():
            if param_name not in tool_input:
                continue

            value = tool_input[param_name]
            expected_type = param_schema.get("type")

            if expected_type is None:
                continue

            # 执行类型转换
            new_value, was_converted = self._convert_value(
                value, expected_type, param_name, param_schema
            )

            if was_converted:
                tool_input[param_name] = new_value
                modified = True
                logger.info(
                    f"参数类型转换: {tool_name}.{param_name} "
                    f"({type(value).__name__} → {expected_type})",
                    extra={
                        "tool_name": tool_name,
                        "param_name": param_name,
                        "expected_type": expected_type,
                        "original_type": type(value).__name__,
                        "original_value": str(value)[:100],
                        "new_value": str(new_value)[:100],
                    },
                )

        if modified:
            logger.info(f"✅ 工具参数类型转换完成: {tool_name}")

    def _convert_value(
        self, value: Any, expected_type: str, param_name: str, param_schema: dict
    ) -> tuple[Any, bool]:
        """将值转换为期望的类型

        基于 JSON Schema 标准类型（7 种）执行转换。
        只处理 LLM 最常见的错误：string → other

        Args:
            value: 原始值
            expected_type: 期望的 JSON Schema 类型
            param_name: 参数名（用于日志）
            param_schema: 完整的参数 schema（可能包含 items 等）

        Returns:
            (转换后的值, 是否进行了转换)
        """
        # 如果类型已经匹配，不需要转换
        if self._type_matches(value, expected_type):
            return value, False

        # 只处理 string → other 的转换（LLM 最常见的错误）
        if not isinstance(value, str):
            return value, False

        # 基于 JSON Schema 类型执行转换
        try:
            if expected_type == "array":
                return self._string_to_array(value, param_schema), True

            elif expected_type == "integer":
                return int(value), True

            elif expected_type == "number":
                return float(value), True

            elif expected_type == "boolean":
                return self._string_to_boolean(value), True

            elif expected_type == "object":
                return json.loads(value), True

        except (ValueError, json.JSONDecodeError) as e:
            logger.warning(
                f"类型转换失败: {param_name}",
                extra={
                    "param_name": param_name,
                    "expected_type": expected_type,
                    "value": str(value)[:100],
                    "error": str(e),
                },
            )

        return value, False

    def _type_matches(self, value: Any, expected_type: str) -> bool:
        """检查值是否匹配期望的 JSON Schema 类型

        Args:
            value: 要检查的值
            expected_type: JSON Schema 类型（string, integer, number, boolean, array, object）

        Returns:
            True 如果类型匹配
        """
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }

        expected_python_type = type_map.get(expected_type)
        if expected_python_type is None:
            return True  # 未知类型，假设匹配

        return isinstance(value, expected_python_type)

    def _string_to_array(self, value: str, param_schema: dict) -> list:
        """将字符串转换为数组

        支持三种格式：
        1. JSON 数组: '["a", "b", "c"]'
        2. 逗号分隔: 'a, b, c'
        3. 单值: 'single' → ['single']

        如果 schema 定义了 items.type，会尝试转换每个元素。

        Args:
            value: 字符串值
            param_schema: 参数的完整 schema（可能包含 items）

        Returns:
            转换后的数组
        """
        value = value.strip()

        # 尝试 JSON 解析
        if value.startswith("["):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass

        # 空字符串返回空数组
        if not value:
            return []

        # 逗号分隔
        items = [item.strip() for item in value.split(",") if item.strip()]

        # 单值字符串
        if not items:
            items = [value]

        # 如果 schema 定义了 items 类型，尝试转换每个元素
        items_schema = param_schema.get("items", {})
        items_type = items_schema.get("type")

        if items_type == "integer":
            items = [int(item) for item in items]
        elif items_type == "number":
            items = [float(item) for item in items]

        return items

    def _string_to_boolean(self, value: str) -> bool:
        """将字符串转换为布尔值

        Args:
            value: 字符串值

        Returns:
            True 如果值为 "true", "1", "yes", "on"（不区分大小写）
            False 否则
        """
        return value.lower() in ("true", "1", "yes", "on")
