import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QMimeData, QPointF, QUrl
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QApplication
from PIL import Image

from core.model_registry import ModelRegistry, ModelRoleRouter
from core.specialist_workspaces import get_specialist_workspace_registry
from plugins.photoshop import PhotoshopPlugin
from ui.specialist_workspaces import (ImageDropList, MockupWorkspaceWindow,
                                      SpecialistWorkspaceWindow, ZoomableImageView)
from core.specialist_team import SpecialistTeamRuntime
from core.executor import ExecutionOutcome
from core.harness import SafetyLayer
from core.tool_result import Artifact, Evidence, ToolRunResult
from ui.knowledge_graph_workspace import KnowledgeGraphWindow, NativeGraphView
from ui.main_window import JarvisMainWindow


def test_natural_commands_route_to_declared_specialist_workspace():
    registry = get_specialist_workspace_registry()
    assert registry.match_open_command("문서 작업모드 실행해줘").key == "document"
    assert registry.match_open_command("포토샵 전문가 작업공간 열어줘").key == "photoshop"
    assert registry.match_open_command("지식 그래프 열어줘").key == "knowledge_graph"
    assert registry.match_open_command("그래프 뷰 실행해줘").key == "knowledge_graph"
    assert registry.match_open_command("포토샵에서 사진을 보정해줘") is None


def test_photoshop_has_separate_multimodal_model_role():
    profile = ModelRegistry().resolve("photoshop")
    assert profile.role == "image_editing"
    assert "image" in profile.modalities
    assert ModelRoleRouter().route(allowed_tools=["photoshop_open_document"], modalities=["image"]) == "image_editing"


def test_photoshop_plugin_contract_is_discoverable_without_photoshop_installed():
    plugin = PhotoshopPlugin()
    names = {tool.name for tool in plugin.get_tools()}
    assert names == {"photoshop_status", "photoshop_open_document", "photoshop_active_document"}
    assert {intent.name for intent in plugin.get_intents()} == {"photoshop.status", "photoshop.open"}


def test_document_workspace_constructs_and_accepts_agent_results():
    app = QApplication.instance() or QApplication([])
    spec = get_specialist_workspace_registry().get("document")
    window = SpecialistWorkspaceWindow(spec)
    window.show_result("Word 문서 검증 완료")
    assert "Word 문서 검증 완료" in window.results.toPlainText()
    window.close()
    assert app is not None


def test_main_window_opens_both_specialist_workspaces():
    app = QApplication.instance() or QApplication([])
    main = JarvisMainWindow()
    document = main.open_specialist_workspace("document")
    photoshop = main.open_specialist_workspace("photoshop")
    assert set(main.specialist_windows) == {"document", "photoshop"}
    assert document.spec.model_role == "document"
    assert photoshop.spec.model_role == "image_editing"
    assert document.parent() is None and photoshop.parent() is None
    assert not document.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert not photoshop.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    photoshop.close(); document.close(); main.close()


def test_main_window_opens_native_knowledge_graph_workspace():
    app = QApplication.instance() or QApplication([])
    main = JarvisMainWindow()
    graph = main.open_specialist_workspace("knowledge_graph")
    assert isinstance(graph, KnowledgeGraphWindow)
    assert not graph.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert "전체" in graph.stats.text()
    graph.close(); main.close()


def test_native_knowledge_graph_uses_live_force_layout_and_tracks_edges():
    app = QApplication.instance() or QApplication([])

    class Owner:
        def show_note(self, _path):
            pass

        def open_note(self, _path):
            pass

    view = NativeGraphView(Owner())
    view.set_graph({
        "nodes": [
            {"id": "a", "relative_path": "a.md", "label": "A", "type": "fact", "importance": .8, "degree": 1},
            {"id": "b", "relative_path": "b.md", "label": "B", "type": "project", "importance": .6, "degree": 1},
        ],
        "edges": [{"source": "a", "target": "b"}],
    })
    view.physics_timer.stop()
    before = {node_id: item.pos() for node_id, item in view.nodes.items()}
    for _ in range(8):
        view._physics_step()
    assert any(view.nodes[node_id].pos() != point for node_id, point in before.items())

    view.nodes["a"].setPos(321, 123)
    line = view.edges[0].line()
    assert (line.x1(), line.y1()) == (321, 123)
    view.set_motion_enabled(False)
    assert not view.physics_timer.isActive()
    assert view.nodes["a"].flags() & view.nodes["a"].GraphicsItemFlag.ItemIsMovable
    view.close()
    assert app is not None


