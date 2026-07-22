import ctypes
import json

from core.plugin import PluginRegistry
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
    assert not result.startswith("오류:"), result
    return result


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
    assert WindowsControlPlugin().execute_tool("windows_launch_app",{"target":"definitely-not-installed-jarvis-app"}).startswith("오류:")


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


def test_new_file_verifier_checks_real_output(tmp_path):
    path=tmp_path/"result.docx"; path.write_bytes(b"content")
    assert ToolVerifier().verify("word_create_document",{"path":str(path)},"성공").success


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
