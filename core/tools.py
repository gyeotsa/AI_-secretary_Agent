import os
import subprocess
import tempfile
from typing import Optional
from config import Config
from core.harness import SafetyLayer
from core.user_profile import get_user_profile
from core.rag import get_rag_manager
from core.scheduler import get_scheduler_manager
from core.hardware import get_hardware_manager
from core.multimodal import get_multimodal_manager

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    import whisper
    import sounddevice as sd
    import numpy as np
    import scipy.io.wavfile as wav
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False


class ToolExecutor:
    def __init__(self):
        self.safety = SafetyLayer()
        self.user_profile = get_user_profile()
        self.rag_manager = get_rag_manager()
        self.scheduler_manager = get_scheduler_manager()
        self.hardware_manager = get_hardware_manager()
        self.multimodal_manager = get_multimodal_manager()
        # TTS engine
        self._tts_engine = None

    def read_file(self, path: str) -> str:
        is_valid, error_msg = self.safety.validate_path(path)
        if not is_valid:
            return f"오류: {error_msg}"

        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            return f"파일 읽기 오류: {str(e)}"

    def write_file(self, path: str, content: str) -> str:
        is_valid, error_msg = self.safety.validate_path(path)
        if not is_valid:
            return f"오류: {error_msg}"

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"파일이 성공적으로 저장되었습니다: {path}"
        except Exception as e:
            return f"파일 쓰기 오류: {str(e)}"

    def list_directory(self, path: str) -> str:
        is_valid, error_msg = self.safety.validate_path(path)
        if not is_valid:
            return f"오류: {error_msg}"

        try:
            items = os.listdir(path)
            result = []
            for item in items:
                item_path = os.path.join(path, item)
                is_dir = os.path.isdir(item_path)
                prefix = "[폴더] " if is_dir else "[파일] "
                result.append(prefix + item)
            return "\n".join(result)
        except Exception as e:
            return f"디렉토리 목록 오류: {str(e)}"

    def run_command(self, command: str) -> str:
        is_valid, error_msg = self.safety.validate_command(command)
        if not is_valid:
            return f"오류: {error_msg}"

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = []
            if result.stdout:
                output.append(f"출력:\n{result.stdout}")
            if result.stderr:
                output.append(f"에러:\n{result.stderr}")
            output.append(f"종료 코드: {result.returncode}")
            return "\n".join(output)
        except Exception as e:
            return f"명령어 실행 오류: {str(e)}"

    def web_search(self, query: str, num_results: int = 5) -> str:
        if DDGS is None:
            return "오류: duckduckgo-search가 설치되지 않았습니다. requirements.txt를 확인하세요."

        try:
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=num_results):
                    results.append(f"제목: {r.get('title', '')}")
                    results.append(f"링크: {r.get('href', '')}")
                    results.append(f"요약: {r.get('body', '')}")
                    results.append("-" * 50)
            return "\n".join(results)
        except Exception as e:
            return f"웹 검색 오류: {str(e)}"

    def set_profile(self, key: str, value: str) -> str:
        try:
            self.user_profile.set(key, value)
            return f"프로필이 저장되었습니다: {key} = {value}"
        except Exception as e:
            return f"프로필 저장 오류: {str(e)}"

    def get_profile(self, key: str = "") -> str:
        try:
            if key:
                value = self.user_profile.get(key)
                return f"{key}: {value}" if value else f"{key}가 프로필에 없습니다."
            else:
                return self.user_profile.get_profile_summary()
        except Exception as e:
            return f"프로필 조회 오류: {str(e)}"

    def set_preference(self, pref_key: str, pref_value: str) -> str:
        try:
            self.user_profile.set_preference(pref_key, pref_value)
            return f"환경설정이 저장되었습니다: {pref_key} = {pref_value}"
        except Exception as e:
            return f"환경설정 저장 오류: {str(e)}"

    def speak_text(self, text: str) -> str:
        if pyttsx3 is None:
            return "오류: pyttsx3가 설치되지 않았습니다. requirements.txt를 확인하세요."
        
        try:
            # TTS engine이 초기화되지 않았다면 초기화
            if self._tts_engine is None:
                self._tts_engine = pyttsx3.init()
                # 한국어 음성 설정 (가능한 경우)
                voices = self._tts_engine.getProperty('voices')
                for voice in voices:
                    if 'ko' in str(voice.languages).lower() or 'korean' in voice.name.lower():
                        self._tts_engine.setProperty('voice', voice.id)
                        break
            
            self._tts_engine.say(text)
            self._tts_engine.runAndWait()
            return f"음성으로 읽어주었습니다: {text}"
        except Exception as e:
            # 오류 발생시 engine 재초기화
            self._tts_engine = None
            return f"TTS 오류: {str(e)}"

    def listen(self, duration: int = 3) -> str:
        if not WHISPER_AVAILABLE:
            return "오류: openai-whisper, sounddevice, scipy, numpy가 설치되지 않았습니다. requirements.txt를 확인하세요."
        
        try:
            print(f"🎤 {duration}초 동안 말씀하세요...")
            sample_rate = 16000
            recording = sd.rec(
                int(duration * sample_rate), 
                samplerate=sample_rate,
                channels=1, 
                dtype='int16'
            )
            sd.wait()
            
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav.write(f.name, sample_rate, recording)
                model = whisper.load_model("base")
                result = model.transcribe(f.name, language="ko")
                os.unlink(f.name)
                text = result["text"].strip()
                if text:
                    return f"음성 인식 결과: {text}"
                else:
                    return "음성이 인식되지 않았습니다."
        except Exception as e:
            return f"STT 오류: {str(e)}"

    def add_document(self, file_path: str) -> str:
        is_valid, error_msg = self.safety.validate_path(file_path)
        if not is_valid:
            return f"오류: {error_msg}"
        return self.rag_manager.add_document(file_path)

    def search_docs(self, query: str, top_k: int = 3) -> str:
        return self.rag_manager.search_docs(query, top_k)

    def list_documents(self) -> str:
        return self.rag_manager.list_documents()

    def add_schedule_job(self, description: str, schedule_type: str, schedule_value: str, prompt: str) -> str:
        return self.scheduler_manager.add_job(description, schedule_type, schedule_value, prompt)

    def list_schedule_jobs(self) -> str:
        return self.scheduler_manager.list_jobs()

    def delete_schedule_job(self, job_id: int) -> str:
        return self.scheduler_manager.delete_job(job_id)

    def start_scheduler(self) -> str:
        return self.scheduler_manager.start_scheduler()

    def stop_scheduler(self) -> str:
        return self.scheduler_manager.stop_scheduler()

    def start_wakeword_detection(self) -> str:
        return self.hardware_manager.start_wakeword_detection()

    def stop_wakeword_detection(self) -> str:
        return self.hardware_manager.stop_wakeword_detection()

    def start_clap_detection(self) -> str:
        return self.hardware_manager.start_clap_detection()

    def stop_clap_detection(self) -> str:
        return self.hardware_manager.stop_clap_detection()

    def analyze_image(self, image_path: str, prompt: str = "이 이미지에 무엇이 있나요?") -> str:
        return self.multimodal_manager.analyze_image(image_path, prompt)

    def extract_text_from_pdf(self, pdf_path: str, page_num: Optional[int] = None) -> str:
        return self.multimodal_manager.extract_text_from_pdf(pdf_path, page_num)

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        tool_functions = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "list_directory": self.list_directory,
            "run_command": self.run_command,
            "web_search": self.web_search,
            "set_profile": self.set_profile,
            "get_profile": self.get_profile,
            "set_preference": self.set_preference,
            "speak_text": self.speak_text,
            "listen": self.listen,
            "add_document": self.add_document,
            "search_docs": self.search_docs,
            "list_documents": self.list_documents,
            "add_schedule_job": self.add_schedule_job,
            "list_schedule_jobs": self.list_schedule_jobs,
            "delete_schedule_job": self.delete_schedule_job,
            "start_scheduler": self.start_scheduler,
            "stop_scheduler": self.stop_scheduler,
            "start_wakeword_detection": self.start_wakeword_detection,
            "stop_wakeword_detection": self.stop_wakeword_detection,
            "start_clap_detection": self.start_clap_detection,
            "stop_clap_detection": self.stop_clap_detection,
            "analyze_image": self.analyze_image,
            "extract_text_from_pdf": self.extract_text_from_pdf,
        }

        if tool_name not in tool_functions:
            return f"오류: 알 수 없는 툴 '{tool_name}'"

        try:
            return tool_functions[tool_name](**tool_input)
        except TypeError as e:
            return f"툴 파라미터 오류: {str(e)}"


