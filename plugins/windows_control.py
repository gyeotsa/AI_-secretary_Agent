"""Permission-gated Windows application discovery, launch and focus tools."""
from pathlib import Path
from typing import Any, Dict, List
import json, os, re, shutil, subprocess, winreg
import pygetwindow
from core.plugin import BasePlugin, ToolSchema, IntentSchema, SlotSchema


class WindowsControlPlugin(BasePlugin):
    def __init__(self): super().__init__(); self.name="windows_control"; self.description="Windows 앱 검색·실행·창 활성화"
    def get_tools(self)->List[ToolSchema]:
        return [
            ToolSchema("windows_find_apps","Windows에 등록된 프로그램을 검색합니다",{"type":"object","properties":{"query":{"type":"string"},"max_results":{"type":"integer","default":20}},"required":["query"]},["windows_api"]),
            ToolSchema("windows_launch_app","등록된 앱 이름 또는 실행 파일 경로로 프로그램을 실행합니다",{"type":"object","properties":{"target":{"type":"string"},"arguments":{"type":"array","items":{"type":"string"}}},"required":["target"]},["windows_api"]),
            ToolSchema("windows_focus_window","제목이 일치하는 창을 복원하고 활성화합니다",{"type":"object","properties":{"title":{"type":"string"}},"required":["title"]},["windows_api"]),
        ]
    def get_intents(self):
        return [IntentSchema("windows.launch_app","Windows 프로그램 실행","windows_launch_app",["실행해","실행해줘","열어줘","켜줘"],[SlotSchema("target","실행할 프로그램","어떤 프로그램을 실행할까요, 보스?")],follow_up_hints=["다시"])]
    def extract_slots(self,intent_name,text,current_slots):
        slots=dict(current_slots)
        if intent_name=="windows.launch_app":
            match=re.search(r"(.+?)\s*(?:실행|열어|켜)",text)
            if match: slots["target"]=match.group(1).strip()
        return slots
    @staticmethod
    def _aliases()->Dict[str,str]:
        path=Path(__file__).resolve().parent.parent/"data"/"app_aliases.json"
        try: return {str(k).casefold():str(v) for k,v in json.loads(path.read_text(encoding="utf-8")).items()}
        except (OSError,ValueError,TypeError): return {}
    @staticmethod
    def _registered_apps()->Dict[str,str]:
        apps={}
        roots=((winreg.HKEY_LOCAL_MACHINE,r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),(winreg.HKEY_LOCAL_MACHINE,r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),(winreg.HKEY_CURRENT_USER,r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"))
        for root,key_path in roots:
            try:
                with winreg.OpenKey(root,key_path) as key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        name=winreg.EnumKey(key,index)
                        try:
                            with winreg.OpenKey(key,name) as sub: apps[name.casefold()]=str(winreg.QueryValue(sub,None))
                        except OSError: pass
            except OSError: pass
        return apps
    def execute_tool(self,name:str,data:Dict[str,Any])->str:
        try:
            if name=="windows_find_apps":
                query=str(data["query"]).casefold(); limit=max(1,min(int(data.get("max_results",20)),100)); found=[f"{k}: {v}" for k,v in self._registered_apps().items() if query in k or query in v.casefold()]
                return "\n".join(found[:limit]) if found else "검색 결과가 없습니다."
            if name=="windows_focus_window":
                wins=pygetwindow.getWindowsWithTitle(str(data["title"]));
                if not wins:return "오류: 일치하는 창이 없습니다."
                win=wins[0]; win.restore() if win.isMinimized else None; win.activate(); return f"창 활성화 성공: {win.title}"
            if name=="windows_launch_app":
                target=str(data["target"]); apps=self._registered_apps(); alias=self._aliases().get(target.casefold(),target); resolved=apps.get(alias.casefold()) or apps.get((alias+".exe").casefold()) or shutil.which(alias)
                if not resolved and Path(target).is_absolute() and Path(target).is_file(): resolved=str(Path(target).resolve())
                if not resolved:return "오류: Windows 등록 앱 또는 실행 파일을 찾을 수 없습니다. 먼저 windows_find_apps로 검색하세요."
                arguments=[str(x) for x in data.get("arguments") or []]
                if len(arguments)>32:return "오류: 인자가 너무 많습니다."
                process=subprocess.Popen([resolved,*arguments],shell=False); return f"프로그램 실행 성공: {resolved} (PID: {process.pid})"
            return f"오류: 알 수 없는 툴 '{name}'"
        except Exception as exc:return f"오류: {exc}"
