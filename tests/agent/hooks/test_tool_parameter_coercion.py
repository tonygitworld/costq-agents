"""测试 ToolParameterCoercionHook

测试用例覆盖：
1. 基本类型转换（string → array/integer/number/boolean/object）
2. 边界情况（空字符串、已正确类型、转换失败）
3. 数组元素类型转换
4. Hook 集成测试
"""

import pytest

from costq_agents.agent.hooks.tool_parameter_coercion import ToolParameterCoercionHook


@pytest.fixture
def hook():
    """创建 Hook 实例"""
    return ToolParameterCoercionHook()


class TestStringToArray:
    """测试字符串转数组"""

    def test_comma_separated_string(self, hook):
        """TC-001: 逗号分隔字符串转数组"""
        value, changed = hook._convert_value("a, b, c", "array", "test_param", {})
        assert changed is True
        assert value == ["a", "b", "c"]

    def test_json_array_string(self, hook):
        """TC-002: JSON 数组字符串转数组"""
        value, changed = hook._convert_value('["a", "b"]', "array", "test_param", {})
        assert changed is True
        assert value == ["a", "b"]

    def test_single_value_string(self, hook):
        """TC-003: 单值字符串转数组"""
        value, changed = hook._convert_value("single", "array", "test_param", {})
        assert changed is True
        assert value == ["single"]

    def test_empty_string(self, hook):
        """TC-004: 空字符串转数组"""
        value, changed = hook._convert_value("", "array", "test_param", {})
        assert changed is True
        assert value == []

    def test_array_element_type_conversion_integer(self, hook):
        """TC-005: 数组元素类型转换（整数）"""
        schema = {"items": {"type": "integer"}}
        value, changed = hook._convert_value("1, 2, 3", "array", "test_param", schema)
        assert changed is True
        assert value == [1, 2, 3]

    def test_array_element_type_conversion_number(self, hook):
        """TC-005b: 数组元素类型转换（浮点数）"""
        schema = {"items": {"type": "number"}}
        value, changed = hook._convert_value("1.1, 2.2", "array", "test_param", schema)
        assert changed is True
        assert value == [1.1, 2.2]


class TestStringToInteger:
    """测试字符串转整数"""

    def test_string_to_integer(self, hook):
        """TC-006: 字符串转整数"""
        value, changed = hook._convert_value("123", "integer", "test_param", {})
        assert changed is True
        assert value == 123


class TestStringToNumber:
    """测试字符串转浮点数"""

    def test_string_to_number(self, hook):
        """TC-007: 字符串转浮点数"""
        value, changed = hook._convert_value("45.67", "number", "test_param", {})
        assert changed is True
        assert value == 45.67


class TestStringToBoolean:
    """测试字符串转布尔值"""

    def test_string_to_boolean_true(self, hook):
        """TC-008: 字符串转布尔值（true）"""
        test_cases = ["true", "True", "TRUE", "1", "yes", "YES", "on", "ON"]
        for test_value in test_cases:
            value, changed = hook._convert_value(test_value, "boolean", "test_param", {})
            assert changed is True
            assert value is True, f"Failed for: {test_value}"

    def test_string_to_boolean_false(self, hook):
        """TC-008b: 字符串转布尔值（false）"""
        test_cases = ["false", "False", "FALSE", "0", "no", "NO", "off", "OFF"]
        for test_value in test_cases:
            value, changed = hook._convert_value(test_value, "boolean", "test_param", {})
            assert changed is True
            assert value is False, f"Failed for: {test_value}"


class TestStringToObject:
    """测试字符串转对象"""

    def test_string_to_object(self, hook):
        """TC-009: JSON 字符串转对象"""
        value, changed = hook._convert_value('{"key": "value"}', "object", "test_param", {})
        assert changed is True
        assert value == {"key": "value"}


class TestTypeMatching:
    """测试类型匹配检测"""

    def test_already_correct_type_array(self, hook):
        """TC-010: 已是正确类型（数组）不转换"""
        value, changed = hook._convert_value(["a", "b"], "array", "test_param", {})
        assert changed is False
        assert value == ["a", "b"]

    def test_already_correct_type_integer(self, hook):
        """TC-010b: 已是正确类型（整数）不转换"""
        value, changed = hook._convert_value(123, "integer", "test_param", {})
        assert changed is False
        assert value == 123

    def test_already_correct_type_string(self, hook):
        """TC-010c: 已是正确类型（字符串）不转换"""
        value, changed = hook._convert_value("hello", "string", "test_param", {})
        assert changed is False
        assert value == "hello"


class TestConversionFailure:
    """测试转换失败"""

    def test_invalid_integer_conversion(self, hook):
        """TC-011: 转换失败保留原值（无效整数）"""
        value, changed = hook._convert_value("abc", "integer", "test_param", {})
        assert changed is False
        assert value == "abc"

    def test_invalid_json_object(self, hook):
        """TC-011b: 转换失败保留原值（无效 JSON）"""
        value, changed = hook._convert_value("{invalid json", "object", "test_param", {})
        assert changed is False
        assert value == "{invalid json"