def get_tools_schema() -> list[dict]:
    return [
        {
            "name": "read_file",
            "description": "파일의 내용을 읽어옵니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "읽을 파일의 경로",
                    }
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "파일에 내용을 씁니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "쓸 파일의 경로",
                    },
                    "content": {
                        "type": "string",
                        "description": "파일에 쓸 내용",
                    },
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "list_directory",
            "description": "디렉토리의 파일과 폴더 목록을 보여줍니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "목록을 볼 디렉토리 경로",
                    }
                },
                "required": ["path"],
            },
        },
        {
            "name": "run_command",
            "description": "시스템 명령어를 실행합니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "실행할 명령어",
                    }
                },
                "required": ["command"],
            },
        },
        {
            "name": "web_search",
            "description": "웹에서 정보를 검색합니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색 쿼리",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "검색 결과 수 (기본 5개)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "set_profile",
            "description": "사용자 프로필에 정보를 저장합니다 (예: 이름, 취미, 선호도 등)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "프로필 키 (예: '이름', '취미', '선호_언어')",
                    },
                    "value": {
                        "type": "string",
                        "description": "프로필 값",
                    },
                },
                "required": ["key", "value"],
            },
        },
        {
            "name": "get_profile",
            "description": "사용자 프로필 정보를 조회합니다. 키를 지정하지 않으면 전체 프로필을 보여줍니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "조회할 프로필 키 (선택사항)",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "set_preference",
            "description": "사용자 환경설정을 저장합니다 (예: 응답_스타일, 언어 등)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pref_key": {
                        "type": "string",
                        "description": "환경설정 키",
                    },
                    "pref_value": {
                        "type": "string",
                        "description": "환경설정 값",
                    },
                },
                "required": ["pref_key", "pref_value"],
            },
        },
        {
            "name": "speak_text",
            "description": "텍스트를 음성으로 읽어줍니다 (TTS)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "음성으로 읽을 텍스트",
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "listen",
            "description": "마이크에서 음성을 녹음하고 텍스트로 변환합니다 (STT)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "duration": {
                        "type": "integer",
                        "description": "녹음 시간 (초, 기본 3초)",
                        "default": 3,
                    },
                },
                "required": [],
            },
        },
        {
            "name": "add_document",
            "description": "문서를 RAG 지식 베이스에 추가합니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "추가할 문서의 파일 경로",
                    },
                },
                "required": ["file_path"],
            },
        },
        {
            "name": "search_docs",
            "description": "RAG 지식 베이스에서 관련 문서를 검색합니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색 쿼리",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "반환할 결과 개수 (기본 3개)",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "list_documents",
            "description": "RAG 지식 베이스에 저장된 문서 목록을 보여줍니다",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "add_schedule_job",
            "description": "스케줄 작업을 추가합니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "작업 설명",
                    },
                    "schedule_type": {
                        "type": "string",
                        "description": "스케줄 타입: every_minutes, every_hours, every_days, daily_at, every_weeks",
                    },
                    "schedule_value": {
                        "type": "string",
                        "description": "스케줄 값: 숫자 또는 시간(HH:MM)",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "실행할 프롬프트",
                    },
                },
                "required": ["description", "schedule_type", "schedule_value", "prompt"],
            },
        },
        {
            "name": "list_schedule_jobs",
            "description": "등록된 스케줄 작업 목록을 보여줍니다",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "delete_schedule_job",
            "description": "스케줄 작업을 삭제합니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "삭제할 작업 ID",
                    },
                },
                "required": ["job_id"],
            },
        },
        {
            "name": "start_scheduler",
            "description": "스케줄러를 시작합니다",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "stop_scheduler",
            "description": "스케줄러를 중지합니다",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "start_wakeword_detection",
            "description": "웨이크워드('자비스') 감지를 시작합니다",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "stop_wakeword_detection",
            "description": "웨이크워드 감지를 중지합니다",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "start_clap_detection",
            "description": "박수 감지를 시작합니다 (두 번 박수 치면 트리거)",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "stop_clap_detection",
            "description": "박수 감지를 중지합니다",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "analyze_image",
            "description": "이미지를 분석합니다 (메타데이터 확인)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "이미지 파일 경로",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "분석 프롬프트 (선택사항)",
                        "default": "이 이미지에 무엇이 있나요?",
                    },
                },
                "required": ["image_path"],
            },
        },
        {
            "name": "extract_text_from_pdf",
            "description": "PDF에서 텍스트를 추출합니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pdf_path": {
                        "type": "string",
                        "description": "PDF 파일 경로",
                    },
                    "page_num": {
                        "type": "integer",
                        "description": "특정 페이지 번호 (선택사항, 지정하지 않으면 전체)",
                    },
                },
                "required": ["pdf_path"],
            },
        },
    ]


_tools_executor = None


def get_tool_executor() -> ToolExecutor:
    global _tools_executor
    if _tools_executor is None:
        _tools_executor = ToolExecutor()
    return _tools_executor
