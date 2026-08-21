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
from core.tool_result import Artifact, Evidence, ToolRunResult, ToolRunStatus
from core.windows_automation import WindowsAutomationRuntime


class WindowsControlPlugin(BasePlugin):
    MAX_SEARCH_DEPTH = 4
    MAX_SCANNED_FILES = 100_000
    SKIPPED_ROOT_NAMES = {"windows", "users", "programdata", "program files", "program files (x86)",
                          "$recycle.bin", "system volume information", "recovery"}

    def __init__(self):
        super().__init__()
        self.name = "windows_control"
        self.description = "Windows 앱 자동 검색·실행·UAC 상승·창 활성화"
        self.automation = WindowsAutomationRuntime()

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("windows_find_apps", "등록 정보와 설치 폴더에서 프로그램을 검색합니다", {
                "type": "object", "properties": {
                    "query": {"type": "string"}, "max_results": {"type": "integer", "default": 20},
                    "deep_search": {"type": "boolean", "default": True},
                    "full_drive_search": {"type": "boolean", "default": True},
                }, "required": ["query"]}, ["windows_api"]),
            ToolSchema("windows_launch_app", "앱 이름·별칭·실행 파일을 찾아 프로그램을 실행합니다", {
                "type": "object", "properties": {
                    "target": {"type": "string"}, "arguments": {"type": "array", "items": {"type": "string"}},
                    "elevation": {"type": "string", "enum": ["auto", "never", "always"], "default": "auto"},
                }, "required": ["target"]}, ["windows_api"]),
            ToolSchema("windows_focus_window", "제목이 일치하는 창을 복원하고 활성화합니다", {
                "type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]
            }, ["windows_api"]),
            ToolSchema("windows_close_app", "앱 별칭이나 이름에 대응하는 창에 정상 종료를 요청합니다", {
                "type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]
            }, ["windows_api"]),
            ToolSchema("windows_add_app_aliases", "앱을 찾아 사용자 별칭을 추가합니다", {
                "type": "object", "properties": {
                    "target": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                }, "required": ["target", "aliases"]}, ["windows_api"]),
            ToolSchema("windows_list_app_aliases", "사용자 앱 별칭 목록을 조회합니다", {
                "type": "object", "properties": {}, "required": []}, ["windows_api"]),
            ToolSchema("windows_remove_app_aliases", "사용자 앱 별칭을 삭제합니다", {
                "type": "object", "properties": {
                    "aliases": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                }, "required": ["aliases"]}, ["windows_api"]),
            ToolSchema("windows_list_handles", "창 Handle·PID·포커스 상태를 조회합니다", {
                "type": "object", "properties": {}, "additionalProperties": False}, ["windows_api"]),
            ToolSchema("windows_accessibility_tree", "창의 Windows UI Automation 접근성 트리를 조회합니다", {
                "type": "object", "properties": {"handle": {"type": "integer", "minimum": 1},
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 8}},
                "required": ["handle"], "additionalProperties": False}, ["windows_api"]),
            ToolSchema("windows_automation_policy", "API·CLI·COM·UIA 우선 자동화 정책을 조회합니다", {
                "type": "object", "properties": {}, "additionalProperties": False}, ["windows_api"]),
            ToolSchema("windows_coordinate_click", "구조화 자동화가 불가능할 때만 명시 승인 후 좌표를 클릭합니다", {
                "type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                    "reason": {"type": "string", "minLength": 5}},
                "required": ["x", "y", "reason"], "additionalProperties": False},
                ["coordinate_control"], side_effect="execute"),
        ]

    def get_intents(self):
        return [
            IntentSchema("windows.launch_app", "Windows 프로그램 실행", "windows_launch_app",
                         ["실행해", "실행해줘", "열어줘", "켜줘"],
                         [SlotSchema("target", "실행할 프로그램", "어떤 프로그램을 실행할까요, 보스?")],
                         execution_hints=["실행해", "실행해줘", "열어줘", "켜줘", "켜"],
                         follow_up_hints=["다시"]),
            IntentSchema("windows.close_app", "Windows 프로그램 종료", "windows_close_app",
                         ["꺼줘", "종료해줘", "닫아줘", "종료해", "닫아"],
                         [SlotSchema("target", "종료할 프로그램", "어떤 프로그램을 종료할까요, 보스?")],
                         execution_hints=["꺼줘", "꺼", "종료해줘", "종료해", "닫아줘", "닫아"]),
            IntentSchema("windows.find_app", "Windows 프로그램 검색", "windows_find_apps",
                         ["앱을 찾아", "프로그램 찾아", "실행 파일 찾아"],
                         [SlotSchema("query", "검색할 프로그램", "어떤 프로그램을 찾을까요, 보스?")],
                         execution_hints=["찾아", "검색해"]),
            IntentSchema("windows.add_aliases", "Windows 프로그램 별칭 추가", "windows_add_app_aliases",
                         ["별칭에", "별칭으로", "별칭 추가"],
                         [SlotSchema("target", "대상 프로그램", "어떤 프로그램의 별칭인가요, 보스?"),
                          SlotSchema("aliases", "추가할 별칭", "어떤 별칭을 추가할까요, 보스?")],
                         execution_hints=["추가해", "추가"]),
            IntentSchema("windows.list_aliases", "Windows 프로그램 별칭 조회", "windows_list_app_aliases",
                         ["별칭 목록", "별칭 보여", "별칭 조회"], [],
                         execution_hints=["목록", "보여", "조회"]),
            IntentSchema("windows.remove_aliases", "Windows 프로그램 별칭 삭제", "windows_remove_app_aliases",
                         ["별칭 삭제", "별칭 제거"],
                         [SlotSchema("aliases", "삭제할 별칭", "어떤 별칭을 삭제할까요, 보스?")],
                         execution_hints=["삭제", "제거"]),
        ]

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        if intent_name == "windows.launch_app":
            match = re.search(r"(.+?)\s*(?:실행|열어|켜)", text)
            if match:
                slots["target"] = match.group(1).strip()
        elif intent_name == "windows.close_app":
            match = re.search(r"(.+?)\s*(?:꺼|종료|닫아)", text)
            if match:
                slots["target"] = match.group(1).strip()
        elif intent_name == "windows.find_app":
            match = re.search(r"(.+?)\s*(?:앱|프로그램|실행\s*파일)?\s*(?:을|를)?\s*찾", text)
            if match and match.group(1).strip() not in {"해당", "그", "그럼 해당"}:
                slots["query"] = match.group(1).strip()
        elif intent_name == "windows.add_aliases":
            target = re.search(r"^\s*(.+?)(?:\s*앱)?(?:의)?\s*별칭", text)
            values = re.search(r"별칭(?:에|으로)?\s*(.+?)\s*(?:을|를)?\s*추가", text)
            if target:
                slots["target"] = target.group(1).strip()
            if values:
                slots["aliases"] = self._split_aliases(values.group(1))
        elif intent_name == "windows.remove_aliases":
            values = re.search(r"(?:별칭(?:에서)?\s*)?(.+?)\s*(?:을|를)?\s*(?:삭제|제거)", text)
            if values:
                slots["aliases"] = self._split_aliases(values.group(1))
        return slots

    @staticmethod
    def _split_aliases(value: str) -> List[str]:
        cleaned = re.sub(r"[\"']", "", value)
        return [part.strip() for part in re.split(r"\s*(?:,|와|과|및)\s*", cleaned) if part.strip()]

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
        return {**cls._json_map("app_aliases.json"), **cls._json_map("user_app_aliases.json")}

    @classmethod
    def _user_aliases(cls) -> Dict[str, str]:
        return cls._json_map("user_app_aliases.json")

    @classmethod
    def _write_user_aliases(cls, aliases: Dict[str, str]) -> None:
        target = cls._data_path("user_app_aliases.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

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

    @staticmethod
    def _shortcut_apps() -> Dict[str, str]:
        roots = [
            Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.getenv("ProgramData", "")) / "Microsoft/Windows/Start Menu/Programs",
        ]
        apps = {}
        for root in roots:
            if not root.is_dir():
                continue
            try:
                for shortcut in root.rglob("*.lnk"):
                    apps[shortcut.stem.casefold()] = str(shortcut)
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
    def _discover_executables(cls, query: str, limit: int = 20,
                              full_drive_search: bool = True) -> List[Path]:
        needle = Path(query).stem.casefold()
        if not needle:
            return []
        found, scanned = [], 0
        roots = []
        # Squirrel/Electron and per-user installers commonly live directly under
        # %LOCALAPPDATA% (not %LOCALAPPDATA%\Programs). Search matching app
        # directories first, keeping the scan bounded and domain-independent.
        local_app_data = Path(os.getenv("LOCALAPPDATA", ""))
        if local_app_data.is_dir():
            try:
                roots.extend(item for item in local_app_data.iterdir()
                             if item.is_dir() and needle in item.name.casefold())
            except OSError:
                pass
        roots.extend(path for path in cls._search_roots() if path not in roots)
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
        if found or not full_drive_search:
            return found

        # Final fallback requested by the user: scan the entire system drive.
        # Permission errors are skipped and links/junction-like entries are not
        # followed, preventing loops. Results are cached so this normally runs once.
        system_drive = Path(os.getenv("SystemDrive", "C:") + "\\")
        for current, dirs, files in os.walk(system_drive, topdown=True, followlinks=False,
                                            onerror=lambda _error: None):
            dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(current, name))]
            for filename in files:
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
        aliases, catalog = cls._aliases(), cls._catalog()
        shortcuts, registered = cls._shortcut_apps(), cls._registered_apps()
        alias = aliases.get(target.casefold(), target)
        key, exe_key = alias.casefold(), (alias if alias.casefold().endswith(".exe") else alias + ".exe").casefold()
        resolved = (catalog.get(key) or catalog.get(Path(alias).stem.casefold())
                    or shortcuts.get(key) or shortcuts.get(Path(alias).stem.casefold())
                    or registered.get(key) or registered.get(exe_key) or shutil.which(alias))
        if not resolved and Path(alias).is_absolute() and Path(alias).is_file():
            resolved = str(Path(alias).resolve())
        if not resolved:
            candidates = cls._discover_executables(alias, 20, True)
            exact = next((p for p in candidates if p.name.casefold() == exe_key), None)
            launcher = next((p for p in candidates if "launcher" in p.stem.casefold()), None)
            resolved = str(exact or launcher or (candidates[0] if candidates else ""))
        return str(resolved or "")

    @staticmethod
    def _run_elevated(executable: str, arguments: List[str]):
        parameters = subprocess.list2cmdline(arguments) if arguments else None
        code = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, parameters, str(Path(executable).parent), 1)
        if code <= 32:
            return ToolRunResult.failed(
                tool_name="windows_launch_app",
                error=f"Windows UAC 실행 요청 실패 (코드: {code})",
            )
        return ToolRunResult(
            tool_name="windows_launch_app",
            status=ToolRunStatus.UNVERIFIED,
            raw_output=f"Windows 관리자 권한 실행 요청 성공: {executable}. UAC 창에서 승인해 주세요.",
            evidence=[Evidence(
                "uac_request",
                "Windows에 관리자 권한 실행 요청을 전달했지만 사용자 승인은 아직 확인되지 않았습니다.",
                {"executable": executable, "shell_execute_code": code},
            )],
            artifacts=[Artifact("executable", executable)],
        )

    def execute_tool(self, name: str, data: Dict[str, Any]):
        try:
            if name == "windows_list_handles":
                windows = [item.__dict__ for item in self.automation.list_windows()]
                return ToolRunResult.successful(tool_name=name, raw_output=json.dumps(windows, ensure_ascii=False),
                    evidence=[Evidence("window_handles", f"표시된 창 {len(windows)}개의 Handle을 조회했습니다.", {"windows": windows})])
            if name == "windows_accessibility_tree":
                tree = self.automation.accessibility_tree(int(data["handle"]), int(data.get("max_depth", 4)))
                return ToolRunResult.successful(tool_name=name, raw_output=json.dumps(tree, ensure_ascii=False),
                    evidence=[Evidence("accessibility_tree", "UI Automation Control 트리를 조회했습니다.", tree)])
            if name == "windows_automation_policy":
                policy = self.automation.policy()
                return ToolRunResult.successful(tool_name=name, raw_output=json.dumps(policy, ensure_ascii=False),
                    evidence=[Evidence("automation_policy", "좌표 입력은 명시 승인 fallback으로 제한됩니다.", policy)])
            if name == "windows_coordinate_click":
                x, y = int(data["x"]), int(data["y"])
                width, height = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
                if not (0 <= x < width and 0 <= y < height):
                    return ToolRunResult.failed(tool_name=name, error="클릭 좌표가 주 화면 범위를 벗어났습니다.")
                ctypes.windll.user32.SetCursorPos(x, y)
                ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
                return ToolRunResult(tool_name=name, status=ToolRunStatus.UNVERIFIED,
                    raw_output=f"좌표 클릭 요청을 전달했습니다: ({x}, {y})",
                    evidence=[Evidence("coordinate_fallback", "대상 UI 상태는 좌표만으로 검증할 수 없습니다.",
                        {"x": x, "y": y, "reason": data["reason"], "explicit_fallback": True})])
            if name == "windows_add_app_aliases":
                target = str(data.get("target", "")).strip()
                raw_aliases = data.get("aliases") or []
                aliases = list(dict.fromkeys(str(item).strip() for item in raw_aliases if str(item).strip()))
                if not target or not aliases:
                    return ToolRunResult.failed(tool_name=name,error="대상 프로그램과 하나 이상의 별칭이 필요합니다.")
                if len(aliases) > 20 or any(len(alias) > 80 for alias in aliases):
                    return ToolRunResult.failed(tool_name=name,error="별칭 개수 또는 길이가 허용 범위를 초과했습니다.")
                resolved = self._resolve_target(target)
                if not resolved:
                    return ToolRunResult.failed(tool_name=name,error=f"별칭을 연결할 프로그램을 찾지 못했습니다: {target}")
                user_aliases = self._user_aliases()
                for alias in aliases:
                    user_aliases[alias.casefold()] = resolved
                self._write_user_aliases(user_aliases)
                saved = self._user_aliases()
                if any(saved.get(alias.casefold()) != resolved for alias in aliases):
                    raise ValueError("사용자 별칭 저장 결과가 요청과 일치하지 않습니다.")
                return ToolRunResult.successful(
                    tool_name=name,
                    raw_output=f"앱 별칭 추가 성공: {', '.join(aliases)} → {resolved}",
                    evidence=[Evidence("app_alias_store","사용자 앱 별칭 저장을 확인했습니다.",{"aliases":aliases,"resolved":resolved})],
                    artifacts=[Artifact("executable",resolved,{"aliases":aliases})],
                )
            if name == "windows_list_app_aliases":
                aliases = self._user_aliases()
                return ToolRunResult.successful(
                    tool_name=name,
                    raw_output=json.dumps(aliases, ensure_ascii=False, indent=2),
                    evidence=[Evidence("app_alias_store",f"사용자 앱 별칭 {len(aliases)}개를 조회했습니다.",{"count":len(aliases)})],
                )
            if name == "windows_remove_app_aliases":
                aliases = [str(item).strip() for item in data.get("aliases") or [] if str(item).strip()]
                if not aliases:
                    return ToolRunResult.failed(tool_name=name,error="삭제할 별칭이 필요합니다.")
                user_aliases = self._user_aliases()
                removed = [alias for alias in aliases if user_aliases.pop(alias.casefold(), None) is not None]
                if not removed:
                    return ToolRunResult.failed(tool_name=name,error="삭제할 사용자 별칭을 찾지 못했습니다.")
                self._write_user_aliases(user_aliases)
                saved = self._user_aliases()
                if any(alias.casefold() in saved for alias in removed):
                    raise ValueError("삭제한 별칭이 저장소에 남아 있습니다.")
                return ToolRunResult.successful(
                    tool_name=name,
                    raw_output=f"앱 별칭 삭제 성공: {', '.join(removed)}",
                    evidence=[Evidence("app_alias_store","사용자 앱 별칭 삭제를 확인했습니다.",{"removed":removed})],
                )
            if name == "windows_find_apps":
                query = str(data.get("query", "")).strip()
                if not query:
                    return ToolRunResult.failed(tool_name=name,error="검색할 프로그램 이름이 필요합니다.")
                limit = max(1, min(int(data.get("max_results", 20)), 100))
                sources = {**self._registered_apps(), **self._shortcut_apps(), **self._catalog()}
                found = [(key, value) for key, value in sources.items()
                         if query.casefold() in key or query.casefold() in value.casefold()]
                if data.get("deep_search", True):
                    for path in self._discover_executables(
                            query, limit, bool(data.get("full_drive_search", True))):
                        pair = (path.name.casefold(), str(path))
                        if pair not in found:
                            found.append(pair)
                output = "\n".join(f"{key}: {value}" for key, value in found[:limit]) if found else "검색 결과가 없습니다."
                return ToolRunResult.successful(
                    tool_name=name,
                    raw_output=output,
                    evidence=[Evidence("app_discovery",f"Windows 앱 검색 후보 {len(found[:limit])}개를 확인했습니다.",{"query":query,"count":len(found[:limit])})],
                    artifacts=[Artifact("executable",value,{"name":key}) for key,value in found[:limit]],
                )
            if name == "windows_focus_window":
                wins = pygetwindow.getWindowsWithTitle(str(data["title"]))
                if not wins:
                    return ToolRunResult.failed(tool_name=name,error="일치하는 창이 없습니다.")
                win = wins[0]
                if win.isMinimized:
                    win.restore()
                win.activate()
                handle = int(getattr(win, "_hWnd", 0) or 0)
                verified = self.automation.focus(handle) if handle else None
                return ToolRunResult.successful(
                    tool_name=name,
                    raw_output=f"창 활성화 성공: {win.title}",
                    evidence=[Evidence("window_state","대상 창 Handle이 foreground인지 확인했습니다." if verified else "대상 창 복원·활성화 요청을 적용했습니다.",
                        {"title":win.title,"handle":handle,"process_id":verified.process_id if verified else 0,
                         "foreground":verified.foreground if verified else None,"minimized":bool(win.isMinimized)})],
                    artifacts=[Artifact("window",win.title)],
                )
            if name == "windows_close_app":
                target = str(data.get("target", "")).strip()
                if not target:
                    return ToolRunResult.failed(tool_name=name,error="종료할 프로그램 이름이 필요합니다.")
                resolved = self._resolve_target(target)
                keywords = {target.casefold()}
                if resolved:
                    stem = Path(resolved).stem.casefold()
                    keywords.update({stem, re.sub(r"[_-]?(?:launcher|setup)$", "", stem)})
                keywords = {item for item in keywords if len(item) >= 2}
                windows = [
                    window for window in pygetwindow.getAllWindows()
                    if window.title and any(key in window.title.casefold() for key in keywords)
                ]
                if not windows:
                    return ToolRunResult.failed(tool_name=name,error=f"종료할 앱 창을 찾지 못했습니다: {target}")
                title = windows[0].title
                windows[0].close()
                remaining = [
                    window for window in pygetwindow.getAllWindows()
                    if window.title and window.title.casefold() == title.casefold()
                    and not bool(getattr(window, "closed", False))
                ]
                if remaining:
                    return ToolRunResult(
                        tool_name=name,status=ToolRunStatus.UNVERIFIED,
                        raw_output=f"앱 종료 요청 성공: {title}",
                        evidence=[Evidence("window_close_request","창 종료 요청은 전달했지만 창 소멸은 아직 확인되지 않았습니다.",{"title":title})],
                        artifacts=[Artifact("window",title)],
                    )
                return ToolRunResult.successful(
                    tool_name=name,
                    raw_output=f"앱 종료 요청 성공: {title}",
                    evidence=[Evidence("window_absent","종료 요청 후 대상 창이 사라진 것을 확인했습니다.",{"title":title})],
                )
            if name == "windows_launch_app":
                resolved = self._resolve_target(str(data["target"]))
                if not resolved:
                    return ToolRunResult.failed(tool_name=name,error="실행 파일을 자동으로 찾지 못했습니다. 검색 위치를 추가하거나 설치 상태를 확인하세요.")
                arguments = [str(item) for item in data.get("arguments") or []]
                if len(arguments) > 32:
                    return ToolRunResult.failed(tool_name=name,error="인자가 너무 많습니다.")
                elevation = str(data.get("elevation", "auto")).casefold()
                if elevation == "always":
                    return self._run_elevated(resolved, arguments)
                try:
                    if Path(resolved).suffix.casefold() == ".lnk":
                        os.startfile(resolved)
                        return ToolRunResult(
                            tool_name=name,status=ToolRunStatus.UNVERIFIED,
                            raw_output=f"Windows 시작 메뉴 앱 실행 요청 성공: {resolved}",
                            evidence=[Evidence("shortcut_launch_request","Windows 시작 메뉴 바로가기에 실행 요청을 전달했습니다.",{"shortcut":resolved})],
                            artifacts=[Artifact("shortcut",resolved)],
                        )
                    process = subprocess.Popen([resolved, *arguments], shell=False)
                    if process.poll() is not None:
                        raise RuntimeError(f"프로세스가 즉시 종료되었습니다: PID {process.pid}")
                    return ToolRunResult.successful(
                        tool_name=name,
                        raw_output=f"프로그램 실행 성공: {resolved} (PID: {process.pid})",
                        evidence=[Evidence("process_state","실행된 프로세스가 활성 상태임을 확인했습니다.",{"executable":resolved,"pid":process.pid})],
                        artifacts=[Artifact("process",str(process.pid),{"executable":resolved})],
                    )
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 740 and elevation == "auto":
                        return self._run_elevated(resolved, arguments)
                    raise
            return ToolRunResult.failed(tool_name=name,error=f"알 수 없는 툴 '{name}'")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=name,error=str(exc))
