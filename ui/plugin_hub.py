"""In-app browser for built-in plugins and user supplied MCP connections."""
from __future__ import annotations

import json
import re
import threading
import urllib.request
from urllib.parse import urlsplit, unquote
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QTextEdit,
    QVBoxLayout,
)

from core.mcp_bridge import connect_mcp, load_saved_mcp_plugins, manifest_for_url, saved_mcp_count
from .theme import set_widget_style


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("승인한 파일 주소의 리디렉션은 허용하지 않습니다. 최종 HTTPS 주소를 직접 입력하세요.")


class PluginHub(QDialog):
    operation_done = pyqtSignal(object)
    operation_failed = pyqtSignal(str)

    def __init__(self, registry, parent=None):
        super().__init__(parent)
        self.registry = registry
        self._running = False
        self._calendar_connecting = False
        self.setWindowTitle("플러그인 및 MCP")
        self.resize(980, 680)
        set_widget_style(self, "")
        self.operation_done.connect(self._done)
        self.operation_failed.connect(self._failed)
        self._statuses = []
        self._build()
        self.refresh()

    def _build(self):
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("플러그인 및 MCP")
        title.setObjectName("heading")
        header.addWidget(title)
        header.addStretch()
        self.kind = QComboBox()
        self.kind.addItems(["전체", "플러그인", "MCP"])
        self.kind.currentTextChanged.connect(self._filter)
        header.addWidget(self.kind)
        self.search = QLineEdit()
        self.search.setPlaceholderText("이름·설명 검색")
        self.search.textChanged.connect(self._filter)
        header.addWidget(self.search)
        root.addLayout(header)
        guide = QLabel("HTTPS MCP를 연결하거나 Python 플러그인 파일을 안전한 검토함에 보관할 수 있습니다.")
        guide.setObjectName("muted")
        root.addWidget(guide)

        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._show_details)
        body.addWidget(self.list, 1)
        detail = QFrame()
        detail.setObjectName("panel")
        detail_layout = QVBoxLayout(detail)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        detail_layout.addWidget(self.details, 1)
        body.addWidget(detail, 2)
        root.addLayout(body, 1)

        controls = QHBoxLayout()
        file_button = QPushButton("파일 추가")
        file_button.clicked.connect(self.add_file)
        controls.addWidget(file_button)
        link_button = QPushButton("링크 연결")
        link_button.clicked.connect(self.add_link)
        controls.addWidget(link_button)
        saved_button = QPushButton("저장된 MCP 다시 연결")
        saved_button.clicked.connect(self.reconnect_saved)
        controls.addWidget(saved_button)
        self.oauth_account_button = QPushButton("Google · Microsoft 연결")
        self.oauth_account_button.setAutoDefault(False)
        self.oauth_account_button.clicked.connect(self.open_oauth_account)
        controls.addWidget(self.oauth_account_button)
        self.calendar_button = QPushButton("네이버 캘린더 연결")
        self.calendar_button.setAutoDefault(False)
        self.calendar_button.clicked.connect(self.connect_calendar)
        controls.addWidget(self.calendar_button)
        self.calendar_disconnect_button = QPushButton("캘린더 연결 해제")
        self.calendar_disconnect_button.setAutoDefault(False)
        self.calendar_disconnect_button.clicked.connect(self.disconnect_calendar)
        controls.addWidget(self.calendar_disconnect_button)
        controls.addStretch()
        self.busy = QLabel("")
        self.busy.setObjectName("muted")
        controls.addWidget(self.busy)
        refresh = QPushButton("새로고침")
        refresh.clicked.connect(self.refresh)
        controls.addWidget(refresh)
        root.addLayout(controls)

    def open_oauth_account(self):
        from ui.oauth_account_dialog import open_oauth_account_dialog
        open_oauth_account_dialog(self.registry, self)

    def connect_calendar(self):
        if self._running:
            return
        plugin = self.registry.get_plugin("naver_calendar")
        if plugin is None:
            QMessageBox.warning(self, "네이버 캘린더", "캘린더 플러그인을 등록하려면 앱을 다시 실행하세요.")
            return
        if QMessageBox.question(self, "현재 Chrome 캘린더 연결",
                "공식 Playwright 확장 연결 화면에서 로그인된 캘린더 탭만 직접 선택하세요.\n"
                "쿠키·프로필은 복사하지 않고 현재 화면을 조회합니다. 일정 저장은 하지 않습니다.\n"
                "새 요청에서 5분 안에 Allow & select를 누르세요. 검사 종료/시간 초과 후 남은 요청은 무효입니다.\n"
                "서버 설치가 없다면 NAVER_INTEGRATION.md의 설치 안내를 따르세요.\n연결할까요?") != QMessageBox.StandardButton.Yes:
            return
        def connect():
            status = plugin.service.connect()
            plugin.service.observe()
            return f"캘린더 탭 연결·현재 화면 조회 확인: {status['account']}\n일정 등록은 별도 승인 후 진행합니다."
        self._calendar_connecting = True
        self._run(connect)
        self.busy.setText("Chrome 탭 선택 대기 · 최대 5분 · 연결 해제로 취소")

    def disconnect_calendar(self):
        if self._running and not self._calendar_connecting:
            return
        plugin = self.registry.get_plugin("naver_calendar")
        if plugin:
            if self._calendar_connecting:
                self.busy.setText("캘린더 연결 해제 중…")
                threading.Thread(target=plugin.service.disconnect, daemon=True,
                                 name="anis-calendar-disconnect").start()
            else:
                self._run(lambda: (plugin.service.disconnect(), "캘린더 연결을 해제했습니다.")[1])

    def refresh(self):
        self._statuses = list(self.registry.get_plugin_statuses())
        self._filter()

    def _filter(self, *_args):
        query = self.search.text().casefold().strip()
        kind = self.kind.currentText()
        self.list.clear()
        for status in self._statuses:
            is_mcp = status.name.startswith("mcp:")
            if kind == "MCP" and not is_mcp or kind == "플러그인" and is_mcp:
                continue
            text = f"{status.name} {' '.join(status.diagnostics)} {status.runtime_summary}"
            if query and query not in text.casefold():
                continue
            state = "연결됨" if status.connected is True or status.connection_state == "not_applicable" else "확인 필요"
            item = QListWidgetItem(f"{status.name}\n{state} · 도구 실행 {status.runtime_state}")
            item.setData(Qt.ItemDataRole.UserRole, status)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.details.setPlainText("조건에 맞는 연결이 없습니다.")

    def _show_details(self, current, _previous=None):
        status = current.data(Qt.ItemDataRole.UserRole) if current else None
        if status is None:
            return
        issues = "\n".join(f"• {item}" for item in status.diagnostics) or "• 발견된 문제가 없습니다."
        self.details.setPlainText(
            f"{status.name}  v{status.version}\n\n"
            f"설치: {status.installation_state}\n연결: {status.connection_state}\n"
            f"인증: {status.authentication_state}\n계약: {status.contract_state}\n"
            f"실행: {status.runtime_state}\n검증: {status.verification_state}\n\n"
            f"{status.runtime_summary}\n\n진단\n{issues}"
        )

    def add_file(self):
        source, _ = QFileDialog.getOpenFileName(
            self, "플러그인 또는 MCP 파일", "", "지원 파일 (*.json *.py)"
        )
        if not source:
            return
        path = Path(source)
        try:
            if path.stat().st_size > 2_000_000 or path.suffix.casefold() not in {".json", ".py"}:
                raise ValueError("2MB 이하의 JSON/Python 파일만 추가할 수 있습니다.")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "파일 확인", str(exc))
            return
        if path.suffix.casefold() == ".json":
            try:
                manifest = json.loads(self._read_file(path).decode("utf-8"))
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "파일 확인", f"JSON을 읽지 못했습니다.\n{exc}")
                return
            self._connect_mcp(manifest)
            return
        self._confirm_python(path)

    def add_link(self):
        value, accepted = QInputDialog.getText(self, "링크 연결", "MCP 엔드포인트 또는 .json/.py 링크:")
        url = value.strip()
        if not accepted or not url:
            return
        if Path(urlsplit(url).path).suffix.casefold() in {".json", ".py"}:
            if QMessageBox.question(
                self, "외부 파일 다운로드",
                "이 링크의 파일을 내려받을까요? Python 파일은 실행하지 않고 검토함에 보관합니다."
            ) != QMessageBox.StandardButton.Yes:
                return
            self._run(lambda: self._download(url))
            return
        try:
            manifest = manifest_for_url(url)
        except ValueError as exc:
            QMessageBox.warning(self, "링크 확인", str(exc))
            return
        self._connect_mcp(manifest)

    def _download(self, url: str):
        from plugins.browser import BrowserPlugin
        # Use the same credential policy as MCP, plus the existing public-URL gate.
        manifest_for_url(url)
        if urlsplit(url).scheme != "https":
            raise ValueError("파일 다운로드는 공개 HTTPS 주소만 허용합니다.")
        BrowserPlugin._validate_url(url)
        filename = Path(unquote(urlsplit(url).path)).name
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".json", ".py"}:
            raise ValueError("JSON 또는 Python 파일만 검토함에 보관할 수 있습니다.")
        request = urllib.request.Request(url, headers={"User-Agent": "JARVIS-PluginHub/1"})
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
            payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("다운로드 파일이 2MB를 초과합니다.")
        if suffix == ".json":
            # An untrusted downloaded manifest cannot approve its own new endpoint.
            json.loads(payload.decode("utf-8"))
            return self._stage_file(filename, payload, suffix)
        return self._stage_python(filename, payload)

    def _confirm_python(self, source: Path):
        if QMessageBox.question(
            self, "Python 플러그인 보관",
            "이 파일을 실행하지 않고 검토함에 보관할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run(lambda: self._stage_python(source.name, self._read_file(source)))

    @staticmethod
    def _read_file(path):
        with path.open("rb") as stream:
            content = stream.read(2_000_001)
        if len(content) > 2_000_000:
            raise ValueError("보관 파일은 2MB 이하여야 합니다.")
        return content

    def _stage_python(self, filename: str, payload: bytes):
        return self._stage_file(filename, payload, ".py")

    def _stage_file(self, filename: str, payload: bytes, suffix: str):
        if not isinstance(payload, bytes) or len(payload) > 2_000_000:
            raise ValueError("보관 파일은 2MB 이하의 bytes여야 합니다.")
        source = Path(filename)
        stem = re.sub(r"[^a-zA-Z0-9_]+", "_", source.stem).strip("_")
        if not stem:
            raise ValueError("올바른 플러그인 파일 이름이 필요합니다.")
        target = Path(__file__).resolve().parent.parent / "data" / "plugin_inbox" / f"{stem}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation is authoritative; don't overwrite after an exists race.
        with target.open("xb") as stream:
            try:
                stream.write(payload)
            except BaseException:
                stream.close()
                target.unlink(missing_ok=True)
                raise
        return f"{target.name}을 검토함에 보관했습니다: {target}. 코드 실행·MCP 연결은 하지 않았습니다. JSON은 파일 추가에서 별도로 승인하세요."

    def reconnect_saved(self):
        count = saved_mcp_count()
        if not count:
            QMessageBox.information(self, "저장된 MCP", "다시 연결할 MCP가 없습니다.")
            return
        if QMessageBox.question(
            self, "저장된 MCP 다시 연결", f"저장된 HTTPS MCP {count}개에 다시 연결할까요?"
        ) == QMessageBox.StandardButton.Yes:
            self._run(lambda: load_saved_mcp_plugins(self.registry))

    def _connect_mcp(self, manifest):
        from core.mcp_bridge import validate_manifest
        try:
            manifest = validate_manifest(manifest)
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "MCP 확인", str(exc))
            return
        if QMessageBox.question(
            self, "MCP 연결", f"{manifest['name']}\n{manifest['url']}\n이 서버에 연결해 도구 목록을 가져올까요?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run(lambda: connect_mcp(self.registry, manifest) and "MCP 연결을 추가했습니다.")

    def _run(self, operation):
        if self._running:
            return
        self._running = True
        self.busy.setText("연결 확인 중…")
        def work():
            try:
                self.operation_done.emit(operation())
            except Exception as exc:
                self.operation_failed.emit(f"{type(exc).__name__}: {exc}")
        threading.Thread(target=work, daemon=True, name="jarvis-plugin-connect").start()

    def _done(self, message):
        self._running = False
        self._calendar_connecting = False
        self.busy.clear()
        self.refresh()
        if isinstance(message, dict) and set(message) == {"connected", "failed"}:
            connected, failed = message["connected"], message["failed"]
            if failed:
                title = "일부 MCP 재연결 실패" if connected else "MCP 재연결 실패"
                QMessageBox.warning(self, title, f"재연결 성공 {connected}개 · 실패 {failed}개. 실패한 연결은 플러그인 진단에서 확인하세요.")
                return
            message = f"MCP 재연결 성공 {connected}개 · 실패 0개."
        QMessageBox.information(self, "작업 결과", str(message))

    def _failed(self, message):
        self._running = False
        self._calendar_connecting = False
        self.busy.clear()
        self.refresh()
        QMessageBox.warning(self, "연결 실패", message)

    def can_close_workspace_tab(self):
        return not self._running

    def done(self, result):
        if not self._running:
            super().done(result)

    def closeEvent(self, event):
        if self._running:
            event.ignore()
        else:
            super().closeEvent(event)
