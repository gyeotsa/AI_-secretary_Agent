import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from core.model_registry import ModelRegistry, ModelRoleRouter
from core.specialist_workspaces import get_specialist_workspace_registry
from plugins.photoshop import PhotoshopPlugin
from ui.specialist_workspaces import MockupWorkspaceWindow, SpecialistWorkspaceWindow
from core.specialist_team import SpecialistTeamRuntime
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
    window.close(); assert app is not None


def test_specialist_team_scopes_recall_and_stores_only_compact_success():
    class Rag:
        namespace = "global"
        def __init__(self): self.added = []
        def set_namespace(self, value): self.namespace = value
        def search_docs(self, query, top_k=5): return [{"content": f"memory:{query}"}]
        def add_text_document(self, text, **kwargs): self.added.append((text, kwargs)); return kwargs["doc_id"]

    rag = Rag(); team = SpecialistTeamRuntime(rag, namespace_provider=lambda: "project-a")
    context = team.recall("mockup", "문구 습관")
    assert "memory:문구 습관" in context.as_prompt()
    assert rag.namespace == "global"
    team.remember_success("mockup", instruction="문구를 아래로", result={"renderer": "v5"}, approved=True)
    assert rag.added[0][1]["namespace"] == "project-a:specialist:mockup"
    assert "output" not in rag.added[0][0]


def test_specialist_team_exposes_sequential_role_pipeline():
    team = SpecialistTeamRuntime()
    assert team.role_pipeline("mockup") == (
        "기억 검색", "Vision 관찰", "레이아웃 설계", "제약 검증", "비파괴 렌더링"
    )
    assert "순차 실행/해제" in team.describe_team("mockup")
