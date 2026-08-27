import ctypes
import json

from docx import Document

from core.plugin import PluginRegistry
from core.tool_result import ToolRunResult, ToolRunStatus
from core.verifier import ToolVerifier
from plugins.excel import ExcelPlugin
from plugins.word import WordPlugin
from plugins.powerpoint import PowerPointPlugin
from plugins.pdf import PdfPlugin
from plugins.hwpx import HwpxPlugin
from plugins.windows_control import WindowsControlPlugin
from core.intent_router import IntentRouter


def _run(plugin, tool, data):
    result = plugin.execute_tool(tool, data)
    if isinstance(result, ToolRunResult):
        assert result.status == ToolRunStatus.SUCCEEDED, result.raw_output
        assert result.evidence and result.artifacts
        return result.raw_output
    assert not result.startswith("오류:"), result
    return result


def _raw(result):
    return result.raw_output if isinstance(result, ToolRunResult) else result


def test_office_plugins_create_and_read_real_files(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    xlsx=tmp_path/"업무.xlsx"; excel=ExcelPlugin()
    _run(excel,"excel_create_workbook",{"path":str(xlsx),"rows":[["항목","값"],["매출",10]]})
    assert json.loads(_run(excel,"excel_read_workbook",{"path":str(xlsx)}))["rows"][1]==["매출",10]
    docx=tmp_path/"보고서.docx"; word=WordPlugin()
    _run(word,"word_create_document",{"path":str(docx),"title":"보고서","paragraphs":["본문"]})
    assert "본문" in _run(word,"word_read_document",{"path":str(docx)})
    pptx=tmp_path/"발표.pptx"; power=PowerPointPlugin()
    _run(power,"powerpoint_create_presentation",{"path":str(pptx),"title":"발표","slides":[{"title":"요약","bullets":["내용"]}]})
    assert "내용" in _run(power,"powerpoint_read_presentation",{"path":str(pptx)})
    hwpx=tmp_path/"공문.hwpx"; hwp=HwpxPlugin()
    _run(hwp,"hwpx_create_document",{"path":str(hwpx),"title":"공문","paragraphs":["내용"]})
    assert "내용" in _run(hwp,"hwpx_read_document",{"path":str(hwpx)})


def test_pdf_plugin_creates_and_extracts_text(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    path=tmp_path/"문서.pdf"; plugin=PdfPlugin()
    _run(plugin,"pdf_create_document",{"path":str(path),"title":"PDF","paragraphs":["테스트 문서"]})
    assert "테스트" in _run(plugin,"pdf_extract_text",{"path":str(path)})


def test_registry_exposes_office_and_windows_tools(monkeypatch):
    registry=PluginRegistry()
    for plugin in (ExcelPlugin(),WordPlugin(),PowerPointPlugin(),PdfPlugin(),HwpxPlugin(),WindowsControlPlugin()): registry.register_plugin(plugin)
    names={tool.name for tool in registry.get_all_tools()}
    assert {"excel_create_workbook","word_create_document","powerpoint_create_presentation","pdf_create_document","hwpx_create_document","windows_launch_app"} <= names
    monkeypatch.setattr(WindowsControlPlugin,"_discover_executables",lambda *_args:[])
    assert _raw(WindowsControlPlugin().execute_tool("windows_launch_app",{"target":"definitely-not-installed-jarvis-app"})).startswith("오류:")


def test_document_intents_resolve_desktop_paths_without_core_branches():
    registry=PluginRegistry()
    for plugin in (ExcelPlugin(),WordPlugin(),PowerPointPlugin(),PdfPlugin(),HwpxPlugin()): registry.register_plugin(plugin)
    router=IntentRouter(registry)
    cases=[("바탕화면에 매출이라는 이름으로 엑셀 파일 만들어줘","excel_create_workbook","매출.xlsx"),("바탕화면에 보고서라는 이름으로 워드 만들어줘","word_create_document","보고서.docx"),("바탕화면에 발표라는 이름으로 pptx 만들어줘","powerpoint_create_presentation","발표.pptx"),("바탕화면에 안내라는 이름으로 PDF 만들어줘","pdf_create_document","안내.pdf"),("바탕화면에 공문이라는 이름으로 한글 문서 만들어줘","hwpx_create_document","공문.hwpx")]
    for text,tool,filename in cases:
        resolved=router.resolve(text)
        assert resolved.ready, (text,resolved)
        assert resolved.tool_name==tool
        assert resolved.slots["path"].endswith(filename)


def test_word_intent_preserves_explicit_title_and_body_for_fast_path(tmp_path):
    registry = PluginRegistry()
    registry.register_plugin(WordPlugin())
    router = IntentRouter(registry)
    path = tmp_path / "contract.docx"
    request = (
        f'{path} 경로에 제목은 "전문가 팀 실사용 검증", '
        '본문은 "실제 도구 증거와 문서 내용이 모두 확인되었습니다."인 Word 문서를 생성해줘.'
    )

    resolved = router.resolve(request)

    assert resolved.ready
    assert resolved.slots["path"] == str(path)
    assert resolved.slots["title"] == "전문가 팀 실사용 검증"
    assert resolved.slots["paragraphs"] == [
        "실제 도구 증거와 문서 내용이 모두 확인되었습니다."
    ]
    assert router.resolution_preserves_user_content(request, resolved)


def test_new_file_verifier_checks_real_output(tmp_path):
    path=tmp_path/"result.docx"
    document = Document()
    document.add_paragraph("검증할 실제 문서")
    document.save(path)
    assert ToolVerifier().verify("word_create_document",{"path":str(path)},"성공").success


def test_new_file_verifier_rejects_corrupted_office_output(tmp_path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"content")
    result = ToolVerifier().verify("word_create_document", {"path": str(path)}, "성공")
    assert not result.success
    assert result.verified


def test_office_create_tools_return_typed_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    cases = [
        (ExcelPlugin(), "excel_create_workbook", {"path": str(tmp_path/"a.xlsx")}, "xlsx_structure"),
        (WordPlugin(), "word_create_document", {"path": str(tmp_path/"a.docx")}, "docx_structure"),
        (PowerPointPlugin(), "powerpoint_create_presentation", {"path": str(tmp_path/"a.pptx")}, "pptx_structure"),
        (PdfPlugin(), "pdf_create_document", {"path": str(tmp_path/"a.pdf"), "paragraphs": ["본문"]}, "pdf_structure"),
        (HwpxPlugin(), "hwpx_create_document", {"path": str(tmp_path/"a.hwpx")}, "hwpx_structure"),
    ]
    for plugin, tool_name, data, evidence_kind in cases:
        result = plugin.execute_tool(tool_name, data)
        assert isinstance(result, ToolRunResult)
        assert result.succeeded
        assert result.evidence[0].kind == evidence_kind
        assert result.artifacts[0].uri == data["path"]


def test_word_create_evidence_contains_reopened_document_text(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    path = tmp_path / "verified-content.docx"

    result = WordPlugin().execute_tool("word_create_document", {
        "path": str(path),
        "title": "전문가 팀 실사용 검증",
        "paragraphs": ["실제 도구 증거와 문서 내용이 모두 확인되었습니다."],
    })

    assert isinstance(result, ToolRunResult) and result.succeeded
    data = result.evidence[0].data
    assert data["paragraphs"] == 2
    assert data["paragraph_texts"] == [
        "전문가 팀 실사용 검증",
        "실제 도구 증거와 문서 내용이 모두 확인되었습니다.",
    ]


def test_windows_launch_intent_bypasses_planner_and_resolves_configured_alias():
    registry=PluginRegistry(); registry.register_plugin(WindowsControlPlugin())
    resolution=IntentRouter(registry).resolve("메모장 실행해줄래?")
    assert resolution.ready
    assert resolution.tool_name=="windows_launch_app"
    assert resolution.slots["target"]=="메모장"
    assert WindowsControlPlugin()._aliases()["메모장"]=="notepad.exe"


def test_windows_auto_discovers_executable_without_manual_path(tmp_path, monkeypatch):
    launcher=tmp_path/"NIKKE_GOOGLE"/"launcher"/"nikke_launcher_google.exe"
    launcher.parent.mkdir(parents=True); launcher.write_bytes(b"MZ")
    plugin=WindowsControlPlugin()
    monkeypatch.setattr(WindowsControlPlugin,"_search_roots",lambda:[tmp_path])
    monkeypatch.setattr(WindowsControlPlugin,"_aliases",lambda:{})
    monkeypatch.setattr(WindowsControlPlugin,"_catalog",lambda:{})
    monkeypatch.setattr(WindowsControlPlugin,"_registered_apps",lambda:{})
    monkeypatch.setattr(WindowsControlPlugin,"_shortcut_apps",lambda:{})
    monkeypatch.setattr(WindowsControlPlugin,"_remember",lambda _paths:None)
    assert plugin._resolve_target("nikke.exe")==str(launcher)


def test_windows_auto_elevates_once_on_winerror_740(tmp_path, monkeypatch):
    target=tmp_path/"admin.exe"; target.write_bytes(b"MZ")
    plugin=WindowsControlPlugin()
    monkeypatch.setattr(plugin,"_resolve_target",lambda _target:str(target))
    monkeypatch.setattr("plugins.windows_control.subprocess.Popen",lambda *_a,**_k:(_ for _ in ()).throw(ctypes.WinError(740)))
    monkeypatch.setattr(plugin,"_run_elevated",lambda executable,arguments:f"UAC:{executable}")
    result=plugin.execute_tool("windows_launch_app",{"target":"admin.exe","elevation":"auto"})
    assert result==f"UAC:{target}"


def test_windows_direct_launch_returns_verified_process(tmp_path, monkeypatch):
    target=tmp_path/"app.exe"; target.write_bytes(b"MZ")
    plugin=WindowsControlPlugin()
    monkeypatch.setattr(plugin,"_resolve_target",lambda _target:str(target))

    class Process:
        pid=4321

        def poll(self):
            return None

    monkeypatch.setattr("plugins.windows_control.subprocess.Popen",lambda *_a,**_k:Process())
    result=plugin.execute_tool("windows_launch_app",{"target":"app"})
    assert isinstance(result,ToolRunResult)
    assert result.succeeded
    assert result.evidence[0].kind=="process_state"
    assert result.evidence[0].data["pid"]==4321


def test_windows_discovers_per_user_electron_install(tmp_path, monkeypatch):
    local=tmp_path/"Local"; executable=local/"Discord"/"app-1.2.3"/"Discord.exe"
    executable.parent.mkdir(parents=True); executable.write_bytes(b"MZ")
    plugin=WindowsControlPlugin()
    monkeypatch.setenv("LOCALAPPDATA",str(local))
    monkeypatch.setattr(WindowsControlPlugin,"_search_roots",lambda:[])
    monkeypatch.setattr(WindowsControlPlugin,"_remember",lambda _paths:None)
    found=plugin._discover_executables("Discord",10,True)
    assert found==[executable]


def test_windows_full_drive_fallback_runs_only_after_fast_search_misses(tmp_path, monkeypatch):
    drive=tmp_path/"drive"; executable=drive/"Custom"/"OnlyHere.exe"
    executable.parent.mkdir(parents=True); executable.write_bytes(b"MZ")
    monkeypatch.setenv("SystemDrive",str(drive))
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path/"missing-local"))
    monkeypatch.setattr(WindowsControlPlugin,"_search_roots",lambda:[])
    monkeypatch.setattr(WindowsControlPlugin,"_remember",lambda _paths:None)
    assert WindowsControlPlugin._discover_executables("OnlyHere",5,True)==[executable]
    assert WindowsControlPlugin._discover_executables("OnlyHere",5,False)==[]


def test_windows_resolves_and_launches_start_menu_shortcut(tmp_path, monkeypatch):
    shortcut=tmp_path/"Discord.lnk"; shortcut.write_bytes(b"shortcut")
    plugin=WindowsControlPlugin()
    monkeypatch.setattr(WindowsControlPlugin,"_aliases",lambda:{})
    monkeypatch.setattr(WindowsControlPlugin,"_catalog",lambda:{})
    monkeypatch.setattr(WindowsControlPlugin,"_registered_apps",lambda:{})
    monkeypatch.setattr(WindowsControlPlugin,"_shortcut_apps",lambda:{"discord":str(shortcut)})
    launched=[]; monkeypatch.setattr("plugins.windows_control.os.startfile",lambda path:launched.append(path))
    result=plugin.execute_tool("windows_launch_app",{"target":"Discord"})
    assert isinstance(result,ToolRunResult)
    assert result.status==ToolRunStatus.UNVERIFIED
    assert result.raw_output.startswith("Windows 시작 메뉴 앱 실행 요청 성공:")
    assert launched==[str(shortcut)]


def test_windows_alias_intent_and_crud_tools(tmp_path, monkeypatch):
    shortcut=tmp_path/"Discord.lnk"; shortcut.write_bytes(b"shortcut")
    monkeypatch.setattr(WindowsControlPlugin,"_data_path",staticmethod(lambda name:tmp_path/name))
    monkeypatch.setattr(WindowsControlPlugin,"_resolve_target",classmethod(lambda _cls,_target:str(shortcut)))
    plugin=WindowsControlPlugin(); registry=PluginRegistry(); registry.register_plugin(plugin)
    resolution=IntentRouter(registry).resolve("Discord 앱의 별칭에 디스코드와 디코를 추가해줘")
    assert resolution.ready
    assert resolution.tool_name=="windows_add_app_aliases"
    assert resolution.slots=={"target":"Discord","aliases":["디스코드","디코"]}
    added=plugin.execute_tool(resolution.tool_name,resolution.slots)
    assert isinstance(added,ToolRunResult) and added.succeeded
    assert added.raw_output.startswith("앱 별칭 추가 성공:")
    assert plugin._aliases()["디스코드"]==str(shortcut)
    assert "디코" in _raw(plugin.execute_tool("windows_list_app_aliases",{}))
    removed=plugin.execute_tool("windows_remove_app_aliases",{"aliases":["디코"]})
    assert isinstance(removed,ToolRunResult) and removed.succeeded
    assert removed.raw_output.startswith("앱 별칭 삭제 성공:")
    assert "디코" not in plugin._user_aliases()


def test_windows_close_alias_resolves_window_and_requests_graceful_close(monkeypatch):
    class Window:
        title = "Discord"
        closed = False

        def close(self):
            self.closed = True

    window = Window()
    monkeypatch.setattr(
        WindowsControlPlugin, "_resolve_target",
        classmethod(lambda _cls, _target: r"C:\Start Menu\Discord.lnk"),
    )
    monkeypatch.setattr("plugins.windows_control.pygetwindow.getAllWindows", lambda: [window])
    plugin = WindowsControlPlugin()
    registry = PluginRegistry()
    registry.register_plugin(plugin)

    resolution = IntentRouter(registry).resolve("디코 꺼줘")
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)

    assert resolution.ready
    assert resolution.tool_name == "windows_close_app"
    assert resolution.slots == {"target": "디코"}
    assert isinstance(result,ToolRunResult)
    assert result.succeeded
    assert result.raw_output == "앱 종료 요청 성공: Discord"
    assert window.closed
