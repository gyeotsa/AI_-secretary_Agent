"""Persistent product-completion and live acceptance evidence registry.

Automated tests prove software contracts.  They cannot prove that a message
arrived in a real account or that a human heard a speaker.  This module keeps
those two claims separate and provides one fail-closed completion gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
import json
import os
import tempfile
import threading

from core.productization import SensitiveDataRedactor


VALID_STATUSES = {"not_run", "blocked", "failed", "passed"}
LOCAL_EVIDENCE_KINDS = {
    "evaluation_report", "artifact", "image", "device_report", "audio",
    "application_report", "soak_report", "runtime_report",
}


@dataclass(frozen=True)
class AcceptanceScenario:
    key: str
    label: str
    category: str
    live_required: bool
    evidence_kinds: tuple[str, ...]
    expires_days: int = 30


@dataclass
class AcceptanceResult:
    key: str
    status: str
    executed_at: str = ""
    operator: str = ""
    environment: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    blockers: list[str] = field(default_factory=list)


DEFAULT_SCENARIOS = (
    AcceptanceScenario("conversation_human_eval", "자연 대화·명령 인간 평가", "agent", True,
                       ("evaluation_report",), 14),
    AcceptanceScenario("specialist_human_eval", "전문가 산출물 인간 품질 평가", "specialist", True,
                       ("evaluation_report", "artifact"), 14),
    AcceptanceScenario("mockup_visual_eval", "시안 렌더·수정 시각 수락", "specialist", True,
                       ("evaluation_report", "image"), 14),
    AcceptanceScenario("camera_gesture", "실카메라 한·두 손 제스처", "device", True,
                       ("device_report",), 30),
    AcceptanceScenario("microphone_stt", "실마이크 STT·호출어", "device", True,
                       ("device_report",), 30),
    AcceptanceScenario("speaker_tts", "실스피커 TTS 청취", "device", True,
                       ("device_report", "audio"), 30),
    AcceptanceScenario("kakao_delivery", "카카오톡 실제 상대방 전달", "external", True,
                       ("remote_receipt",), 14),
    AcceptanceScenario("mail_delivery", "메일 실제 계정 전달", "external", True,
                       ("remote_id", "remote_receipt"), 14),
    AcceptanceScenario("oauth_roundtrip", "OAuth 로그인·갱신·조회", "external", True,
                       ("remote_id",), 14),
    AcceptanceScenario("office_com", "설치형 Office/HWP COM", "external", True,
                       ("artifact", "application_report"), 30),
    AcceptanceScenario("wall_clock_soak", "장시간·절전·재시작 운영", "operations", True,
                       ("soak_report",), 30),
    AcceptanceScenario("packaged_runtime", "배포본 설치·기동", "operations", False,
                       ("runtime_report",), 30),
)


class AcceptanceRuntime:
    """Stores sanitized, attributable evidence and evaluates completion."""

    def __init__(self, path: str = "data/acceptance/results.json",
                 scenarios: Iterable[AcceptanceScenario] = DEFAULT_SCENARIOS):
        self.path = Path(path)
        self.scenarios = {item.key: item for item in scenarios}
        self._lock = threading.RLock()
        self._results: dict[str, AcceptanceResult] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        for key, item in (payload.get("results") or {}).items():
            if key not in self.scenarios or not isinstance(item, dict):
                continue
            status = str(item.get("status", "not_run"))
            if status not in VALID_STATUSES:
                continue
            self._results[key] = AcceptanceResult(
                key=key, status=status, executed_at=str(item.get("executed_at", "")),
                operator=str(item.get("operator", "")),
                environment=dict(item.get("environment") or {}),
                evidence=list(item.get("evidence") or []), notes=str(item.get("notes", "")),
                blockers=[str(value) for value in item.get("blockers") or []],
            )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = SensitiveDataRedactor.redact({
            "version": 1,
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "results": {key: asdict(value) for key, value in self._results.items()},
        })
        handle, temporary = tempfile.mkstemp(prefix=self.path.name, suffix=".tmp",
                                             dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _validate_evidence(scenario: AcceptanceScenario,
                           evidence: list[dict[str, Any]]) -> None:
        if not evidence:
            raise ValueError("통과 결과에는 실제 증거가 필요합니다.")
        kinds = {str(item.get("kind", "")) for item in evidence if isinstance(item, dict)}
        missing = set(scenario.evidence_kinds) - kinds
        if missing:
            raise ValueError("필수 증거가 없습니다: " + ", ".join(sorted(missing)))
        for item in evidence:
            if not isinstance(item, dict) or not str(item.get("value", "")).strip():
                raise ValueError("각 증거에는 비어 있지 않은 kind와 value가 필요합니다.")
            kind, value = str(item.get("kind", "")), str(item.get("value", "")).strip()
            if kind in LOCAL_EVIDENCE_KINDS:
                target = Path(value)
                if not target.is_file() or target.stat().st_size <= 0:
                    raise ValueError(f"로컬 증거 파일을 확인할 수 없습니다: {value}")

    def record(self, key: str, status: str, *, operator: str = "",
               environment: dict[str, Any] | None = None,
               evidence: list[dict[str, Any]] | None = None, notes: str = "",
               blockers: list[str] | None = None) -> AcceptanceResult:
        if key not in self.scenarios:
            raise KeyError(f"등록되지 않은 수락 시나리오입니다: {key}")
        if status not in VALID_STATUSES:
            raise ValueError(f"지원하지 않는 수락 상태입니다: {status}")
        evidence = list(evidence or [])
        blockers = [str(value).strip() for value in blockers or [] if str(value).strip()]
        if status == "passed":
            if self.scenarios[key].live_required and not str(operator).strip():
                raise ValueError("실환경 수락 통과에는 확인한 사용자가 필요합니다.")
            self._validate_evidence(self.scenarios[key], evidence)
        elif status == "blocked" and not blockers:
            raise ValueError("차단 상태에는 해제할 수 있는 차단 사유가 필요합니다.")
        result = AcceptanceResult(
            key=key, status=status,
            executed_at=datetime.now(timezone.utc).astimezone().isoformat(),
            operator=str(operator).strip(), environment=dict(environment or {}),
            evidence=evidence, notes=str(notes), blockers=blockers,
        )
        with self._lock:
            self._results[key] = result
            self._save()
        return result

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc).astimezone()
        rows = []
        with self._lock:
            for scenario in self.scenarios.values():
                result = self._results.get(scenario.key, AcceptanceResult(scenario.key, "not_run"))
                status, reason = result.status, ""
                if status == "passed" and result.executed_at:
                    try:
                        executed = datetime.fromisoformat(result.executed_at)
                        if executed.tzinfo is None:
                            executed = executed.replace(tzinfo=timezone.utc)
                        if now > executed + timedelta(days=scenario.expires_days):
                            status, reason = "expired", "환경 변화 재검증 기한이 지났습니다."
                    except ValueError:
                        status, reason = "failed", "수락 시각 기록이 손상되었습니다."
                rows.append({
                    **asdict(scenario), **asdict(result), "status": status,
                    "reason": reason, "evidence_count": len(result.evidence),
                })
        counts = {name: sum(row["status"] == name for row in rows)
                  for name in ("passed", "failed", "blocked", "not_run", "expired")}
        return {
            "generated_at": now.isoformat(), "scenarios": rows, "counts": counts,
            "all_passed": bool(rows) and counts["passed"] == len(rows),
            "completion_percent": round(counts["passed"] / len(rows) * 100, 1) if rows else 0.0,
        }


_runtime: AcceptanceRuntime | None = None


def get_acceptance_runtime() -> AcceptanceRuntime:
    global _runtime
    if _runtime is None:
        _runtime = AcceptanceRuntime()
    return _runtime