def test_knowledge_graph_motion_controls_pause_and_restart():
    app = QApplication.instance() or QApplication([])
    graph = KnowledgeGraphWindow()
    graph.motion_toggle.setChecked(True)
    assert not graph.graph_view.motion_enabled
    assert "재개" in graph.motion_toggle.text()
    graph.motion_toggle.setChecked(False)
    assert graph.graph_view.motion_enabled
    graph.graph_view.physics_timer.stop()
    graph.close()
    assert app is not None


def test_mockup_workspace_exposes_zoom_sketch_font_and_drop_guidance_controls():
    app = QApplication.instance() or QApplication([])
    spec = get_specialist_workspace_registry().get("mockup")

    class Runtime:
        def list_profiles(self): return []
        def generation_status(self): return {"ready": False, "base_ready": False,
                                              "adapter_ready": False, "cuda": False}

    window = MockupWorkspaceWindow(spec, runtime=Runtime())
    assert window.preview._zoom == 1.0
    assert window.sketch_canvas is not None
    assert window.font_family is not None and window.font_size.value() == 64
    assert window.edit_attachments.acceptDrops()
    window.sketch_canvas.image.setPixelColor(10, 10, QColor("#000000"))
    sketch_path = window.sketch_canvas.save_guidance()
    assert Path(sketch_path).is_file()
    assert SafetyLayer.validate_path(sketch_path)[0]
    window.close(); assert app is not None


def test_mockup_preview_defaults_to_centered_fit_and_supports_drag_pan():
    app = QApplication.instance() or QApplication([])
    view = ZoomableImageView(); view.resize(500, 360); view.show(); app.processEvents()
    view.set_preview(QPixmap(1600, 1200)); app.processEvents()
    assert view._fit_to_view
    assert view._zoom < 1.0
    assert view.label.width() <= view.viewport().width()
    assert view.label.height() <= view.viewport().height()
    assert view.alignment() & Qt.AlignmentFlag.AlignCenter

    view.zoom_by(3); app.processEvents()
    view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum() // 2)
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum() // 2)
    start = (view.horizontalScrollBar().value(), view.verticalScrollBar().value())

    class MouseEvent:
        def __init__(self, point, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton):
            self._point, self._button, self._buttons = QPointF(*point), button, buttons
        def position(self): return self._point
        def button(self): return self._button
        def buttons(self): return self._buttons
        def accept(self): pass

    view.mousePressEvent(MouseEvent((180, 140), Qt.MouseButton.LeftButton,
                                    Qt.MouseButton.LeftButton))
    view.mouseMoveEvent(MouseEvent((130, 100), buttons=Qt.MouseButton.LeftButton))
    assert (view.horizontalScrollBar().value(), view.verticalScrollBar().value()) != start
    view.mouseReleaseEvent(MouseEvent((130, 100), Qt.MouseButton.LeftButton))
    view.close()


def test_edit_reference_drop_box_accepts_local_image_urls(tmp_path):
    app = QApplication.instance() or QApplication([])
    image = tmp_path / "reference.png"; QPixmap(20, 20).save(str(image), "PNG")
    mime = QMimeData(); mime.setUrls([QUrl.fromLocalFile(str(image))])
    dropped = []
    widget = ImageDropList(); widget.paths_dropped.connect(dropped.extend)

    class DropEvent:
        def mimeData(self): return mime
        def acceptProposedAction(self): self.accepted = True
        accepted = False

    event = DropEvent(); widget.dropEvent(event)
    assert event.accepted
    assert dropped == [str(image.resolve())]
    widget.close(); assert app is not None


def test_specialist_team_scopes_recall_and_stores_only_compact_success():
    class Rag:
        namespace = "global"
        def __init__(self): self.added = []
        def set_namespace(self, value): self.namespace = value
        def search_docs(self, query, top_k=5, metadata_filter=None):
            return [{"content": f"memory:{query}", "approved": True}]
        def add_text_document(self, text, **kwargs): self.added.append((text, kwargs)); return kwargs["doc_id"]

    rag = Rag(); team = SpecialistTeamRuntime(rag, namespace_provider=lambda: "project-a")
    context = team.recall("mockup", "문구 습관")
    assert "memory:문구 습관" in context.as_prompt()
    assert rag.namespace == "global"
    team.remember_success("mockup", instruction="문구를 아래로", result={"renderer": "v5"}, approved=True)
    assert rag.added[0][1]["namespace"] == "project-a:specialist:mockup"
    assert "output" not in rag.added[0][0]


