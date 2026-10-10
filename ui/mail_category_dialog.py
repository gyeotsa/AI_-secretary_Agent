"""Edit local mail classification criteria and future notification targets."""
from __future__ import annotations

from uuid import uuid4

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .dialog_theme import apply_dark_dialog_theme


class MailCategoryDialog(QDialog):
    settings_changed = pyqtSignal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("메일 요약·카테고리 설정")
        self.setMinimumSize(680, 430)
        apply_dark_dialog_theme(self)
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox("목록 조회 시 메일 본문을 자동으로 읽고 요약·분류하기")
        self.enabled.setAccessibleName("메일 자동 요약 분류")
        layout.addWidget(self.enabled)
        intro = QLabel(
            "요약과 카테고리는 계정별로 로컬에 기억합니다. 목록에는 분류 결과를 표시하지 않습니다.\n"
            "이름과 분류 기준을 지정하세요. 알림 대상으로 선택한 카테고리는 이후 메일 알림의 필터로 사용합니다.\n"
            "자동 수집 시 조회 목록의 메일이 읽음 처리될 수 있습니다."
        )
        intro.setTextFormat(Qt.TextFormat.PlainText)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 3)
        self.table.setAccessibleName("메일 카테고리 설정 목록")
        self.table.setHorizontalHeaderLabels(["카테고리 이름", "분류 기준", "알림 대상"])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        self.add_button = QPushButton("카테고리 추가")
        self.delete_button = QPushButton("선택 카테고리 삭제")
        for button in (self.add_button, self.delete_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.status_label = QLabel("최대 20개 · 이름 40자 · 분류 기준 300자. 기타 카테고리는 삭제할 수 없습니다.")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        footer = QHBoxLayout()
        footer.addStretch()
        self.save_button = QPushButton("설정 저장")
        self.close_button = QPushButton("취소")
        for button in (self.save_button, self.close_button):
            button.setAutoDefault(False)
            footer.addWidget(button)
        layout.addLayout(footer)
        self.add_button.clicked.connect(self._add)
        self.delete_button.clicked.connect(self._delete)
        self.table.currentCellChanged.connect(self._update_delete)
        self.save_button.clicked.connect(self._save)
        self.close_button.clicked.connect(self.reject)
        try:
            settings = service.settings()
            self.enabled.setChecked(settings["enabled"] is True)
            for category in settings["categories"]:
                self._append(category)
            self._update_delete()
        except Exception:
            self.table.setRowCount(0)
            for widget in (self.enabled, self.table, self.add_button, self.delete_button, self.save_button):
                widget.setEnabled(False)
            self.status_label.setText("메일 분류 설정을 불러오지 못했습니다. 창을 닫고 다시 시도하세요.")

    def _append(self, category):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, key in enumerate(("name", "description")):
            cell = QTableWidgetItem(category[key])
            cell.setData(Qt.ItemDataRole.UserRole, category["id"])
            self.table.setItem(row, column, cell)
        notify = QTableWidgetItem()
        notify.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
        notify.setCheckState(Qt.CheckState.Checked if category["notify"] else Qt.CheckState.Unchecked)
        self.table.setItem(row, 2, notify)
        self.add_button.setEnabled(self.table.rowCount() < 20)

    def _add(self):
        if self.table.rowCount() >= 20:
            return
        self._append({"id": uuid4().hex, "name": "", "description": "", "notify": False})
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)
        self.table.editItem(self.table.currentItem())

    def _update_delete(self, *_args):
        cell = self.table.item(self.table.currentRow(), 0)
        self.delete_button.setEnabled(cell is not None and cell.data(Qt.ItemDataRole.UserRole) != "other")

    def _delete(self):
        cell = self.table.item(self.table.currentRow(), 0)
        if cell is not None and cell.data(Qt.ItemDataRole.UserRole) != "other":
            self.table.removeRow(self.table.currentRow())
            self.add_button.setEnabled(self.table.rowCount() < 20)
            self._update_delete()

    def _save(self):
        # Finish an active cell edit before reading the table values.
        self.save_button.setFocus()
        categories = [
            {"id": self.table.item(row, 0).data(Qt.ItemDataRole.UserRole),
             "name": self.table.item(row, 0).text().strip(),
             "description": self.table.item(row, 1).text().strip(),
             "notify": self.table.item(row, 2).checkState() == Qt.CheckState.Checked}
            for row in range(self.table.rowCount())
        ]
        if any(not category["name"] or len(category["name"]) > 40 or len(category["description"]) > 300
               for category in categories):
            self.status_label.setText("이름은 1~40자, 분류 기준은 300자 이하로 입력하세요.")
            return
        if len({category["name"].casefold() for category in categories}) != len(categories):
            self.status_label.setText("카테고리 이름은 서로 다르게 입력하세요.")
            return
        try:
            self.service.save_settings(self.enabled.isChecked(), categories)
        except Exception:
            self.status_label.setText("메일 분류 설정을 저장하지 못했습니다. 입력 내용을 확인하고 다시 시도하세요.")
            return
        self.settings_changed.emit()
        self.accept()
