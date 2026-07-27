import sys
import os
import json
import uuid
import threading
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer, QObject, pyqtSignal
from PyQt6.QtGui import QIcon
from config import Config, request_windows_permissions
from core.state_machine import StateMachine, State
from core.mode_manager import ModeManager
from core.memory import get_memory
from core.llm import get_llm_client
from core.tools import get_tool_executor
from core.user_profile import get_user_profile
from core.rag import get_rag_manager
from core.hardware import get_hardware_manager
from core.audio_processor import get_audio_processor
from core.workspace import get_workspace_manager
from core.permission import get_permission_manager
from core.executor import get_executor
from core.scheduler import get_automation_engine
from core.proactive import ProactiveNotificationPolicy
from core.response_presenter import present_response
from core.runtime_services import get_runtime_service_manager
from ui.main_window import JarvisMainWindow


def resource_path(relative_path: str) -> Path:
    """개발 실행과 PyInstaller 배포 실행 모두에서 리소스 경로를 찾는다."""
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / relative_path


def configure_windows_app_identity():
    """Windows 작업 표시줄이 Python이 아닌 JARVIS 아이콘으로 그룹화하게 한다."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("JARVIS.AI.Assistant")
    except Exception as exc:
        print(f"[UI] Windows AppUserModelID 설정 실패: {exc}")


def get_runtime_warning() -> str:
    expected = (Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe").resolve()
    actual = Path(sys.executable).resolve()
    if expected.exists() and actual != expected:
        return (
            f"[Runtime 경고] 현재 Python: {actual}\n"
            f"[Runtime 경고] 프로젝트 Python 3.12를 사용하세요: {expected}"
        )
    return ""


class ConsoleReader(QObject):
    input_received = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self._running = False
        self._thread = None
    
    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        self._running = False
    
    def _read_loop(self):
        while self._running:
            try:
                user_input = input("\n나: ").strip()
                if user_input:
                    self.input_received.emit(user_input)
            except (EOFError, KeyboardInterrupt):
                break


class AppSignals(QObject):
    """Thread-safe bridge for callbacks that must run on the Qt GUI thread."""
    ai_response_ready = pyqtSignal(str)
    tts_finished = pyqtSignal()
    # 권한 요청용 시그널: (permission_name, permission_description)
    permission_request = pyqtSignal(str, str)
    # 권한 응답용 시그널: (result_bool)
    permission_response = pyqtSignal(bool)
    progress_update = pyqtSignal(str)
    proactive_message = pyqtSignal(str)
    control_response_ready = pyqtSignal(object)
    voice_text_detected = pyqtSignal(str)


class JarvisApp:
    def __init__(self):
        configure_windows_app_identity()
        self.app = QApplication(sys.argv)
        icon = QIcon(str(resource_path("assets/jarvis.ico")))
        self.app.setWindowIcon(icon)
        self.audio_processor = get_audio_processor()
        self.window = JarvisMainWindow(self.audio_processor)
        self.window.setWindowIcon(icon)
        self.state_machine = StateMachine()
        self.runtime_services = get_runtime_service_manager()
        self.runtime_services.configure_model_environment()
        self.runtime_services.ensure_ollama()
        self.mode_manager = ModeManager()
        self.memory = get_memory()
        self.window.set_memory_manager(self.memory)
        self.llm = get_llm_client()
        self.tool_executor = get_tool_executor()
        self.window.set_tts_settings_manager(self.tool_executor.tts_settings)
        self.user_profile = get_user_profile()
        self.rag_manager = get_rag_manager()
        self.hardware_manager = get_hardware_manager()
        self.workspace_manager = get_workspace_manager()
        self.permission_manager = get_permission_manager()
        self.window.set_permission_manager(self.permission_manager)
        self.executor = get_executor()
        self.automation_engine = get_automation_engine()
        self.signals = AppSignals()  # <-- 여기로 옮겼어요!
        
        # 권한 요청 결과 저장용 변수
        self._permission_result = None
        self._permission_event = threading.Event()
        
        # PermissionManager에 UI callback 연결
        def permission_callback(permission):
            self._permission_result = None
            self._permission_event.clear()
            # 시그널로 메인 스레드에 요청 보내기
            self.signals.permission_request.emit(permission.name, permission.description)
            # 결과 기다리기 (최대 30초)
            if self._permission_event.wait(timeout=30):
                return self._permission_result
            return False
        self.permission_manager.set_request_callback(permission_callback)
        
        # 권한 요청 시그널 연결 (메인 스레드에서 실행)
        self.signals.permission_request.connect(self._on_permission_request)
        # 권한 응답 시그널 연결 (필요시)
        self.signals.permission_response.connect(self._on_permission_response)
        
        self.messages = []
        self.session_id = str(uuid.uuid4())
        self.last_response = ""
        self._response_user_request = ""
        
        self._is_processing_ai = False
        
        # 콘솔 리더 초기화
        self.console_reader = ConsoleReader()
        self.console_reader.input_received.connect(self._on_console_input)
        self.signals.ai_response_ready.connect(self._on_ai_response)
        self.signals.progress_update.connect(self._on_progress_update)
        self.signals.proactive_message.connect(self._on_proactive_message)
        self.signals.control_response_ready.connect(self._on_control_response)
        self.signals.voice_text_detected.connect(self._on_user_input)
        self.signals.tts_finished.connect(self._reset_all)
        self.automation_engine.set_result_callback(self._on_automation_result)
        print(f"[Automation] 시작 상태: {self.automation_engine.start()}")
        self.proactive_policy = ProactiveNotificationPolicy(self.notify_user)
        self.proactive_policy.start()
        
        # 시그널 연결
        self.state_machine.state_changed.connect(self._on_state_changed)
        self.window.text_submitted.connect(self._on_user_input)
        self.window.close_requested.connect(self._on_close_requested)  # 종료 요청 연결
        self.window.workspace_selected.connect(self._on_workspace_selected)  # Workspace 선택 연결
        self.window.session_selected.connect(self._select_session)
        self.window.session_created.connect(self._create_session)
        self.window.session_deleted.connect(self._delete_session)
        self.window.session_reset.connect(self._reset_session)
        
        self.heartbeat_timer = QTimer()
        self.heartbeat_timer.timeout.connect(lambda: None)
        self.heartbeat_timer.start(3000)
        
        self._init_ui()
        threading.Thread(target=self.tool_executor.prepare_selected_tts, daemon=True).start()
        QTimer.singleShot(0, self._run_next_queued_task)
    
    def _init_ui(self):
        # 시스템 프롬프트 설정
        self.llm.set_system_prompt(Config.get_system_prompt(self.user_profile))
        
        # 초기 상태 설정
        self.window.update_state(self.state_machine.state)
        
        # 가장 최근 세션 불러오기
        sessions = self.memory.list_sessions()
        if sessions:
            last_session_id = sessions[0][0]
            self.session_id = last_session_id
            self.messages = self.memory.load_session(last_session_id)
            print(f"[기억] 가장 최근 세션 {last_session_id}를 불러왔습니다, 메시지 {len(self.messages)}개")
            
            # UI에 최근 메시지 표시
            last_user_msg = None
            last_assistant_msg = None
            for msg in reversed(self.messages):
                if msg["role"] == "user" and not last_user_msg:
                    last_user_msg = msg["content"]
                elif msg["role"] == "assistant" and not last_assistant_msg:
                    last_assistant_msg = msg["content"]
                if last_user_msg and last_assistant_msg:
                    break
            
            if last_user_msg:
                self.window.show_user_text(last_user_msg)
            if last_assistant_msg:
                self.window.show_assistant_text(last_assistant_msg)
        else:
            self.session_id = self.memory.create_session("새 대화")
        self.window.set_current_session(self.session_id)
        
        # 콘솔 입력 처리 시작 (테스트용) - 잠시 주석
        # print("\n자비스가 준비되었습니다! 질문을 입력하세요 (종료하려면 'exit'):")
        # self.console_reader.start()
        
        # 자동으로 지속적인 음성 감지 시작
        print("[마이크] 자동으로 음성 감지를 시작합니다...")
        self._start_continuous_listen()
        
        print("\n자비스가 준비되었습니다! UI 하단 텍스트 상자에 질문을 입력하세요, 보스.")
    
    def _on_console_input(self, user_input: str):
        if user_input.lower() in ['exit', 'quit', '종료']:
            self.console_reader.stop()
            self.app.quit()
            return
        
        self._on_user_input(user_input)

    def _select_session(self, session_id: str):
        if self._is_processing_ai:
            self.window.show_assistant_text("현재 작업이 끝난 뒤 세션을 전환해 주세요, 보스.")
            return
        self.session_id = session_id
        self.messages = self.memory.load_session(session_id)
        self.window.set_current_session(session_id)
        self.window.clear_conversation_display()
        for role in ("user", "assistant"):
            message = next((item for item in reversed(self.messages) if item["role"] == role), None)
            if message and role == "user":
                self.window.show_user_text(message["content"])
            elif message:
                self.window.show_assistant_text(message["content"])

    def _create_session(self, title: str):
        if self._is_processing_ai:
            self.window.show_assistant_text("현재 작업이 끝난 뒤 새 세션을 만들어 주세요, 보스.")
            return
        self._select_session(self.memory.create_session(title))

    def _reset_session(self, session_id: str):
        if self._is_processing_ai and session_id == self.session_id:
            self.window.show_assistant_text("현재 작업 중인 세션은 리셋할 수 없습니다, 보스.")
            return
        self.memory.clear_session(session_id)
        self.executor.dialogue_state.clear_session(session_id)
        if session_id == self.session_id:
            self.messages = []
            self.last_response = ""
            self.window.clear_conversation_display()

    def _delete_session(self, session_id: str):
        if self._is_processing_ai and session_id == self.session_id:
            self.window.show_assistant_text("현재 작업 중인 세션은 삭제할 수 없습니다, 보스.")
            return
        self.executor.dialogue_state.clear_session(session_id)
        self.memory.delete_session(session_id)
        if session_id == self.session_id:
            remaining = self.memory.list_sessions()
            if remaining:
                self._select_session(remaining[0][0])
            else:
                self._select_session(self.memory.create_session("새 대화"))
    
    def _on_state_changed(self, old_state: State, new_state: State):
        self.window.update_state(new_state)
    
    def _on_user_input(self, text: str, existing_task_id=None):
        print("[DEBUG] _on_user_input called with:", text)
        self.window.show_user_text(text)
        self.state_machine.start_listening()

        if text.strip().casefold() == Config.WAKE_WORD.casefold():
            response = self._personalize_address("네, 보스. 말씀하세요.")
            self.window.show_assistant_text(response)
            self.last_response = response
            self.messages.append({"role": "user", "content": text})
            self.messages.append({"role": "assistant", "content": response})
            self.memory.save_message(self.session_id, "user", text)
            self.memory.save_message(self.session_id, "assistant", response)
            self.state_machine.go_idle()
            return
        
        # LISTENING 상태에서 사운드바 활성화: speaking=False, audio level 설정
        self.window.set_soundbar_speaking(False)
        self.window.set_soundbar_audio_level(50)  # 임시로 50% 레벨로 설정
        
        if self._is_processing_ai:
            if hasattr(self.executor, "is_control_command") and self.executor.is_control_command(text):
                self.messages.append({"role": "user", "content": text})
                self.memory.save_message(self.session_id, "user", text)
                thread = threading.Thread(target=self._process_control_command, args=(text,), daemon=True)
                thread.start()
                return
            if text.strip().lower().startswith(("새 작업:", "새 작업：")):
                queued_goal = text.split(":", 1)[-1].split("：", 1)[-1].strip()
                task = self.executor.enqueue_goal(queued_goal, self.session_id)
                self.window.show_assistant_text(
                    f"현재 작업 다음에 새 작업 {task.task_id}을 이어서 진행하겠습니다, 보스."
                )
                return
            task = self.executor.enqueue_goal(text.strip(), self.session_id)
            self.window.show_assistant_text(
                f"현재 작업이 끝나면 이어서 처리하겠습니다, 보스. 대기 작업 ID: {task.task_id}"
            )
            print(f"[DEBUG] AI busy, request queued: {task.task_id}")
            return
        
        self._is_processing_ai = True
        self.state_machine.start_processing()
        
        # AI 호출을 별도 스레드로 처리
        thread = threading.Thread(target=self._process_ai, args=(text, existing_task_id), daemon=True)
        thread.start()

    def _process_control_command(self, text: str):
        outcome = self.executor.handle_control_command(text, self.session_id)
        self.signals.control_response_ready.emit(outcome)

    def _on_control_response(self, outcome):
        """실행 중 제어 응답은 원래 AI 작업의 processing 상태를 변경하지 않는다."""
        response_text = self._personalize_address(outcome.response)
        self.window.show_assistant_text(response_text)
        self.messages.append({"role": "assistant", "content": response_text})
        self.memory.save_message(self.session_id, "assistant", response_text)
        if getattr(outcome, "next_goal", ""):
            self.executor.enqueue_goal(outcome.next_goal, self.session_id, priority=100)
    
    def _process_ai(self, text: str, existing_task_id=None):
        print("[DEBUG] _process_ai called with:", text)
        self._response_user_request = text
        
        # RAG로 문서 검색
        try:
            rag_context = self.rag_manager.search_docs(text)
            print("[DEBUG] RAG context:", rag_context)
            # 검색 결과는 Executor의 ContextManager가 다시 조립한다. 여기서 사용자
            # 발화 자체를 RAG 문자열로 바꾸면 대화 기록과 확인 질문 재개가 오염된다.
        except Exception as e:
            print(f"⚠️ RAG 검색 오류: {e}")
        
        # AI 호출: Planner → Executor → Reflection 파이프라인 사용!
        print("[DEBUG] Calling Executor.execute_goal...")
        conversation_history = list(self.messages[-10:])
        self.messages.append({"role": "user", "content": text})
        
        try:
            # Executor로 목표 실행!
            if hasattr(self.executor, "execute_turn"):
                outcome = self.executor.execute_turn(
                    text, self.session_id, conversation_history,
                    self.signals.progress_update.emit,
                    existing_task_id,
                )
                response_text = outcome.response
                if getattr(outcome, "next_goal", ""):
                    self.executor.enqueue_goal(outcome.next_goal, self.session_id, priority=100)
            else:
                response_text = self.executor.execute_goal(text, self.session_id, conversation_history)
            print("[DEBUG] Executor.execute_goal returned:", response_text)

            # 최종 응답 전송
            self.signals.ai_response_ready.emit(response_text)
        except Exception as e:
            print(f"⚠️ Executor 오류: {e}")
            import traceback
            traceback.print_exc()
            error_response = f"죄송해요, 보스! 작업 실행 중 오류가 발생했어요: {str(e)}"
            self.signals.ai_response_ready.emit(error_response)

    def _on_progress_update(self, message: str):
        """최종 답변 전의 짧은 작업 진행 상황을 GUI에 표시한다."""
        self.window.show_assistant_text(self._personalize_address(message))

    def notify_user(self, message: str):
        """Observer·Scheduler 등이 사용자에게 먼저 말을 걸 수 있는 공개 진입점."""
        if message and message.strip():
            self.signals.proactive_message.emit(message.strip())

    def _on_automation_result(self, event):
        if event.get("error"):
            message = f"보스, 예약 작업 실행 중 문제가 생겼습니다: {event['error']}"
        elif event.get("action_type") == "alarm":
            message = f"보스, {event.get('result') or '알람 시간입니다.'}"
        else:
            message = f"보스, 예약 작업을 완료했습니다. {event.get('result', '')}"
        self.notify_user(message)

    def _on_proactive_message(self, message: str):
        """사용자 입력 없이 발생한 알림도 일반 대화 기록과 UI에 남긴다."""
        message = self._personalize_address(message)
        self.last_response = message
        self.window.show_assistant_text(message)
        self.messages.append({"role": "assistant", "content": message})
        self.memory.save_message(self.session_id, "assistant", message)
        self.state_machine.start_responding()
        tool_executor = getattr(self, "tool_executor", None)
        if tool_executor is not None and callable(
            getattr(tool_executor, "speak_text", None)
        ):
            threading.Thread(
                target=lambda: self._speak_with_check(message),
                daemon=True,
            ).start()
    
    def _on_ai_response(self, response_text: str):
        print("[DEBUG] _on_ai_response called with:", response_text)
        response_text = present_response(response_text, self._response_user_request)
        response_text = self._personalize_address(response_text)
        print("[DEBUG] User-facing response:", response_text)
        # 이모지는 제거하되 상세정보 요청 시 경로와 PID 문법은 보존한다.
        import re
        response_text = re.sub(r'[^\w\s가-힣.,!?:/\\()\-]', '', response_text)
        print("[DEBUG] After emoji filter:", response_text)
        
        # 마지막 응답 저장
        self.last_response = response_text
        print(f"[DEBUG] self.last_response 설정됨: {self.last_response}")
        
        print("[DEBUG] Calling window.show_assistant_text")
        self.window.show_assistant_text(response_text)
        
        # [GAME] / [WORK] 모드 처리
        if response_text.startswith("[GAME]"):
            clean_text = response_text[6:].strip()
            self.window.show_assistant_text(clean_text)
            self.messages.append({"role": "assistant", "content": clean_text})
            self.mode_manager.activate_game_mode()
            self.last_response = clean_text
            print(f"[DEBUG] [GAME] 모드, self.last_response 업데이트: {self.last_response}")
        elif response_text.startswith("[WORK]"):
            clean_text = response_text[6:].strip()
            self.window.show_assistant_text(clean_text)
            self.messages.append({"role": "assistant", "content": clean_text})
            self.mode_manager.activate_work_mode()
            self.last_response = clean_text
            print(f"[DEBUG] [WORK] 모드, self.last_response 업데이트: {self.last_response}")
        else:
            self.messages.append({"role": "assistant", "content": response_text})
        
        self._is_processing_ai = False
        QTimer.singleShot(0, self._run_next_queued_task)
        
        # 대화 저장
        self.memory.save_message(self.session_id, "user", self.messages[-2]["content"])
        self.memory.save_message(self.session_id, "assistant", response_text)
        
        # 자동으로 음성 응답 (RESPONDING 상태로)
        print(f"[DEBUG] TTS 스레드 시작 전, self.last_response: {self.last_response}")
        self.state_machine.start_responding()  # RESPONDING 상태로 변경
        thread = threading.Thread(target=lambda: self._speak_with_check(self.last_response), daemon=True)
        thread.start()
        print("[DEBUG] TTS 스레드 시작됨")

    def _personalize_address(self, text: str) -> str:
        settings = getattr(getattr(self, "tool_executor", None), "tts_settings", None)
        if settings is not None and hasattr(settings, "personalize_address"):
            return settings.personalize_address(text)
        return str(text)

    def _run_next_queued_task(self):
        if self._is_processing_ai or not hasattr(self.executor, "dialogue_state"):
            return
        queued = [
            task for task in self.executor.dialogue_state.list_tasks(self.session_id, include_finished=False)
            if task.status == "queued"
        ]
        if queued:
            task = queued[0]
            self._on_user_input(task.goal, task.task_id)
    
    def _on_command_triggered(self, command: str):
        self.state_machine.start_processing()
        
        if command == "WORK":
            result = self.mode_manager.activate_work_mode()
            self.window.show_assistant_text(result)
            self.last_response = result
            # 자동으로 음성 응답
            thread = threading.Thread(target=lambda: self._speak_with_check(result), daemon=True)
            thread.start()
        elif command == "GAME":
            result = self.mode_manager.activate_game_mode()
            self.window.show_assistant_text(result)
            self.last_response = result
            # 자동으로 음성 응답
            thread = threading.Thread(target=lambda: self._speak_with_check(result), daemon=True)
            thread.start()
        elif command == "LISTEN_START":
            # 지속적인 음성 감지 시작
            thread = threading.Thread(target=self._start_continuous_listen, daemon=True)
            thread.start()
        elif command == "LISTEN_STOP":
            # 지속적인 음성 감지 중지
            thread = threading.Thread(target=self._stop_continuous_listen, daemon=True)
            thread.start()
        elif command == "SPEAK":
            # 마지막 응답 다시 읽어주기
            if self.last_response:
                thread = threading.Thread(target=lambda: self._speak_with_check(self.last_response), daemon=True)
                thread.start()
            else:
                result = "⚠️ 응답이 없어서 음성으로 읽어줄 수 없어요, 보스!"
                self.window.show_assistant_text(result)
                self.last_response = result
        elif command.startswith("ADD_DOC:"):
            # 문서 추가 처리
            file_path = command[len("ADD_DOC:"):]
            thread = threading.Thread(target=lambda: self._add_document(file_path), daemon=True)
            thread.start()
        elif command == "VIEW_PROFILE":
            # 프로필 보기
            profile_summary = self.user_profile.get_profile_summary()
            self.window.show_assistant_text(profile_summary)
            self.last_response = profile_summary
            # 자동으로 음성 응답
            thread = threading.Thread(target=lambda: self._speak_with_check(profile_summary), daemon=True)
            thread.start()
        
        self.state_machine.start_responding()
        QTimer.singleShot(1000, lambda: self.state_machine.go_idle())
    
    def _start_continuous_listen(self):
        # 지속적인 음성 감지 시작
        result = self.hardware_manager.start_continuous_listen(self._on_continuous_text_detected, self.audio_processor)
        print(result)
        self.window.show_assistant_text(result)
        self.last_response = result
    
    def _stop_continuous_listen(self):
        # 지속적인 음성 감지 중지
        result = self.hardware_manager.stop_continuous_listen()
        print(result)
        self.window.show_assistant_text(result)
        self.last_response = result
    
    def _on_continuous_text_detected(self, text: str):
        # 하드웨어 worker에서 Qt GUI thread로 안전하게 전달
        self.signals.voice_text_detected.emit(text)
    
    def _speak_with_check(self, text: str):
        print(f"[DEBUG] _speak_with_check 호출됨: {text}")
        hardware = getattr(self, "hardware_manager", None)
        if hardware is not None:
            hardware.set_output_active(True)
        try:
            print(f"[DEBUG] tool_executor.speak_text 호출 전")
            result = self.tool_executor.speak_text(text, self.audio_processor)
            print(f"[DEBUG] tool_executor.speak_text 반환값: {result}")
            if result.lstrip().startswith("TTS 오류:"):
                print(f"[TTS] 오류: {result}")
                print("[TTS] pyttsx3를 설치하세요: pip install pyttsx3")
        except Exception as e:
            print(f"[TTS] 오류: {e}")
            import traceback
            traceback.print_exc()
            print("[TTS] pyttsx3를 설치하세요: pip install pyttsx3")
        finally:
            if hardware is not None:
                hardware.set_output_active(False)
            # TTS가 끝나면 IDLE 상태로 돌아가고 사운드바 리셋
            self.signals.tts_finished.emit()
    
    def _reset_all(self):
        # 모든 상태를 초기화하고 IDLE로 돌아가기
        print("[DEBUG] IDLE 상태로 전환")
        self.state_machine.go_idle()
        self.window.set_soundbar_speaking(False)
        self.window.reset_soundbar()
    
    def _listen_from_mic(self):
        # 음성 입력 처리
        try:
            result = self.tool_executor.listen()
            if "오류:" in result:
                print(f"⚠️ STT 오류: {result}")
                print("💡 openai-whisper, sounddevice, scipy, numpy를 설치하세요!\n(requirements.txt Phase 3 확인)")
                QTimer.singleShot(0, lambda: self.window.show_assistant_text(result))
                self.last_response = result
                self.state_machine.go_idle()
                return
            if "음성 인식 결과:" in result:
                text = result.replace("음성 인식 결과:", "").strip()
                if text:
                    # 메인 스레드에서 UI 업데이트
                    QTimer.singleShot(0, lambda: self._on_user_input(text))
                else:
                    result = "⚠️ 음성이 인식되지 않았어요, 보스!"
                    QTimer.singleShot(0, lambda: self.window.show_assistant_text(result))
                    self.last_response = result
                    self.state_machine.go_idle()
        except Exception as e:
            print(f"⚠️ STT 오류: {e}")
            print("💡 openai-whisper, sounddevice, scipy, numpy를 설치하세요!\n(requirements.txt Phase 3 확인)")
            result = f"⚠️ 음성 인식 오류: {e}"
            QTimer.singleShot(0, lambda: self.window.show_assistant_text(result))
            self.last_response = result
            self.state_machine.go_idle()
    
    def _add_document(self, file_path: str):
        # 문서 추가 처리
        result = self.rag_manager.add_document(file_path)
        self.last_response = result
        self.window.show_assistant_text(result)
        # 자동으로 음성 응답
        thread = threading.Thread(target=lambda: self.tool_executor.speak_text(result), daemon=True)
        thread.start()
    
    def _ensure_visible(self):
        """명시적으로 필요할 때만 창을 복원한다. 주기적으로 앞으로 가져오지 않는다."""
        if not self.window.isVisible():
            self.window.show()
    
    def _on_workspace_selected(self, folder_path: str):
        # Workspace가 선택되면 처리
        print(f"[Workspace] 선택됨: {folder_path}")
        success = self.workspace_manager.set_workspace(folder_path)
        if success:
            info = self.workspace_manager.get_info()
            self.window.set_workspace_info(info.name, info.path)
            print(f"[Workspace] 설정 완료: {info.name} (파일: {info.file_count}개)")
            # Personal Memory에 즐겨찾기 경로로 추가
            try:
                self.user_profile.add_favorite_path(folder_path, info.name)
            except Exception as e:
                print(f"[Workspace] Personal Memory 저장 오류: {e}")
        else:
            self.window.set_workspace_info("")
            print(f"[Workspace] 설정 실패: 유효하지 않은 경로")
    
    def _on_permission_request(self, permission_name: str, permission_description: str):
        """메인 스레드에서 권한 요청 대화상자를 보여주고 결과를 반환"""
        result = self.window.request_permission(permission_name, permission_description)
        self._permission_result = result
        self._permission_event.set()
    
    def _on_permission_response(self, result: bool):
        """권한 응답 처리 (필요시)"""
        pass
    
    def _on_close_requested(self):
        # 종료 버튼 클릭시 프로그램 자체 종료
        if hasattr(self, "proactive_policy"):
            self.proactive_policy.stop()
        if hasattr(self, "runtime_services"):
            self.runtime_services.stop()
        self.app.quit()
    
    def run(self):
        return self.app.exec()

if __name__ == "__main__":
    if os.getenv("JARVIS_PACKAGING_SMOKE") == "1":
        configure_windows_app_identity()
        smoke_app = QApplication(sys.argv)
        smoke_icon = QIcon(str(resource_path("assets/jarvis.ico")))
        smoke_app.setWindowIcon(smoke_icon)
        if smoke_icon.isNull():
            raise RuntimeError("배포 아이콘을 불러오지 못했습니다.")
        smoke_report = os.getenv("JARVIS_PACKAGING_SMOKE_REPORT")
        if smoke_report:
            Path(smoke_report).write_text("OK", encoding="utf-8")
        sys.exit(0)
    runtime_warning = get_runtime_warning()
    if runtime_warning:
        print(runtime_warning)
    # 데이터 폴더 생성
    if not os.path.exists("data"):
        os.makedirs("data")
    
    jarvis = JarvisApp()
    sys.exit(jarvis.run())
