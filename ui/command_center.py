"""Integrated, live Command Center and first-run diagnostic wizard."""
from __future__ import annotations

import json
import math
import threading

from PyQt6.QtCore import QObject, QPointF, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout,
    QFileDialog, QHBoxLayout, QLabel, QInputDialog, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget,
)
from core.specialist_workspaces import get_specialist_workspace_registry
from core.acceptance_runtime import LOCAL_EVIDENCE_KINDS, get_acceptance_runtime
from .brain_orbit import BrainOrbitWidget


COMMAND_CENTER_STYLE = """
QDialog, QWidget#commandCenterRoot { background: #07101c; color: #dcecff; font-family: 'Segoe UI'; }
QFrame#hero, QFrame#card { background: #0b1727; border: 1px solid #1b3850; border-radius: 14px; }
QLabel#title { color: #f1fbff; font-size: 24px; font-weight: 700; }
QLabel#subtitle { color: #7894ad; font-size: 11px; }
QLabel#metricValue { color: #6fe8ff; font-size: 20px; font-weight: 700; }
QLabel#metricLabel { color: #7995ad; font-size: 10px; }
QPushButton { background: #10253a; border: 1px solid #25506b; border-radius: 9px; padding: 8px 12px; color: #dff8ff; }
QPushButton:hover { background: #16324a; border-color: #58d9ed; }
QPushButton:disabled { color: #516678; border-color: #203143; }
QTabWidget::pane { border: 1px solid #1a3449; border-radius: 10px; background: #081421; }
QTabBar::tab { background: #0a1827; color: #7793aa; padding: 9px 15px; border: 1px solid #172f43; }
QTabBar::tab:selected { color: #75e9ff; background: #10283b; }
QTableWidget, QTextEdit { background: #07111e; border: 1px solid #18334a; border-radius: 8px; gridline-color: #172c3d; }
QHeaderView::section { background: #102238; color: #8fb3ca; border: 0; padding: 7px; }
QProgressBar { background: #091522; border: 1px solid #1c3a50; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #27c4d9; border-radius: 4px; }
QComboBox { background: #0c1b2c; border: 1px solid #24455e; border-radius: 8px; padding: 7px; }
"""


class _AsyncBridge(QObject):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)


class RuntimeOrb(QWidget):
    """GPU-light 2D state visualization. No camera or 3D runtime is required."""
    COLORS = {
        "idle": QColor("#37d3e8"), "running": QColor("#8f7cff"),
        "verifying": QColor("#ffcc66"), "approval": QColor("#ff9b58"),
        "failed": QColor("#ff5f75"), "completed": QColor("#65e09b"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(150, 150)
        self.phase, self.state = 0.0, "idle"
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

    def set_state(self, state: str):
        self.state = state if state in self.COLORS else "idle"
        self.update()

    def _tick(self):
        self.phase = (self.phase + 0.035) % (math.pi * 2)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        radius = min(self.width(), self.height()) * (0.27 + math.sin(self.phase) * 0.012)
        color = self.COLORS[self.state]
        glow = QRadialGradient(center, radius * 1.8)
        glow.setColorAt(0, QColor(color.red(), color.green(), color.blue(), 190))
        glow.setColorAt(0.42, QColor(color.red(), color.green(), color.blue(), 55))
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, radius * 1.8, radius * 1.8)
        for offset, alpha in ((0, 235), (13, 105), (25, 50)):
            pen = QPen(QColor(color.red(), color.green(), color.blue(), alpha), 2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius + offset, radius + offset)
        painter.setPen(QColor("#dffcff"))
        painter.setFont(QFont("Segoe UI", 9, 600))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.state.upper())


class MetricCard(QFrame):
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        self.value = QLabel("—")
        self.value.setObjectName("metricValue")
        caption = QLabel(label)
        caption.setObjectName("metricLabel")
        layout.addWidget(self.value)
        layout.addWidget(caption)


