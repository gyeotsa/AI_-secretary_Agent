import json
import threading
from types import SimpleNamespace
import pytest

import core.scheduler as scheduler_module
import core.knowledge_graph as knowledge_graph_module
import core.multi_agent as multi_agent_module
import core.multimodal as multimodal_module
import core.tools as tools_module
from core.executor import Executor
from core.scratchpad import Task
from core.tool_result import Evidence
from core.tool_result import ToolRunResult, ToolRunStatus
from core.tools import ToolExecutor
from core.user_profile import UserProfile
from core.verifier import ToolVerifier, VerificationResult


def test_verified_file_result_contains_evidence_and_artifact():
    raw = json.dumps({
        "status": "created",
        "type": "file",
        "path": r"C:\workspace\report.txt",
    })
    result = ToolRunResult.from_verification(
        tool_name="filesystem_create_file",
        raw_output=raw,
        verification=VerificationResult(
            True,
            "실제 파일 확인",
            {"method": "path_exists"},
        ),
        duration_ms=12.5,
    )
    assert result.status == ToolRunStatus.SUCCEEDED
    assert result.succeeded
    assert result.evidence[0].data["method"] == "path_exists"
    assert result.artifacts[0].uri == r"C:\workspace\report.txt"
    assert result.duration_ms == 12.5


def test_failed_verification_cannot_claim_success():
    result = ToolRunResult.from_verification(
        tool_name="filesystem_write_file",
        raw_output="오류: 저장 실패",
        verification=VerificationResult(False, "파일이 변경되지 않았습니다."),
    )
    assert result.status == ToolRunStatus.FAILED
    assert not result.succeeded
    assert result.error == "파일이 변경되지 않았습니다."


def test_web_search_result_exposes_source_artifacts():
    raw = json.dumps({
        "query": "latest",
        "results": [
            {"title": "Official", "url": "https://example.com/product"},
            {"title": "News", "url": "https://example.org/news"},
        ],
    })
    result = ToolRunResult.from_verification(
        tool_name="browser_web_search",
        raw_output=raw,
        verification=VerificationResult(True, "출처 2건 확인"),
    )
    assert [artifact.kind for artifact in result.artifacts] == ["url", "url"]
    assert result.to_dict()["status"] == "succeeded"


def test_unknown_tool_is_unverified_instead_of_success():
    verification = ToolVerifier().verify(
        "plugin_without_verifier",
        {},
        "작업을 완료했습니다.",
    )
    result = ToolRunResult.from_verification(
        tool_name="plugin_without_verifier",
        raw_output="작업을 완료했습니다.",
        verification=verification,
    )
    assert not verification.verified
    assert not verification.success
    assert result.status == ToolRunStatus.UNVERIFIED
    assert not result.succeeded
    assert result.error is None


def test_successful_result_requires_evidence():
    with pytest.raises(ValueError, match="검증 증거"):
        ToolRunResult.successful(
            tool_name="filesystem_create_file",
            raw_output="{}",
            evidence=[],
        )


def test_executor_preserves_direct_typed_result_and_rejects_name_mismatch():
    executor = Executor.__new__(Executor)
    direct = ToolRunResult.successful(
        tool_name="filesystem_create_file",
        raw_output='{"status":"created"}',
        evidence=[Evidence("filesystem_state", "파일 확인")],
    )
    preserved = executor.build_tool_run_result(
        Task("task-1", "파일 생성"),
        "filesystem_create_file",
        {},
        direct,
        9.5,
    )
    assert preserved is direct
    assert preserved.duration_ms == 9.5

    mismatch = executor.build_tool_run_result(
        Task("task-2", "파일 생성"),
        "filesystem_write_file",
        {},
        direct,
    )
    assert mismatch.status == ToolRunStatus.FAILED
    assert "일치하지 않습니다" in mismatch.error


def test_executor_downgrades_direct_success_without_evidence():
    executor = Executor.__new__(Executor)
    unsupported = ToolRunResult(
        tool_name="unsafe_plugin",
        status=ToolRunStatus.SUCCEEDED,
        raw_output="완료",
    )
    result = executor.build_tool_run_result(
        Task("task-3", "미검증 작업"),
        "unsafe_plugin",
        {},
        unsupported,
    )
    assert result.status == ToolRunStatus.UNVERIFIED
    assert not result.succeeded


