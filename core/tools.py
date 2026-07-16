import os
import subprocess
import tempfile
import json
from typing import Optional
from datetime import datetime
from dataclasses import asdict
from config import Config
from core.harness import SafetyLayer
from core.user_profile import get_user_profile
from core.workspace import get_workspace_manager
from core.plugin import get_plugin_registry

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
        self.workspace = get_workspace_manager()
        self.plugin_registry = get_plugin_registry()
        
        # Lazy initialization for optional modules
        self._rag_manager = None
        self._scheduler_manager = None
        self._hardware_manager = None
        self._multimodal_manager = None
        self._project_indexer = None
        
        # TTS engine
        self._tts_engine = None
        
        # Load plugins from plugins directory
        try:
            self.plugin_registry.load_plugins_from_directory()
        except Exception as e:
            print(f"[ToolExecutor] Plugin 로딩 오류: {e}")
    
    @property
    def rag_manager(self):
        if self._rag_manager is None:
            try:
                from core.rag import get_rag_manager
                self._rag_manager = get_rag_manager()
            except Exception as e:
                self._rag_manager = None
        return self._rag_manager
    
    @property
    def scheduler_manager(self):
        if self._scheduler_manager is None:
            try:
                from core.scheduler import get_scheduler_manager
                self._scheduler_manager = get_scheduler_manager()
            except Exception as e:
                self._scheduler_manager = None
        return self._scheduler_manager
    
    @property
    def hardware_manager(self):
        if self._hardware_manager is None:
            try:
                from core.hardware import get_hardware_manager
                self._hardware_manager = get_hardware_manager()
            except Exception as e:
                self._hardware_manager = None
        return self._hardware_manager
    
    @property
    def multimodal_manager(self):
        if self._multimodal_manager is None:
            try:
                from core.multimodal import get_multimodal_manager
                self._multimodal_manager = get_multimodal_manager()
            except Exception as e:
                self._multimodal_manager = None
        return self._multimodal_manager
        
    @property
    def project_indexer(self):
        if self._project_indexer is None:
            try:
                from core.project_indexer import get_project_indexer
                self._project_indexer = get_project_indexer()
            except Exception as e:
                self._project_indexer = None
        return self._project_indexer

    def read_file(self, path: str) -> str:
        # Workspace가 설정되어 있으면 상대경로로 처리
        try:
            if self.workspace.is_set():
                real_path = self.workspace.resolve(path)
                path = str(real_path)
        except Exception as e:
            return f"오류: {e}"
                
        is_valid, error_msg = self.safety.validate_path(path)
        if not is_valid:
            return f"오류: {error_msg}"

        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            return f"파일 읽기 오류: {str(e)}"

    def write_file(self, path: str, content: str) -> str:
        # Workspace가 설정되어 있으면 상대경로로 처리
        try:
            if self.workspace.is_set():
                real_path = self.workspace.resolve(path)
                path = str(real_path)
        except Exception as e:
            return f"오류: {e}"
                
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

    def list_directory(self, path: str = "") -> str:
        # 경로가 비어있고 Workspace가 설정되어 있으면 Workspace 루트
        try:
            if not path and self.workspace.is_set():
                real_path = self.workspace.get_workspace_path()
                if real_path:
                    path = real_path
            elif self.workspace.is_set():
                real_path = self.workspace.resolve(path)
                path = str(real_path)
        except Exception as e:
            return f"오류: {e}"
                
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
            
    # ------------------------------
    # Workspace 관련 도구 추가
    # ------------------------------
    def set_workspace(self, path: str) -> str:
        """작업 공간을 설정합니다."""
        try:
            success = self.workspace.set_workspace(path)
            if success:
                info = self.workspace.get_info()
                return f"Workspace가 설정되었습니다: {info.name} (파일: {info.file_count}개)"
            else:
                return f"Workspace 설정 실패: 유효하지 않은 경로입니다: {path}"
        except Exception as e:
            return f"Workspace 설정 오류: {str(e)}"
            
    def get_workspace_info(self) -> str:
        """현재 Workspace 정보를 반환합니다."""
        if not self.workspace.is_set():
            info = self.workspace.get_info()
            return (f"Workspace: {info.name}\n"
                   f"경로: {info.path}\n"
                   f"파일 개수: {info.file_count}")
        else:
            return "Workspace가 설정되지 않았습니다."
            
    def get_workspace_tree(self) -> str:
        """Workspace의 파일 트리를 반환합니다."""
        if not self.workspace.is_set():
            return "Workspace가 설정되지 않았습니다."
            
        try:
            import json
            tree = self.workspace.get_file_tree()
            return json.dumps(tree, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"트리 가져오기 오류: {str(e)}"
            
    # ------------------------------
    # Project Indexer 관련 도구 추가
    # ------------------------------
    def index_project(self) -> str:
        """현재 Workspace나 프로젝트 루트를 인덱싱합니다."""
        if self.project_indexer is None:
            return "오류: Project Indexer를 초기화할 수 없습니다."
            
        try:
            if not self.project_indexer.project_root and self.workspace.is_set():
                self.project_indexer.set_project_root(self.workspace.current_workspace)
                
            if not self.project_indexer.project_root:
                return "오류: 프로젝트 루트가 설정되지 않았습니다. 먼저 Workspace를 설정하세요."
                
            count = self.project_indexer.index_project()
            return f"프로젝트 인덱싱 완료: {count}개 파일"
        except Exception as e:
            return f"프로젝트 인덱싱 오류: {str(e)}"
            
    def search_files(self, query: str, search_type: str = "name") -> str:
        """파일을 검색합니다 (이름, 내용, 확장자)."""
        if self.project_indexer is None:
            return "오류: Project Indexer를 초기화할 수 없습니다."
            
        try:
            results = self.project_indexer.search_files(query, search_type)
            if not results:
                return "검색 결과가 없습니다."
                
            import json
            return json.dumps(results, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"파일 검색 오류: {str(e)}"
            
    def search_symbols(self, query: str, symbol_type: Optional[str] = None) -> str:
        """코드 심볼을 검색합니다 (함수, 클래스, 변수)."""
        if self.project_indexer is None:
            return "오류: Project Indexer를 초기화할 수 없습니다."
            
        try:
            results = self.project_indexer.search_symbols(query, symbol_type)
            if not results:
                return "검색 결과가 없습니다."
                
            import json
            return json.dumps(results, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"심볼 검색 오류: {str(e)}"
            
    def get_file_info(self, path: str) -> str:
        """파일의 상세 정보를 반환합니다."""
        if self.project_indexer is None:
            return "오류: Project Indexer를 초기화할 수 없습니다."
            
        try:
            file_info = self.project_indexer.get_file_info(path)
            if not file_info:
                return f"파일 정보를 찾을 수 없습니다: {path}"
                
            import json
            return json.dumps({
                "path": file_info.path,
                "name": file_info.name,
                "extension": file_info.extension,
                "size": file_info.size,
                "modified_at": datetime.fromtimestamp(file_info.modified_at).isoformat() if file_info.modified_at else "",
                "is_text": file_info.is_text,
                "content_preview": file_info.content_preview,
                "symbols": file_info.symbols
            }, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"파일 정보 가져오기 오류: {str(e)}"
            
    def get_project_tree(self) -> str:
        """프로젝트의 파일 트리 구조를 반환합니다."""
        if self.project_indexer is None:
            return "오류: Project Indexer를 초기화할 수 없습니다."
            
        try:
            tree = self.project_indexer.get_file_tree()
            import json
            return json.dumps(tree, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"프로젝트 트리 가져오기 오류: {str(e)}"
            
    # ------------------------------
    # Memory 관련 도구 추가
    # ------------------------------
    def add_semantic_memory(self, key: str, content: str, category: str = "기타") -> str:
        """시맨틱 메모리 추가"""
        try:
            from core.memory import get_semantic_memory, SemanticMemory
            semantic_manager = get_semantic_memory()
            memory = SemanticMemory(
                key=key,
                content=content,
                category=category
            )
            semantic_manager.add_memory(memory)
            return f"시맨틱 메모리가 추가되었습니다: {key}"
        except Exception as e:
            return f"시맨틱 메모리 추가 오류: {str(e)}"
            
    def get_semantic_memory(self, key: str) -> str:
        """시맨틱 메모리 조회"""
        try:
            from core.memory import get_semantic_memory
            semantic_manager = get_semantic_memory()
            memory = semantic_manager.get_memory(key)
            if memory:
                return f"{memory.key} ({memory.category}): {memory.content}"
            else:
                return f"메모리를 찾을 수 없습니다: {key}"
        except Exception as e:
            return f"시맨틱 메모리 조회 오류: {str(e)}"
            
    def search_semantic_memory(self, query: str, category: Optional[str] = None) -> str:
        """시맨틱 메모리 검색"""
        try:
            from core.memory import get_semantic_memory
            semantic_manager = get_semantic_memory()
            memories = semantic_manager.search_memories(query, category=category)
            if not memories:
                return "검색 결과가 없습니다."
            result = []
            for mem in memories:
                result.append(f"- [{mem.category}] {mem.key}: {mem.content}")
            return "\n".join(result)
        except Exception as e:
            return f"시맨틱 메모리 검색 오류: {str(e)}"
            
    def delete_semantic_memory(self, key: str) -> str:
        """시맨틱 메모리 삭제"""
        try:
            from core.memory import get_semantic_memory
            semantic_manager = get_semantic_memory()
            deleted = semantic_manager.delete_memory(key)
            if deleted:
                return f"메모리가 삭제되었습니다: {key}"
            else:
                return f"메모리를 찾을 수 없습니다: {key}"
        except Exception as e:
            return f"시맨틱 메모리 삭제 오류: {str(e)}"
            
    # ------------------------------
    # Automation Engine 관련 도구 추가
    # ------------------------------
    def add_automation_job(self, description: str, schedule_type: str, schedule_value: str, prompt: str) -> str:
        """자동화 작업 추가"""
        try:
            from core.scheduler import get_automation_engine
            engine = get_automation_engine()
            return engine.add_job(description, schedule_type, schedule_value, prompt)
        except Exception as e:
            return f"자동화 작업 추가 오류: {str(e)}"
            
    def list_automation_jobs(self) -> str:
        """자동화 작업 목록 보기"""
        try:
            from core.scheduler import get_automation_engine
            engine = get_automation_engine()
            return engine.list_jobs()
        except Exception as e:
            return f"자동화 작업 목록 오류: {str(e)}"
            
    def get_job_history(self, job_id: int, limit: int = 10) -> str:
        """자동화 작업 실행 기록 보기"""
        try:
            from core.scheduler import get_automation_engine
            engine = get_automation_engine()
            return engine.get_job_history(job_id, limit)
        except Exception as e:
            return f"실행 기록 조회 오류: {str(e)}"
            
    def toggle_automation_job(self, job_id: int, enabled: bool) -> str:
        """자동화 작업 활성화/비활성화"""
        try:
            from core.scheduler import get_automation_engine
            engine = get_automation_engine()
            return engine.toggle_job(job_id, enabled)
        except Exception as e:
            return f"작업 상태 변경 오류: {str(e)}"
            
    def delete_automation_job(self, job_id: int) -> str:
        """자동화 작업 삭제"""
        try:
            from core.scheduler import get_automation_engine
            engine = get_automation_engine()
            return engine.delete_job(job_id)
        except Exception as e:
            return f"자동화 작업 삭제 오류: {str(e)}"
            
    def start_automation_engine(self) -> str:
        """자동화 엔진 시작"""
        try:
            from core.scheduler import get_automation_engine
            engine = get_automation_engine()
            return engine.start()
        except Exception as e:
            return f"자동화 엔진 시작 오류: {str(e)}"
            
    def stop_automation_engine(self) -> str:
        """자동화 엔진 중지"""
        try:
            from core.scheduler import get_automation_engine
            engine = get_automation_engine()
            return engine.stop()
        except Exception as e:
            return f"자동화 엔진 중지 오류: {str(e)}"
            
    def is_automation_engine_running(self) -> str:
        """자동화 엔진 실행 여부"""
        try:
            from core.scheduler import get_automation_engine
            engine = get_automation_engine()
            return "실행 중" if engine.is_running() else "중지됨"
        except Exception as e:
            return f"상태 확인 오류: {str(e)}"
            
    # ------------------------------
    # Knowledge Graph 관련 도구 추가
    # ------------------------------
    def add_entity(self, name: str, entity_type: str, metadata: Optional[str] = None) -> str:
        """지식 그래프에 엔티티 추가"""
        try:
            from core.knowledge_graph import get_knowledge_graph, Entity
            kg = get_knowledge_graph()
            meta_dict = json.loads(metadata) if metadata else {}
            entity = Entity(name=name, entity_type=entity_type, metadata=meta_dict)
            entity_id = kg.add_entity(entity)
            return f"✅ 엔티티가 추가되었습니다 (ID: {entity_id}): {name} ({entity_type})"
        except Exception as e:
            return f"엔티티 추가 오류: {str(e)}"
            
    def get_entity(self, name: str, entity_type: Optional[str] = None) -> str:
        """지식 그래프에서 엔티티 조회"""
        try:
            from core.knowledge_graph import get_knowledge_graph
            kg = get_knowledge_graph()
            entity = kg.get_entity(name, entity_type)
            if not entity:
                return f"엔티티를 찾을 수 없습니다: {name}"
            return json.dumps(entity.to_dict(), indent=2, ensure_ascii=False)
        except Exception as e:
            return f"엔티티 조회 오류: {str(e)}"
            
    def search_entities(self, query: str, entity_type: Optional[str] = None, limit: int = 20) -> str:
        """지식 그래프에서 엔티티 검색"""
        try:
            from core.knowledge_graph import get_knowledge_graph
            kg = get_knowledge_graph()
            entities = kg.search_entities(query, entity_type, limit)
            if not entities:
                return "검색 결과가 없습니다."
            result = [f"📋 검색 결과 ({len(entities)}건):"]
            for entity in entities:
                result.append(f"\n- {entity.name} ({entity.entity_type})")
                if entity.metadata:
                    result.append(f"  메타데이터: {json.dumps(entity.metadata, ensure_ascii=False)}")
            return "\n".join(result)
        except Exception as e:
            return f"엔티티 검색 오류: {str(e)}"
            
    def delete_entity(self, name: str, entity_type: Optional[str] = None) -> str:
        """지식 그래프에서 엔티티 삭제"""
        try:
            from core.knowledge_graph import get_knowledge_graph
            kg = get_knowledge_graph()
            deleted = kg.delete_entity(name, entity_type)
            if deleted:
                return f"✅ 엔티티가 삭제되었습니다: {name}"
            else:
                return f"엔티티를 찾을 수 없습니다: {name}"
        except Exception as e:
            return f"엔티티 삭제 오류: {str(e)}"
            
    def add_triple(self, subject: str, predicate: str, object_: str, 
                   subject_type: str = "thing", object_type: str = "thing",
                   metadata: Optional[str] = None) -> str:
        """지식 그래프에 트리플 추가"""
        try:
            from core.knowledge_graph import get_knowledge_graph
            kg = get_knowledge_graph()
            meta_dict = json.loads(metadata) if metadata else {}
            relation_id = kg.add_triple(subject, predicate, object_, subject_type, object_type, meta_dict)
            return f"✅ 트리플이 추가되었습니다 (ID: {relation_id}): ({subject}) -[{predicate}]-> ({object_})"
        except Exception as e:
            return f"트리플 추가 오류: {str(e)}"
            
    def get_relations(self, entity_name: str, direction: str = "both") -> str:
        """엔티티의 관계 조회"""
        try:
            from core.knowledge_graph import get_knowledge_graph
            kg = get_knowledge_graph()
            relations = kg.get_relations(entity_name, direction)
            if not relations:
                return f"엔티티 {entity_name}의 관계가 없습니다."
            result = [f"🔗 {entity_name}의 관계 ({len(relations)}건):"]
            for rel in relations:
                if rel.from_entity == entity_name:
                    result.append(f"\n- → {rel.relation_type} → {rel.to_entity}")
                else:
                    result.append(f"\n- ← {rel.relation_type} ← {rel.from_entity}")
                if rel.metadata:
                    result.append(f"  메타데이터: {json.dumps(rel.metadata, ensure_ascii=False)}")
            return "\n".join(result)
        except Exception as e:
            return f"관계 조회 오류: {str(e)}"
            
    def get_subgraph(self, entity_name: str, depth: int = 2) -> str:
        """서브그래프 조회"""
        try:
            from core.knowledge_graph import get_knowledge_graph
            kg = get_knowledge_graph()
            subgraph = kg.get_subgraph(entity_name, depth)
            return json.dumps(subgraph, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"서브그래프 조회 오류: {str(e)}"
            
    # ------------------------------
    # Multi-Agent 관련 도구 추가
    # ------------------------------
    def execute_multi_agent(self, query: str) -> str:
        """멀티 에이전트 파이프라인 실행 (계획 → 실행 → 반성)"""
        try:
            from core.multi_agent import get_multi_agent_orchestrator
            orchestrator = get_multi_agent_orchestrator()
            result = orchestrator.execute_full_pipeline(query)
            return result
        except Exception as e:
            return f"멀티 에이전트 실행 오류: {str(e)}"
            
    def get_task_history(self) -> str:
        """멀티 에이전트 작업 히스토리 조회"""
        try:
            from core.multi_agent import get_multi_agent_orchestrator
            orchestrator = get_multi_agent_orchestrator()
            tasks = [asdict(task) for task in orchestrator.tasks]
            return json.dumps(tasks, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"작업 히스토리 조회 오류: {str(e)}"

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

    def speak_text(self, text: str, audio_processor=None) -> str:
        print(f"[DEBUG] ToolExecutor.speak_text 호출됨: {text}")
        if pyttsx3 is None:
            print("[DEBUG] pyttsx3 is None")
            return "오류: pyttsx3가 설치되지 않았습니다. requirements.txt를 확인하세요."
        
        # 빈 문자열이나 공백만 있을 때 처리
        if not text or text.strip() == "":
            text = "네, 보스."
        
        temp_wav_path = None
        engine = None
        try:
            # 매번 새로운 TTS engine 생성 (상태 꼬임 방지)
            print("[DEBUG] pyttsx3.init() 호출 전")
            engine = pyttsx3.init()
            print("[DEBUG] pyttsx3.init() 호출 성공")
            
            # 한국어 음성 설정 (가능한 경우)
            voices = engine.getProperty('voices')
            print(f"[DEBUG] voices 개수: {len(voices)}")
            korean_voice_id = None
            for voice in voices:
                if 'ko' in str(voice.languages).lower() or 'korean' in voice.name.lower():
                    korean_voice_id = voice.id
                    print(f"[DEBUG] 한국어 음성 설정: {voice.name}")
                    break
            
            if korean_voice_id:
                engine.setProperty('voice', korean_voice_id)
            
            # 1. TTS를 WAV 파일로 저장
            temp_wav_path = tempfile.mktemp(suffix=".wav")
            print(f"[DEBUG] TTS WAV 저장 경로: {temp_wav_path}")
            engine.save_to_file(text, temp_wav_path)
            engine.runAndWait()
            
            # 2. AudioProcessor로 WAV 파일 재생 + 분석
            if audio_processor:
                audio_processor.play_and_analyze_tts(temp_wav_path)
            else:
                # AudioProcessor가 없으면 그냥 재생
                engine.say(text)
                engine.runAndWait()
            
            print("[DEBUG] TTS 처리 완료")
            return f"음성으로 읽어주었습니다: {text}"
        except Exception as e:
            print(f"[DEBUG] TTS 오류 발생: {e}")
            import traceback
            traceback.print_exc()
            return f"TTS 오류: {str(e)}"
        finally:
            # engine 정리
            if engine:
                try:
                    engine.stop()
                except Exception:
                    pass
            
            # 임시 WAV 파일 삭제
            if temp_wav_path and os.path.exists(temp_wav_path):
                try:
                    os.unlink(temp_wav_path)
                except Exception:
                    pass

    def listen(self, duration: int = 3) -> str:
        if not WHISPER_AVAILABLE:
            return "오류: openai-whisper, sounddevice, scipy, numpy가 설치되지 않았습니다. requirements.txt를 확인하세요."
        
        temp_file_path = None
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
            
            # 임시 파일 생성
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                temp_file_path = f.name
                wav.write(temp_file_path, sample_rate, recording)
            
            # Whisper로 음성 인식
            model = whisper.load_model("base")
            result = model.transcribe(temp_file_path, language="ko")
            text = result["text"].strip()
            
            if text:
                return f"음성 인식 결과: {text}"
            else:
                return "음성이 인식되지 않았습니다."
        except FileNotFoundError as e:
            # FFmpeg가 설치되지 않았는지 확인
            ffmpeg_available = True
            try:
                import subprocess
                subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                ffmpeg_available = False
            
            if not ffmpeg_available:
                return "STT 오류: FFmpeg가 설치되지 않았습니다. Windows에서는 https://ffmpeg.org/download.html에서 다운로드한 뒤 PATH에 추가해주세요. (또는 'winget install ffmpeg'로 설치)"
            return f"STT 오류: {str(e)}"
        except Exception as e:
            return f"STT 오류: {str(e)}"
        finally:
            # 임시 파일 삭제
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except Exception:
                    # 파일 삭제 오류는 무시
                    pass

    def add_document(self, file_path: str) -> str:
        if self.rag_manager is None:
            return "오류: RAG 기능을 초기화할 수 없습니다."
        is_valid, error_msg = self.safety.validate_path(file_path)
        if not is_valid:
            return f"오류: {error_msg}"
        return self.rag_manager.add_document(file_path)

    def search_docs(self, query: str, top_k: int = 3) -> str:
        if self.rag_manager is None:
            return "오류: RAG 기능을 초기화할 수 없습니다."
        return self.rag_manager.search_docs(query, top_k)

    def list_documents(self) -> str:
        if self.rag_manager is None:
            return "오류: RAG 기능을 초기화할 수 없습니다."
        return self.rag_manager.list_documents()

    def add_schedule_job(self, description: str, schedule_type: str, schedule_value: str, prompt: str) -> str:
        if self.scheduler_manager is None:
            return "오류: 스케줄러 기능을 사용하려면 schedule를 설치하세요."
        return self.scheduler_manager.add_job(description, schedule_type, schedule_value, prompt)

    def list_schedule_jobs(self) -> str:
        if self.scheduler_manager is None:
            return "오류: 스케줄러 기능을 사용하려면 schedule를 설치하세요."
        return self.scheduler_manager.list_jobs()

    def delete_schedule_job(self, job_id: int) -> str:
        if self.scheduler_manager is None:
            return "오류: 스케줄러 기능을 사용하려면 schedule를 설치하세요."
        return self.scheduler_manager.delete_job(job_id)

    def start_scheduler(self) -> str:
        if self.scheduler_manager is None:
            return "오류: 스케줄러 기능을 사용하려면 schedule를 설치하세요."
        return self.scheduler_manager.start_scheduler()

    def stop_scheduler(self) -> str:
        if self.scheduler_manager is None:
            return "오류: 스케줄러 기능을 사용하려면 schedule를 설치하세요."
        return self.scheduler_manager.stop_scheduler()

    def start_wakeword_detection(self) -> str:
        if self.hardware_manager is None:
            return "오류: 하드웨어 기능을 사용하려면 sounddevice, whisper, librosa를 설치하세요."
        return self.hardware_manager.start_wakeword_detection()

    def stop_wakeword_detection(self) -> str:
        if self.hardware_manager is None:
            return "오류: 하드웨어 기능을 사용하려면 sounddevice, whisper, librosa를 설치하세요."
        return self.hardware_manager.stop_wakeword_detection()

    def start_clap_detection(self) -> str:
        if self.hardware_manager is None:
            return "오류: 하드웨어 기능을 사용하려면 sounddevice, whisper, librosa를 설치하세요."
        return self.hardware_manager.start_clap_detection()

    def stop_clap_detection(self) -> str:
        if self.hardware_manager is None:
            return "오류: 하드웨어 기능을 사용하려면 sounddevice, whisper, librosa를 설치하세요."
        return self.hardware_manager.stop_clap_detection()

    def analyze_image(self, image_path: str, prompt: str = "이 이미지에 무엇이 있나요?") -> str:
        if self.multimodal_manager is None:
            return "오류: 멀티모달 기능을 사용하려면 Pillow, PyMuPDF를 설치하세요."
        return self.multimodal_manager.analyze_image(image_path, prompt)

    def extract_text_from_pdf(self, pdf_path: str, page_num: Optional[int] = None) -> str:
        if self.multimodal_manager is None:
            return "오류: 멀티모달 기능을 사용하려면 Pillow, PyMuPDF를 설치하세요."
        return self.multimodal_manager.extract_text_from_pdf(pdf_path, page_num)
    
    def create_excel_file(self, file_path: str, data: Optional[list] = None) -> str:
        """
        엑셀 파일을 생성합니다.
        data: 2차원 리스트 (예: [["이름", "나이"], ["철수", 30], ["영희", 25]])
        """
        try:
            # openpyxl이 설치된지 확인
            try:
                import openpyxl
            except ImportError:
                return "오류: openpyxl 라이브러리가 설치되지 않았습니다. 'pip install openpyxl'로 설치하세요."
            
            # 새 워크북 생성
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Sheet1"
            
            # 데이터 입력
            if data:
                for row in data:
                    ws.append(row)
            
            # 파일 저장
            wb.save(file_path)
            return f"사장님, 엑셀 파일이 성공적으로 생성되었습니다: {file_path}"
            
        except Exception as e:
            return f"사장님, 엑셀 파일 생성 중 오류가 발생했습니다: {str(e)}"
    
    def write_excel_cell(self, file_path: str, sheet_name: str, cell: str, value: str) -> str:
        """
        엑셀 파일의 특정 셀에 값을 씁니다.
        """
        try:
            try:
                import openpyxl
            except ImportError:
                return "오류: openpyxl 라이브러리가 설치되지 않았습니다."
            
            if not os.path.exists(file_path):
                return f"사장님, 파일을 찾을 수 없습니다: {file_path}"
            
            wb = openpyxl.load_workbook(file_path)
            
            if sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
            else:
                ws = wb.create_sheet(sheet_name)
            
            ws[cell] = value
            wb.save(file_path)
            return f"사장님, 셀 {sheet_name}!{cell}에 값이 성공적으로 입력되었습니다."
            
        except Exception as e:
            return f"사장님, 셀 쓰기 중 오류가 발생했습니다: {str(e)}"
    
    def create_directory(self, dir_path: str) -> str:
        """폴더를 생성합니다."""
        try:
            is_valid, error_msg = self.safety.validate_path(dir_path)
            if not is_valid:
                return f"오류: {error_msg}"
            
            os.makedirs(dir_path, exist_ok=True)
            return f"사장님, 폴더가 성공적으로 생성되었습니다: {dir_path}"
        except Exception as e:
            return f"사장님, 폴더 생성 중 오류가 발생했습니다: {str(e)}"
    
    def delete_directory(self, dir_path: str) -> str:
        """폴더를 삭제합니다 (주의: 내용물도 함께 삭제됨)."""
        try:
            is_valid, error_msg = self.safety.validate_path(dir_path)
            if not is_valid:
                return f"오류: {error_msg}"
            
            if not os.path.exists(dir_path):
                return f"사장님, 폴더를 찾을 수 없습니다: {dir_path}"
            
            import shutil
            shutil.rmtree(dir_path)
            return f"사장님, 폴더가 성공적으로 삭제되었습니다: {dir_path}"
        except Exception as e:
            return f"사장님, 폴더 삭제 중 오류가 발생했습니다: {str(e)}"

    def capture_camera(self, save_path: Optional[str] = None) -> str:
        """카메라에서 프레임을 캡처합니다."""
        try:
            result = self.multimodal_manager.capture_camera_frame(save_path)
            return result
        except Exception as e:
            return f"카메라 캡처 오류: {str(e)}"

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
            "create_excel_file": self.create_excel_file,
            "write_excel_cell": self.write_excel_cell,
            "create_directory": self.create_directory,
            "delete_directory": self.delete_directory,
            "capture_camera": self.capture_camera,
            # Workspace 관련 도구 추가
            "set_workspace": self.set_workspace,
            "get_workspace_info": self.get_workspace_info,
            "get_workspace_tree": self.get_workspace_tree,
            # Project Indexer 관련 도구 추가
            "index_project": self.index_project,
            "search_files": self.search_files,
            "search_symbols": self.search_symbols,
            "get_file_info": self.get_file_info,
            "get_project_tree": self.get_project_tree,
            "add_semantic_memory": self.add_semantic_memory,
            "get_semantic_memory": self.get_semantic_memory,
            "search_semantic_memory": self.search_semantic_memory,
            "delete_semantic_memory": self.delete_semantic_memory,
            "add_automation_job": self.add_automation_job,
            "list_automation_jobs": self.list_automation_jobs,
            "get_job_history": self.get_job_history,
            "toggle_automation_job": self.toggle_automation_job,
            "delete_automation_job": self.delete_automation_job,
            "start_automation_engine": self.start_automation_engine,
            "stop_automation_engine": self.stop_automation_engine,
            "is_automation_engine_running": self.is_automation_engine_running,
            "add_entity": self.add_entity,
            "get_entity": self.get_entity,
            "search_entities": self.search_entities,
            "delete_entity": self.delete_entity,
            "add_triple": self.add_triple,
            "get_relations": self.get_relations,
            "get_subgraph": self.get_subgraph,
            "execute_multi_agent": self.execute_multi_agent,
            "get_task_history": self.get_task_history,
        }

        if tool_name in tool_functions:
            try:
                return tool_functions[tool_name](**tool_input)
            except TypeError as e:
                return f"툴 파라미터 오류: {str(e)}"
        else:
            # Try plugin tools
            try:
                return self.plugin_registry.execute_tool(tool_name, tool_input)
            except Exception as e:
                return f"오류: 알 수 없는 툴 '{tool_name}' (플러그인 오류: {str(e)})"


def get_tools_schema() -> list[dict]:
    schema = [
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
        {
            "name": "create_excel_file",
            "description": "엑셀 파일을 생성합니다. data 매개변수로 2차원 리스트를 전달하면 데이터를 입력할 수 있습니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "생성할 엑셀 파일 경로 (예: C:/Users/ice31/Desktop/test.xlsx)",
                    },
                    "data": {
                        "type": "array",
                        "description": "2차원 리스트 데이터 (예: [['이름', '나이'], ['철수', 30]])",
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            }
                        }
                    }
                },
                "required": ["file_path"],
            },
        },
        {
            "name": "write_excel_cell",
            "description": "엑셀 파일의 특정 셀에 값을 씁니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "엑셀 파일 경로",
                    },
                    "sheet_name": {
                        "type": "string",
                        "description": "시트 이름",
                    },
                    "cell": {
                        "type": "string",
                        "description": "셀 위치 (예: A1, B2)",
                    },
                    "value": {
                        "type": "string",
                        "description": "입력할 값",
                    }
                },
                "required": ["file_path", "sheet_name", "cell", "value"],
            },
        },
        {
            "name": "create_directory",
            "description": "폴더를 생성합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "dir_path": {
                        "type": "string",
                        "description": "생성할 폴더 경로",
                    }
                },
                "required": ["dir_path"],
            },
        },
        {
            "name": "delete_directory",
            "description": "폴더를 삭제합니다 (주의: 폴더 내부 파일도 함께 삭제됨).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "dir_path": {
                        "type": "string",
                        "description": "삭제할 폴더 경로",
                    }
                },
                "required": ["dir_path"],
            },
        },
        {
            "name": "capture_camera",
            "description": "카메라에서 프레임을 캡처합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "save_path": {
                        "type": "string",
                        "description": "저장할 경로 (선택사항, 지정하지 않으면 임시 파일에 저장)",
                    },
                },
                "required": [],
            },
        },
        # ------------------------------
        # Workspace 관련 도구 스키마
        # ------------------------------
        {
            "name": "set_workspace",
            "description": "작업 공간을 설정합니다. 설정된 경로 내에서만 파일 작업이 가능합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "작업 공간으로 사용할 폴더 경로",
                    }
                },
                "required": ["path"],
            },
        },
        {
            "name": "get_workspace_info",
            "description": "현재 설정된 Workspace의 정보를 반환합니다.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "get_workspace_tree",
            "description": "Workspace의 파일 트리 구조를 반환합니다.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        # ------------------------------
        # Project Indexer 관련 도구 스키마
        # ------------------------------
        {
            "name": "index_project",
            "description": "현재 Workspace나 프로젝트 루트의 파일들을 인덱싱합니다.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "search_files",
            "description": "프로젝트에서 파일을 검색합니다. search_type으로 name, content, extension을 선택할 수 있습니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색 쿼리",
                    },
                    "search_type": {
                        "type": "string",
                        "description": "검색 타입: name, content, extension (기본 name)",
                        "default": "name",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "search_symbols",
            "description": "코드에서 심볼을 검색합니다 (함수, 클래스, 메서드 등).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색 쿼리",
                    },
                    "symbol_type": {
                        "type": "string",
                        "description": "심볼 타입: function, class, method, variable (선택사항)",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_file_info",
            "description": "파일의 상세 정보를 반환합니다 (크기, 수정일, 내용 미리보기, 심볼 등).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "파일 경로",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "get_project_tree",
            "description": "프로젝트의 파일 트리 구조를 반환합니다.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        # ------------------------------
        # Memory 관련 도구 스키마
        # ------------------------------
        {
            "name": "add_semantic_memory",
            "description": "시맨틱 메모리를 추가합니다. 사용자 프로필, 프로젝트 정보, 기술 지식 등을 저장할 수 있습니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "메모리 키 (검색용)",
                    },
                    "content": {
                        "type": "string",
                        "description": "메모리 내용",
                    },
                    "category": {
                        "type": "string",
                        "description": "카테고리 (예: 사용자_프로필, 프로젝트_정보, 기술_지식, 기타)",
                        "default": "기타",
                    },
                },
                "required": ["key", "content"],
            },
        },
        {
            "name": "get_semantic_memory",
            "description": "키로 시맨틱 메모리를 조회합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "메모리 키",
                    },
                },
                "required": ["key"],
            },
        },
        {
            "name": "search_semantic_memory",
            "description": "시맨틱 메모리를 검색합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색 쿼리",
                    },
                    "category": {
                        "type": "string",
                        "description": "카테고리 (선택사항)",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "delete_semantic_memory",
            "description": "시맨틱 메모리를 삭제합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "메모리 키",
                    },
                },
                "required": ["key"],
            },
        },
        # ------------------------------
        # Automation Engine 관련 도구 스키마
        # ------------------------------
        {
            "name": "add_automation_job",
            "description": "자동화 작업을 추가합니다. 스케줄 기반으로 LLM 작업을 자동 실행합니다.",
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
            "name": "list_automation_jobs",
            "description": "자동화 작업 목록을 보여줍니다.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "get_job_history",
            "description": "자동화 작업의 실행 기록을 보여줍니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "작업 ID",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "최근 몇 건을 보여줄지 (기본 10)",
                        "default": 10,
                    },
                },
                "required": ["job_id"],
            },
        },
        {
            "name": "toggle_automation_job",
            "description": "자동화 작업을 활성화하거나 비활성화합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "작업 ID",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "활성화 여부 (true/false)",
                    },
                },
                "required": ["job_id", "enabled"],
            },
        },
        {
            "name": "delete_automation_job",
            "description": "자동화 작업을 삭제합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "작업 ID",
                    },
                },
                "required": ["job_id"],
            },
        },
        {
            "name": "start_automation_engine",
            "description": "자동화 엔진을 시작합니다.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "stop_automation_engine",
            "description": "자동화 엔진을 중지합니다.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "is_automation_engine_running",
            "description": "자동화 엔진이 실행 중인지 확인합니다.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        # ------------------------------
        # Knowledge Graph 관련 도구 스키마
        # ------------------------------
        {
            "name": "add_entity",
            "description": "지식 그래프에 엔티티를 추가합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "엔티티 이름",
                    },
                    "entity_type": {
                        "type": "string",
                        "description": "엔티티 타입",
                    },
                    "metadata": {
                        "type": "string",
                        "description": "메타데이터 (JSON 문자열, 선택사항)",
                    },
                },
                "required": ["name", "entity_type"],
            },
        },
        {
            "name": "get_entity",
            "description": "지식 그래프에서 엔티티를 조회합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "엔티티 이름",
                    },
                    "entity_type": {
                        "type": "string",
                        "description": "엔티티 타입 (선택사항)",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "search_entities",
            "description": "지식 그래프에서 엔티티를 검색합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색 쿼리",
                    },
                    "entity_type": {
                        "type": "string",
                        "description": "엔티티 타입 (선택사항)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "최대 결과 수 (기본 20)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "delete_entity",
            "description": "지식 그래프에서 엔티티를 삭제합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "엔티티 이름",
                    },
                    "entity_type": {
                        "type": "string",
                        "description": "엔티티 타입 (선택사항)",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "add_triple",
            "description": "지식 그래프에 트리플(주어-술어-목적어)을 추가합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "주어 엔티티",
                    },
                    "predicate": {
                        "type": "string",
                        "description": "술어(관계 타입)",
                    },
                    "object_": {
                        "type": "string",
                        "description": "목적어 엔티티",
                    },
                    "subject_type": {
                        "type": "string",
                        "description": "주어 엔티티 타입 (기본: thing)",
                        "default": "thing",
                    },
                    "object_type": {
                        "type": "string",
                        "description": "목적어 엔티티 타입 (기본: thing)",
                        "default": "thing",
                    },
                    "metadata": {
                        "type": "string",
                        "description": "메타데이터 (JSON 문자열, 선택사항)",
                    },
                },
                "required": ["subject", "predicate", "object_"],
            },
        },
        {
            "name": "get_relations",
            "description": "엔티티의 관계를 조회합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "entity_name": {
                        "type": "string",
                        "description": "엔티티 이름",
                    },
                    "direction": {
                        "type": "string",
                        "description": "관계 방향: from, to, both (기본: both)",
                        "default": "both",
                    },
                },
                "required": ["entity_name"],
            },
        },
        {
            "name": "get_subgraph",
            "description": "엔티티를 중심으로 서브그래프를 조회합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "entity_name": {
                        "type": "string",
                        "description": "엔티티 이름",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "탐색 깊이 (기본: 2)",
                        "default": 2,
                    },
                },
                "required": ["entity_name"],
            },
        },
        # ------------------------------
        # Multi-Agent 관련 도구 스키마
        # ------------------------------
        {
            "name": "execute_multi_agent",
            "description": "멀티 에이전트 파이프라인을 실행합니다 (계획 → 실행 → 반성).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "사용자 쿼리",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_task_history",
            "description": "멀티 에이전트 작업 히스토리를 조회합니다.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    ]
    # Add plugin tools
    try:
        plugin_registry = get_plugin_registry()
        for tool in plugin_registry.get_all_tools():
            schema.append({
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            })
    except Exception as e:
        print(f"[get_tools_schema] Plugin 스키마 로딩 오류: {e}")
    return schema


_tools_executor = None


def get_tool_executor() -> ToolExecutor:
    global _tools_executor
    if _tools_executor is None:
        _tools_executor = ToolExecutor()
    return _tools_executor