class CommandCenterDialog(QDialog):
    def __init__(self, runtime, *, workflow_runtime=None, diagnostics=None,
                 gesture_runtime=None, context_provider=None,
                 control_callback=None, surface_callback=None,
                 gesture_callback=None, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self.workflow_runtime = workflow_runtime
        self.diagnostics = diagnostics
        self.gesture_runtime = gesture_runtime
        self.context_provider = context_provider or (lambda: {})
        self.control_callback = control_callback
        self.surface_callback = surface_callback
        self.gesture_callback = gesture_callback
        self._contract_rows = []
        self._acceptance_rows = []
        self.bridge = _AsyncBridge(self)
        self.bridge.completed.connect(self._async_completed)
        self.bridge.failed.connect(self._async_failed)
        self.setWindowTitle("ANIS · COMMAND CENTER")
        self.resize(1240, 820)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setStyleSheet(COMMAND_CENTER_STYLE)
        self._build()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(1200)
        self.refresh()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        hero = QFrame()
        hero.setObjectName("hero")
        hero_layout = QHBoxLayout(hero)
        heading = QVBoxLayout()
        title = QLabel("COMMAND CENTER")
        title.setObjectName("title")
        self.updated_label = QLabel("실제 런타임 상태를 연결하는 중…")
        self.updated_label.setObjectName("subtitle")
        heading.addWidget(title)
        heading.addWidget(self.updated_label)
        hero_layout.addLayout(heading, 2)
        self.orb = RuntimeOrb()
        hero_layout.addWidget(self.orb, 0)
        metric_layout = QGridLayout()
        self.cards = {key: MetricCard(label) for key, label in (
            ("tasks", "ACTIVE TASKS"), ("ram", "RAM"), ("vram", "VRAM"),
            ("plugins", "PLUGINS"), ("approvals", "APPROVALS"), ("health", "HEALTH"),
        )}
        for index, card in enumerate(self.cards.values()):
            metric_layout.addWidget(card, index // 3, index % 3)
        hero_layout.addLayout(metric_layout, 4)
        root.addWidget(hero)

        controls = QHBoxLayout()
        self.workflow_combo = QComboBox()
        if self.workflow_runtime:
            for item in self.workflow_runtime.presets():
                self.workflow_combo.addItem(item.get("label", item["id"]), item["id"])
        controls.addWidget(QLabel("WORKFLOW"))
        controls.addWidget(self.workflow_combo)
        run_btn = QPushButton("실행")
        run_btn.clicked.connect(self._run_workflow)
        controls.addWidget(run_btn)
        resume_btn = QPushButton("선택 워크플로 승인·계속")
        resume_btn.clicked.connect(self._resume_selected_workflow)
        controls.addWidget(resume_btn)
        diag_btn = QPushButton("핵심 진단")
        diag_btn.clicked.connect(lambda: self._run_diagnostics("core", False))
        controls.addWidget(diag_btn)
        device_btn = QPushButton("장치 진단 · 카메라 사용")
        device_btn.clicked.connect(lambda: self._run_diagnostics("full", True))
        controls.addWidget(device_btn)
        self.gesture_btn = QPushButton("제스처 켜기")
        self.gesture_btn.clicked.connect(self._toggle_gesture)
        controls.addWidget(self.gesture_btn)
        controls.addStretch()
        root.addLayout(controls)

        self.tabs = QTabWidget()
        self.brain_map = BrainOrbitWidget(compact=True)
        self.brain_map.set_surfaces(get_specialist_workspace_registry().all())
        if self.surface_callback:
            self.brain_map.surface_requested.connect(self.surface_callback)
        self.brain_map.camera_toggle_requested.connect(self._request_gesture_state)
        self.tabs.addTab(self.brain_map, "BRAIN MAP")
        self.task_table = self._table(("계약 ID", "상위 작업", "상태", "전문가", "목표", "시도", "실패/승인"))
        self.task_table.itemSelectionChanged.connect(self._show_selected_contract)
        self.tabs.addTab(self.task_table, "작업·계약")
        self.team_table = self._table(("상태", "작업공간", "현재 역할", "지시"))
        self.tabs.addTab(self.team_table, "전문가 팀")
        self.dag_table = self._table(("작업", "단계", "상태", "의존성", "도구", "시도", "설명"))
        self.tabs.addTab(self.dag_table, "실행 DAG")
        self.model_table = self._table(("역할", "모델", "실제 상태", "VRAM", "모달리티", "Keep Alive"))
        self.tabs.addTab(self.model_table, "모델·자원")
        self.automation_table = self._table(("종류", "상태", "ID", "설명", "스케줄/최근 실행"))
        self.tabs.addTab(self.automation_table, "자동화·관찰")
        self.action_table = self._table(("성공", "종류", "출처", "설명"))
        self.tabs.addTab(self.action_table, "도구·행동")
        self.permission_table = self._table(("결정", "권한", "위험도", "ID"))
        self.tabs.addTab(self.permission_table, "권한·승인")
        self.artifact_table = self._table(("작업", "종류", "URI/값"))
        self.tabs.addTab(self.artifact_table, "산출물·증거")
        self.diagnostic_table = self._table(("상태", "검사", "결과", "조치"))
        self.tabs.addTab(self.diagnostic_table, "진단")
        self.quality_table = self._table(("상태", "지표", "측정", "표본", "목표"))
        self.tabs.addTab(self.quality_table, "품질 지표")
        self.acceptance_table = self._table(("상태", "영역", "실환경 수락", "증거", "차단/만료"))
        self.tabs.addTab(self.acceptance_table, "제품 완료 게이트")
        self.event_table = self._table(("시간", "이벤트", "출처", "데이터"))
        self.tabs.addTab(self.event_table, "이벤트")
        root.addWidget(self.tabs, 1)

        task_controls = QHBoxLayout()
        task_controls.addWidget(QLabel("선택 작업 제어"))
        approve_btn = QPushButton("승인하고 계속")
        approve_btn.clicked.connect(lambda: self._control_selected("승인"))
        approve_btn.setEnabled(bool(self.control_callback))
        task_controls.addWidget(approve_btn)
        cancel_btn = QPushButton("취소")
        cancel_btn.clicked.connect(lambda: self._control_selected("취소"))
        cancel_btn.setEnabled(bool(self.control_callback))
        task_controls.addWidget(cancel_btn)
        task_controls.addStretch()
        root.addLayout(task_controls)

        acceptance_controls = QHBoxLayout()
        acceptance_controls.addWidget(QLabel("제품 완료 게이트"))
        pass_btn = QPushButton("선택 항목 수락 증거 기록")
        pass_btn.clicked.connect(self._record_selected_acceptance)
        acceptance_controls.addWidget(pass_btn)
        blocked_btn = QPushButton("선택 항목 차단 사유 기록")
        blocked_btn.clicked.connect(self._block_selected_acceptance)
        acceptance_controls.addWidget(blocked_btn)
        acceptance_controls.addStretch()
        root.addLayout(acceptance_controls)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(120)
        self.detail.setPlaceholderText("워크플로·진단 실행 결과가 여기에 표시됩니다.")
        root.addWidget(self.detail)

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setStretchLastSection(True)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        return table

    @staticmethod
    def _set_rows(table, rows):
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                item = QTableWidgetItem(str(value if value is not None else ""))
                table.setItem(row_index, column_index, item)
        table.resizeColumnsToContents()

    def refresh(self):
        try:
            data = self.runtime.snapshot()
        except Exception as exc:
            self.updated_label.setText(f"상태 갱신 실패: {exc}")
            self.orb.set_state("failed")
            return
        self.updated_label.setText(f"LIVE · {data['updated_at']} · Workspace: {data.get('workspace', {}).get('name') or '없음'}")
        contracts = data["contracts"]
        self._contract_rows = list(contracts)
        active_statuses = {"queued", "running", "verifying", "awaiting_approval", "failed", "escalated"}
        active = [item for item in contracts if item.get("status") in active_statuses]
        pending = [item for item in data["permissions"] if item["decision"] == "undecided"]
        system, gpu = data["system"], data["gpu"]
        self.cards["tasks"].value.setText(str(len(active) + len(data["dialogue_tasks"])))
        self.cards["ram"].value.setText("—" if system.get("ram_percent") is None else f"{system['ram_percent']:.0f}%")
        vram_total = system.get("vram_total_mb")
        self.cards["vram"].value.setText("—" if not vram_total else f"{system.get('vram_used_mb', 0) / vram_total * 100:.0f}%")
        plugin_confirmed = sum(
            item.get("verification_state") == "confirmed" for item in data["plugins"]
        )
        plugin_failed = sum(
            item.get("verification_state") == "failed" for item in data["plugins"]
        )
        self.cards["plugins"].value.setText(
            f"{plugin_confirmed}/{len(data['plugins'])}"
            + (f" · 실패 {plugin_failed}" if plugin_failed else "")
        )
        self.cards["approvals"].value.setText(str(len(pending)))
        health = data["diagnostics"].get("summary", {}).get("status", "not_run")
        self.cards["health"].value.setText(health.upper())
        state = ("failed" if any(item.get("status") in {"failed", "escalated"} for item in active)
                 else "approval" if pending or any(item.get("status") == "awaiting_approval" for item in active)
                 else "verifying" if any(item.get("status") == "verifying" for item in active)
                 else "running" if active or data["teams"] else "idle")
        self.orb.set_state(state)
        self._set_rows(self.task_table, [(item.get("contract_id"), item.get("parent_id"),
                                         item.get("status"), item.get("specialist"), item.get("goal", "")[:120],
                                         item.get("attempts"), item.get("failure_reason", "")) for item in contracts])
        self._set_rows(self.team_table, [(item.get("status"), item.get("workspace_key"), item.get("current_role"),
                                         item.get("instruction", "")[:140]) for item in data["teams"]])
        self._set_rows(self.dag_table, [(item.get("task_id"), item.get("step_id"), item.get("status"),
                                        ", ".join(map(str, item.get("dependencies", []))), item.get("tool"),
                                        item.get("attempts"), item.get("description", "")[:150])
                                       for item in data.get("plan_steps", [])])
        self._set_rows(self.model_table, [(item["role"], item["model"], item.get("runtime_status"),
                                          f"{item.get('loaded_vram_mb', 0)} MB" if item.get("loaded") else "—",
                                          ", ".join(item["modalities"]), item["keep_alive"])
                                          for item in data["models"]])
        automation_rows = []
        scheduler = data.get("automation", {}).get("scheduler", {})
        automation_rows.append(("scheduler", "running" if scheduler.get("running") else "stopped",
                                "", f"등록 {scheduler.get('scheduled_jobs', 0)} · 실행 {scheduler.get('running_jobs', 0)}",
                                scheduler.get("state", {}).get("last_restore", {}).get("updated_at", "")))
        for item in data.get("automation", {}).get("jobs", []):
            automation_rows.append(("job", "enabled" if item.get("enabled") else "disabled", item.get("id"),
                                    item.get("description"),
                                    f"{item.get('schedule_type', '')} {item.get('schedule_value', '')} · {item.get('last_run') or '미실행'}"))
        for item in data.get("workflow_runs", []):
            automation_rows.append(("workflow", item.get("status"), item.get("run_id"), item.get("label"),
                                    item.get("finished_at")))
        observer = data.get("observer", {})
        automation_rows.append(("observer", observer.get("status"), "", ", ".join(observer.get("watched_paths", [])),
                                f"최근 이벤트 {len(observer.get('recent_events', []))}개"))
        for item in data.get("gpu", {}).get("active_requests", []):
            automation_rows.append(("gpu", "active", item.get("request_id"), item.get("role"),
                                    f"{item.get('vram_mb')} MB"))
        self._set_rows(self.automation_table, automation_rows)
        self._set_rows(self.action_table, [("✓" if item.get("success") else "✕", item.get("action_type"), item.get("source"),
                                           item.get("description") or item.get("error", "")) for item in data["actions"]])
        self._set_rows(self.permission_table, [(item["decision"], item["name"], item["level"], item["id"])
                                               for item in data["permissions"]])
        self._set_rows(self.artifact_table, [(item.get("contract_id"), item.get("kind") or item.get("type"),
                                             item.get("uri") or item.get("value_type") or item) for item in data["artifacts"]])
        probes = data["diagnostics"].get("probes", [])
        self._set_rows(self.diagnostic_table, [(item.get("status"), item.get("label"), item.get("summary"), item.get("remediation"))
                                               for item in probes])
        scenarios = data["acceptance"].get("scenarios", [])
        self._set_rows(self.quality_table, [(item["status"], item["key"], item["value"],
                                            f"{item.get('sample_count', 0)}/{item.get('minimum_samples', 0)}",
                                            f"{item['operator']} {item['target']}") for item in scenarios])
        live_scenarios = data.get("live_acceptance", {}).get("scenarios", [])
        self._acceptance_rows = list(live_scenarios)
        self._set_rows(self.acceptance_table, [(
            item.get("status"), item.get("category"), item.get("label"),
            item.get("evidence_count", 0),
            item.get("reason") or ", ".join(item.get("blockers") or []),
        ) for item in live_scenarios])
        self._set_rows(self.event_table, [(item["timestamp"][11:19], item["type"], item["source"],
                                          json.dumps(item["data"], ensure_ascii=False)[:180]) for item in data["events"][:50]])
        if self.gesture_runtime:
            gesture = self.gesture_runtime.status()
            self.gesture_btn.setText("제스처 끄기" if gesture["running"] else "제스처 켜기")
            self.gesture_btn.setToolTip(gesture.get("error", ""))
            self.brain_map.set_camera_status(gesture)

    def _selected_acceptance(self):
        row = self.acceptance_table.currentRow()
        if row < 0 or row >= len(self._acceptance_rows):
            QMessageBox.information(self, "제품 완료 게이트", "먼저 제품 완료 게이트 탭에서 항목을 선택하세요.")
            return None
        return self._acceptance_rows[row]

    def _record_selected_acceptance(self):
        scenario = self._selected_acceptance()
        if not scenario:
            return
        operator, accepted = QInputDialog.getText(
            self, "수락 확인자", "실제로 결과를 확인한 사용자 이름을 입력하세요.")
        if not accepted or not operator.strip():
            return
        evidence = []
        for kind in scenario.get("evidence_kinds") or []:
            if kind in LOCAL_EVIDENCE_KINDS:
                value, _ = QFileDialog.getOpenFileName(
                    self, f"{kind} 증거 파일 선택", "", "모든 파일 (*.*)")
            else:
                value, ok = QInputDialog.getText(
                    self, "원격 증거", f"{kind}의 원격 ID 또는 수신 확인값을 입력하세요.")
                if not ok:
                    value = ""
            if not str(value).strip():
                QMessageBox.warning(self, "수락 중단", f"필수 증거 {kind}가 선택되지 않았습니다.")
                return
            evidence.append({"kind": kind, "value": str(value)})
        notes, _ = QInputDialog.getMultiLineText(
            self, "수락 메모", "시험 환경·절차·관찰 결과를 기록하세요.")
        try:
            get_acceptance_runtime().record(
                scenario["key"], "passed", operator=operator,
                environment={"source": "command_center"}, evidence=evidence, notes=notes,
            )
        except Exception as exc:
            QMessageBox.critical(self, "수락 기록 실패", str(exc))
            return
        self.refresh()

    def _block_selected_acceptance(self):
        scenario = self._selected_acceptance()
        if not scenario:
            return
        reason, accepted = QInputDialog.getMultiLineText(
            self, "차단 사유", "완료를 위해 필요한 계정·장치·사용자 선택을 구체적으로 적으세요.")
        if not accepted or not reason.strip():
            return
        try:
            get_acceptance_runtime().record(
                scenario["key"], "blocked", environment={"source": "command_center"},
                blockers=[reason.strip()],
            )
        except Exception as exc:
            QMessageBox.critical(self, "차단 기록 실패", str(exc))
            return
        self.refresh()

    def _run_async(self, callback):
        def worker():
            try:
                self.bridge.completed.emit(callback())
            except Exception as exc:
                self.bridge.failed.emit(f"{type(exc).__name__}: {exc}")
        threading.Thread(target=worker, daemon=True).start()

    def _run_workflow(self):
        preset = self.workflow_combo.currentData()
        if not preset or not self.workflow_runtime:
            return
        context = self.context_provider()
        self.detail.setPlainText(f"워크플로 실행 중: {preset}")
        self._run_async(lambda: self.workflow_runtime.execute(preset, context=context,
                                                               approve=lambda step: False))

    def _run_diagnostics(self, scope, live):
        if not self.diagnostics:
            return
        if live and QMessageBox.question(
            self, "장치 진단",
            "카메라를 잠시 켜 실제 프레임을 확인하고 짧은 TTS 진단음을 재생할까요?",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.detail.setPlainText("실제 진단을 실행 중입니다…")
        self._run_async(lambda: self.diagnostics.run(scope=scope, live=live))

    def _resume_selected_workflow(self):
        if not self.workflow_runtime:
            return
        row = self.automation_table.currentRow()
        kind = self.automation_table.item(row, 0) if row >= 0 else None
        run_id = self.automation_table.item(row, 2) if row >= 0 else None
        if not kind or kind.text() != "workflow" or not run_id or not run_id.text().strip():
            QMessageBox.information(self, "워크플로 선택", "자동화·관찰 탭에서 승인 대기 워크플로를 선택하세요.")
            return
        if QMessageBox.question(
            self, "워크플로 승인", f"워크플로 {run_id.text()}의 대기 단계를 승인하고 계속할까요?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.detail.setPlainText(f"워크플로 {run_id.text()} 재개 중…")
        self._run_async(lambda: self.workflow_runtime.resume(
            run_id.text().strip(), approve=True, context=self.context_provider(),
        ))

    def _selected_contract(self) -> dict:
        row = self.task_table.currentRow()
        return self._contract_rows[row] if 0 <= row < len(self._contract_rows) else {}

    def _show_selected_contract(self):
        row = self.task_table.currentRow()
        if 0 <= row < len(self._contract_rows):
            self.detail.setPlainText(json.dumps(
                self._contract_rows[row], ensure_ascii=False, indent=2, default=str,
            ))

    def _control_selected(self, command: str):
        contract = self._selected_contract()
        if not contract:
            QMessageBox.information(self, "작업 선택", "제어할 작업 행을 먼저 선택하세요.")
            return
        contract_id = str(contract.get("contract_id") or "")
        task_id = str(contract.get("parent_id") or "")
        if command == "취소" and self.runtime.supervisor and contract_id:
            self.runtime.supervisor.request_cancel(contract_id)
        if not task_id or not self.control_callback:
            if command == "취소":
                self.detail.setPlainText(f"계약 {contract_id}의 취소 요청을 저장했습니다.")
                self.refresh()
                return
            QMessageBox.information(
                self, "직접 제어할 수 없음",
                "이 계약에는 재개 가능한 대화 작업 ID가 없습니다. 작업 목록 탭의 상위 작업을 확인하세요.",
            )
            return
        self.detail.setPlainText(f"작업 {task_id} {command} 처리 중…")
        self._run_async(lambda: self.control_callback(f"작업 {task_id} {command}"))

    def _toggle_gesture(self):
        if not self.gesture_runtime:
            return
        self._request_gesture_state(not bool(self.gesture_runtime.status().get("running")))

    def _request_gesture_state(self, enabled: bool):
        if self.gesture_callback:
            self.gesture_callback(bool(enabled))
            return
        try:
            self.gesture_runtime.start() if enabled else self.gesture_runtime.stop()
        except Exception as exc:
            QMessageBox.warning(self, "제스처 상태를 변경할 수 없음", str(exc))
        self.refresh()

    def apply_gesture_motion(self, payload: dict):
        self.brain_map.apply_gesture_motion(payload)

    def set_gesture_camera_status(self, status: dict):
        self.brain_map.set_camera_status(status)
        self.gesture_btn.setText("제스처 끄기" if status.get("running") else "제스처 켜기")
        self.gesture_btn.setToolTip(str(status.get("error") or ""))

    def _async_completed(self, result):
        if hasattr(result, "__dict__"):
            result = vars(result)
        self.detail.setPlainText(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        self.refresh()

    def _async_failed(self, error):
        self.detail.setPlainText(f"실행 실패: {error}")
        self.refresh()


class FirstRunWizard(QDialog):
    """Runs core probes before marking setup complete."""
    SETTINGS_KEY = "setup/diagnostics_completed_v2"

    def __init__(self, diagnostics, parent=None):
        super().__init__(parent)
        self.diagnostics = diagnostics
        self.bridge = _AsyncBridge(self)
        self.bridge.completed.connect(self._complete)
        self.bridge.failed.connect(self._failed)
        self.setWindowTitle("ANIS 첫 실행 점검")
        self.resize(720, 520)
        self.setStyleSheet(COMMAND_CENTER_STYLE)
        layout = QVBoxLayout(self)
        title = QLabel("실행 환경을 먼저 확인할게요")
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("설치 여부가 아닌 실제 서버·모델·CUDA·장치·Plugin·Tool 흐름을 검사합니다. 카메라는 켜지 않습니다.")
        subtitle.setWordWrap(True)
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.result = QTextEdit()
        self.result.setReadOnly(True)
        layout.addWidget(self.result, 1)
        controls = QHBoxLayout()
        self.run_btn = QPushButton("실제 진단 시작")
        self.run_btn.clicked.connect(self.run_diagnostics)
        controls.addWidget(self.run_btn)
        self.open_btn = QPushButton("Command Center에서 계속")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.accept)
        controls.addWidget(self.open_btn)
        layout.addLayout(controls)

    @classmethod
    def should_show(cls) -> bool:
        return not QSettings("JARVIS", "ANIS").value(cls.SETTINGS_KEY, False, type=bool)

    def run_diagnostics(self):
        self.run_btn.setEnabled(False)
        self.progress.show()
        self.result.setPlainText("진단 중…")
        def worker():
            try:
                self.bridge.completed.emit(self.diagnostics.run(scope="core", live=False))
            except Exception as exc:
                self.bridge.failed.emit(f"{type(exc).__name__}: {exc}")
        threading.Thread(target=worker, daemon=True).start()

    def _complete(self, report):
        self.progress.hide()
        lines = [f"종합 상태: {report['summary']['status']}"]
        lines.extend(f"[{item['status']}] {item['label']}: {item['summary']}" for item in report["probes"])
        self.result.setPlainText("\n".join(lines))
        if report["summary"]["status"] != "failed":
            QSettings("JARVIS", "ANIS").setValue(self.SETTINGS_KEY, True)
        else:
            QSettings("JARVIS", "ANIS").setValue(self.SETTINGS_KEY, False)
            lines.append("실패 항목이 있어 다음 실행 때 점검 창을 다시 표시합니다.")
            self.result.setPlainText("\n".join(lines))
        self.open_btn.setEnabled(True)
        self.run_btn.setEnabled(True)

    def _failed(self, error):
        self.progress.hide()
        self.result.setPlainText(f"진단 자체가 실패했습니다: {error}")
        self.run_btn.setEnabled(True)