class TestCompleteWorkflow:
    """测试完整工作流程"""

    def test_multiple_parameters_conversion(self, hook):
        """测试多个参数同时转换"""
        # 模拟 tool_spec
        properties = {
            "data_type": {"type": "array"},
            "max_results": {"type": "integer"},
            "threshold": {"type": "number"},
            "include_forecast": {"type": "boolean"},
            "filters": {"type": "object"},
        }

        # 模拟 LLM 生成的参数（全部是字符串）
        tool_input = {
            "data_type": "ATTR1, ATTR2",
            "max_results": "100",
            "threshold": "0.15",
            "include_forecast": "true",
            "filters": '{"service": "EC2"}',
        }

        # 逐个转换
        for param_name, param_schema in properties.items():
            expected_type = param_schema["type"]
            original_value = tool_input[param_name]

            new_value, changed = hook._convert_value(
                original_value, expected_type, param_name, param_schema
            )

            assert changed is True
            tool_input[param_name] = new_value

        # 验证转换结果
        assert tool_input["data_type"] == ["ATTR1", "ATTR2"]
        assert tool_input["max_results"] == 100
        assert tool_input["threshold"] == 0.15
        assert tool_input["include_forecast"] is True
        assert tool_input["filters"] == {"service": "EC2"}


class TestTypeMatchesHelper:
    """测试 _type_matches 辅助方法"""

    def test_type_matches_string(self, hook):
        """测试字符串类型匹配"""
        assert hook._type_matches("hello", "string") is True
        assert hook._type_matches(123, "string") is False

    def test_type_matches_integer(self, hook):
        """测试整数类型匹配"""
        assert hook._type_matches(123, "integer") is True
        assert hook._type_matches("123", "integer") is False

    def test_type_matches_number(self, hook):
        """测试数字类型匹配（包括整数和浮点数）"""
        assert hook._type_matches(123, "number") is True
        assert hook._type_matches(45.67, "number") is True
        assert hook._type_matches("123", "number") is False

    def test_type_matches_boolean(self, hook):
        """测试布尔类型匹配"""
        assert hook._type_matches(True, "boolean") is True
        assert hook._type_matches(False, "boolean") is True
        assert hook._type_matches("true", "boolean") is False

    def test_type_matches_array(self, hook):
        """测试数组类型匹配"""
        assert hook._type_matches([], "array") is True
        assert hook._type_matches([1, 2], "array") is True
        assert hook._type_matches("[]", "array") is False

    def test_type_matches_object(self, hook):
        """测试对象类型匹配"""
        assert hook._type_matches({}, "object") is True
        assert hook._type_matches({"key": "value"}, "object") is True
        assert hook._type_matches("{}", "object") is False


class TestStringToBooleanHelper:
    """测试 _string_to_boolean 辅助方法"""

    def test_true_values(self, hook):
        """测试真值字符串"""
        true_values = ["true", "True", "TRUE", "1", "yes", "Yes", "YES", "on", "On", "ON"]
        for value in true_values:
            assert hook._string_to_boolean(value) is True, f"Failed for: {value}"

    def test_false_values(self, hook):
        """测试假值字符串"""
        false_values = ["false", "False", "FALSE", "0", "no", "No", "NO", "off", "Off", "OFF", ""]
        for value in false_values:
            assert hook._string_to_boolean(value) is False, f"Failed for: {value}"


class TestStringToArrayHelper:
    """测试 _string_to_array 辅助方法"""

    def test_json_array_format(self, hook):
        """测试 JSON 数组格式"""
        result = hook._string_to_array('["a", "b", "c"]', {})
        assert result == ["a", "b", "c"]

    def test_comma_separated_format(self, hook):
        """测试逗号分隔格式"""
        result = hook._string_to_array("a, b, c", {})
        assert result == ["a", "b", "c"]

    def test_single_value_format(self, hook):
        """测试单值格式"""
        result = hook._string_to_array("single", {})
        assert result == ["single"]

    def test_empty_string_format(self, hook):
        """测试空字符串格式"""
        result = hook._string_to_array("", {})
        assert result == []

    def test_with_items_type_integer(self, hook):
        """测试带元素类型转换（整数）"""
        schema = {"items": {"type": "integer"}}
        result = hook._string_to_array("1, 2, 3", schema)
        assert result == [1, 2, 3]

    def test_with_items_type_number(self, hook):
        """测试带元素类型转换（浮点数）"""
        schema = {"items": {"type": "number"}}
        result = hook._string_to_array("1.1, 2.2, 3.3", schema)
        assert result == [1.1, 2.2, 3.3]
