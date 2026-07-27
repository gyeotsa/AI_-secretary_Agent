"""Typed execution result and evidence contract for the Agent Runtime."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ToolRunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Evidence:
    kind: str
    summary: str
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Artifact:
    kind: str
    uri: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolRunResult:
    tool_name: str
    status: ToolRunStatus
    raw_output: str
    duration_ms: float = 0.0
    error: Optional[str] = None
    evidence: List[Evidence] = field(default_factory=list)
    artifacts: List[Artifact] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == ToolRunStatus.SUCCEEDED

    def __str__(self) -> str:
        """Legacy 문자열 소비자가 전환 기간에도 원문 결과를 사용할 수 있게 한다."""
        return self.raw_output

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_verification(
        cls,
        *,
        tool_name: str,
        raw_output: str,
        verification: Any,
        duration_ms: float = 0.0,
    ) -> "ToolRunResult":
        details = dict(getattr(verification, "details", None) or {})
        success = bool(getattr(verification, "success", False))
        verified = bool(getattr(verification, "verified", True))
        message = str(getattr(verification, "message", "검증 결과 없음"))
        evidence = [Evidence("tool_verification", message, details)]
        artifacts = _extract_artifacts(raw_output)
        if not verified:
            status = ToolRunStatus.UNVERIFIED
        elif success:
            status = ToolRunStatus.SUCCEEDED
        else:
            status = ToolRunStatus.FAILED
        return cls(
            tool_name=tool_name,
            status=status,
            raw_output=str(raw_output),
            duration_ms=max(0.0, float(duration_ms)),
            error=message if status == ToolRunStatus.FAILED else None,
            evidence=evidence,
            artifacts=artifacts,
        )

    @classmethod
    def successful(
        cls,
        *,
        tool_name: str,
        raw_output: str,
        evidence: List[Evidence],
        artifacts: Optional[List[Artifact]] = None,
        duration_ms: float = 0.0,
    ) -> "ToolRunResult":
        if not evidence:
            raise ValueError("성공한 Tool 결과에는 하나 이상의 검증 증거가 필요합니다.")
        return cls(
            tool_name=tool_name,
            status=ToolRunStatus.SUCCEEDED,
            raw_output=str(raw_output),
            duration_ms=max(0.0, float(duration_ms)),
            evidence=list(evidence),
            artifacts=list(artifacts or []),
        )

    @classmethod
    def failed(
        cls,
        *,
        tool_name: str,
        error: str,
        raw_output: Optional[str] = None,
        evidence: Optional[List[Evidence]] = None,
        duration_ms: float = 0.0,
    ) -> "ToolRunResult":
        message = str(error)
        return cls(
            tool_name=tool_name,
            status=ToolRunStatus.FAILED,
            raw_output=str(raw_output if raw_output is not None else f"오류: {message}"),
            duration_ms=max(0.0, float(duration_ms)),
            error=message,
            evidence=list(evidence or [Evidence("tool_error", message)]),
        )

    @classmethod
    def unverified(
        cls,
        *,
        tool_name: str,
        raw_output: str,
        evidence: Optional[List[Evidence]] = None,
        artifacts: Optional[List[Artifact]] = None,
        duration_ms: float = 0.0,
    ) -> "ToolRunResult":
        return cls(
            tool_name=tool_name,
            status=ToolRunStatus.UNVERIFIED,
            raw_output=str(raw_output),
            duration_ms=max(0.0, float(duration_ms)),
            evidence=list(evidence or []),
            artifacts=list(artifacts or []),
        )


def _extract_artifacts(raw_output: str) -> List[Artifact]:
    """Extract common path and URL artifacts from structured plugin output."""
    try:
        payload = json.loads(str(raw_output))
    except (json.JSONDecodeError, TypeError):
        return []
    artifacts: List[Artifact] = []
    if isinstance(payload, dict):
        path = payload.get("path")
        if path:
            artifacts.append(Artifact(str(payload.get("type", "file")), str(path)))
        for item in payload.get("results", []) if isinstance(payload.get("results"), list) else []:
            if isinstance(item, dict) and item.get("url"):
                artifacts.append(Artifact(
                    "url",
                    str(item["url"]),
                    {"title": str(item.get("title", ""))},
                ))
    return artifacts