def test_specialist_workspace_contract_declares_tools_artifacts_and_acceptance():
    spec = get_specialist_workspace_registry().get("coding")
    contract = spec.execution_contract()
    assert contract["required_tools"] == ["git", "filesystem", "coding", "system_tools"]
    assert "file" in contract["artifact_types"]
    assert contract["acceptance_criteria"]
    assert "required_tools_registered" in contract["readiness_checks"]
    assert spec.tools == spec.required_tools


def test_specialist_recall_excludes_drafts_and_does_not_store_unapproved():
    class Rag:
        namespace = "global"
        def __init__(self): self.added = []
        def set_namespace(self, value): self.namespace = value
        def search_docs(self, query, top_k=5, metadata_filter=None):
            return [
                {"content": "승인됨", "approved": True},
                {"content": "초안", "approved": False},
            ]
        def add_text_document(self, text, **kwargs): self.added.append(text); return "id"

    rag = Rag(); team = SpecialistTeamRuntime(rag)
    assert team.recall("document", "보고서").as_prompt() == "- 승인됨"
    assert team.remember_success(
        "document", instruction="초안", result={"renderer": "draft"}, approved=False,
    ) == ""
    assert rag.added == []


def test_specialist_reviewer_requires_real_contract_artifact_and_evidence(tmp_path):
    team = SpecialistTeamRuntime()
    contract = {
        "artifact_types": ["document", "file"],
        "acceptance_criteria": ["문서 저장 검증"],
        "readiness": {"resolved_tools": ["word_create_document"]},
    }
    fake = {
        "status": "completed", "tool_status": "succeeded",
        "tool_name": "word_create_document", "completed_steps": 1,
        "evidence_count": 99, "artifact_count": 99,
        "evidence": [], "artifacts": [{"kind": "document", "uri": str(tmp_path / "missing.docx")}],
    }
    rejected = team._review_execution(fake, contract)
    assert not rejected["passed"]
    assert "실제 검증 근거" in rejected["reason"]
    assert "실제 산출물" in rejected["reason"]

    output = tmp_path / "report.docx"; output.write_bytes(b"verified")
    verified = {
        **fake,
        "evidence": [{"kind": "file_content", "summary": "저장 후 해시 확인", "data": {}}],
        "artifacts": [{"kind": "document", "uri": str(output), "metadata": {}}],
    }
    accepted = team._review_execution(verified, contract)
    assert accepted["passed"]
    assert accepted["accepted_artifacts"][0]["uri"] == str(output)


def test_specialist_reviewer_rejects_empty_word_document_evidence(tmp_path):
    output = tmp_path / "empty.docx"
    output.write_bytes(b"docx-shell")
    contract = {
        "artifact_types": ["document"],
        "acceptance_criteria": ["요청한 내용이 저장된 Word 문서"],
        "readiness": {"resolved_tools": ["word_create_document"]},
    }
    payload = {
        "status": "completed",
        "tool_status": "succeeded",
        "tool_name": "word_create_document",
        "tool_names": ["word_create_document"],
        "evidence": [{
            "kind": "docx_structure",
            "summary": "저장된 Word 문서를 다시 열었습니다.",
            "data": {"paragraphs": 0, "paragraph_texts": []},
        }],
        "artifacts": [{"kind": "document", "uri": str(output)}],
    }

    reviewed = SpecialistTeamRuntime._review_execution(payload, contract)

    assert not reviewed["passed"]
    assert "빈 문서" in reviewed["reason"]


def test_specialist_reviewer_rejects_zero_byte_artifact_shell(tmp_path):
    output = tmp_path / "empty-result.bin"
    output.touch()
    contract = {
        "artifact_types": ["file"],
        "acceptance_criteria": ["실제 파일이 생성되어야 한다"],
        "readiness": {"resolved_tools": ["tool_a"]},
    }
    payload = {
        "status": "completed",
        "tool_status": "succeeded",
        "tool_name": "tool_a",
        "tool_names": ["tool_a"],
        "evidence": [{
            "kind": "file_hash", "summary": "파일 경로를 확인했습니다.", "data": {},
        }],
        "artifacts": [{"kind": "file", "uri": str(output)}],
    }

    reviewed = SpecialistTeamRuntime._review_execution(payload, contract)

    assert not reviewed["passed"]
    assert "실제 산출물" in reviewed["reason"]


