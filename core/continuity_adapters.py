"""Read-only adapters for Codex rollout and Claude Code transcript JSONL files.

Only complete records advance the cursor. Parser state travels with the cursor
so callers can atomically commit receipts and offsets. No application credentials,
private APIs, model calls, JavaScript evaluation, or source-file writes are used.
"""
from __future__ import annotations

import hashlib
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_TEXT = 32000
PARSER_VERSION = 2


def _hash(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def discover_sources():
    candidates = [
        ("codex", Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")),
        ("claude_code", Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")),
    ]
    return [{"provider": provider, "path": str(path.expanduser().resolve()), "exists": path.is_dir()}
            for provider, path in candidates]


def _text(value):
    if isinstance(value, str):
        # Image data never enters the derived database.
        return re.sub(r"data:[^;\s]+;base64,[A-Za-z0-9+/=]+", "[첨부 데이터 생략]", value)[:MAX_TEXT]
    if isinstance(value, list):
        return "\n".join(_text(item.get("text", "")) for item in value
                         if isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"})[:MAX_TEXT]
    if isinstance(value, dict):
        return _text(value.get("content") or value.get("text") or json.dumps(value, ensure_ascii=False))
    return ""


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


class LocalLogAdapter:
    def __init__(self, provider):
        self.provider = provider

    def iter_files(self, root):
        root = Path(root).resolve()
        if self.provider == "codex" and (root / "sessions").is_dir():
            roots = [root / "sessions", root / "archived_sessions"]
        elif self.provider == "claude_code" and (root / "projects").is_dir():
            roots = [root / "projects"]
        else:
            roots = [root]
        found = []
        for directory in roots:
            if not directory.is_dir():
                continue
            for current, children, names in os.walk(directory, followlinks=False):
                children[:] = [name for name in children if name not in {".git", "node_modules", "cache", "debug"}
                               and not Path(current, name).is_symlink()
                               and not getattr(Path(current, name), "is_junction", lambda: False)()]
                for name in names:
                    path = Path(current, name)
                    if path.suffix.lower() != ".jsonl" or path.is_symlink():
                        continue
                    if path.resolve().is_relative_to(root):
                        found.append(path)
        # Recent work becomes available before archival backfill.
        return sorted(found, key=lambda path: path.stat().st_mtime_ns, reverse=True)

    def read_incremental(self, path, cursor=None):
        path = Path(path)
        cursor = dict(cursor or {})
        stat = path.stat()
        identity = [stat.st_dev, stat.st_ino]
        offset = int(cursor.get("offset", 0))
        reset = (cursor.get("parser_version") != PARSER_VERSION or cursor.get("identity") != identity or offset > stat.st_size or
                 (cursor.get("mtime_ns") != stat.st_mtime_ns and stat.st_size <= offset))
        with path.open("rb") as handle:
            length = int(cursor.get("prefix_length", 0))
            if length and _hash(handle.read(length)) != cursor.get("prefix_hash"):
                reset = True
            if offset and not reset:
                handle.seek(max(0, offset - 512))
                if _hash(handle.read(min(offset, 512))) != cursor.get("tail_hash"):
                    reset = True
            if reset:
                cursor, offset = {}, 0
            state = dict(cursor.get("state") or {})
            state.setdefault("session_id", path.stem)
            events, diagnostics = [], []
            consumed, lines = 0, int(cursor.get("line", 0))
            skipping = bool(cursor.get("skipping", False))
            skipped = int(cursor.get("skipped", 0))
            handle.seek(offset)
            while consumed < MAX_BATCH_BYTES and len(events) < 1000:
                start = handle.tell()
                raw = handle.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                consumed += len(raw)
                if skipping:
                    offset = handle.tell()
                    if raw.endswith(b"\n"):
                        skipping = False
                        lines += 1
                    continue
                if len(raw) > MAX_LINE_BYTES:
                    skipped += 1
                    diagnostics.append(f"{path.name}: 대형 레코드 생략 (원본 바이트 {start})")
                    skipping = not raw.endswith(b"\n")
                    lines += int(not skipping)
                    offset = handle.tell()
                    continue
                if not raw.endswith(b"\n"):
                    # A producer may still be writing this UTF-8/JSON record.
                    break
                offset = handle.tell()
                lines += 1
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw.decode("utf-8-sig"))
                    if not isinstance(record, dict):
                        raise ValueError("record is not an object")
                    stamp = record.get("timestamp")
                    if stamp:
                        if isinstance(stamp, (int, float)):
                            datetime.fromtimestamp(stamp / 1000 if stamp > 100000000000 else stamp, timezone.utc)
                        else:
                            datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                    next_state = copy.deepcopy(state)
                    normalized = self._normalize(record, next_state)
                    state = next_state
                    for index, event in enumerate(normalized):
                        event.update(provider=self.provider, session_id=state["session_id"],
                            workspace_path=state.get("cwd", ""), timestamp=record.get("timestamp") or "",
                            event_id=_hash([self.provider, state["session_id"], _hash(raw.rstrip()), index]),
                            source_ref={"file_path": str(path), "line": lines, "offset": start})
                    events.extend(normalized)
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as exc:
                    skipped += 1
                    diagnostics.append(f"{path.name}:{lines}: 레코드 파싱 실패 ({type(exc).__name__})")
            prefix_length = min(offset, 512)
            handle.seek(0)
            prefix_hash = _hash(handle.read(prefix_length))
            handle.seek(max(0, offset - 512))
            tail_hash = _hash(handle.read(min(offset, 512)))
        return events, {"parser_version": PARSER_VERSION, "offset": offset, "line": lines, "identity": identity,
            "mtime_ns": stat.st_mtime_ns, "prefix_length": prefix_length, "prefix_hash": prefix_hash,
            "tail_hash": tail_hash, "state": state, "skipping": skipping, "skipped": skipped,
            "pending_bytes": max(0, stat.st_size - offset)}, diagnostics

    def _normalize(self, record, state):
        return self._codex(record, state) if self.provider == "codex" else self._claude(record, state)

    @staticmethod
    def _event(kind, *, role="system", text="", payload=None):
        return {"kind": kind, "role": role, "text": _text(text), "payload": payload or {}}

    def _message(self, role, text, representation, record, state):
        if role not in {"user", "assistant"} or not text:
            return []
        recent = list(state.get("recent_messages", []))
        fingerprint = _hash([role, text])
        # Codex can write the same message as response_item and event_msg.
        # Consume only a matching opposite representation, keeping repeated turns.
        match = next((i for i in range(len(recent) - 1, -1, -1)
                      if recent[i][0] == fingerprint and recent[i][1] != representation), None)
        if match is not None:
            recent.pop(match)
            state["recent_messages"] = recent
            return []
        recent.append([fingerprint, representation])
        state["recent_messages"] = recent[-32:]
        events = [self._event("message", role=role, text=text,
                             payload={"message_id": record.get("uuid") or record.get("id") or "",
                                      "text_truncated": len(text) >= MAX_TEXT})]
        if role == "assistant" and not state.get("has_structured_plan"):
            steps = self._prose_steps(text)
            if steps:
                events.append(self._event("plan", role="assistant", payload={
                    "plan_id": "candidate-plan", "authoritative": False, "verification": "reported",
                    "steps": [{"id": _hash(step), "description": step, "status": "suggested"} for step in steps],
                    "extraction": "markdown_plan_candidate"}))
        return events

    @staticmethod
    def _prose_steps(text):
        """Conservative, free candidate extraction; prose never proves completion."""
        active, fenced, result = False, False, []
        heading = re.compile(r"계획|구현 순서|다음 단계|할 일|진행 순서|\b(?:plan|todo|next steps|implementation steps)\b", re.I)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            if len(stripped) <= 100 and heading.search(stripped) and not re.match(r"^(?:[-*]|\d+[.)])\s", stripped):
                active = True
                continue
            if active and stripped.startswith("#"):
                break
            match = re.match(r"^ {0,3}(?:\d+[.)]|[-*])\s+(?:\[[ xX]\]\s*)?(.+)", line)
            if active and match:
                description = match.group(1).strip().strip("*")[:500]
                if description:
                    result.append(description)
            if len(result) >= 20:
                break
        return result if len(result) >= 2 else []

    def _codex(self, record, state):
        kind, payload = record.get("type"), record.get("payload") or {}
        if not isinstance(payload, dict):
            return []
        if kind == "session_meta":
            state["session_id"] = str(payload.get("id") or payload.get("session_id") or state["session_id"])
            state["cwd"] = payload.get("cwd") or ""
            return [self._event("session", payload={"title": payload.get("title", ""),
                                "version": payload.get("cli_version", "")})]
        if kind == "turn_context":
            state["cwd"] = payload.get("cwd") or state.get("cwd", "")
            return []
        if kind == "event_msg":
            if payload.get("type") in {"user_message", "agent_message"}:
                role = "user" if payload["type"] == "user_message" else "assistant"
                return self._message(role, _text(payload.get("message")), "event", record, state)
            return []
        if kind != "response_item":
            return []
        item_type = payload.get("type")
        if item_type == "message":
            return self._message(payload.get("role"), _text(payload.get("content")), "response", record, state)
        if item_type in {"function_call", "custom_tool_call"}:
            name = str(payload.get("name", ""))
            arguments = payload.get("arguments", payload.get("input", ""))
            call_id = str(payload.get("call_id") or payload.get("id") or _hash(payload))
            return self._tool_call(name, arguments, call_id, state)
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            return self._tool_result(str(payload.get("call_id", "")), payload.get("output", ""), False, state)
        return []

    def _claude(self, record, state):
        raw_id = str(record.get("sessionId") or state["session_id"])
        agent = record.get("agentId")
        if agent and not raw_id.endswith(":" + str(agent)):
            raw_id += ":" + str(agent)
        state["session_id"] = raw_id
        state["cwd"] = record.get("cwd") or state.get("cwd", "")
        kind = record.get("type")
        message = record.get("message") or {}
        if kind not in {"user", "assistant"} or not isinstance(message, dict):
            return []
        role = message.get("role") or kind
        content = message.get("content", "")
        events = self._message(role, _text(content), "claude", record, state)
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    events += self._tool_call(block.get("name", ""), block.get("input", {}), str(block.get("id", "")), state)
                elif block.get("type") == "tool_result":
                    events += self._tool_result(str(block.get("tool_use_id", "")), block.get("content", ""),
                                               bool(block.get("is_error")), state)
        return events

    def _tool_call(self, name, arguments, call_id, state):
        short_name = name.rsplit(".", 1)[-1]
        data = _object(arguments)
        # Code-mode wrappers are parsed only for literal JSON; never evaluated.
        if short_name == "exec" and isinstance(arguments, str):
            match = re.search(r"(?:tools|functions)\.update_plan\s*\(\s*(\{)", arguments)
            if match:
                try:
                    data, _ = json.JSONDecoder().raw_decode(arguments[match.start(1):])
                    short_name = "update_plan"
                except ValueError:
                    pass
        if short_name in {"update_plan", "TodoWrite", "TaskCreate", "TaskUpdate"}:
            pending = dict(state.get("pending_calls", {}))
            pending[call_id] = {"name": short_name, "input": data}
            state["pending_calls"] = dict(list(pending.items())[-100:])
        return [self._event("tool", role="assistant", text=_text(arguments),
                           payload={"tool_name": name, "call_id": call_id, "phase": "requested"})]

    def _tool_result(self, call_id, output, failed, state):
        text = _text(output)
        result_data = _object(output)
        failed = bool(failed or result_data.get("isError") or result_data.get("is_error")
                      or result_data.get("error") or result_data.get("status") in {"failed", "error", "cancelled"})
        events = [self._event("tool", role="tool", text=text,
                             payload={"call_id": call_id, "phase": "returned", "is_error": failed})]
        pending = dict(state.get("pending_calls", {}))
        request = pending.pop(call_id, None)
        state["pending_calls"] = pending
        if not request or failed or re.match(r"\s*(?:Error[: ]|Traceback|Tool error|Invalid)", text, re.I):
            return events
        name, data = request["name"], request["input"]
        if name in {"update_plan", "TodoWrite"}:
            raw_steps = data.get("plan") if name == "update_plan" else data.get("todos")
            if not isinstance(raw_steps, list):
                return events
            state["has_structured_plan"] = True
            steps = []
            for item in raw_steps:
                if not isinstance(item, dict):
                    continue
                description = str(item.get("step") or item.get("content") or item.get("description") or "").strip()
                if description:
                    steps.append({"id": str(item.get("id") or _hash(description)), "description": description,
                                  "status": item.get("status", "pending"), "verification": "reported"})
            events.append(self._event("plan", role="assistant", payload={"plan_id": "main", "steps": steps,
                "explanation": data.get("explanation", ""), "verification": "reported", "call_id": call_id,
                "authoritative": True}))
        elif name == "TaskCreate":
            result = _object(output)
            task_id = result.get("taskId") or result.get("task_id") or result.get("id")
            if not task_id:
                match = re.search(r"Task\s+#?(\d+)", text, re.I)
                task_id = match.group(1) if match else None
            if task_id:
                state["has_structured_plan"] = True
                events.append(self._event("task", role="assistant", payload={"plan_id": "tasks", "task_id": str(task_id),
                    "description": data.get("subject") or data.get("description") or f"작업 {task_id}",
                    "status": "pending", "verification": "reported"}))
        elif name == "TaskUpdate" and data.get("taskId"):
            task = {"plan_id": "tasks", "task_id": str(data["taskId"]), "verification": "reported"}
            if data.get("subject"):
                task["description"] = data["subject"]
            if data.get("status"):
                task["status"] = data["status"]
            if data.get("addBlockedBy"):
                task["add_dependencies"] = data["addBlockedBy"]
            events.append(self._event("task", role="assistant", payload=task))
        return events


def adapter_for(provider):
    if provider not in {"codex", "claude_code"}:
        raise ValueError(f"Unsupported local log provider: {provider}")
    return LocalLogAdapter(provider)