def test_legacy_file_mutations_return_verified_typed_results(tmp_path):
    executor = ToolExecutor.__new__(ToolExecutor)
    executor._resolve_and_validate_path = lambda path: (True, "", str(path))

    target = tmp_path / "nested" / "note.txt"
    written = executor.write_file(str(target), "안녕")
    assert written.status == ToolRunStatus.SUCCEEDED
    assert written.evidence[0].kind == "file_content"
    assert target.read_text(encoding="utf-8") == "안녕"

    directory = tmp_path / "folder"
    created = executor.create_directory(str(directory))
    assert created.status == ToolRunStatus.SUCCEEDED
    assert directory.is_dir()

    deleted = executor.delete_directory(str(directory))
    assert deleted.status == ToolRunStatus.SUCCEEDED
    assert deleted.evidence[0].kind == "directory_absent"
    assert not directory.exists()


def test_run_command_uses_exit_code_as_success_boundary(monkeypatch):
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.safety = SimpleNamespace(
        validate_command=lambda command: (True, "", ["git", "--version"])
    )

    monkeypatch.setattr(
        "core.tools.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="git version 1.0\n", stderr="", returncode=0
        ),
    )
    succeeded = executor.run_command("git --version")
    assert succeeded.status == ToolRunStatus.SUCCEEDED
    assert succeeded.evidence[0].data["returncode"] == 0

    monkeypatch.setattr(
        "core.tools.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="", stderr="fatal", returncode=2
        ),
    )
    failed = executor.run_command("git --version")
    assert failed.status == ToolRunStatus.FAILED
    assert failed.evidence[0].data["returncode"] == 2


def test_device_queries_and_camera_capture_return_direct_evidence(tmp_path):
    executor = ToolExecutor.__new__(ToolExecutor)
    executor._hardware_manager = SimpleNamespace(
        list_input_devices=lambda: [{
            "index": 3,
            "name": "Test microphone",
            "default_samplerate": 48000.0,
            "max_input_channels": 1,
        }]
    )
    image_path = tmp_path / "capture.jpg"
    image_path.write_bytes(b"jpeg-bytes")
    executor._multimodal_manager = SimpleNamespace(
        list_camera_devices=lambda: [{
            "index": 1, "backend": "TEST", "width": 640, "height": 480,
        }],
        capture_camera_frame_details=lambda save_path: {
            "path": str(image_path),
            "camera_index": 1,
            "backend": "TEST",
            "width": 640,
            "height": 480,
            "size": image_path.stat().st_size,
        },
    )

    microphones = executor.list_audio_input_devices()
    cameras = executor.list_camera_devices()
    capture = executor.capture_camera(str(image_path))

    assert microphones.status == ToolRunStatus.SUCCEEDED
    assert microphones.evidence[0].data["count"] == 1
    assert cameras.status == ToolRunStatus.SUCCEEDED
    assert cameras.evidence[0].data["count"] == 1
    assert capture.status == ToolRunStatus.SUCCEEDED
    assert capture.artifacts[0].uri == str(image_path)


def test_automation_tools_verify_database_and_engine_state(monkeypatch, tmp_path):
    engine = scheduler_module.AutomationEngine.__new__(scheduler_module.AutomationEngine)
    engine.data_dir = str(tmp_path)
    engine.scheduler_db_path = str(tmp_path / "scheduler.db")
    engine.scheduled_jobs = []
    engine.scheduler = scheduler_module.schedule.Scheduler()
    engine.running = False
    engine.scheduler_thread = None
    engine.stop_event = threading.Event()
    engine.job_results = {}
    engine.result_callback = None
    engine._init_db()
    monkeypatch.setattr(scheduler_module, "get_automation_engine", lambda: engine)
    executor = ToolExecutor.__new__(ToolExecutor)

    added = executor.add_automation_job("테스트", "every_minutes", "5", "상태 확인")
    assert added.status == ToolRunStatus.SUCCEEDED
    job_id = added.evidence[0].data["id"]

    listed = executor.list_automation_jobs()
    assert listed.status == ToolRunStatus.SUCCEEDED
    assert listed.evidence[0].data["job_ids"] == [job_id]

    toggled = executor.toggle_automation_job(job_id, False)
    assert toggled.status == ToolRunStatus.SUCCEEDED
    assert toggled.evidence[0].data["enabled"] is False

    missing = executor.toggle_automation_job(9999, True)
    assert missing.status == ToolRunStatus.FAILED

    history = executor.get_job_history(job_id)
    assert history.status == ToolRunStatus.SUCCEEDED
    assert history.evidence[0].data["count"] == 0

    started = executor.start_automation_engine()
    assert started.status == ToolRunStatus.SUCCEEDED
    stopped = executor.stop_automation_engine()
    assert stopped.status == ToolRunStatus.SUCCEEDED

    deleted = executor.delete_automation_job(job_id)
    assert deleted.status == ToolRunStatus.SUCCEEDED
    assert engine.get_job_record(job_id) is None