def test_specialist_reviewer_rejects_corrupt_image_and_pdf_payloads(tmp_path):
    corrupt_image = tmp_path / "result.png"
    corrupt_pdf = tmp_path / "result.pdf"
    corrupt_image.write_bytes(b"not-an-image")
    corrupt_pdf.write_bytes(b"not-a-pdf")

    assert not SpecialistTeamRuntime._artifact_exists({
        "kind": "image", "uri": str(corrupt_image),
    })
    assert not SpecialistTeamRuntime._artifact_exists({
        "kind": "pdf", "uri": str(corrupt_pdf),
    })


def test_specialist_artifact_validation_accepts_decodable_image_and_pdf_envelope(tmp_path):
    image_path = tmp_path / "result.png"
    pdf_path = tmp_path / "result.pdf"
    Image.new("RGB", (4, 4), "white").save(image_path)
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    assert SpecialistTeamRuntime._artifact_exists({
        "kind": "image", "uri": str(image_path),
    })
    assert SpecialistTeamRuntime._artifact_exists({
        "kind": "pdf", "uri": str(pdf_path),
    })


def test_specialist_planning_failure_is_visible_degraded(monkeypatch):
    class BrokenClient:
        profile = None
        def chat_structured(self, messages, json_schema=None): return "JSON이 아닌 응답"

    monkeypatch.setattr("core.llm.get_llm_client", lambda _role: BrokenClient())
    plan = SpecialistTeamRuntime._build_specialist_plan(
        SpecialistTeamRuntime.TEAM_BLUEPRINTS["document"][1],
        "보고서를 작성해줘", "관련 기억 없음", [],
        {"acceptance_criteria": ["문서 검증"],
         "readiness": {"resolved_tools": ["word_create_document"]}},
    )
    assert plan["planning_status"] == "degraded"
    assert plan["planning_error"] == "구조화된 요구사항을 반환하지 않았습니다."


def test_specialist_plan_rejects_incomplete_structured_json(monkeypatch):
    class IncompleteClient:
        profile = None
        def chat_structured(self, messages, json_schema=None):
            return '{"goal":"보고서 작성","acceptance":"문자열이면 안 됨"}'

    monkeypatch.setattr("core.llm.get_llm_client", lambda _role: IncompleteClient())
    plan = SpecialistTeamRuntime._build_specialist_plan(
        SpecialistTeamRuntime.TEAM_BLUEPRINTS["document"][1],
        "보고서를 작성해줘", "관련 기억 없음", [],
        {"acceptance_criteria": ["문서 검증"], "readiness": {"resolved_tools": []}},
    )
    assert plan["planning_status"] == "degraded"
    assert "planning_error" in plan


def test_specialist_plan_normalizes_schema_near_local_model_output(monkeypatch):
    class LocalClient:
        profile = None

        def chat_structured(self, messages, json_schema=None):
            assert json_schema["properties"]["inputs"]["items"] == {"type": "string"}
            return {
                "goal": "검증 문서 작성",
                "inputs": [{"value": "제목과 본문"}],
                "constraints": [{"description": "사용자 원문 보존"}],
                "acceptance": [{"criterion": "저장 후 본문 재확인"}],
                "tools": [{"name": "word_create_document"}],
            }

    monkeypatch.setattr("core.llm.get_llm_client", lambda _role: LocalClient())
    plan = SpecialistTeamRuntime._build_specialist_plan(
        SpecialistTeamRuntime.TEAM_BLUEPRINTS["document"][1],
        "검증 문서를 작성해줘", "관련 기억 없음", [],
        {"acceptance_criteria": ["문서 검증"],
         "readiness": {"resolved_tools": ["word_create_document"]}},
    )

    assert plan["planning_status"] == "ready"
    assert plan["inputs"] == ["제목과 본문"]
    assert plan["constraints"] == ["사용자 원문 보존"]
    assert plan["acceptance"] == ["저장 후 본문 재확인"]
    assert plan["tools"] == ["word_create_document"]


