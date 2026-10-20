"""Task-scoped Playwright and handle-scoped Windows input adapters."""
from __future__ import annotations

import io
import ctypes
import hashlib
import os
import re
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from core.computer_use import Observation, StaleObservation
from core.turn_context import check_turn_cancelled


def _png(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _value_hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _wait(check, seconds=0.3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check()
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))


# This code is owned by the adapter; the model never supplies JavaScript/selectors.
_ELEMENT_INFO = """el => {
    const r = el.getBoundingClientRect();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const value = type === 'password' ? '' : String(el.isContentEditable ? el.innerText : (el.value ?? ''));
    return {connected: el.isConnected, tag: el.tagName.toLowerCase(),
        name: (el.getAttribute('aria-label') || el.labels?.[0]?.innerText ||
               el.innerText || el.getAttribute('placeholder') || el.getAttribute('title') || '').slice(0, 240),
        role: el.getAttribute('role') || '', type,
        value: value.slice(0, 1000), value_length: Array.from(value).length, _full_value: value,
        checked: el.checked ?? el.getAttribute('aria-checked'),
        expanded: el.getAttribute('aria-expanded'),
        options: el.tagName === 'SELECT' ? Array.from(el.options).slice(0, 50).map(o => o.label) : [],
        secret: type === 'password' || /password|cc-number|cc-csc|one-time-code/i.test(el.autocomplete || ''),
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
        href: el.tagName === 'A' ? el.href : '',
        bounds: [r.x, r.y, r.width, r.height].map(Math.round)};
}"""
_SELECTOR = ('button, a[href], input, textarea, select, [role="button"], [role="tab"], '
             '[role="checkbox"], [contenteditable="true"], [tabindex="0"]')


def _element_state(element):
    info = element.evaluate(_ELEMENT_INFO)
    # Full input values exist only during this host-side hash calculation.
    fingerprint = _value_hash(info.pop("_full_value"))
    return info, fingerprint


