"""事件流解析与总线测试（实时输出功能第一轮）。

需求：任务页面实时看到 Claude Code / hermes 的输出（逐事件流）。
- parse_claude_stream_line：把一行 claude `--output-format stream-json`
  输出解析为归一化事件列表（status/thinking/text/tool/tool_result/result）
- parse_hermes_event_line：把一行 hermes runner 流式事件解析为归一化事件
- EventBus：executor 线程发布 → SSE 订阅消费的进程内事件总线
"""

import json

import pytest

from botler.events import (
    EventBus, parse_claude_stream_line, parse_hermes_event_line,
)


# ---- claude stream-json 行构造 ----

def _system_init(session_id="sess-1", **extra):
    return json.dumps({
        "type": "system", "subtype": "init", "session_id": session_id,
        "cwd": "/work/demo", "model": "claude-fable-5",
        **extra,
    }, ensure_ascii=False)


def _assistant_line(*blocks):
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": list(blocks)},
    }, ensure_ascii=False)


def _user_tool_result(tool_use_id="tu1", text="ok", is_error=False):
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": text, "is_error": is_error},
        ]},
    }, ensure_ascii=False)


def _result_line(result="完成", **extra):
    return json.dumps({
        "type": "result", "subtype": "success", "result": result,
        "exit_code": 0, **extra,
    }, ensure_ascii=False)


