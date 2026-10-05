"""Discoverable Computer Use capability using the existing permission/turn runtime."""
from __future__ import annotations

import importlib.util
import json
import re
import threading
import uuid
from pathlib import Path

from config import Config
from core.computer_use import ComputerUseModel, ComputerUseRuntime
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema, ToolCancelledError
from core.runtime.event_bus import Event, get_event_bus
from core.tool_result import Artifact, Evidence, ToolRunResult, ToolRunStatus
from core.turn_context import check_turn_cancelled


# ponytail: one desktop/profile owner per process; per-profile locks if concurrent browsers are needed.
_RUN_LOCK = threading.Lock()


class ComputerUsePlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "computer_use"
        self.description = "화면을 관찰하고 클릭·입력·스크롤한 뒤 결과를 검증하는 Computer Use"

    def get_tools(self):
        return [ToolSchema("computer_use_run", self.description, {
            "type": "object", "properties": {
                "goal": {"type": "string", "minLength": 1, "maxLength": 4000},
                "backend": {"enum": ["browser", "windows"]},
                "url": {"type": "string", "maxLength": 2000},
                "window_title": {"type": "string", "minLength": 1, "maxLength": 300},
                "profile": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,40}$", "default": "default"},
                "allowed_origins": {"type": "array", "maxItems": 10, "uniqueItems": True,
                                    "items": {"type": "string", "maxLength": 500}},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 40, "default": 20},
                "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 600, "default": 300},
                "allow_coordinates": {"type": "boolean", "default": False},
            }, "required": ["goal", "backend"], "additionalProperties": False,
            "allOf": [{"if": {"properties": {"backend": {"const": "browser"}}},
                       "then": {"required": ["url"]}, "else": {"required": ["window_title"]}}],
        }, ["screen_capture", "screen_read"], side_effect="execute", timeout_seconds=660,
            cancellable=True, max_retries=0),
            ToolSchema("computer_use_status", "Computer Use 구성·실행 여부를 조회합니다.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, [], side_effect="read")]

    def get_intents(self):
        return [IntentSchema(
            "computer.use_browser", "브라우저 화면을 보고 여러 단계의 UI 작업 수행", "computer_use_run",
            ["브라우저 조작", "웹 화면을 보고", "브라우저에서 직접", "computer use", "컴퓨터 유즈"],
            [SlotSchema("goal", "수행할 전체 작업", "화면에서 어떤 작업을 할까요?"),
             SlotSchema("backend", "제어 환경", "브라우저와 Windows 중 어느 환경인가요?"),
             SlotSchema("url", "시작할 웹 주소", "작업할 웹 주소를 알려주세요.")],
            execution_hints=["해줘", "해 줘", "조작", "입력", "눌러", "클릭", "선택", "스크롤"],
            utterance_patterns=[r"(?:브라우저|웹\s*화면).{0,100}(?:조작|입력|클릭|눌러|선택)"],
            request_type="execute"),
            IntentSchema(
                "computer.use_windows", "지정 Windows 앱 화면에서 UI 작업 수행", "computer_use_run",
                ["윈도우 화면", "앱 화면을 보고", "데스크톱 조작", "창에서 직접"],
                [SlotSchema("goal", "수행할 전체 작업", "화면에서 어떤 작업을 할까요?"),
                 SlotSchema("backend", "제어 환경", "어느 환경을 제어할까요?"),
                 SlotSchema("window_title", "대상 창 제목", "조작할 창의 제목을 알려주세요.")],
                execution_hints=["해줘", "해 줘", "조작", "입력", "눌러", "클릭", "선택", "스크롤"],
                utterance_patterns=[r"(?:윈도우|데스크톱|앱\s*화면).{0,100}(?:조작|입력|클릭|눌러|선택)"],
                request_type="execute"),
            IntentSchema("computer.status", "Computer Use 구성 확인", "computer_use_status",
                         ["computer use 상태", "컴퓨터 유즈 상태", "화면 조작 상태"], [],
                         execution_hints=["알려", "상태", "확인"], request_type="query")]

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        if intent_name in {"computer.use_browser", "computer.use_windows"}:
            slots.setdefault("goal", text.strip())
            slots["backend"] = "browser" if intent_name.endswith("browser") else "windows"
            if slots["backend"] == "browser":
                from plugins.browser import BrowserPlugin
                urls = BrowserPlugin._extract_url_literals(text)
                if len(urls) == 1:
                    slots["url"] = urls[0]
            else:
                match = re.search(r'["“]([^"”]+)["”]\s*(?:창|앱)', text)
                if match:
                    slots["window_title"] = match.group(1)
                elif (current_slots.get("goal") and not current_slots.get("window_title")
                      and 0 < len(text.strip()) <= 300):
                    slots["window_title"] = text.strip().strip('"“”')
        return slots

    def _checkpoint(self):
        check_turn_cancelled()
        context = self.get_execution_context()
        if context:
            context.raise_if_cancelled()

    @staticmethod
    def _approve(manager, goal, observation, decision):
        action = decision["action"]
        target = observation.targets.get(decision.get("target"), {})
        destination = observation.context.get("url") or observation.context.get("title")
        detail = {k: v for k, v in decision.items() if k not in {"observation_id", "reason"}}
        description = (f"작업: {goal}\n대상: {destination}\n요소: {target.get('name', '')}\n"
                       f"실행 내용: {json.dumps(detail, ensure_ascii=False)}\n이번 행동에만 적용됩니다.")
        permission = "coordinate_control" if action in {"coordinate_click", "type_text"} else "computer_control"
        return manager.request_once(permission, description)

    def execute_tool(self, name, data):
        if name == "computer_use_status":
            status = {"running": _RUN_LOCK.locked(), "model": Config.OLLAMA_COMPUTER_USE_MODEL,
                      "model_endpoint": "configured_ollama", "model_readiness": "not_probed",
                      "playwright_installed": importlib.util.find_spec("playwright") is not None,
                      "pywinauto_installed": importlib.util.find_spec("pywinauto") is not None,
                      "coordinate_fallback": "opt_in_and_one_time_approval"}
            return ToolRunResult.successful(tool_name=name, raw_output=json.dumps(status, ensure_ascii=False),
                                           evidence=[Evidence("computer_use_status", "로컬 구성 상태", status)])
        if name != "computer_use_run":
            return ToolRunResult.failed(tool_name=name, error="지원하지 않는 Computer Use 도구입니다.")
        from jsonschema import Draft202012Validator
        if list(Draft202012Validator(self.get_tools()[0].input_schema).iter_errors(data)):
            return ToolRunResult.failed(tool_name=name, error="목표·제어 환경·시작 대상 설정이 올바르지 않습니다.")
        if not _RUN_LOCK.acquire(blocking=False):
            return ToolRunResult.failed(tool_name=name, error="이미 화면 조작이 진행 중입니다. 기존 작업을 종료해 주세요.")
        result = None
        try:
            from core.permission import get_permission_manager
            from core.productization import SafeModeManager
            from core.computer_use_backends import BrowserComputerBackend, WindowsComputerBackend
            self._checkpoint()
            if SafeModeManager().enabled():
                return ToolRunResult.failed(tool_name=name, error="안전 모드에서는 화면 조작을 실행하지 않습니다.")
            manager = get_permission_manager()
            for permission in ["screen_capture", "screen_read", "browser" if data["backend"] == "browser" else "windows_api"]:
                if not manager.request_permission(permission):
                    return ToolRunResult.cancelled(tool_name=name, message=f"필요한 권한이 거부되었습니다: {permission}")
                self._checkpoint()
            root = Path(Config.DB_PATH).resolve().parent
            backend = (BrowserComputerBackend(data["url"], profile_path=root / "browser_profiles" / "computer_use" / data.get("profile", "default"),
                                              allowed_origins=data.get("allowed_origins", ()))
                       if data["backend"] == "browser" else WindowsComputerBackend(data["window_title"]))
            model = ComputerUseModel()

            def approve(goal, observation, decision):
                return self._approve(manager, goal, observation, decision)

            def progress(entry):
                get_event_bus().publish(Event("computer_use.step", "computer_use", data=entry))

            self._checkpoint()
            with backend:
                runtime = ComputerUseRuntime(backend, model, approve, self._checkpoint, progress)
                result = runtime.run(data["goal"], max_steps=data.get("max_steps", 20),
                                     timeout_seconds=data.get("timeout_seconds", 300),
                                     allow_coordinates=data.get("allow_coordinates", False))
                # Only the final observation is persisted. Intermediate frames stay in memory.
                if runtime.last_observation and result.status != ToolRunStatus.CANCELLED:
                    self._checkpoint()
                    output = root / "computer_use" / uuid.uuid4().hex
                    output.mkdir(parents=True)
                    frame = output / "final.png"
                    frame.write_bytes(runtime.last_observation.screenshot)
                    trace = output / "trace.json"
                    trace.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
                    result.artifacts.extend([Artifact("image", str(frame)), Artifact("computer_use_trace", str(trace))])
                return result
        except ToolCancelledError:
            if result is not None:
                result.status = ToolRunStatus.CANCELLED
                result.raw_output = "화면 조작 요청이 취소되었습니다. 이미 실행한 행동은 기록을 확인하세요."
                return result
            return ToolRunResult.cancelled(tool_name=name, message="화면 조작 요청이 취소되었습니다.")
        except Exception as exc:
            if result is not None:
                # Storage/cleanup failures do not mean a completed click never happened.
                result.evidence.append(Evidence("computer_use_cleanup", "결과 저장 또는 자원 정리 실패",
                                                {"error_type": type(exc).__name__}))
                result.raw_output += f" (결과 저장 또는 자원 정리 실패: {type(exc).__name__})"
                return result
            return ToolRunResult.failed(tool_name=name, error=f"Computer Use 실행 실패 ({type(exc).__name__}): {exc}")
        finally:
            _RUN_LOCK.release()
