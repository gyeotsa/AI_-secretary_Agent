import json
from pathlib import Path

from core.continuity_adapters import adapter_for, discover_sources


def write_records(path, records, mode="w"):
    with path.open(mode, encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def codex(kind, payload, stamp="2026-09-01T10:00:00Z"):
    return {"type": kind, "timestamp": stamp, "payload": payload}


def test_codex_plan_result_and_messages_incremental(tmp_path):
    path = tmp_path / "session.jsonl"
    records = [codex("session_meta", {"id": "s", "cwd": "C:/project"}),
        codex("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "기능 구현"}]}),
        codex("event_msg", {"type": "user_message", "message": "기능 구현"}),
        codex("response_item", {"type": "function_call", "name": "functions.update_plan", "call_id": "c",
             "arguments": json.dumps({"plan": [{"step": "구현", "status": "in_progress"}, {"step": "테스트", "status": "pending"}]})})]
    write_records(path, records)
    adapter = adapter_for("codex")
    events, cursor, errors = adapter.read_incremental(path, {})
    assert not errors
    assert len([e for e in events if e["kind"] == "message"]) == 1
    assert not any(e["kind"] == "plan" for e in events)
    write_records(path, [codex("response_item", {"type": "function_call_output", "call_id": "c", "output": "Plan updated"})], "a")
    next_events, next_cursor, errors = adapter.read_incremental(path, cursor)
    plan = next(e for e in next_events if e["kind"] == "plan")
    assert plan["payload"]["steps"][0]["description"] == "구현"
    assert plan["workspace_path"] == "C:/project"
    assert plan["payload"]["verification"] == "reported"
    assert not adapter.read_incremental(path, next_cursor)[0]


def test_partial_utf8_line_keeps_cursor_until_newline(tmp_path):
    path = tmp_path / "session.jsonl"
    raw = json.dumps(codex("event_msg", {"type": "user_message", "message": "안녕하세요"}), ensure_ascii=False).encode("utf-8")
    path.write_bytes(raw[:-6])
    adapter = adapter_for("codex")
    assert adapter.read_incremental(path, {})[1]["offset"] == 0
    with path.open("ab") as handle:
        handle.write(raw[-6:] + b"\n")
    events, cursor, errors = adapter.read_incremental(path, {})
    assert events[0]["text"] == "안녕하세요"
    assert cursor["offset"] == path.stat().st_size


def test_rewrite_and_malformed_line_are_reported_without_poisoning_future_records(tmp_path):
    path = tmp_path / "session.jsonl"
    adapter = adapter_for("codex")
    write_records(path, [codex("session_meta", {"id": "old"})])
    _, cursor, _ = adapter.read_incremental(path)
    write_records(path, [codex("session_meta", {"id": "new"})])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    write_records(path, [codex("event_msg", {"type": "user_message", "message": "new message"})], "a")
    events, cursor, errors = adapter.read_incremental(path, cursor)
    assert events[-1]["session_id"] == "new"
    assert cursor["skipped"] == 1
    assert errors


def test_invalid_timestamp_does_not_block_next_record(tmp_path):
    path = tmp_path / "invalid-time.jsonl"
    write_records(path, [codex("session_meta", {"id": "bad"}, "bad-date"),
                         codex("event_msg", {"type": "user_message", "message": "valid"})])
    events, cursor, errors = adapter_for("codex").read_incremental(path)
    assert cursor["skipped"] == 1
    assert not cursor["pending_bytes"]
    assert events[0]["text"] == "valid"
    assert events[0]["session_id"] == "invalid-time"
    assert errors


def test_oversize_record_stream_skips_boundedly_and_resumes(tmp_path, monkeypatch):
    import core.continuity_adapters as adapters
    monkeypatch.setattr(adapters, "MAX_LINE_BYTES", 200)
    monkeypatch.setattr(adapters, "MAX_BATCH_BYTES", 400)
    path = tmp_path / "long.jsonl"
    path.write_text('"' + "x" * 1500 + '"\n', encoding="utf-8")
    write_records(path, [codex("event_msg", {"type": "user_message", "message": "valid"})], "a")
    cursor, events = {}, []
    for _ in range(10):
        more, cursor, _ = adapter_for("codex").read_incremental(path, cursor)
        events += more
    assert events[-1]["text"] == "valid"
    assert cursor["skipped"] == 1
    assert not cursor["pending_bytes"]


def test_claude_todos_and_task_lifecycle(tmp_path):
    path = tmp_path / "claude.jsonl"
    def record(role, blocks):
        return {"type": role, "sessionId": "claude-s", "timestamp": "2026-09-01T10:00:00Z",
                "cwd": "C:/repo", "message": {"role": role, "content": blocks}}
    write_records(path, [
        record("assistant", [{"type": "tool_use", "id": "todo", "name": "TodoWrite", "input": {"todos": [{"content": "설계", "status": "completed"}]}}]),
        record("user", [{"type": "tool_result", "tool_use_id": "todo", "content": "Todos updated"}]),
        record("assistant", [{"type": "tool_use", "id": "create", "name": "TaskCreate", "input": {"subject": "구현"}}]),
        record("user", [{"type": "tool_result", "tool_use_id": "create", "content": "Task #1 created successfully: 구현"}]),
        record("assistant", [{"type": "tool_use", "id": "update", "name": "TaskUpdate", "input": {"taskId": "1", "status": "completed"}}]),
        record("user", [{"type": "tool_result", "tool_use_id": "update", "content": "Updated"}]),
    ])
    events, _, errors = adapter_for("claude_code").read_incremental(path)
    assert not errors
    assert len([e for e in events if e["kind"] == "plan"]) == 1
    tasks = [e for e in events if e["kind"] == "task"]
    assert tasks[0]["payload"]["description"] == "구현"
    assert tasks[1]["payload"]["status"] == "completed"


def test_failed_plan_and_unstructured_claim_never_become_completed_plan(tmp_path):
    path = tmp_path / "bad-plan.jsonl"
    write_records(path, [
        codex("response_item", {"type": "function_call", "name": "update_plan", "call_id": "x", "arguments": json.dumps({"plan": [{"step": "all", "status": "completed"}]})}),
        codex("response_item", {"type": "function_call_output", "call_id": "x", "output": '{"error":"invalid plan"}'}),
        codex("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Everything is done"}]})])
    events, _, _ = adapter_for("codex").read_incremental(path)
    assert not any(e["kind"] == "plan" for e in events)
    assert events[-1]["kind"] == "message"