class BrowserComputerBackend:
    def __init__(self, url, *, profile_path: Path, allowed_origins=(), headless=False):
        from plugins.browser import BrowserPlugin
        self.validate_url = BrowserPlugin._validate_url
        self.url = self.validate_url(url)
        self.origins = {self._origin(self.url), *(self._origin(self.validate_url(u)) for u in allowed_origins)}
        self.profile_path, self.headless = profile_path, headless
        self.stack = ExitStack()
        self.page = None
        self.context = None
        self.elements = {}
        self.tabs = {}

    @staticmethod
    def _origin(url):
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc.casefold()}"

    def _check_navigation(self, url):
        self.validate_url(url)
        if self._origin(url) not in self.origins:
            raise ValueError("작업에 허용되지 않은 출처로 이동할 수 없습니다.")

    def _route(self, route):
        try:
            self.validate_url(route.request.url)
            if route.request.is_navigation_request():
                self._check_navigation(route.request.url)
            route.continue_()
        except ValueError:
            route.abort()

    def _socket(self, socket):
        try:
            self._check_navigation(socket.url.replace("wss://", "https://", 1).replace("ws://", "http://", 1))
            socket.connect_to_server()
        except ValueError:
            socket.close()

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
            playwright = self.stack.enter_context(sync_playwright())
            self.profile_path.mkdir(parents=True, exist_ok=True)
            self.context = playwright.chromium.launch_persistent_context(
                str(self.profile_path), headless=self.headless,
                viewport={"width": 1280, "height": 800}, device_scale_factor=1,
                accept_downloads=False, service_workers="block",
            )
            self.stack.callback(self.context.close)
            self.context.set_default_timeout(5000)
            self.context.set_default_navigation_timeout(15000)
            self.context.route("**/*", self._route)
            self.context.route_web_socket("**/*", self._socket)
            self.context.on("page", self._new_page)
            for page in self.context.pages:
                self._new_page(page)
            self.page = self.context.new_page()
            self.page.goto(self.url, wait_until="domcontentloaded")
            return self
        except BaseException:
            self.stack.close()
            raise

    def _new_page(self, page):
        self.tabs[f"tab{len(self.tabs) + 1}"] = page
        # Native dialogs and downloads require manual handling, not implicit acceptance.
        page.on("dialog", lambda dialog: dialog.dismiss())

    def __exit__(self, *_):
        self._dispose_elements()
        self.stack.close()

    def _dispose_elements(self):
        for element in self.elements.values():
            try:
                element.dispose()
            except Exception:
                pass
        self.elements.clear()

    def observe(self):
        if self.page.is_closed():
            raise RuntimeError("제어 중인 브라우저 탭이 닫혔습니다.")
        self._check_navigation(self.page.url)
        self._dispose_elements()
        targets, text, value_hashes = {}, [], {}
        for frame in self.page.frames[:12]:
            try:
                text.append(frame.locator("body").inner_text(timeout=2000)[:6000])
            except Exception:
                continue
            for element in frame.query_selector_all(_SELECTOR):
                keep = False
                try:
                    if len(targets) < 60 and element.is_visible():
                        info, fingerprint = _element_state(element)
                        if not info["secret"] and not info["disabled"] and info["type"] != "file":
                            key = f"e{len(targets) + 1}"
                            targets[key], self.elements[key] = info, element
                            value_hashes[key] = fingerprint
                            keep = True
                finally:
                    if not keep:
                        element.dispose()
        visible_tabs = {key: {"title": page.title()[:160], "url": page.url}
                        for key, page in self.tabs.items() if not page.is_closed()
                        and self._origin(page.url) in self.origins}
        screenshot = self.page.screenshot(scale="css", animations="disabled",
                                          mask=[self.page.locator('input[type="password"]')])
        viewport = self.page.viewport_size
        active = next(key for key, page in self.tabs.items() if page is self.page)
        context = {"backend": "browser", "url": self.page.url, "active_tab": active,
                   "tabs": visible_tabs, **viewport,
                   "actions": ["click", "fill", "select", "press", "scroll", "back", "switch_tab",
                               "coordinate_click", "wait", "done", "blocked"]}
        observed_text = "\n".join(text)[:8000] + "\n" + "\n".join(
            f"{target['name']}: {target['value']}" for target in targets.values() if target["value"])
        return Observation(uuid.uuid4().hex, screenshot, observed_text, targets, context,
                           host_state={"value_hashes": value_hashes})

    def restore_after_approval(self):
        # Playwright sends input to its own page, never to the globally focused app.
        pass

    def validate_action(self, observation, decision):
        self._check_navigation(self.page.url)
        if (time.monotonic() - observation.captured_at > 120
                or self.page.url != observation.context["url"]):
            raise StaleObservation("페이지 주소 또는 관찰 유효기간이 변경되었습니다.")
        action = decision["action"]
        if action not in observation.context["actions"]:
            raise ValueError("브라우저에서 지원하지 않는 행동입니다.")
        if action in {"click", "fill", "select", "press"}:
            target = decision["target"]
            element = self.elements.get(target)
            try:
                current, fingerprint = _element_state(element) if element else (None, None)
            except Exception as exc:
                raise StaleObservation("관찰한 DOM 요소가 사라졌습니다.") from exc
            if current != observation.targets[target] or not current["connected"]:
                raise StaleObservation("조작 직전 DOM 요소가 변경되었습니다.")
            if fingerprint != observation.host_state.get("value_hashes", {}).get(target):
                raise StaleObservation("관찰한 입력값 전체가 변경되었습니다.")
            if current["secret"] or current["type"] == "file":
                raise ValueError("비밀 입력 및 파일 업로드는 사용자가 직접 처리해야 합니다.")
            if action == "click" and current["href"]:
                self._check_navigation(current["href"])
            if action == "select" and decision["text"] not in current["options"]:
                raise ValueError("관찰한 선택지에 없는 값을 선택할 수 없습니다.")
        elif action == "switch_tab":
            page = self.tabs.get(decision["target"])
            if page is None or page.is_closed():
                raise StaleObservation("관찰한 탭이 닫혔습니다.")
            self._check_navigation(page.url)
            if page.url != observation.context["tabs"][decision["target"]]["url"]:
                raise StaleObservation("선택할 탭의 주소가 변경되었습니다.")
        elif action == "coordinate_click":
            screenshot = self.page.screenshot(scale="css", animations="disabled",
                                              mask=[self.page.locator('input[type="password"]')])
            if screenshot != observation.screenshot:
                raise StaleObservation("좌표 클릭 직전 화면 픽셀이 변경되었습니다.")

    def execute(self, decision, check):
        check()
        action = decision["action"]
        element = self.elements.get(decision.get("target"))
        if action == "click":
            element.click(timeout=3000)
        elif action == "fill":
            element.fill(decision["text"], timeout=3000)
            actual = element.evaluate("el => el.isContentEditable ? el.innerText : el.value")
            if actual != decision["text"]:
                raise RuntimeError("입력값을 다시 읽은 결과가 요청과 다릅니다.")
        elif action == "press":
            element.press(decision["key"], timeout=3000)
        elif action == "select":
            element.select_option(label=decision["text"], timeout=3000)
            if element.evaluate("el => el.selectedOptions[0]?.label") != decision["text"]:
                raise RuntimeError("선택한 옵션의 읽기 검증에 실패했습니다.")
        elif action == "scroll":
            self.page.mouse.wheel(0, 600 if decision["direction"] == "down" else -600)
        elif action == "back":
            self.page.go_back(wait_until="domcontentloaded", timeout=10000)
        elif action == "switch_tab":
            self.page = self.tabs[decision["target"]]
            self.page.bring_to_front()
        elif action == "coordinate_click":
            self.page.mouse.click(decision["x"], decision["y"])
        elif action != "wait":
            raise ValueError("지원하지 않는 브라우저 행동입니다.")
        _wait(check, 0.5 if action == "wait" else 0.2)


