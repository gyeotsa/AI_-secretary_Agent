"""Permission-gated Windows application discovery, launch, focus and UAC tools."""
from pathlib import Path
from typing import Any, Dict, List
import ctypes
import json
import os
import re
import shutil
import subprocess
import winreg

import pygetwindow

from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema


class WindowsControlPlugin(BasePlugin):
    MAX_SEARCH_DEPTH = 4
    MAX_SCANNED_FILES = 100_000
    SKIPPED_ROOT_NAMES = {"windows", "users", "programdata", "program files", "program files (x86)",
                          "$recycle.bin", "system volume information", "recovery"}

    def __init__(self):
        super().__init__()
        self.name = "windows_control"
        self.description = "Windows 앱 자동 검색·실행·UAC 상승·창 활성화"

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("windows_find_apps", "등록 정보와 설치 폴더에서 프로그램을 검색합니다", {
                "type": "object", "properties": {
                    "query": {"type": "string"}, "max_results": {"type": "integer", "default": 20},
                    "deep_search": {"type": "boolean", "default": True},
                }, "required": ["query"]}, ["windows_api"]),
            ToolSchema("windows_launch_app", "앱 이름·별칭·실행 파일을 찾아 프로그램을 실행합니다", {
                "type": "object", "properties": {
                    "target": {"type": "string"}, "arguments": {"type": "array", "items": {"type": "string"}},
                    "elevation": {"type": "string", "enum": ["auto", "never", "always"], "default": "auto"},
                }, "required": ["target"]}, ["windows_api"]),
            ToolSchema("windows_focus_window", "제목이 일치하는 창을 복원하고 활성화합니다", {
                "type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]
            }, ["windows_api"]),
        ]

    def get_intents(self):
        return [
            IntentSchema("windows.launch_app", "Windows 프로그램 실행", "windows_launch_app",
                         ["실행해", "실행해줘", "열어줘", "켜줘"],
                         [SlotSchema("target", "실행할 프로그램", "어떤 프로그램을 실행할까요, 보스?")],
                         follow_up_hints=["다시"]),
            IntentSchema("windows.find_app", "Windows 프로그램 검색", "windows_find_apps",
                         ["앱을 찾아", "프로그램 찾아", "실행 파일 찾아"],
                         [SlotSchema("query", "검색할 프로그램", "어떤 프로그램을 찾을까요, 보스?")]),
        ]

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        if intent_name == "windows.launch_app":
            match = re.search(r"(.+?)\s*(?:실행|열어|켜)", text)
            if match:
                slots["target"] = match.group(1).strip()
        elif intent_name == "windows.find_app":
            match = re.search(r"(.+?)\s*(?:앱|프로그램|실행\s*파일)?\s*(?:을|를)?\s*찾", text)
            if match and match.group(1).strip() not in {"해당", "그", "그럼 해당"}:
                slots["query"] = match.group(1).strip()
        return slots

    @staticmethod
    def _data_path(name: str) -> Path:
        return Path(__file__).resolve().parent.parent / "data" / name

    @classmethod
    def _json_map(cls, name: str) -> Dict[str, str]:
        try:
            data = json.loads(cls._data_path(name).read_text(encoding="utf-8"))
            return {str(k).casefold(): str(v) for k, v in data.items() if str(v).strip()}
        except (OSError, ValueError, TypeError):
            return {}

    @classmethod
    def _aliases(cls) -> Dict[str, str]:
        return cls._json_map("app_aliases.json")

    @classmethod
    def _catalog(cls) -> Dict[str, str]:
        return cls._json_map("discovered_apps.json")

    @classmethod
    def _remember(cls, paths: List[Path]) -> None:
        catalog = cls._catalog()
        for path in paths:
            catalog[path.name.casefold()] = str(path)
            catalog[path.stem.casefold()] = str(path)
        target = cls._data_path("discovered_apps.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _registered_apps() -> Dict[str, str]:
        apps = {}
        roots = (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        )
        for root, key_path in roots:
            try:
                with winreg.OpenKey(root, key_path) as key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        name = winreg.EnumKey(key, index)
                        try:
                            with winreg.OpenKey(key, name) as sub:
                                value = str(winreg.QueryValue(sub, None)).strip().strip('"')
                            if value and Path(value).is_file():
                                apps[name.casefold()] = value
                        except OSError:
                            pass
            except OSError:
                pass
        return apps

    @classmethod
    def _search_roots(cls) -> List[Path]:
        candidates = [os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"),
                      os.path.join(os.getenv("LOCALAPPDATA", ""), "Programs")]
        system_drive = Path(os.getenv("SystemDrive", "C:") + "\\")
        try:
            candidates.extend(str(item) for item in system_drive.iterdir()
                              if item.is_dir() and item.name.casefold() not in cls.SKIPPED_ROOT_NAMES)
        except OSError:
            pass
        result = []
        for value in candidates:
            if value:
                path = Path(value)
                if path.is_dir() and path not in result:
                    result.append(path)
        return result

    @classmethod
    def _discover_executables(cls, query: str, limit: int = 20) -> List[Path]:
        needle = Path(query).stem.casefold()
        if not needle:
            return []
        found, scanned = [], 0
        roots = cls._search_roots()
        roots.sort(key=lambda path: (needle not in path.name.casefold(), len(path.parts)))
        for root in roots:
            root_depth = len(root.parts)
            for current, dirs, files in os.walk(root, onerror=lambda _error: None):
                depth = len(Path(current).parts) - root_depth
                if depth >= cls.MAX_SEARCH_DEPTH:
                    dirs[:] = []
                dirs[:] = [d for d in dirs if d.casefold() not in cls.SKIPPED_ROOT_NAMES]
                for filename in files:
                    scanned += 1
                    if scanned > cls.MAX_SCANNED_FILES:
                        if found:
                            cls._remember(found)
                        return found
                    if filename.casefold().endswith(".exe") and needle in Path(filename).stem.casefold():
                        found.append(Path(current) / filename)
                        if len(found) >= limit:
                            cls._remember(found)
                            return found
        if found:
            cls._remember(found)
        return found

    @classmethod
    def _resolve_target(cls, target: str) -> str:
        aliases, catalog, registered = cls._aliases(), cls._catalog(), cls._registered_apps()
        alias = aliases.get(target.casefold(), target)
        key, exe_key = alias.casefold(), (alias if alias.casefold().endswith(".exe") else alias + ".exe").casefold()
        resolved = catalog.get(key) or catalog.get(Path(alias).stem.casefold()) or registered.get(key) or registered.get(exe_key) or shutil.which(alias)
        if not resolved and Path(alias).is_absolute() and Path(alias).is_file():
            resolved = str(Path(alias).resolve())
        if not resolved:
            candidates = cls._discover_executables(alias, 20)
            exact = next((p for p in candidates if p.name.casefold() == exe_key), None)
            launcher = next((p for p in candidates if "launcher" in p.stem.casefold()), None)
            resolved = str(exact or launcher or (candidates[0] if candidates else ""))
        return str(resolved or "")

    @staticmethod
    def _run_elevated(executable: str, arguments: List[str]) -> str:
        parameters = subprocess.list2cmdline(arguments) if arguments else None
        code = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, parameters, str(Path(executable).parent), 1)
        if code <= 32:
            return f"오류: Windows UAC 실행 요청 실패 (코드: {code})"
        return f"Windows 관리자 권한 실행 요청 성공: {executable}. UAC 창에서 승인해 주세요."

    def execute_tool(self, name: str, data: Dict[str, Any]) -> str:
        try:
            if name == "windows_find_apps":
                query = str(data.get("query", "")).strip()
                if not query:
                    return "오류: 검색할 프로그램 이름이 필요합니다."
                limit = max(1, min(int(data.get("max_results", 20)), 100))
                sources = {**self._registered_apps(), **self._catalog()}
                found = [(key, value) for key, value in sources.items()
                         if query.casefold() in key or query.casefold() in value.casefold()]
                if data.get("deep_search", True):
                    for path in self._discover_executables(query, limit):
                        pair = (path.name.casefold(), str(path))
                        if pair not in found:
                            found.append(pair)
                return "\n".join(f"{key}: {value}" for key, value in found[:limit]) if found else "검색 결과가 없습니다."
            if name == "windows_focus_window":
                wins = pygetwindow.getWindowsWithTitle(str(data["title"]))
                if not wins:
                    return "오류: 일치하는 창이 없습니다."
                win = wins[0]
                if win.isMinimized:
                    win.restore()
                win.activate()
                return f"창 활성화 성공: {win.title}"
            if name == "windows_launch_app":
                resolved = self._resolve_target(str(data["target"]))
                if not resolved:
                    return "오류: 실행 파일을 자동으로 찾지 못했습니다. 검색 위치를 추가하거나 설치 상태를 확인하세요."
                arguments = [str(item) for item in data.get("arguments") or []]
                if len(arguments) > 32:
                    return "오류: 인자가 너무 많습니다."
                elevation = str(data.get("elevation", "auto")).casefold()
                if elevation == "always":
                    return self._run_elevated(resolved, arguments)
                try:
                    process = subprocess.Popen([resolved, *arguments], shell=False)
                    return f"프로그램 실행 성공: {resolved} (PID: {process.pid})"
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 740 and elevation == "auto":
                        return self._run_elevated(resolved, arguments)
                    raise
            return f"오류: 알 수 없는 툴 '{name}'"
        except Exception as exc:
            return f"오류: {exc}"