def test_workspace_readiness_records_each_check_and_blocks_missing_executor():
    team = SpecialistTeamRuntime(tool_name_provider=lambda: ("word_create_document",))
    contract = team._resolve_workspace_contract({
        "required_tools": ["word"],
        "readiness_checks": ["required_tools_registered", "executor_available", "verified_evidence"],
    }, executor_available=False)
    assert contract["readiness"]["checks"] == {
        "required_tools_registered": True,
        "executor_available": False,
        "verified_evidence": False,
    }
    assert contract["readiness"]["ready"] is False
    assert contract["readiness"]["failed_checks"] == ["executor_available"]


def test_workspace_readiness_exposes_tool_registry_failure():
    def broken_registry():
        raise RuntimeError("registry unavailable")

    team = SpecialistTeamRuntime(tool_name_provider=broken_registry)
    contract = team._resolve_workspace_contract({
        "required_tools": ["word"],
        "readiness_checks": ["required_tools_registered"],
    })
    assert contract["readiness"]["ready"] is False
    assert contract["readiness"]["registry_error"] == "RuntimeError: registry unavailable"
    assert contract["readiness"]["failed_checks"] == ["required_tools_registered"]


def test_team_run_records_workspace_contract_and_degraded_plan(tmp_path, monkeypatch):
    output = tmp_path / "report.docx"; output.write_bytes(b"document")
    available = (
        "word_create_document", "excel_create_workbook", "hwpx_create_document",
        "powerpoint_create_presentation", "pdf_create_document",
    )
    team = SpecialistTeamRuntime(tool_name_provider=lambda: available)
    monkeypatch.setattr(team, "_build_specialist_plan", lambda *args, **kwargs: {
        "goal": "보고서 작성", "acceptance": ["문서 검증"],
        "planning_status": "degraded", "planning_error": "invalid JSON",
    })

    def invoke(_prompt):
        result = ToolRunResult.successful(
            tool_name="word_create_document", raw_output=str(output),
            evidence=[Evidence("file_content", "저장 후 파일 해시를 확인했습니다.")],
            artifacts=[Artifact("document", str(output))],
        )
        return ExecutionOutcome(
            "문서를 저장하고 검증했습니다.", status="completed",
            tool_result=result, completed_steps=1,
        )

    _, run = team.execute_workspace_request(
        "document", "보고서를 작성해줘", invoke_executor=invoke,
    )
    assert run.status == "degraded"
    assert run.workspace_contract["required_tools"]
    assert run.artifacts["execution_contract_result"]["review"]["passed"] is False
    assert any(event["status"] == "degraded" for event in run.events)


def test_team_run_completes_only_when_plan_and_contract_evidence_pass(tmp_path, monkeypatch):
    output = tmp_path / "report.docx"; output.write_bytes(b"document")
    available = (
        "word_create_document", "excel_create_workbook", "hwpx_create_document",
        "powerpoint_create_presentation", "pdf_create_document",
    )
    team = SpecialistTeamRuntime(tool_name_provider=lambda: available)
    monkeypatch.setattr(team, "_build_specialist_plan", lambda *args, **kwargs: {
        "goal": "보고서 작성", "inputs": [], "constraints": [],
        "acceptance": ["저장 후 검증"], "tools": ["word_create_document"],
        "planning_status": "ready",
    })

    def invoke(_prompt):
        result = ToolRunResult.successful(
            tool_name="word_create_document", raw_output=str(output),
            evidence=[Evidence("file_hash", "저장된 파일의 해시를 확인했습니다.")],
            artifacts=[Artifact("document", str(output))],
        )
        return ExecutionOutcome(
            "문서를 저장하고 검증했습니다.", status="completed",
            tool_result=result, completed_steps=1,
        )

    _, run = team.execute_workspace_request(
        "document", "보고서를 작성해줘", invoke_executor=invoke,
    )
    review = run.artifacts["execution_contract_result"]["review"]
    assert run.status == "completed"
    assert review["passed"] is True
    assert all(item["verified"] for item in review["criteria_results"])
    assert review["readiness_checks"]["verified_evidence"] is True