class WindowsComputerBackend:
    KEYS = {"Enter": "{ENTER}", "Tab": "{TAB}", "Escape": "{ESC}",
            "ArrowDown": "{DOWN}", "ArrowUp": "{UP}", "ArrowLeft": "{LEFT}",
            "ArrowRight": "{RIGHT}", "Home": "{HOME}", "End": "{END}",
            "PageDown": "{PGDN}", "PageUp": "{PGUP}", "Backspace": "{BACKSPACE}"}
    HOTKEYS = {"Ctrl+S": "^s", "Ctrl+Shift+S": "^+s", "Ctrl+A": "^a", "Ctrl+C": "^c",
               "Ctrl+V": "^v", "Ctrl+Z": "^z", "Ctrl+Y": "^y", "Ctrl+F": "^f",
               "Ctrl+O": "^o", "Ctrl+N": "^n", "Alt+F4": "%{F4}", "F5": "{F5}"}
    BLOCKED_APPS = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "regedit.exe",
                    "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe", "python.exe", "pythonw.exe"}

    def __init__(self, window_title="", app="", checkpoint=check_turn_cancelled, app_arguments=()):
        from core.windows_automation import WindowsAutomationRuntime
        self.automation = WindowsAutomationRuntime()
        self.window_title, self.app, self.checkpoint = window_title, app, checkpoint
        self.app_arguments = list(app_arguments)
        self.window = None
        self.controls, self.targets, self.windows = {}, {}, {}
        self._typing_identity = None
        self._scope_root = None
        self._scope_started = None
        self.app_name = "Windows"
        self._dpi_setter, self._dpi_previous = None, None
        self._observation = None
        self._pending_new_window = None

    def _choose_window(self, windows):
        import psutil
        if not self.window_title and not self.app:
            raise ValueError("대상 창 제목이나 실행할 앱이 필요합니다.")
        executable = None
        arguments = []
        if len(self.app_arguments) > 8 or (self.app_arguments and not self.app):
            raise ValueError("앱 문서 인자는 실행할 앱과 최대 8개 파일만 허용됩니다.")
        for argument in self.app_arguments:
            file = Path(argument)
            if not file.is_absolute() or not file.is_file():
                raise ValueError("앱 인자는 실제 존재하는 절대 파일 경로여야 합니다.")
            arguments.append(str(file.resolve()))
        if arguments and not self.window_title:
            raise ValueError("문서를 열 때는 대상 창 제목도 지정해야 합니다.")
        if self.app:
            from plugins.windows_control import WindowsControlPlugin
            executable = Path(WindowsControlPlugin._resolve_target(self.app))
            if not executable.is_absolute() or not executable.is_file() or executable.suffix.casefold() != ".exe":
                raise ValueError("자동 실행할 앱의 실제 실행 파일을 특정하지 못했습니다.")
            if executable.name.casefold() in self.BLOCKED_APPS:
                raise ValueError("터미널·스크립트 실행기는 화면 조작 대상으로 사용할 수 없습니다.")

        def matches_app(window):
            if executable is None:
                return True
            try:
                actual = Path(psutil.Process(window.process_id).exe())
                if actual.resolve() == executable.resolve():
                    return True
                # Windows' Notepad launcher delegates to the installed packaged application.
                return (executable.name.casefold() == actual.name.casefold() == "notepad.exe"
                        and executable.parent == Path(os.environ.get("WINDIR", "C:/Windows")) / "System32"
                        and "microsoft.windowsnotepad_" in str(actual).casefold())
            except (psutil.Error, OSError):
                return False

        def title_matches(candidates):
            needle = self.window_title.casefold()
            exact = [w for w in candidates if w.title.casefold() == needle]
            return exact or [w for w in candidates if needle in w.title.casefold()]

        candidates = [w for w in windows if matches_app(w)]
        matches = title_matches(candidates) if self.window_title else candidates
        if not matches and executable:
            from plugins.windows_control import WindowsControlPlugin
            launched = WindowsControlPlugin().execute_tool("windows_launch_app", {
                "target": str(executable), "arguments": arguments, "elevation": "never"})
            deadline = time.monotonic() + 15
            prior_handles = {w.handle for w in windows}
            matches = []
            while time.monotonic() < deadline:
                self.checkpoint()
                matches = [w for w in self.automation.list_windows() if matches_app(w)
                           and (self.window_title or w.handle not in prior_handles)]
                if self.window_title:
                    matches = title_matches(matches)
                if matches:
                    break
                _wait(self.checkpoint, 0.1)
            if not matches:
                raise RuntimeError("앱 실행 후 새 대상 창을 확인하지 못했습니다: " + launched.status.value)
        if len(matches) != 1:
            raise ValueError("대상 창을 하나로 특정할 수 없습니다. windows_list_handles로 창 제목을 확인하세요.")
        return matches[0]

    def __enter__(self):
        if os.name != "nt":
            raise RuntimeError("Windows Computer Use는 Windows에서만 사용할 수 있습니다.")
        import psutil
        from core.windows_automation import windows_com_apartment
        try:
            setter = ctypes.WinDLL("user32", use_last_error=True).SetThreadDpiAwarenessContext
            setter.argtypes, setter.restype = [ctypes.c_void_p], ctypes.c_void_p
            previous = setter(ctypes.c_void_p(-4))  # per-monitor aware v2, scoped to this worker thread
            if previous:
                self._dpi_setter, self._dpi_previous = setter, previous
        except (AttributeError, OSError):
            pass
        initialized = False
        try:
            self._com_scope = windows_com_apartment()
            self._com_scope.__enter__()
            initialized = True
            self.checkpoint()
            self.window = self._choose_window(self.automation.list_windows())
            process = psutil.Process(self.window.process_id)
            self.app_name = process.name().casefold()
            if self.app_name in self.BLOCKED_APPS:
                raise ValueError("터미널·스크립트 실행기 조작은 전용 도구를 사용해야 합니다.")
            self._scope_root = self.window.handle
            self._scope_started = process.create_time()
            self.automation.focus(self.window.handle)
            return self
        except BaseException:
            if initialized:
                self._com_scope.__exit__(None, None, None)
            self._restore_dpi()
            raise

    def _restore_dpi(self):
        if self._dpi_setter and self._dpi_previous:
            self._dpi_setter(self._dpi_previous)
            self._dpi_setter, self._dpi_previous = None, None

    def __exit__(self, *_):
        self.controls.clear()
        self._com_scope.__exit__(None, None, None)
        self._restore_dpi()

    def _scope_windows(self):
        if self._scope_root is None:
            return [self.window]
        return [w for w in self.automation.list_windows() if w.process_id == self.window.process_id]

    def _follow_owned_dialog(self):
        if self._scope_root is None:
            return
        windows = self._scope_windows()
        foreground = next((w for w in windows if w.foreground), None)
        if foreground and (foreground.handle == self._scope_root
                           or self.automation.is_owned_window(foreground.handle, self._scope_root)):
            self.window = foreground
        elif foreground and self._pending_new_window:
            prior, expires = self._pending_new_window
            if time.monotonic() <= expires and foreground.handle not in prior:
                self.window = foreground
                self._scope_root = foreground.handle
                self._pending_new_window = None
        if self._pending_new_window and time.monotonic() > self._pending_new_window[1]:
            self._pending_new_window = None

    def _root(self):
        window = self.automation.verify_foreground(self.window.handle)
        if window.process_id != self.window.process_id:
            raise StaleObservation("창 Handle의 소유 프로세스가 변경되었습니다.")
        if self._scope_started is not None:
            import psutil
            if psutil.Process(window.process_id).create_time() != self._scope_started:
                raise StaleObservation("대상 앱 프로세스가 다시 시작되었습니다.")
        return self.automation._accessibility_root(self.window.handle)

    @staticmethod
    def _screen_point(bounds, size, x, y):
        left, top, right, bottom = bounds
        if size[0] <= 0 or size[1] <= 0 or right <= left or bottom <= top:
            raise ValueError("화면 캡처의 좌표 기준이 올바르지 않습니다.")
        if not 0 <= x < size[0] or not 0 <= y < size[1]:
            raise ValueError("마우스 좌표가 캡처 영역을 벗어났습니다.")
        return (left + min(right - left - 1, round(x * (right - left) / size[0])),
                top + min(bottom - top - 1, round(y * (bottom - top) / size[1])))

    @staticmethod
    def _image_rectangle(bounds, size, rectangle):
        left, top, right, bottom = bounds
        if right <= left or bottom <= top:
            raise ValueError("대상 창의 크기를 확인할 수 없습니다.")
        return [round((rectangle[0] - left) * size[0] / (right - left)),
                round((rectangle[1] - top) * size[1] / (bottom - top)),
                round((rectangle[2] - left) * size[0] / (right - left)),
                round((rectangle[3] - top) * size[1] / (bottom - top))]

    def _capture(self, root, candidates):
        from PIL import ImageDraw
        bounds = self.automation._rectangle_value(root.rectangle())
        image = root.capture_as_image()
        secrets = []
        for _score, wrapper, metadata in candidates:
            if self._secret_control(wrapper, metadata):
                rectangle = metadata.get("rectangle")
                if not rectangle or len(rectangle) != 4:
                    raise RuntimeError("비밀 입력 영역의 위치를 확인할 수 없습니다.")
                secrets.append(self._image_rectangle(bounds, image.size, rectangle))
        painter = ImageDraw.Draw(image)
        for rectangle in secrets:
            painter.rectangle(rectangle, fill="black")
        return image, bounds, secrets

    @staticmethod
    def _secret_control(wrapper, metadata):
        return bool(wrapper.element_info.element.CurrentIsPassword or (
            metadata.get("control_type") in {"Edit", "Document"} and re.search(
                r"password|passwd|api.?key|access.?token|refresh.?token|secret|비밀번호|인증.?토큰|API.?키",
                str(metadata.get("name", "")) + " " + str(metadata.get("automation_id", "")), re.I)))

    def observe(self):
        self._follow_owned_dialog()
        from core.windows_automation import WindowsAutomationRuntime
        root = self._root()
        targets, controls, value_hashes, focused = {}, {}, {}, []
        self._typing_identity = None
        candidates = self.automation._accessibility_candidates(
            self.window.handle, allow_type_only=True, include_disabled=True)
        image, bounds, secrets = self._capture(root, candidates)
        for _score, wrapper, metadata in candidates:
            try:
                if self._secret_control(wrapper, metadata):
                    continue
                if not getattr(wrapper, "is_enabled", lambda: True)():
                    continue
                if len(targets) == 60:
                    break
                key = f"e{len(targets) + 1}"
                detail = dict(metadata)
                if len(detail.get("rectangle", ())) == 4:
                    detail["bounds"] = self._image_rectangle(bounds, image.size, detail["rectangle"])
                if detail["control_type"] in {"Edit", "Document"}:
                    try:
                        value = self.automation._read_wrapper_value(wrapper)
                        canonical = WindowsAutomationRuntime._canonical_edit_value(value, detail.get("class_name", ""))
                        detail["value"], detail["value_length"] = canonical[:1000], len(canonical)
                        value_hashes[key] = _value_hash(value)
                    except RuntimeError:
                        pass
                actions = []
                if detail["control_identity"].get("stable"):
                    rectangle = detail.get("rectangle", ())
                    if len(rectangle) == 4 and rectangle[2] > rectangle[0] and rectangle[3] > rectangle[1]:
                        actions.extend(["double_click", "right_click"])
                    if WindowsAutomationRuntime._activation_pattern(wrapper)[1] is not None:
                        actions.append("click")
                    if detail["control_type"] in {"Edit", "Document"}:
                        if key in value_hashes:
                            actions.append("fill")
                        if getattr(wrapper.element_info.element, "CurrentIsKeyboardFocusable", False):
                            actions.append("press")
                detail["actions"] = actions
                targets[key], controls[key] = detail, wrapper
                if wrapper.has_keyboard_focus():
                    focused.append(detail["control_identity"])
            except Exception:
                continue
        self.controls, self.targets = controls, targets
        windows = self._scope_windows()
        self.windows = {f"w{w.handle}": w for w in windows}
        text = "\n".join(str(t.get("name", "")) + "\n" + str(t.get("value", ""))
                         for t in targets.values())[:12000]
        observation = Observation(uuid.uuid4().hex, _png(image), text, targets, {
            "backend": "windows", "handle": self.window.handle, "title": root.window_text(),
            "app_name": self.app_name, "width": image.width, "height": image.height, "bounds": bounds,
            "windows": {key: {"handle": w.handle, "title": getattr(w, "title", self.window_title),
                               "process_id": w.process_id} for key, w in self.windows.items()},
            "actions": ["click", "double_click", "right_click", "fill", "press", "hotkey", "scroll",
                        "coordinate_click", "move", "drag", "type_text", "switch_window", "wait", "done", "blocked"],
        }, host_state={"value_hashes": value_hashes, "secret_rectangles": secrets,
                       "focused_identity": dict(focused[0]) if len(focused) == 1 else None})
        self._observation = observation
        return observation

    def restore_after_approval(self):
        window = self.automation.find_window(handle=self.window.handle)
        if window.process_id != self.window.process_id:
            raise StaleObservation("승인 도중 대상 앱이 변경되었습니다.")
        self.automation.focus(self.window.handle)

    def _verify_typing_focus(self, identity):
        if not identity or not identity.get("stable"):
            raise ValueError("관찰 당시 입력 포커스의 안정적인 identity가 없습니다.")
        try:
            wrapper, metadata = self.automation._accessibility_text_control(
                self.window.handle, expected_identity=identity, control_types=(), allow_type_only=True,
                require_unique=True, require_keyboard_focus=True, require_foreground=True)
        except (LookupError, RuntimeError) as exc:
            raise StaleObservation("관찰한 입력 요소의 포커스 또는 identity가 변경되었습니다.") from exc
        if self._secret_control(wrapper, metadata):
            raise ValueError("비밀번호 입력창은 사용자가 직접 처리해야 합니다.")

    def _validate_pointer(self, observation, root, points):
        from PIL import Image, ImageChops
        candidates = self.automation._accessibility_candidates(
            self.window.handle, allow_type_only=True, include_disabled=True)
        current, bounds, secrets = self._capture(root, candidates)
        if bounds != observation.context["bounds"] or current.size != (observation.context["width"], observation.context["height"]):
            raise StaleObservation("마우스 조작 직전 창 위치 또는 캡처 크기가 변경되었습니다.")
        previous = Image.open(io.BytesIO(observation.screenshot)).convert("RGB")
        for x, y in points:
            if any(l <= x <= r and t <= y <= b for l, t, r, b in secrets):
                raise ValueError("비밀 입력 영역은 마우스로 조작할 수 없습니다.")
            point = self._screen_point(bounds, current.size, x, y)
            self.automation.verify_pointer_target(self.window.handle, *point)
            region = (max(0, x - 24), max(0, y - 24), min(current.width, x + 25), min(current.height, y + 25))
            difference = ImageChops.difference(previous.crop(region), current.convert("RGB").crop(region))
            pixels = difference.tobytes()
            changed = sum(any(pixels[index:index + 3]) for index in range(0, len(pixels), 3))
            # shortcut: tolerate only a narrow blinking caret; reobserve larger visual changes.
            if changed > min(32, max(1, difference.width * difference.height // 50)):
                raise StaleObservation("마우스 대상 영역이 변경되었습니다.")

    def validate_action(self, observation, decision):
        root = self._root()
        if observation.context.get("handle", self.window.handle) != self.window.handle:
            raise StaleObservation("관찰한 대상 창이 변경되었습니다.")
        if time.monotonic() - observation.captured_at > 120:
            raise StaleObservation("화면 관찰의 유효기간이 지났습니다.")
        action = decision["action"]
        if action not in observation.context["actions"]:
            raise ValueError("Windows에서 지원하지 않는 행동입니다.")
        self._observation = observation
        if action in {"click", "double_click", "right_click", "fill", "press"}:
            supported = observation.targets[decision["target"]].get("actions")
            if supported is not None and action not in supported:
                raise ValueError("관찰한 UI 요소에서 지원하지 않는 행동입니다.")
            identity = observation.targets[decision["target"]]["control_identity"]
            if not identity["stable"]:
                raise ValueError("UI 요소의 안정적인 identity를 확인하지 못했습니다.")
            current = self.automation.verify_accessibility_control(
                self.window.handle, expected_identity=identity, allow_type_only=True,
                require_unique=True, require_keyboard_focus=False)
            original = observation.targets[decision["target"]]
            if any(current.get(k) != original.get(k) for k in ("name", "automation_id", "rectangle", "control_type")):
                raise StaleObservation("UI 요소의 이름 또는 위치가 변경되었습니다.")
            wrapper = self.controls[decision["target"]]
            if self._secret_control(wrapper, current):
                raise ValueError("비밀번호 입력창은 사용자가 직접 처리해야 합니다.")
            fingerprint = observation.host_state.get("value_hashes", {}).get(decision["target"])
            if action == "fill" and fingerprint is None:
                raise ValueError("입력값 전체를 관찰하지 못한 요소는 교체할 수 없습니다.")
            if fingerprint is not None and _value_hash(self.automation._read_wrapper_value(wrapper)) != fingerprint:
                raise StaleObservation("사용자가 입력값 전체를 변경했습니다.")
            if action in {"double_click", "right_click"}:
                left, top, right, bottom = self._image_rectangle(
                    observation.context["bounds"],
                    (observation.context["width"], observation.context["height"]), original["rectangle"])
                self._validate_pointer(observation, root, [((left + right) // 2, (top + bottom) // 2)])
        elif action in {"coordinate_click", "move", "drag"}:
            points = [(decision["x"], decision["y"])]
            if action == "drag":
                points.append((decision["to_x"], decision["to_y"]))
            self._validate_pointer(observation, root, points)
        elif action == "type_text":
            identity = observation.host_state.get("focused_identity")
            self._verify_typing_focus(identity)
            self._typing_identity = identity
        elif action == "hotkey":
            if decision["key"] not in self.HOTKEYS:
                raise ValueError("허용되지 않은 단축키입니다.")
            # App shortcuts can act on the current editor; never dispatch into secret fields.
            candidates = self.automation._accessibility_candidates(
                self.window.handle, allow_type_only=True, include_disabled=True)
            if any(w.has_keyboard_focus() and self._secret_control(w, m) for _, w, m in candidates):
                raise ValueError("비밀 입력창에서는 단축키를 실행할 수 없습니다.")
        elif action == "switch_window":
            target = self.windows.get(decision["target"])
            expected = observation.context.get("windows", {}).get(decision["target"])
            current = next((w for w in self._scope_windows() if target and w.handle == target.handle), None)
            if not current or not expected or current.process_id != expected["process_id"] or current.title != expected["title"]:
                raise StaleObservation("관찰한 앱 창이 변경되거나 닫혔습니다.")

    def _fill_document_text(self, decision, identity, options, check):
        from core.desktop_messaging import DesktopMessagingRuntime
        from core.windows_automation import WindowsAutomationRuntime
        self.automation.activate_accessibility_control(
            self.window.handle, invoke=False, require_keyboard_focus=True, **options)
        check()
        self._root()
        self._verify_typing_focus(identity)
        wrapper, metadata = self.automation._accessibility_text_control(
            self.window.handle, control_types=("Document",), require_keyboard_focus=True, **options)
        observed_hash = self._observation.host_state["value_hashes"][decision["target"]]
        if _value_hash(self.automation._read_wrapper_value(wrapper)) != observed_hash:
            raise StaleObservation("입력 직전 문서 내용이 변경되었습니다.")
        DesktopMessagingRuntime._hotkey(("CTRL", "A"))
        if not decision["text"]:
            check()
            self._root()
            self._verify_typing_focus(identity)
            DesktopMessagingRuntime._press("BACKSPACE")
        for offset in range(0, len(decision["text"]), 64):
            check()
            self._root()
            self._verify_typing_focus(identity)
            DesktopMessagingRuntime._type_unicode(decision["text"][offset:offset + 64])
        _wait(check, 0.08)
        check()
        self._root()
        self._verify_typing_focus(identity)
        wrapper, metadata = self.automation._accessibility_text_control(
            self.window.handle, control_types=("Document",), require_keyboard_focus=True, **options)
        actual = WindowsAutomationRuntime._canonical_edit_value(
            self.automation._read_wrapper_value(wrapper), metadata.get("class_name", ""))
        expected = WindowsAutomationRuntime._canonical_edit_value(decision["text"])
        if actual != expected:
            raise RuntimeError("문서의 실제 입력 결과가 요청한 전체 텍스트와 다릅니다.")

    def execute(self, decision, check):
        root = self._root()
        action = decision["action"]
        target = decision.get("target")
        if action in {"click", "double_click", "right_click", "fill", "press"}:
            identity = self.targets[target]["control_identity"]
            options = dict(expected_identity=identity, allow_type_only=True, require_unique=True,
                           require_foreground=True)
            check()
            if action == "click":
                result = self.automation.activate_accessibility_control(self.window.handle, **options)
                if result["activation"] not in {"invoke", "select"}:
                    raise RuntimeError("UIA 클릭을 지원하지 않습니다. 좌표 fallback을 명시적으로 선택하세요.")
            elif action in {"double_click", "right_click"}:
                from pywinauto.mouse import double_click, click
                metadata = self.automation.verify_accessibility_control(self.window.handle, **options)
                left, top, right, bottom = metadata["rectangle"]
                point = ((left + right) // 2, (top + bottom) // 2)
                self.automation.verify_pointer_target(self.window.handle, *point)
                if action == "double_click":
                    double_click(coords=point)
                else:
                    click(button="right", coords=point)
            elif action == "fill":
                wrapper = self.controls[target]
                if self.targets[target]["control_type"] == "Document":
                    try:
                        value_pattern = getattr(wrapper, "iface_value", None)
                    except Exception:
                        value_pattern = None
                    if value_pattern is None:
                        self._fill_document_text(decision, identity, options, check)
                    else:
                        self.automation.set_accessibility_text(self.window.handle, decision["text"],
                                                              control_types=("Document",), **options)
                else:
                    self.automation.set_accessibility_text(self.window.handle, decision["text"],
                                                          control_types=("Edit",), **options)
            else:
                from pywinauto.keyboard import send_keys
                self.automation.activate_accessibility_control(self.window.handle, invoke=False,
                                                               require_keyboard_focus=True, **options)
                check()
                self.automation.verify_accessibility_control(self.window.handle, **options)
                send_keys(self.KEYS[decision["key"]], pause=0.01)
        elif action == "hotkey":
            from pywinauto.keyboard import send_keys
            check()
            self.validate_action(self._observation, decision)
            prior = {w.handle for w in self._scope_windows()} if decision["key"] == "Ctrl+N" else None
            send_keys(self.HOTKEYS[decision["key"]], pause=0.01)
            if prior is not None:
                self._pending_new_window = (prior, time.monotonic() + 3)
        elif action == "scroll":
            from pywinauto.mouse import scroll
            bounds = self.automation._rectangle_value(root.rectangle())
            point = ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
            check()
            self._root()
            self.automation.verify_pointer_target(self.window.handle, *point)
            scroll(coords=point, wheel_dist=-3 if decision["direction"] == "down" else 3)
        elif action in {"coordinate_click", "move", "drag"}:
            from pywinauto import mouse
            observation = self._observation
            check()
            self.validate_action(observation, decision)
            bounds = observation.context["bounds"]
            size = (observation.context["width"], observation.context["height"])
            start = self._screen_point(bounds, size, decision["x"], decision["y"])
            if action == "coordinate_click":
                mouse.click(coords=start)
            elif action == "move":
                mouse.move(coords=start)
            else:
                end = self._screen_point(bounds, size, decision["to_x"], decision["to_y"])
                point = start
                mouse.press(coords=start)
                try:
                    for step in range(1, 11):
                        check()
                        self._root()
                        point = tuple(round(a + (b - a) * step / 10) for a, b in zip(start, end))
                        self.automation.verify_pointer_target(self.window.handle, *point)
                        mouse.move(coords=point)
                finally:
                    mouse.release(coords=point)
        elif action == "type_text":
            from core.desktop_messaging import DesktopMessagingRuntime
            for offset in range(0, len(decision["text"]), 64):
                check()
                self._root()
                self._verify_typing_focus(self._typing_identity)
                DesktopMessagingRuntime._type_unicode(decision["text"][offset:offset + 64])
        elif action == "switch_window":
            check()
            self.validate_action(self._observation, decision)
            self.window = self.automation.focus(self.windows[target].handle)
            self._scope_root = self.window.handle
        elif action != "wait":
            raise ValueError("지원하지 않는 Windows 행동입니다.")
        _wait(check, 0.5 if action == "wait" else 0.2)
