import sys
import os
import json
import uuid
import threading
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer, QObject, pyqtSignal
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
from ui.main_window import JarvisMainWindow


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

class JarvisApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.audio_processor = get_audio_processor()
        self.window = JarvisMainWindow(self.audio_processor)
        self.state_machine = StateMachine()
        self.mode_manager = ModeManager()
        self.memory = get_memory()
        self.llm = get_llm_client()
        self.tool_executor = get_tool_executor()
        self.user_profile = get_user_profile()
        self.rag_manager = get_rag_manager()
        self.hardware_manager = get_hardware_manager()
        
        self.messages = []
        self.session_id = str(uuid.uuid4())
        self.last_response = ""
        
        self._is_processing_ai = False
        
        # 콘솔 리더 초기화
        self.console_reader = ConsoleReader()
        self.console_reader.input_received.connect(self._on_console_input)
        
        # 시그널 연결
        self.state_machine.state_changed.connect(self._on_state_changed)
        self.window.text_submitted.connect(self._on_user_input)
        self.window.close_requested.connect(self._on_close_requested)  # 종료 요청 연결
        
        # 타이머 설정
        self.visibility_timer = QTimer()
        self.visibility_timer.timeout.connect(self._ensure_visible)
        self.visibility_timer.start(1000)
        
        self.heartbeat_timer = QTimer()
        self.heartbeat_timer.timeout.connect(lambda: None)
        self.heartbeat_timer.start(3000)
        
        self._init_ui()
    
    def _init_ui(self):
        # 시스템 프롬프트 설정
        self.llm.set_system_prompt(Config.get_system_prompt(self.user_profile))
        
        # 초기 상태 설정
        self.window.update_state(self.state_machine.state)
        
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
    
    def _on_state_changed(self, old_state: State, new_state: State):
        self.window.update_state(new_state)
    
    def _on_user_input(self, text: str):
        print("[DEBUG] _on_user_input called with:", text)
        self.window.show_user_text(text)
        self.state_machine.start_listening()
        
        # LISTENING 상태에서 사운드바 활성화: speaking=False, audio level 설정
        self.window.set_soundbar_speaking(False)
        self.window.set_soundbar_audio_level(50)  # 임시로 50% 레벨로 설정
        
        if self._is_processing_ai:
            print("[DEBUG] Already processing AI, skipping")
            return
        
        self._is_processing_ai = True
        self.state_machine.start_processing()
        
        # AI 호출을 별도 스레드로 처리
        thread = threading.Thread(target=self._process_ai, args=(text,), daemon=True)
        thread.start()
    
    def _process_ai(self, text: str):
        print("[DEBUG] _process_ai called with:", text)
        # RAG로 문서 검색
        try:
            rag_context = self.rag_manager.search_docs(text)
            print("[DEBUG] RAG context:", rag_context)
            if "관련 문서를 찾을 수 없습니다." not in rag_context and "저장된 문서가 없습니다." not in rag_context:
                # RAG 결과가 있으면 메시지에 추가
                text = f"[참고 문서:\n{rag_context}\n\n사용자 질문: {text}"
        except Exception as e:
            print(f"⚠️ RAG 검색 오류: {e}")
        
        # AI 호출
        print("[DEBUG] Messages before append:", self.messages)
        self.messages.append({"role": "user", "content": text})
        print("[DEBUG] Messages after append:", self.messages)
        
        while True:
            print("[DEBUG] Calling llm.chat_with_tools...")
            response_text, tool_uses = self.llm.chat_with_tools(self.messages)
            print("[DEBUG] llm.chat_with_tools returned:", response_text, tool_uses)
            
            if tool_uses:
                # 툴 실행
                tool_results = []
                for tool_use in tool_uses:
                    # Anthropic은 객체, Ollama는 dict로 반환됨
                    if hasattr(tool_use, 'id'):
                        tool_id = tool_use.id
                        tool_name = tool_use.name
                        tool_input = tool_use.input
                    else:
                        tool_id = tool_use.get('id')
                        tool_name = tool_use.get('name')
                        tool_input = tool_use.get('input')
                    
                    print(f"[툴 실행 중: {tool_name}]")
                    
                    # 툴 실행
                    tool_result = self.tool_executor.execute_tool(tool_name, tool_input)
                    print(f"[툴 결과: {tool_name} 완료]")
                    tool_results.append(tool_result)
                
                # Ollama는 content에 툴 결과를 텍스트로 넣어줌
                if Config.LLM_PROVIDER == "ollama":
                    self.messages.append({
                        "role": "user",
                        "content": f"다음 툴을 실행했습니다: {json.dumps([{'tool': t[0], 'result': t[1]} for t in zip([tu.get('name', '') for tu in tool_uses], tool_results)], ensure_ascii=False)}"
                    })
                else:
                    # Anthropic용
                    assistant_content_objects = []
                    tool_use_ids = []
                    for tool_use in tool_uses:
                        if hasattr(tool_use, 'id'):
                            tool_id = tool_use.id
                            tool_name = tool_use.name
                            tool_input = tool_use.input
                        else:
                            tool_id = tool_use.get('id')
                            tool_name = tool_use.get('name')
                            tool_input = tool_use.get('input')
                        
                        assistant_content_objects.append({
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": tool_input,
                        })
                        tool_use_ids.append(tool_id)
                    
                    self.messages.append({"role": "assistant", "content": assistant_content_objects})
                    
                    for i, tool_use in enumerate(tool_uses):
                        if hasattr(tool_use, 'id'):
                            tool_id = tool_use.id
                        else:
                            tool_id = tool_use.get('id')
                        
                        self.messages.append({
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_id,
                                    "content": tool_results[i],
                                }
                            ],
                        })
            else:
                # 최종 응답
                # 메인 스레드에서 UI 업데이트하도록 QTimer.singleShot 사용
                QTimer.singleShot(0, lambda: self._on_ai_response(response_text))
                break
    
    def _on_ai_response(self, response_text: str):
        print("[DEBUG] _on_ai_response called with:", response_text)
        # 이모지 제거 (Windows cp949 문제)
        import re
        response_text = re.sub(r'[^\w\s가-힣.,!?]', '', response_text)
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
        
        # 대화 저장
        self.memory.save_message(self.session_id, "user", self.messages[-2]["content"])
        self.memory.save_message(self.session_id, "assistant", response_text)
        
        # 자동으로 음성 응답 (RESPONDING 상태로)
        print(f"[DEBUG] TTS 스레드 시작 전, self.last_response: {self.last_response}")
        self.state_machine.start_responding()  # RESPONDING 상태로 변경
        thread = threading.Thread(target=lambda: self._speak_with_check(self.last_response), daemon=True)
        thread.start()
        print("[DEBUG] TTS 스레드 시작됨")
    
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
        # 지속적인 음성 감지에서 텍스트가 감지되면 호출
        QTimer.singleShot(0, lambda: self._on_user_input(text))
    
    def _speak_with_check(self, text: str):
        print(f"[DEBUG] _speak_with_check 호출됨: {text}")
        try:
            print(f"[DEBUG] tool_executor.speak_text 호출 전")
            result = self.tool_executor.speak_text(text, self.audio_processor)
            print(f"[DEBUG] tool_executor.speak_text 반환값: {result}")
            if "오류:" in result:
                print(f"⚠️ TTS 오류: {result}")
                print("💡 pyttsx3를 설치하세요: pip install pyttsx3")
        except Exception as e:
            print(f"⚠️ TTS 오류: {e}")
            import traceback
            traceback.print_exc()
            print("💡 pyttsx3를 설치하세요: pip install pyttsx3")
        finally:
            # TTS가 끝나면 IDLE 상태로 돌아가고 사운드바 리셋
            QTimer.singleShot(0, lambda: self._reset_all())
    
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
        if not self.window.isVisible():
            self.window.show()
        if not self.window.isActiveWindow():
            self.window.raise_()
    
    def _on_close_requested(self):
        # 종료 버튼 클릭시 프로그램 자체 종료
        self.app.quit()
    
    def run(self):
        return self.app.exec()

if __name__ == "__main__":
    # Windows 권한 요청
    request_windows_permissions()
    
    # 데이터 폴더 생성
    if not os.path.exists("data"):
        os.makedirs("data")
    
    jarvis = JarvisApp()
    sys.exit(jarvis.run())
