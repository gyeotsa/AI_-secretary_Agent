"""Task-scoped Playwright and handle-scoped Windows input adapters."""
from __future__ import annotations

import io
import hashlib
import os
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from core.computer_use import Observation, StaleObservation


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
        value: value.slice(0, 1000), _full_value: value,
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

    def __init__(self, window_title):
        from core.windows_automation import WindowsAutomationRuntime
        self.automation = WindowsAutomationRuntime()
        self.window_title = window_title
        self.window = None
        self.controls = {}
        self.targets = {}
        self._typing_identity = None

    def __enter__(self):
        if os.name != "nt":
            raise RuntimeError("Windows Computer Use는 Windows에서만 사용할 수 있습니다.")
        import comtypes
        import psutil
        comtypes.CoInitialize()
        try:
            windows = self.automation.list_windows()
            matches = [w for w in windows if w.title.casefold() == self.window_title.casefold()]
            matches = matches or [w for w in windows if self.window_title.casefold() in w.title.casefold()]
            if len(matches) != 1:
                raise ValueError("대상 창을 하나로 특정할 수 없습니다. windows_list_handles로 창 제목을 확인하세요.")
            self.window = matches[0]
            process_name = psutil.Process(self.window.process_id).name().casefold()
            if process_name in {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "regedit.exe"}:
                raise ValueError("터미널·레지스트리 조작은 전용 도구를 사용해야 합니다.")
            self.automation.focus(self.window.handle)
            return self
        except BaseException:
            comtypes.CoUninitialize()
            raise

    def __exit__(self, *_):
        import comtypes
        self.controls.clear()
        comtypes.CoUninitialize()

    def _root(self):
        window = self.automation.verify_foreground(self.window.handle)
        if window.process_id != self.window.process_id:
            raise StaleObservation("창 Handle의 소유 프로세스가 변경되었습니다.")
        return self.automation._accessibility_root(self.window.handle)

    def observe(self):
        root = self._root()
        targets, controls, value_hashes, focused = {}, {}, {}, []
        self._typing_identity = None
        for _score, wrapper, metadata in self.automation._accessibility_candidates(
                self.window.handle, allow_type_only=True)[:60]:
            try:
                if wrapper.element_info.element.CurrentIsPassword:
                    continue
                key = f"e{len(targets) + 1}"
                detail = dict(metadata)
                if detail["control_type"] in {"Edit", "Document"}:
                    try:
                        value = self.automation._read_wrapper_value(wrapper)
                        detail["value"] = value[:1000]
                        value_hashes[key] = _value_hash(value)
                    except RuntimeError:
                        pass
                targets[key], controls[key] = detail, wrapper
                if wrapper.has_keyboard_focus():
                    focused.append(detail["control_identity"])
            except Exception:
                continue
        self.controls = controls
        self.targets = targets
        image = root.capture_as_image()
        bounds = self.automation._rectangle_value(root.rectangle())
        text = "\n".join(str(t.get("name", "")) + "\n" + str(t.get("value", ""))
                         for t in targets.values())[:12000]
        return Observation(uuid.uuid4().hex, _png(image), text, targets, {
            "backend": "windows", "handle": self.window.handle, "title": root.window_text(),
            "width": image.width, "height": image.height, "bounds": bounds,
            "actions": ["click", "fill", "press", "scroll", "coordinate_click", "type_text", "wait", "done", "blocked"],
        }, host_state={"value_hashes": value_hashes,
                       "focused_identity": dict(focused[0]) if len(focused) == 1 else None})

    def restore_after_approval(self):
        window = self.automation.find_window(handle=self.window.handle)
        if window.process_id != self.window.process_id:
            raise StaleObservation("승인 도중 대상 앱이 변경되었습니다.")
        self.automation.focus(self.window.handle)

    def _verify_typing_focus(self, identity):
        if not identity or not identity.get("stable"):
            raise ValueError("관찰 당시 입력 포커스의 안정적인 identity가 없습니다.")
        try:
            wrapper, _metadata = self.automation._accessibility_text_control(
                self.window.handle, expected_identity=identity, control_types=(), allow_type_only=True,
                require_unique=True, require_keyboard_focus=True, require_foreground=True)
        except (LookupError, RuntimeError) as exc:
            raise StaleObservation("관찰한 입력 요소의 포커스 또는 identity가 변경되었습니다.") from exc
        if wrapper.element_info.element.CurrentIsPassword:
            raise ValueError("비밀번호 입력창은 사용자가 직접 처리해야 합니다.")

    def validate_action(self, observation, decision):
        root = self._root()
        if time.monotonic() - observation.captured_at > 120:
            raise StaleObservation("화면 관찰의 유효기간이 지났습니다.")
        action = decision["action"]
        if action not in observation.context["actions"]:
            raise ValueError("Windows에서 지원하지 않는 행동입니다.")
        if action in {"click", "fill", "press"}:
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
            if wrapper.element_info.element.CurrentIsPassword:
                raise ValueError("비밀번호 입력창은 사용자가 직접 처리해야 합니다.")
            fingerprint = observation.host_state.get("value_hashes", {}).get(decision["target"])
            if action == "fill" and fingerprint is None:
                raise ValueError("입력값 전체를 관찰하지 못한 요소는 교체할 수 없습니다.")
            if fingerprint is not None and _value_hash(self.automation._read_wrapper_value(wrapper)) != fingerprint:
                raise StaleObservation("사용자가 입력값 전체를 변경했습니다.")
        elif action in {"coordinate_click", "type_text"}:
            if (self.automation._rectangle_value(root.rectangle()) != observation.context["bounds"]
                    or _png(root.capture_as_image()) != observation.screenshot):
                raise StaleObservation("좌표 입력 직전 창 위치 또는 픽셀이 변경되었습니다.")
            if action == "type_text":
                identity = observation.host_state.get("focused_identity")
                self._verify_typing_focus(identity)
                self._typing_identity = identity

    def execute(self, decision, check):
        root = self._root()
        action = decision["action"]
        target = decision.get("target")
        if action in {"click", "fill", "press"}:
            identity = self.targets[target]["control_identity"]
            options = dict(expected_identity=identity, allow_type_only=True, require_unique=True,
                           require_foreground=True)
            check()
            if action == "click":
                result = self.automation.activate_accessibility_control(self.window.handle, **options)
                if result["activation"] not in {"invoke", "select"}:
                    raise RuntimeError("UIA 클릭을 지원하지 않습니다. 좌표 fallback을 명시적으로 선택하세요.")
            elif action == "fill":
                self.automation.set_accessibility_text(self.window.handle, decision["text"],
                                                      control_types=("Edit", "Document"), **options)
            else:
                from pywinauto.keyboard import send_keys
                self.automation.activate_accessibility_control(self.window.handle, invoke=False,
                                                               require_keyboard_focus=True, **options)
                check()
                self.automation.verify_accessibility_control(self.window.handle, **options)
                send_keys(self.KEYS[decision["key"]], pause=0.01)
        elif action == "scroll":
            from pywinauto.mouse import scroll
            bounds = root.rectangle()
            check()
            self._root()
            scroll(coords=((bounds.left + bounds.right) // 2, (bounds.top + bounds.bottom) // 2),
                   wheel_dist=-3 if decision["direction"] == "down" else 3)
        elif action == "coordinate_click":
            from pywinauto.mouse import click
            bounds = root.rectangle()
            check()
            self._root()
            click(coords=(bounds.left + decision["x"], bounds.top + decision["y"]))
        elif action == "type_text":
            from core.desktop_messaging import DesktopMessagingRuntime
            check()
            self._root()
            self._verify_typing_focus(self._typing_identity)
            DesktopMessagingRuntime._type_unicode(decision["text"])
        elif action != "wait":
            raise ValueError("지원하지 않는 Windows 행동입니다.")
        _wait(check, 0.5 if action == "wait" else 0.2)