def test_profile_tools_verify_persisted_values(tmp_path):
    profile = UserProfile.__new__(UserProfile)
    profile.db_path = str(tmp_path / "profile.db")
    profile.init_db()
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.user_profile = profile

    saved = executor.set_profile("nickname", "지휘관")
    preference = executor.set_preference("language", "ko")
    loaded = executor.get_profile("nickname")
    missing = executor.get_profile("missing")

    assert saved.status == ToolRunStatus.SUCCEEDED
    assert saved.evidence[0].data["key"] == "nickname"
    assert preference.status == ToolRunStatus.SUCCEEDED
    assert profile.get_preference("language") == "ko"
    assert loaded.status == ToolRunStatus.SUCCEEDED
    assert str(loaded) == "nickname: 지휘관"
    assert missing.status == ToolRunStatus.FAILED


def test_knowledge_graph_tools_verify_database_state(monkeypatch, tmp_path):
    graph = knowledge_graph_module.KnowledgeGraph(str(tmp_path / "knowledge.db"))
    monkeypatch.setattr(knowledge_graph_module, "get_knowledge_graph", lambda: graph)
    executor = ToolExecutor.__new__(ToolExecutor)

    entity = executor.add_entity("Jarvis", "agent", '{"local": true}')
    triple = executor.add_triple("Jarvis", "uses", "Ollama", "agent", "runtime")
    relations = executor.get_relations("Jarvis")
    subgraph = executor.get_subgraph("Jarvis")
    deleted = executor.delete_entity("Ollama", "runtime")

    assert entity.status == ToolRunStatus.SUCCEEDED
    assert graph.get_entity("Jarvis", "agent") is not None
    assert triple.status == ToolRunStatus.SUCCEEDED
    assert relations.status == ToolRunStatus.SUCCEEDED
    assert relations.evidence[0].data["relation_ids"]
    assert subgraph.status == ToolRunStatus.SUCCEEDED
    assert subgraph.evidence[0].data["entities"] == 2
    assert deleted.status == ToolRunStatus.SUCCEEDED
    assert graph.get_entity("Ollama", "runtime") is None


def test_multi_agent_result_is_unverified_without_final_answer_verifier(monkeypatch):
    tasks = []

    class Orchestrator:
        def __init__(self):
            self.tasks = tasks

        def execute_full_pipeline(self, query):
            self.tasks.extend([
                multi_agent_module.AgentTask("p1", "계획", "planning", "completed"),
                multi_agent_module.AgentTask("e1", "실행", "execution", "completed"),
                multi_agent_module.AgentTask("r1", "검토", "reflection", "completed"),
            ])
            return "최종 답변"

    orchestrator = Orchestrator()
    monkeypatch.setattr(
        multi_agent_module, "get_multi_agent_orchestrator", lambda: orchestrator
    )
    executor = ToolExecutor.__new__(ToolExecutor)

    result = executor.execute_multi_agent("테스트")
    history = executor.get_task_history()

    assert result.status == ToolRunStatus.UNVERIFIED
    assert result.evidence[0].data["task_count"] == 3
    assert history.status == ToolRunStatus.SUCCEEDED
    assert history.evidence[0].data["count"] == 3


def test_scheduler_compatibility_tools_verify_engine_state(tmp_path):
    engine = scheduler_module.AutomationEngine.__new__(scheduler_module.AutomationEngine)
    engine.data_dir = str(tmp_path)
    engine.scheduler_db_path = str(tmp_path / "compat-scheduler.db")
    engine.scheduled_jobs = []
    engine.scheduler = scheduler_module.schedule.Scheduler()
    engine.running = False
    engine.scheduler_thread = None
    engine.stop_event = threading.Event()
    engine.job_results = {}
    engine.result_callback = None
    engine._init_db()
    manager = scheduler_module.SchedulerManager.__new__(scheduler_module.SchedulerManager)
    manager.engine = engine
    executor = ToolExecutor.__new__(ToolExecutor)
    executor._scheduler_manager = manager

    added = executor.add_schedule_job("호환 작업", "every_hours", "1", "점검")
    job_id = added.evidence[0].data["id"]
    assert added.status == ToolRunStatus.SUCCEEDED
    assert executor.list_schedule_jobs().status == ToolRunStatus.SUCCEEDED
    assert executor.start_scheduler().status == ToolRunStatus.SUCCEEDED
    assert executor.stop_scheduler().status == ToolRunStatus.SUCCEEDED
    assert executor.delete_schedule_job(job_id).status == ToolRunStatus.SUCCEEDED
    assert executor.delete_schedule_job(job_id).status == ToolRunStatus.FAILED


