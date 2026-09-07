"""Persistent product-completion and live acceptance evidence registry.

Automated tests prove software contracts.  They cannot prove that a message
arrived in a real account or that a human heard a speaker.  This module keeps
those two claims separate and provides one fail-closed completion gate.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
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
REMOTE_EVIDENCE_KINDS = {"remote_id", "remote_receipt"}
REMOTE_VERIFICATION_METHODS = {
    "provider_readback", "recipient_readback", "human_confirmation",
    "local_ui_readback", "oauth_roundtrip",
}
REMOTE_OPERATION_BY_SCENARIO = {
    "kakao_delivery": "message_delivery",
    "mail_delivery": "mail_delivery",
    "oauth_roundtrip": "oauth_roundtrip",
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
        self.path = Path(path).expanduser().absolute()
        self.scenarios = {item.key: item for item in scenarios}
        self._lock = threading.RLock()
        self._results: dict[str, AcceptanceResult] = {}
        self._load_error = ""
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"),
                                 object_pairs_hook=self._unique_json_object)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            self._load_error = f"수락 원장을 읽을 수 없습니다: {type(exc).__name__}"
            return
        if (not isinstance(payload, dict)
                or not isinstance(payload.get("results"), dict)
                or type(payload.get("version", 1)) is not int
                or payload.get("version", 1) not in (1, 2)):
            self._load_error = "수락 원장의 형식 또는 버전이 손상되었습니다."
            return
        for key, item in payload["results"].items():
            if key not in self.scenarios or not isinstance(item, dict):
                continue
            status = item.get("status", "not_run")
            if not isinstance(status, str) or status not in VALID_STATUSES:
                continue
            try:
                if item.get("key", key) != key:
                    raise ValueError("시나리오 키가 일치하지 않습니다.")
                result = AcceptanceResult(
                    key=key, status=status, executed_at=item.get("executed_at", ""),
                    operator=item.get("operator", ""),
                    environment=item.get("environment", {}),
                    evidence=item.get("evidence", []), notes=item.get("notes", ""),
                    blockers=item.get("blockers", []),
                )
                self._validate_fields(result)
                self._results[key] = self._sanitized_copy(result)
            except (TypeError, ValueError, OverflowError) as exc:
                # One damaged entry must not hide healthy sibling scenarios or
                # become a passed result through permissive str()/list() casts.
                self._results[key] = AcceptanceResult(
                    key, "failed", blockers=[f"수락 기록이 손상되었습니다: {exc}"],
                )

    @staticmethod
    def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"수락 원장에 중복된 필드가 있습니다: {key}")
            result[key] = value
        return result

    def _save(self, results: dict[str, AcceptanceResult]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "results": {key: asdict(self._sanitized_copy(value)) for key, value in results.items()},
        }
        handle, temporary = tempfile.mkstemp(prefix=self.path.name, suffix=".tmp",
                                             dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _validate_fields(result: AcceptanceResult) -> None:
        for name in ("executed_at", "operator", "notes"):
            if not isinstance(getattr(result, name), str):
                raise ValueError(f"{name} 항목은 문자열이어야 합니다.")
        if not isinstance(result.environment, dict):
            raise ValueError("environment 항목은 객체여야 합니다.")
        if (not isinstance(result.evidence, list)
                or any(not isinstance(item, dict) for item in result.evidence)):
            raise ValueError("evidence 항목은 증거 객체 목록이어야 합니다.")
        if (not isinstance(result.blockers, list)
                or any(not isinstance(item, str) for item in result.blockers)):
            raise ValueError("blockers 항목은 문자열 목록이어야 합니다.")

    @staticmethod
    def _sanitized_copy(result: AcceptanceResult) -> AcceptanceResult:
        # Keep the exact, detached JSON representation in memory as on disk.
        # Otherwise caller mutations or redaction can make the two disagree.
        try:
            original = asdict(result)
            payload = SensitiveDataRedactor.redact(original)
            for source, sanitized in zip(original["evidence"], payload["evidence"]):
                fingerprint = source.get("fingerprint")
                digest = fingerprint.get("sha256") if isinstance(fingerprint, dict) else None
                if (isinstance(digest, str) and len(digest) == 64
                        and all(char in "0123456789abcdef" for char in digest)):
                    # Digests are internal integrity data, not phone numbers or
                    # other secrets even when a hex substring resembles one.
                    sanitized["fingerprint"]["sha256"] = digest
                source_verification = source.get("verification")
                sanitized_verification = sanitized.get("verification")
                if isinstance(source_verification, dict) and isinstance(sanitized_verification, dict):
                    for name in ("expected_sha256", "observed_sha256"):
                        digest = source_verification.get(name)
                        if (isinstance(digest, str) and len(digest) == 64
                                and all(char in "0123456789abcdefABCDEF" for char in digest)):
                            # Payload digests are integrity values too. Preserve
                            # them exactly across redaction and validate them on
                            # both the in-memory and persisted representations.
                            sanitized_verification[name] = digest
            return AcceptanceResult(**json.loads(json.dumps(payload, allow_nan=False)))
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError("수락 기록에는 JSON으로 저장 가능한 값만 사용할 수 있습니다.") from exc

    @staticmethod
    def _file_fingerprint(target: Path) -> dict[str, Any]:
        try:
            if not target.is_file():
                raise OSError("not a regular file")
            digest = sha256()
            with target.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if before.st_size <= 0:
                    raise OSError("empty evidence")
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
            current = target.stat()
            # Windows fstat() and path stat() can expose different ctime
            # semantics (change time versus creation time). Compare stable
            # file identity and content metadata, then verify the content hash.
            def identity(stat):
                return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

            if identity(before) != identity(after) or identity(after) != identity(current):
                raise OSError("evidence changed while reading")
            return {"sha256": digest.hexdigest(), "size_bytes": after.st_size}
        except (OSError, ValueError) as exc:
            raise ValueError(f"로컬 증거 파일을 확인할 수 없습니다: {target}") from exc

    @classmethod
    def _validate_evidence(cls, scenario: AcceptanceScenario,
                           evidence: list[dict[str, Any]], *, seal: bool = False,
                           now: datetime | None = None) -> datetime | None:
        if not evidence:
            raise ValueError("통과 결과에는 실제 증거가 필요합니다.")
        for item in evidence:
            if (not isinstance(item, dict)
                    or any(not isinstance(item.get(name), str) or not item[name].strip()
                           for name in ("kind", "value"))):
                raise ValueError("각 증거에는 비어 있지 않은 kind와 value 문자열이 필요합니다.")
        kinds = {item["kind"].strip() for item in evidence}
        missing = set(scenario.evidence_kinds) - kinds
        if missing:
            raise ValueError("필수 증거가 없습니다: " + ", ".join(sorted(missing)))
        oldest_remote_verification = None
        for item in evidence:
            kind, value = item["kind"].strip(), item["value"].strip()
            if kind in LOCAL_EVIDENCE_KINDS:
                try:
                    target = Path(value).expanduser().resolve(strict=True)
                except (OSError, ValueError, RuntimeError) as exc:
                    raise ValueError(f"로컬 증거 파일을 확인할 수 없습니다: {value}") from exc
                actual = cls._file_fingerprint(target)
                if seal:
                    item["value"] = str(target)
                    item["fingerprint"] = actual
                else:
                    expected = item.get("fingerprint")
                    if not isinstance(expected, dict):
                        raise ValueError("로컬 증거 무결성 기록이 없어 다시 수락 확인해야 합니다.")
                    size = expected.get("size_bytes")
                    if (not isinstance(size, int) or isinstance(size, bool)
                            or size != actual["size_bytes"]
                            or expected.get("sha256") != actual["sha256"]):
                        raise ValueError(f"수락 이후 로컬 증거 파일이 변경되었습니다: {value}")
            elif kind in REMOTE_EVIDENCE_KINDS:
                verified_at = cls._validate_remote_evidence(scenario, item, now=now)
                if oldest_remote_verification is None or verified_at < oldest_remote_verification:
                    oldest_remote_verification = verified_at
        return oldest_remote_verification

    @staticmethod
    def _validate_remote_evidence(scenario: AcceptanceScenario,
                                  item: dict[str, Any], *,
                                  now: datetime | None) -> datetime:
        """Require an attributable read-back receipt, not an opaque success ID.

        External acceptance remains an operator attestation, but it must bind the
        requested payload fingerprint to an independently observed fingerprint.
        This prevents a provider ID or arbitrary string from becoming 100% product
        completion evidence by itself.
        """
        verification = item.get("verification")
        if not isinstance(verification, dict):
            raise ValueError("원격 증거에는 구조화된 verification 영수증이 필요합니다.")
        required = (
            "provider", "operation", "target", "remote_id", "status",
            "method", "verified_at", "expected_sha256", "observed_sha256",
        )
        if any(not isinstance(verification.get(name), str)
               or not verification[name].strip() for name in required):
            raise ValueError("원격 verification 영수증의 필수 문자열 항목이 누락되었습니다.")
        values = {name: verification[name].strip() for name in required}
        if values["status"] != "verified":
            raise ValueError("원격 증거는 verified 상태의 재조회 결과여야 합니다.")
        if values["method"] not in REMOTE_VERIFICATION_METHODS:
            raise ValueError("지원하지 않는 원격 증거 확인 방법입니다.")
        expected_operation = REMOTE_OPERATION_BY_SCENARIO.get(scenario.key)
        if expected_operation and values["operation"] != expected_operation:
            raise ValueError("원격 증거의 작업 종류가 수락 시나리오와 일치하지 않습니다.")
        if values["remote_id"] != item["value"].strip():
            raise ValueError("원격 증거 ID와 verification 영수증 ID가 일치하지 않습니다.")
        for name in ("expected_sha256", "observed_sha256"):
            digest = values[name].casefold()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("원격 증거 payload SHA-256 형식이 올바르지 않습니다.")
        if values["expected_sha256"].casefold() != values["observed_sha256"].casefold():
            raise ValueError("요청한 원격 payload와 재조회한 payload가 일치하지 않습니다.")
        try:
            verified_at = datetime.fromisoformat(values["verified_at"])
            if verified_at.tzinfo is None or verified_at.utcoffset() is None:
                raise ValueError("missing timezone")
            comparison_now = now or datetime.now(timezone.utc).astimezone()
            if comparison_now.tzinfo is None:
                comparison_now = comparison_now.replace(tzinfo=timezone.utc)
            if verified_at > comparison_now:
                raise ValueError("future timestamp")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("원격 증거 확인 시각이 누락되었거나 손상·미래 시각입니다.") from exc
        return verified_at

    @classmethod
    def _validate_passed(cls, scenario: AcceptanceScenario, result: AcceptanceResult,
                         *, now: datetime, seal: bool = False) -> datetime:
        if scenario.live_required and not result.operator.strip():
            raise ValueError("실환경 수락 통과에는 확인한 사용자가 필요합니다.")
        if result.blockers:
            raise ValueError("통과 상태에는 해결되지 않은 차단 사유가 없어야 합니다.")
        try:
            executed = datetime.fromisoformat(result.executed_at)
            if executed.tzinfo is None or executed.utcoffset() is None:
                raise ValueError("missing timezone")
            if executed > now:
                raise ValueError("future timestamp")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("수락 시각 기록이 누락되었거나 손상·미래 시각입니다.") from exc
        remote_verified = cls._validate_evidence(scenario, result.evidence, seal=seal, now=now)
        # Re-recording an existing receipt is not a new remote observation. Its
        # original verification time bounds validity even when executed_at is
        # refreshed. Keep historical records readable and classify them expired
        # in snapshot; only reject stale evidence on a new passed attestation.
        validity_start = min(executed, remote_verified) if remote_verified else executed
        if seal and now - validity_start > timedelta(days=scenario.expires_days):
            raise ValueError("원격 증거의 재검증 기한이 지났습니다. 실제 결과를 다시 확인해야 합니다.")
        return validity_start

    def record(self, key: str, status: str, *, operator: str = "",
               environment: dict[str, Any] | None = None,
               evidence: list[dict[str, Any]] | None = None, notes: str = "",
               blockers: list[str] | None = None) -> AcceptanceResult:
        if key not in self.scenarios:
            raise KeyError(f"등록되지 않은 수락 시나리오입니다: {key}")
        if not isinstance(status, str) or status not in VALID_STATUSES:
            raise ValueError(f"지원하지 않는 수락 상태입니다: {status}")
        now = datetime.now(timezone.utc).astimezone()
        result = AcceptanceResult(
            key=key, status=status, executed_at=now.isoformat(),
            operator=operator, environment=environment if environment is not None else {},
            evidence=evidence if evidence is not None else [], notes=notes,
            blockers=blockers if blockers is not None else [],
        )
        self._validate_fields(result)
        result = self._sanitized_copy(result)
        result.operator = result.operator.strip()
        result.blockers = [value.strip() for value in result.blockers if value.strip()]
        if status == "passed":
            self._validate_passed(self.scenarios[key], result, now=now, seal=True)
            # Resolving a relative filename can itself expose sensitive parts of
            # its absolute path. Verify the representation that will be saved.
            result = self._sanitized_copy(result)
            self._validate_passed(self.scenarios[key], result, now=now)
        elif status == "blocked" and not result.blockers:
            raise ValueError("차단 상태에는 해제할 수 있는 차단 사유가 필요합니다.")
        with self._lock:
            if self._load_error:
                raise ValueError(self._load_error + " 기존 원장을 복구한 뒤 다시 시도하세요.")
            candidate = {**self._results, key: result}
            self._save(candidate)
            # Publication is after the atomic file replacement, never before it.
            self._results = candidate
        return deepcopy(result)

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc).astimezone()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        rows = []
        with self._lock:
            for scenario in self.scenarios.values():
                result = self._results.get(scenario.key, AcceptanceResult(scenario.key, "not_run"))
                status, reason = result.status, ""
                if status == "passed":
                    try:
                        self._validate_fields(result)
                        validity_start = self._validate_passed(scenario, result, now=now)
                        if now - validity_start > timedelta(days=scenario.expires_days):
                            status, reason = "expired", "환경 변화 재검증 기한이 지났습니다."
                    except (TypeError, ValueError, OverflowError) as exc:
                        status, reason = "failed", str(exc)
                elif status == "blocked" and not any(value.strip() for value in result.blockers):
                    status, reason = "failed", "차단 상태의 차단 사유가 누락되었습니다."
                elif status == "failed" and result.blockers:
                    reason = ", ".join(result.blockers)
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
            "completion_scope": "acceptance_scenarios", "total_scenarios": len(rows),
            "load_error": self._load_error,
        }


_runtime: AcceptanceRuntime | None = None


def get_acceptance_runtime() -> AcceptanceRuntime:
    global _runtime
    if _runtime is None:
        _runtime = AcceptanceRuntime()
    return _runtime
