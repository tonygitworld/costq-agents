import copy

from costq_agents.agent.filtered_session_manager import FilteredMemorySessionManager


def test_strip_tool_results_replaces_content_and_keeps_ids_status():
    original = {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": "abc-123",
                    "status": "success",
                    "content": [
                        {"text": "very large text"},
                        {"json": {"k": "v"}},
                        {"image": {"format": "png", "source": "..."}},
                    ],
                }
            }
        ],
    }
    snapshot = copy.deepcopy(original)

    filtered = FilteredMemorySessionManager._strip_tool_results(original)

    assert filtered is not original
    assert filtered["content"][0]["toolResult"]["toolUseId"] == "abc-123"
    assert filtered["content"][0]["toolResult"]["status"] == "success"
    assert filtered["content"][0]["toolResult"]["content"] == [
        {"text": "TOOL_RESULT_STRIPPED"}
    ]

    # 原始消息不应被修改
    assert original == snapshot


def test_strip_tool_results_returns_original_when_no_tool_result():
    original = {
        "role": "assistant",
        "content": [{"text": "hello"}],
    }

    filtered = FilteredMemorySessionManager._strip_tool_results(original)

    assert filtered is original


def test_strip_tool_results_skips_invalid_tool_result_structure():
    original = {
        "role": "user",
        "content": [
            {"toolResult": "invalid"},
            {"toolResult": {"toolUseId": "x", "status": "error", "content": []}},
        ],
    }

    filtered = FilteredMemorySessionManager._strip_tool_results(original)

    # 非法结构被跳过，合法结构被替换
    assert filtered["content"][0]["toolResult"] == "invalid"
    assert filtered["content"][1]["toolResult"]["content"] == [
        {"text": "TOOL_RESULT_STRIPPED"}
    ]
