"""Discoverable Computer Use capability using the existing permission/turn runtime."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from config import Config
from core.computer_use import ComputerUseModel, ComputerUseRuntime
from core.computer_use_memory import remember_workflow, retrieve_workflow, semantic_step
from core.harness import SafetyLayer
from core.knowledge_memory import SensitiveMemoryPolicy
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema, ToolCancelledError
from core.productization import SensitiveDataRedactor
from core.runtime.event_bus import Event, get_event_bus
from core.tool_result import Artifact, Evidence, ToolRunResult, ToolRunStatus
from core.turn_context import check_turn_cancelled, current_turn_context


# shortcut: one desktop/profile owner per process; add per-profile locks for concurrent browsers.
_RUN_LOCK = threading.Lock()


class ComputerUsePlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "computer_use"
        self.description = ("사용자 요청에 필요한 Windows 앱 또는 웹 화면을 보고 마우스·키보드로 작업합니다. "
                            "전용 API로 할 수 없는 앱 열기, 한글 입력, 클릭, 단축키, 드래그, 저장에 사용하고 "
                            "매 행동 뒤 화면을 다시 읽어 완료를 검증합니다.")

    def get_tools(self):
        return [ToolSchema("computer_use_run", self.description, {
            "type": "object", "properties": {
                "goal": {"type": "string", "minLength": 1, "maxLength": 4000,
                         "description": "사용자가 요청한 전체 목표와 실제 입력할 내용을 그대로 전달합니다."},
                "backend": {"enum": ["browser", "windows"], "default": "windows"},
                "app": {"type": "string", "minLength": 1, "maxLength": 300,
                        "description": "대상 앱 이름 또는 실행 파일. app 또는 window_title 중 하나로 대상 앱을 지정합니다."},
                "app_arguments": {"type": "array", "maxItems": 8,
                                  "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                                  "description": "앱에서 열 기존 파일의 절대 경로만 허용합니다. 명령행 옵션은 금지합니다."},
                "url": {"type": "string", "maxLength": 2000},
                "window_title": {"type": "string", "minLength": 1, "maxLength": 300},
                "profile": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,40}$", "default": "default"},
                "allowed_origins": {"type": "array", "maxItems": 10, "uniqueItems": True,
                                    "items": {"type": "string", "maxLength": 500}},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 40, "default": 20},
                "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 600, "default": 300},
                "allow_coordinates": {"type": "boolean", "default": False},
                "model_provider": {"enum": ["selected", "local", "cloud"], "default": "selected",
                                   "description": "selected는 아니스의 현재 로컬/GPT 선택을 따릅니다."},
                "verify_file": {"type": "string", "minLength": 1, "maxLength": 1000,
                                "description": "사용자가 저장을 요청한 텍스트 파일의 절대 경로."},
                "expected_text": {"type": "string", "maxLength": 4000,
                                  "description": "verify_file에서 다시 읽어 확인할 정확한 전체 텍스트."},
            }, "required": ["goal"], "additionalProperties": False,
            "dependentRequired": {"verify_file": ["expected_text"], "expected_text": ["verify_file"],
                                  "app_arguments": ["app"]},
            "allOf": [{"if": {"properties": {"backend": {"const": "browser"}}, "required": ["backend"]},
                       "then": {"required": ["url"]}}],
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
                  SlotSchema("backend", "제어 환경", "어느 환경을 제어할까요?", required=False),
                  SlotSchema("app", "대상 앱", "조작할 앱을 알려주세요.", required=False),
                  SlotSchema("window_title", "대상 창 제목", "조작할 창의 제목을 알려주세요.", required=False)],
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

    @staticmethod
    def _target_label(data):
        if data["backend"] == "browser":
            return urlsplit(data["url"]).hostname or "브라우저"
        app = Path(data.get("app", "")).name
        return app if app and not SensitiveMemoryPolicy.detect(app) else "Windows 앱"

    @staticmethod
    def _confirmation_reason(observation, decision):
        action = decision["action"]
        target = observation.targets.get(decision.get("target"), {})
        if not target and action in {"hotkey", "type_text"}:
            focused = getattr(observation, "host_state", {}).get("focused_identity")
            if focused:
                target = next((item for item in observation.targets.values()
                               if item.get("control_identity") == focused), {})
        if not target and action == "coordinate_click":
            x, y = decision["x"], decision["y"]
            labels = []
            for item in observation.targets.values():
                rectangle = item.get("rectangle")
                if rectangle:
                    left, top, right, bottom = rectangle
                    window = observation.context["bounds"]
                    left = (left - window[0]) * observation.context["width"] / (window[2] - window[0])
                    right = (right - window[0]) * observation.context["width"] / (window[2] - window[0])
                    top = (top - window[1]) * observation.context["height"] / (window[3] - window[1])
                    bottom = (bottom - window[1]) * observation.context["height"] / (window[3] - window[1])
                elif item.get("bounds"):
                    left, top, width, height = item["bounds"]
                    right, bottom = left + width, top + height
                else:
                    continue
                if left <= x < right and top <= y < bottom:
                    labels.append(str(item.get("name") or ""))
            target = {"name": " ".join(labels)}
        label = str(target.get("name") or "")
        dangerous = r"삭제|지우기|결제|구매|송금|이체|설치|덮어쓰기|\b(delete|remove|pay|purchase|install|overwrite)\b"
        external = r"전송|보내기|게시|발행|공유|업로드|제출|\b(send|post|publish|share|upload|submit)\b"
        if action in {"click", "double_click", "right_click", "press", "hotkey", "coordinate_click"}:
            if re.search(dangerous + "|" + external, label, re.I):
                return "외부 전송·삭제·결제 또는 변경을 확정하는 행동입니다."
        if action == "fill" and target.get("value") and target["value"] != decision.get("text"):
            return "이미 입력된 내용을 교체하는 행동입니다."
        key = decision.get("key", "")
        focused = getattr(observation, "host_state", {}).get("focused_identity")
        editors = {"edit", "document", "textbox"}
        target_existing = bool(target.get("value") and target.get("control_type", target.get("role", "")).casefold() in editors)
        existing = target_existing or any(item.get("value") and item.get("control_type", item.get("role", "")).casefold() in editors
                       and (not focused or item.get("control_identity") == focused)
                       for item in observation.targets.values())
        if existing and (action == "type_text" or key in {"Backspace", "Ctrl+S", "Ctrl+V", "Ctrl+Z", "Ctrl+Y", "Alt+F4"}):
            return "기존 문서 내용을 바꾸거나 닫을 수 있는 행동입니다."
        communication = r"카카오|메신저|채팅|메일|인스타|당근|slack|outlook|gmail|chat|messenger|instagram"
        if key in {"Enter", "Ctrl+Enter"} and re.search(
                communication, str(observation.context.get("title", "")) + " " + label, re.I):
            return "Enter 키가 메시지 전송을 확정할 수 있습니다."
        if key in {"Delete", "Shift+Delete", "Ctrl+Shift+Delete"}:
            return "삭제를 확정할 수 있는 키 조작입니다."
        return ""

    @staticmethod
    def _file_check(data):
        if "verify_file" not in data:
            return None, None
        path = Path(data["verify_file"]).expanduser().resolve()
        valid, _ = SafetyLayer.validate_path(str(path))
        if not valid or path.suffix.casefold() not in {".txt", ".md", ".csv", ".tsv"}:
            raise ValueError("완료 검증 파일은 허용된 폴더 안의 텍스트 파일이어야 합니다.")
        if path.is_file() and path.stat().st_size > 1_000_000:
            raise ValueError("완료 검증은 1MB 이하의 텍스트 파일에 한해 지원합니다.")
        baseline = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""

        def check_file():
            if not path.is_file() or path.stat().st_size > 1_000_000:
                raise ValueError("저장된 텍스트 파일을 확인할 수 없습니다.")
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if digest == baseline:
                raise ValueError("저장 요청 이후 파일이 생성되거나 변경되지 않았습니다.")
            for encoding in ("utf-8-sig", "utf-16", "cp949"):
                try:
                    actual = content.decode(encoding)
                except (UnicodeDecodeError, UnicodeError):
                    continue
                if actual.replace("\r\n", "\n") == data["expected_text"].replace("\r\n", "\n"):
                    return {"kind": "saved_text_file", "sha256": digest,
                            "changed": True, "exact_text_match": True}
            raise ValueError("저장한 파일의 내용이 요청한 전체 텍스트와 일치하지 않습니다.")
        return check_file, path

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
        permission = "coordinate_control" if action in {"coordinate_click", "move", "drag", "type_text"} else "computer_control"
        return manager.request_once(permission, description)

    def execute_tool(self, name, data):
        if name == "computer_use_status":
            status = {"running": _RUN_LOCK.locked(), "model": Config.OLLAMA_COMPUTER_USE_MODEL,
                      "model_provider": "selected", "model_readiness": "not_probed",
                      "playwright_installed": importlib.util.find_spec("playwright") is not None,
                      "pywinauto_installed": importlib.util.find_spec("pywinauto") is not None,
                      "coordinate_fallback": "opt_in_and_task_scoped_approval"}
            return ToolRunResult.successful(tool_name=name, raw_output=json.dumps(status, ensure_ascii=False),
                                           evidence=[Evidence("computer_use_status", "로컬 구성 상태", status)])
        if name != "computer_use_run":
            return ToolRunResult.failed(tool_name=name, error="지원하지 않는 Computer Use 도구입니다.")
        from jsonschema import Draft202012Validator
        if list(Draft202012Validator(self.get_tools()[0].input_schema).iter_errors(data)):
            return ToolRunResult.failed(tool_name=name, error="목표·제어 환경·시작 대상 설정이 올바르지 않습니다.")
        if not _RUN_LOCK.acquire(blocking=False):
            return ToolRunResult.failed(tool_name=name, error="이미 화면 조작이 진행 중입니다. 기존 작업을 종료해 주세요.")
        data = {"backend": "windows", **data}
        result = None
        turn = current_turn_context()
        turn_id = turn.turn_id if turn else ""
        run_id = uuid.uuid4().hex
        target_label = self._target_label(data)
        steps = []
        last_decision = {}
        literals = {data.get("expected_text", "")}

        def safe(value):
            if isinstance(value, dict):
                return {key: safe(item) for key, item in SensitiveDataRedactor.redact(value).items()}
            if isinstance(value, list):
                return [safe(item) for item in value]
            if isinstance(value, str):
                for literal in sorted(literals - {""}, key=len, reverse=True):
                    value = value.replace(literal, "[작업 입력 생략]")
                value = SensitiveDataRedactor.redact(value)
                if SensitiveMemoryPolicy.detect(value):
                    return "[민감한 내용 생략]"
            return value

        try:
            from core.permission import get_permission_manager
            from core.productization import SafeModeManager
            from core.computer_use_backends import BrowserComputerBackend, WindowsComputerBackend
            self._checkpoint()
            model_provider = data.get("model_provider", "selected")
            if model_provider == "selected":
                from core.llm import is_gpt_enabled
                model_provider = "cloud" if is_gpt_enabled() else "local"
            if SafeModeManager().enabled():
                result = ToolRunResult.failed(tool_name=name, error="안전 모드에서는 화면 조작을 실행하지 않습니다.")
                return result
            completion_check, verified_path = self._file_check(data)
            goal = data["goal"]
            if completion_check:
                goal += ("\n파일 완료 조건: 아래 경로에 문서 전체 내용을 정확히 저장해야 합니다."
                         "\n저장할 파일 경로: " + json.dumps(str(verified_path), ensure_ascii=False)
                         + "\n문서에 입력할 전체 내용: " + json.dumps(data["expected_text"], ensure_ascii=False))
                if len(goal) > 4000:
                    raise ValueError("저장 경로와 전체 텍스트를 포함한 목표는 4000자 이하여야 합니다.")
            manager = get_permission_manager()
            for permission in ["screen_capture", "screen_read", "browser" if data["backend"] == "browser" else "windows_api"]:
                if not manager.request_permission(permission):
                    result = ToolRunResult.cancelled(tool_name=name, message=f"필요한 권한이 거부되었습니다: {permission}")
                    return result
                self._checkpoint()
            scope = (f"작업: {data['goal']}\n대상: {target_label}\n"
                     "이번 작업 안에서 화면 읽기, 클릭, 입력, 키 조작만 허용합니다. "
                     "외부 전송·삭제·결제·기존 내용 교체는 별도로 확인합니다. "
                     "Ctrl+Shift+F11로 즉시 중지할 수 있습니다.")
            scope += ("\n화면 분석: 대상 앱의 화면 이미지와 접근성 메타데이터를 로컬 PC의 AI가 처리합니다."
                      if model_provider == "local" else
                      "\n화면 분석: 대상 앱의 화면 이미지와 접근성 메타데이터를 연결된 Codex 클라우드 모델로 전송합니다.")
            if data.get("app_arguments"):
                scope += "\n앱에서 열 파일: " + ", ".join(data["app_arguments"])
            if not manager.request_once("computer_control", scope):
                result = ToolRunResult.cancelled(tool_name=name, message="이번 화면 작업이 허용되지 않았습니다.")
                return result
            self._checkpoint()
            if data.get("allow_coordinates") and not manager.request_once(
                    "coordinate_control", f"대상: {target_label}\n이번 작업에서만 관찰한 창 안의 좌표 클릭·이동·드래그를 허용합니다."):
                result = ToolRunResult.cancelled(tool_name=name, message="이번 작업의 좌표 제어가 허용되지 않았습니다.")
                return result
            if verified_path and verified_path.exists() and not manager.request_once(
                    "computer_control", f"이번 저장 요청은 기존 파일을 변경합니다: {verified_path}\n작업: {data['goal']}"):
                result = ToolRunResult.cancelled(tool_name=name, message="기존 출력 파일 변경이 허용되지 않았습니다.")
                return result
            self._checkpoint()
            root = Path(Config.DB_PATH).resolve().parent
            backend = (BrowserComputerBackend(data["url"], profile_path=root / "browser_profiles" / "computer_use" / data.get("profile", "default"),
                                              allowed_origins=data.get("allowed_origins", ()))
                       if data["backend"] == "browser" else WindowsComputerBackend(
                           data.get("window_title", ""), app=data.get("app", ""),
                           app_arguments=data.get("app_arguments", ()), checkpoint=self._checkpoint))

            def approve(goal, observation, decision):
                last_decision.clear()
                last_decision.update(decision)
                if decision.get("text"):
                    literals.add(decision["text"])
                if self._confirmation_reason(observation, decision):
                    return self._approve(manager, goal, observation, decision)
                return True

            def progress(entry):
                print(f"[Computer Use] step {entry['step'] + 1}: {entry['action']} ({entry['status']})")
                observation = runtime.last_observation
                if observation:
                    selected = observation.targets.get(entry.get("target"), {})
                    key = last_decision.get("key", "") if last_decision.get("action") == entry["action"] else ""
                    steps.append(semantic_step(entry["action"], selected, key))
                get_event_bus().publish(Event("computer_use.step", "computer_use", data={
                    "step": entry["step"], "action": entry["action"], "status": entry["status"],
                    "target": target_label, "turn_id": turn_id,
                }))

            self._checkpoint()
            with backend:
                app_name = getattr(backend, "app_name", "")
                if app_name and not SensitiveMemoryPolicy.detect(app_name):
                    target_label = app_name
                workflow = ""
                try:
                    workflow = retrieve_workflow(target_label, data["goal"])
                except Exception:
                    pass  # A memory lookup failure must not block the requested screen task.
                self._checkpoint()
                model = ComputerUseModel(provider=model_provider, workflow_context=workflow)
                get_event_bus().publish(Event("computer_use.started", "computer_use", data={
                    "target": target_label, "turn_id": turn_id,
                }))
                runtime = ComputerUseRuntime(backend, model, approve, self._checkpoint, progress)
                result = runtime.run(goal, max_steps=data.get("max_steps", 20),
                                     timeout_seconds=data.get("timeout_seconds", 300),
                                     allow_coordinates=data.get("allow_coordinates", False),
                                     completion_check=completion_check)
                result.raw_output = safe(result.raw_output)
                result.error = safe(result.error)
                result.evidence = [Evidence(item.kind, safe(item.summary), safe(item.data))
                                   for item in result.evidence]
                if result.succeeded:
                    self._checkpoint()
                    if verified_path:
                        result.artifacts.append(Artifact("file", str(verified_path), {"verified": True}))
                    try:
                        memory_id = remember_workflow(target_label, data["goal"], steps, run_id)
                        if memory_id:
                            result.evidence.append(Evidence("computer_use_memory", "검증된 업무 절차를 기억했습니다.",
                                                            {"record_id": memory_id}))
                    except Exception as exc:
                        result.evidence.append(Evidence("computer_use_memory", "업무 절차 기억을 저장하지 못했습니다.",
                                                        {"error_type": type(exc).__name__}))
                return result
        except ToolCancelledError:
            if result is not None:
                result.status = ToolRunStatus.CANCELLED
                result.raw_output = "화면 조작 요청이 취소되었습니다. 이미 실행한 행동은 기록을 확인하세요."
                return result
            result = ToolRunResult.cancelled(tool_name=name, message="화면 조작 요청이 취소되었습니다.")
            return result
        except Exception as exc:
            if result is not None:
                # Storage/cleanup failures do not mean a completed click never happened.
                result.evidence.append(Evidence("computer_use_cleanup", "결과 저장 또는 자원 정리 실패",
                                                {"error_type": type(exc).__name__}))
                result.raw_output += f" (결과 저장 또는 자원 정리 실패: {type(exc).__name__})"
                return result
            result = ToolRunResult.failed(tool_name=name, error=f"Computer Use 실행 실패 ({type(exc).__name__}): {safe(str(exc))}")
            return result
        finally:
            _RUN_LOCK.release()
            get_event_bus().publish(Event("computer_use.finished", "computer_use", data={
                "status": result.status.value if result is not None else "failed", "turn_id": turn_id,
            }))