class TestParseClaudeStreamLine:
    """parse_claude_stream_line：stream-json 行 → 归一化事件列表。"""

    def test_system_init_becomes_status(self):
        events = parse_claude_stream_line(_system_init())
        assert len(events) == 1
        assert events[0]["kind"] == "status"
        assert events[0]["session_id"] == "sess-1"
        assert events[0]["cwd"] == "/work/demo"

    def test_system_other_subtype_ignored(self):
        line = json.dumps({"type": "system", "subtype": "thinking_tokens"})
        assert parse_claude_stream_line(line) == []

    def test_assistant_text_block(self):
        events = parse_claude_stream_line(
            _assistant_line({"type": "text", "text": "我来修复"}))
        assert len(events) == 1
        assert events[0] == {"kind": "text", "text": "我来修复"}

    def test_assistant_thinking_block(self):
        events = parse_claude_stream_line(
            _assistant_line({"type": "thinking", "thinking": "先看代码"}))
        assert len(events) == 1
        assert events[0] == {"kind": "thinking", "text": "先看代码"}

    def test_assistant_tool_use_block(self):
        events = parse_claude_stream_line(
            _assistant_line({"type": "tool_use", "name": "Bash",
                             "input": {"command": "git status"}}))
        assert len(events) == 1
        assert events[0] == {"kind": "tool", "tool": "Bash",
                             "input": {"command": "git status"}}

    def test_assistant_multi_block_preserves_order(self):
        events = parse_claude_stream_line(_assistant_line(
            {"type": "text", "text": "开始"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
            {"type": "thinking", "thinking": "再看一眼"},
        ))
        assert [e["kind"] for e in events] == ["text", "tool", "thinking"]
        assert events[1]["tool"] == "Read"

    def test_assistant_empty_content_ignored(self):
        assert parse_claude_stream_line(_assistant_line()) == []

    def test_assistant_unknown_block_type_ignored(self):
        events = parse_claude_stream_line(
            _assistant_line({"type": "redacted_thinking", "data": "x"}))
        assert events == []

    def test_user_tool_result_text(self):
        events = parse_claude_stream_line(_user_tool_result(text="2 files"))
        assert len(events) == 1
        assert events[0] == {"kind": "tool_result", "tool_use_id": "tu1",
                             "text": "2 files", "is_error": False}

    def test_user_tool_result_error_flag(self):
        events = parse_claude_stream_line(
            _user_tool_result(text="permission denied", is_error=True))
        assert events[0]["is_error"] is True

    def test_user_tool_result_content_list_joined(self):
        """tool_result 的 content 可能是文本块列表。"""
        events = parse_claude_stream_line(_user_tool_result(text=[
            {"type": "text", "text": "第一段"},
            {"type": "text", "text": "第二段"},
        ]))
        assert events[0]["text"] == "第一段\n第二段"

    def test_user_without_tool_result_ignored(self):
        line = json.dumps({"type": "user",
                           "message": {"role": "user", "content": []}})
        assert parse_claude_stream_line(line) == []

    def test_result_line_becomes_result_event(self):
        events = parse_claude_stream_line(_result_line(result="开发完成"))
        assert len(events) == 1
        assert events[0]["kind"] == "result"
        assert events[0]["result"] == "开发完成"
        assert events[0]["subtype"] == "success"

    def test_empty_line_ignored(self):
        assert parse_claude_stream_line("") == []
        assert parse_claude_stream_line("   ") == []

    def test_invalid_json_ignored(self):
        assert parse_claude_stream_line("not json {") == []

    def test_unknown_type_ignored(self):
        line = json.dumps({"type": "mystery", "payload": 1})
        assert parse_claude_stream_line(line) == []

    def test_non_dict_json_ignored(self):
        assert parse_claude_stream_line('[1, 2, 3]') == []


class TestParseHermesEventLine:
    """parse_hermes_event_line：hermes runner 流式事件行 → 归一化事件。

    结果行（final_response 协议）不是事件，返回 None 交由 executor 判定。
    """

    def test_thinking_event(self):
        line = json.dumps({"event": "thinking", "text": "考虑方案 A"})
        events = parse_hermes_event_line(line)
        assert events == [{"kind": "thinking", "text": "考虑方案 A"}]

    def test_tool_start_event(self):
        line = json.dumps({"event": "tool_start", "tool": "bash",
                           "input": "git status"})
        events = parse_hermes_event_line(line)
        assert events == [{"kind": "tool", "tool": "bash",
                           "input": "git status"}]

    def test_tool_complete_event(self):
        line = json.dumps({"event": "tool_complete", "tool": "bash",
                           "output": "clean", "is_error": False})
        events = parse_hermes_event_line(line)
        assert events == [{"kind": "tool_result", "tool": "bash",
                           "text": "clean", "is_error": False}]

    def test_stream_delta_event(self):
        line = json.dumps({"event": "stream_delta", "text": "正在"} )
        events = parse_hermes_event_line(line)
        assert events == [{"kind": "text", "text": "正在"}]

    def test_status_event(self):
        line = json.dumps({"event": "status", "message": "第 2/90 轮"})
        events = parse_hermes_event_line(line)
        assert events == [{"kind": "status", "message": "第 2/90 轮"}]

    def test_result_line_not_an_event(self):
        line = json.dumps({"final_response": "完成", "messages": [],
                           "session_id": "s1", "error": None})
        assert parse_hermes_event_line(line) is None

    def test_invalid_json_not_an_event(self):
        assert parse_hermes_event_line("garbage") is None

    def test_unknown_event_kind_ignored(self):
        line = json.dumps({"event": "mystery"})
        assert parse_hermes_event_line(line) == []


class TestEventBus:
    """EventBus：executor 线程发布 → 订阅者队列消费。

    - 订阅后发布的事件全部可收到（订阅前的事件靠日志文件回放，总线不保留）
    - 多订阅者互不影响
    - 队列满时丢最旧（慢消费者不阻塞 executor 读流）
    - 无订阅者时发布不阻塞不报错
    """

    def test_publish_after_subscribe_received(self):
        bus = EventBus()
        sub = bus.subscribe(1)
        bus.publish(1, {"kind": "text", "text": "a"})
        assert sub.get(timeout=1) == {"kind": "text", "text": "a"}
        sub.close()

    def test_events_before_subscribe_not_received(self):
        """总线只转发实时事件，历史由日志文件回放兜底。"""
        bus = EventBus()
        bus.publish(1, {"kind": "text", "text": "old"})
        sub = bus.subscribe(1)
        with pytest.raises(Exception):
            sub.get(timeout=0.1)
        sub.close()

    def test_multiple_subscribers_receive_all(self):
        bus = EventBus()
        s1 = bus.subscribe(1)
        s2 = bus.subscribe(1)
        bus.publish(1, {"kind": "text", "text": "x"})
        assert s1.get(timeout=1)["text"] == "x"
        assert s2.get(timeout=1)["text"] == "x"
        s1.close()
        s2.close()

    def test_queue_full_drops_oldest(self):
        """慢消费者队列满时丢最旧事件，不阻塞发布者。"""
        bus = EventBus()
        sub = bus.subscribe(1, maxsize=2)
        for i in range(3):
            bus.publish(1, {"kind": "text", "text": str(i)})
        first = sub.get(timeout=1)
        second = sub.get(timeout=1)
        assert first["text"] == "1"  # "0" 已被丢弃
        assert second["text"] == "2"
        sub.close()

    def test_publish_without_subscribers_ok(self):
        bus = EventBus()
        bus.publish(99, {"kind": "text", "text": "no one listens"})

    def test_unsubscribed_no_longer_receives(self):
        bus = EventBus()
        sub = bus.subscribe(1)
        sub.close()
        bus.publish(1, {"kind": "text", "text": "after close"})
        with pytest.raises(Exception):
            sub.get(timeout=0.1)

    def test_tasks_isolated(self):
        bus = EventBus()
        s1 = bus.subscribe(1)
        s2 = bus.subscribe(2)
        bus.publish(1, {"kind": "text", "text": "task1"})
        assert s1.get(timeout=1)["text"] == "task1"
        with pytest.raises(Exception):
            s2.get(timeout=0.1)
        s1.close()
        s2.close()