def test_discovery_respects_config_and_only_session_jsonl_files(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    (root / "sessions").mkdir(parents=True)
    (root / "auth.json").write_text("do-not-read", encoding="utf-8")
    (root / "history.jsonl").write_text("do-not-read", encoding="utf-8")
    (root / "sessions" / "rollout.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(root))
    assert discover_sources()[0]["path"] == str(root.resolve())
    assert adapter_for("codex").iter_files(root) == [root / "sessions" / "rollout.jsonl"]


def test_code_mode_literal_plan_is_parsed_without_executing_code(tmp_path):
    path = tmp_path / "code.jsonl"
    script = 'text(await tools.update_plan({"plan":[{"step":"test","status":"pending"}]})); throw new Error("never execute")'
    write_records(path, [codex("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "x", "input": script}),
                        codex("response_item", {"type": "custom_tool_call_output", "call_id": "x", "output": "Plan updated"})])
    events, _, _ = adapter_for("codex").read_incremental(path)
    assert [e for e in events if e["kind"] == "plan"][0]["payload"]["steps"][0]["description"] == "test"


def test_markdown_plan_is_candidate_even_when_checked_done(tmp_path):
    path = tmp_path / "prose.jsonl"
    write_records(path, [codex("event_msg", {"type": "agent_message", "message": "## 구현 계획\n- [x] 수집기 작성\n- [ ] 회귀 검증"})])
    events, _, _ = adapter_for("codex").read_incremental(path)
    plan = next(e for e in events if e["kind"] == "plan")
    assert plan["payload"]["authoritative"] is False
    assert {step["status"] for step in plan["payload"]["steps"]} == {"suggested"}


def test_unrelated_numbered_answers_do_not_create_plan(tmp_path):
    path = tmp_path / "answer.jsonl"
    write_records(path, [codex("event_msg", {"type": "agent_message", "message": "## 원인\n1. 네트워크 오류\n2. 설정 오류"})])
    events, _, _ = adapter_for("codex").read_incremental(path)
    assert not any(e["kind"] == "plan" for e in events)