def test_specialist_team_passes_resolved_tool_scope_to_executor(tmp_path, monkeypatch):
    output = tmp_path / "report.docx"; output.write_bytes(b"document")
    available = (
        "word_create_document", "excel_create_workbook", "hwpx_create_document",
        "powerpoint_create_presentation", "pdf_create_document",
    )
    team = SpecialistTeamRuntime(tool_name_provider=lambda: available)
    monkeypatch.setattr(team, "_build_specialist_plan", lambda *args, **kwargs: {
        "goal": "보고서 작성", "inputs": [], "constraints": [],
        "acceptance": ["저장 후 검증"], "tools": ["word_create_document"],
        "planning_status": "ready",
    })
    captured = {}

    def invoke(_prompt, *, allowed_tool_names=None, execution_context=""):
        captured["prompt"] = _prompt
        captured["allowed_tool_names"] = list(allowed_tool_names or ())
        captured["execution_context"] = execution_context
        result = ToolRunResult.successful(
            tool_name="word_create_document", raw_output=str(output),
            evidence=[Evidence("file_hash", "저장된 파일의 해시를 확인했습니다.")],
            artifacts=[Artifact("document", str(output))],
        )
        return ExecutionOutcome(
            "문서를 저장하고 검증했습니다.", status="completed",
            tool_result=result, completed_steps=1,
        )

    _, run = team.execute_workspace_request(
        "document", "보고서를 작성해줘", invoke_executor=invoke,
    )
    assert set(captured["allowed_tool_names"]) == set(available)
    assert captured["prompt"] == "보고서를 작성해줘"
    assert "workspace" in captured["execution_context"]
    assert "word_create_document" not in captured["execution_context"]
    assert run.status == "completed"


def test_specialist_team_exposes_sequential_role_pipeline():
    team = SpecialistTeamRuntime()
    assert team.role_pipeline("mockup") == (
        "기억 검색", "Vision 관찰", "레이아웃 설계", "제약 검증", "비파괴 렌더링"
    )
    assert "순차 실행/해제" in team.describe_team("mockup")


def test_specialist_role_rejects_empty_required_artifact():
    team = SpecialistTeamRuntime()
    run = team.run("document", "요구사항을 분석해줘")
    with run as state:
        try:
            team.execute_role(state, "requirements", lambda _run: {})
        except ValueError as exc:
            assert "비어 있는 상태" in str(exc)
        else:
            raise AssertionError("빈 역할 산출물을 완료로 처리했습니다.")
    assert state.status == "failed"
    assert state.events[-1]["status"] == "failed"


def test_specialist_reviewer_aggregates_multi_step_tool_evidence(tmp_path):
    first = tmp_path / "report.docx"
    second = tmp_path / "report.pdf"
    first.write_bytes(b"docx")
    second.write_bytes(b"%PDF-1.4\n%%EOF\n")
    outcome = ExecutionOutcome(
        "문서 작성과 렌더 검증을 완료했습니다.", status="completed",
        completed_steps=2,
        tool_results=(
            ToolRunResult.successful(
                tool_name="word_create_document", raw_output=str(first),
                evidence=[Evidence("file_hash", "DOCX 저장 해시를 확인했습니다.")],
                artifacts=[Artifact("document", str(first))],
            ),
            ToolRunResult.successful(
                tool_name="pdf_create_document", raw_output=str(second),
                evidence=[Evidence("rendered_pdf", "PDF 렌더 결과를 확인했습니다.")],
                artifacts=[Artifact("pdf", str(second))],
            ),
        ),
    )
    payload = SpecialistTeamRuntime._outcome_payload(outcome)
    contract = {
        "artifact_types": ["document", "pdf", "file"],
        "acceptance_criteria": ["저장 후 렌더링을 검증한다"],
        "readiness": {
            "resolved_tools": ["word_create_document", "pdf_create_document"],
            "checks": {"required_tools_registered": True, "executor_available": True},
        },
    }
    review = SpecialistTeamRuntime._review_execution(payload, contract)
    assert payload["tool_status"] == "succeeded"
    assert payload["tool_names"] == ["word_create_document", "pdf_create_document"]
    assert payload["evidence_count"] == 2
    assert payload["artifact_count"] == 2
    assert review["passed"] is True
    assert {item["uri"] for item in review["accepted_artifacts"]} == {
        str(first), str(second),
    }


def test_photoshop_contract_accepts_plugin_image_document_artifact():
    spec = get_specialist_workspace_registry().get("photoshop")
    assert "image_document" in spec.artifact_types