def test_detector_start_is_unverified_until_stream_open_and_stop_is_verified():
    class FakeThread:
        def __init__(self):
            self.alive = True

        def is_alive(self):
            return self.alive

    class Hardware:
        def __init__(self):
            self.running = False
            self.wakeword_thread = FakeThread()
            self.clap_thread = FakeThread()

        def start_wakeword_detection(self):
            self.running = True
            self.wakeword_thread.alive = True
            return "started"

        def stop_wakeword_detection(self):
            self.running = False
            self.wakeword_thread.alive = False
            return "stopped"

        def start_clap_detection(self):
            self.running = True
            self.clap_thread.alive = True
            return "started"

        def stop_clap_detection(self):
            self.running = False
            self.clap_thread.alive = False
            return "stopped"

    executor = ToolExecutor.__new__(ToolExecutor)
    executor._hardware_manager = Hardware()

    assert executor.start_wakeword_detection().status == ToolRunStatus.UNVERIFIED
    assert executor.stop_wakeword_detection().status == ToolRunStatus.SUCCEEDED
    assert executor.start_clap_detection().status == ToolRunStatus.UNVERIFIED
    assert executor.stop_clap_detection().status == ToolRunStatus.SUCCEEDED


def test_legacy_web_search_returns_source_artifacts(monkeypatch):
    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def text(self, query, max_results):
            return [{"title": "공식 문서", "href": "https://example.com/docs", "body": "설명"}]

    monkeypatch.setattr(tools_module, "DDGS", FakeDDGS)
    executor = ToolExecutor.__new__(ToolExecutor)
    result = executor.web_search("테스트", 3)

    assert result.status == ToolRunStatus.SUCCEEDED
    assert result.evidence[0].data["source_count"] == 1
    assert result.artifacts[0].uri == "https://example.com/docs"


def test_image_analysis_preserves_input_evidence_but_stays_unverified(tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"png")
    executor = ToolExecutor.__new__(ToolExecutor)
    executor._multimodal_manager = SimpleNamespace(
        analyze_image=lambda path, prompt: "이미지 설명"
    )

    result = executor.analyze_image(str(image), "무엇인가요?")

    assert result.status == ToolRunStatus.UNVERIFIED
    assert result.evidence[0].data["size"] == 3
    assert result.artifacts[0].uri == str(image.resolve())


@pytest.mark.skipif(not multimodal_module.PYMUPDF_AVAILABLE, reason="PyMuPDF 미설치")
def test_pdf_all_page_extraction_and_excel_tools_are_verified(tmp_path):
    pdf_path = tmp_path / "two-pages.pdf"
    document = multimodal_module.fitz.open()
    document.new_page().insert_text((72, 72), "first")
    document.new_page().insert_text((72, 72), "second")
    document.save(pdf_path)
    document.close()

    manager = multimodal_module.MultimodalManager.__new__(multimodal_module.MultimodalManager)
    manager.safety = SimpleNamespace(validate_path=lambda path: (True, ""))
    executor = ToolExecutor.__new__(ToolExecutor)
    executor._multimodal_manager = manager

    extracted = executor.extract_text_from_pdf(str(pdf_path))
    assert extracted.status == ToolRunStatus.SUCCEEDED
    assert extracted.evidence[0].data["selected_pages"] == 2
    assert "first" in str(extracted)
    assert "second" in str(extracted)

    workbook = tmp_path / "book.xlsx"
    created = executor.create_excel_file(str(workbook), [["name", "value"], ["x", 1]])
    updated = executor.write_excel_cell(str(workbook), "Sheet1", "B2", 2)
    assert created.status == ToolRunStatus.SUCCEEDED
    assert updated.status == ToolRunStatus.SUCCEEDED
    assert updated.evidence[0].data["cell"] == "B2"
