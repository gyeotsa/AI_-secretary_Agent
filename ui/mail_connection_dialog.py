"""User-managed registration of the official Chrome extension connection token."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from core.browser_extension import BrowserExtensionCredentialsError
from .dialog_theme import apply_dark_dialog_theme


class MailConnectionDialog(QDialog):
    registration_changed = pyqtSignal()

    def __init__(self, credentials, parent=None):
        super().__init__(parent)
        self.credentials = credentials
        self.setWindowTitle("메일 자동 연결 설정")
        self.setMinimumWidth(580)
        apply_dark_dialog_theme(self)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Chrome의 공식 Playwright MCP 확장 설정에서 PLAYWRIGHT_MCP_EXTENSION_TOKEN 값을 복사해 아래에 붙여넣으세요.\n"
            "한 번 등록하면 토큰이 유효한 동안 Gmail·네이버 전환과 앱 재실행 시 연결 승인을 생략합니다.\n"
            "토큰은 반복 승인 없이 Chrome에 연결할 권한을 부여합니다. 채팅에 보내지 마세요.\n"
            "이 PC의 암호화 저장소에 저장하며, 소스·문서·RAG·로그에는 기록하지 않습니다."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(intro)
        self.token_input = QLineEdit()
        self.token_input.setAccessibleName("Playwright 확장 연결 토큰")
        self.token_input.setPlaceholderText("토큰 값만 입력 · 저장된 값은 표시하지 않음")
        self.token_input.setMaxLength(4096)
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setInputMethodHints(
            Qt.InputMethodHint.ImhSensitiveData | Qt.InputMethodHint.ImhNoPredictiveText
            | Qt.InputMethodHint.ImhNoAutoUppercase
        )
        layout.addWidget(self.token_input)
        actions = QHBoxLayout()
        self.save_button = QPushButton("자동 연결 토큰 암호화 저장")
        self.delete_button = QPushButton("등록된 토큰 삭제")
        for button in (self.save_button, self.delete_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        note = QLabel("등록 여부는 연결·로그인 성공을 뜻하지 않습니다. 토큰 변경·확장 재설치 시 다시 등록할 수 있습니다.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(note)
        footer = QHBoxLayout()
        footer.addStretch()
        self.close_button = QPushButton("닫기")
        self.close_button.setAutoDefault(False)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.token_input.textChanged.connect(self._update_controls)
        self.save_button.clicked.connect(self._save)
        self.delete_button.clicked.connect(self._delete)
        self.close_button.clicked.connect(self.reject)
        self._configured = False
        self._refresh_status()

    def _update_controls(self):
        self.save_button.setEnabled(bool(self.token_input.text().strip()))
        self.delete_button.setEnabled(self._configured)

    def _refresh_status(self):
        try:
            self._configured = self.credentials.status().get("configured") is True
            self.status_label.setText(
                "자동 연결 토큰: 등록됨 · 메일 연결·로그인은 아직 확인하지 않았습니다." if self._configured else
                "자동 연결 토큰: 미등록 · 기존 Allow & select 연결을 사용할 수 있습니다."
            )
        except BrowserExtensionCredentialsError as exc:
            self._configured = False
            self.status_label.setText(str(exc))
        except Exception:
            self._configured = False
            self.status_label.setText("자동 연결 등록 상태를 확인하지 못했습니다.")
        self._update_controls()

    def _save(self):
        try:
            self.credentials.save(self.token_input.text())
        except BrowserExtensionCredentialsError as exc:
            self.status_label.setText(str(exc))
        except Exception:
            self.status_label.setText("자동 연결 토큰을 저장하지 못했습니다.")
        else:
            self._refresh_status()
            self.registration_changed.emit()
        finally:
            self.token_input.setText("")

    def _delete(self):
        self.token_input.setText("")
        try:
            self.credentials.delete()
        except BrowserExtensionCredentialsError as exc:
            self.status_label.setText(str(exc))
        except Exception:
            self.status_label.setText("자동 연결 토큰 등록을 삭제하지 못했습니다.")
        else:
            self._refresh_status()
            self.registration_changed.emit()

    def done(self, result):
        self.token_input.setText("")
        super().done(result)

    def closeEvent(self, event):
        self.done(QDialog.DialogCode.Rejected)
        event.accept()
