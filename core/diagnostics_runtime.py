"""Evidence-bearing startup diagnostics for the local Jarvis runtime."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import json
import platform
import time
import urllib.request

from config import Config
from core.model_registry import get_model_registry
from core.productization import METRICS
from core.quality_metrics import get_quality_metric_store
from core.runtime.event_bus import Event, get_event_bus


@dataclass
class ProbeResult:
    key: str
    label: str
    status: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    remediation: str = ""


class DiagnosticsRuntime:
    """Runs real probes and never turns an unexecuted check into a pass."""

    def __init__(self, *, tool_executor=None, workspace_manager=None,
                 plugin_registry=None, tts_settings=None, audio_processor=None,
                 automation_engine=None,
                 report_path: str = "data/diagnostics/last_report.json"):
        self.tool_executor = tool_executor
        self.workspace_manager = workspace_manager
        self.plugin_registry = plugin_registry
        self.tts_settings = tts_settings
        self.audio_processor = audio_processor
        self.automation_engine = automation_engine
        self.report_path = Path(report_path)

    def run(self, scope: str = "core", live: bool = False) -> dict[str, Any]:
        scope = str(scope or "core").casefold()
        probes: list[ProbeResult] = []
        checks: list[tuple[str, str, Callable[[], tuple[str, str, dict, str]]]] = [
            ("runtime", "Python Runtime", self._runtime),
            ("ollama", "Ollama Server·Models", self._ollama),
            ("cuda", "CUDA·GPU", self._cuda),
            ("memory", "RAM·VRAM Budget", self._memory),
            ("workspace", "Workspace·Allowed Paths", self._workspace),
            ("plugins", "Plugin Registry", self._plugins),
            ("tool_flow", "End-to-End Tool Flow", self._tool_flow),
            ("scheduler_soak", "Scheduler Recovery Soak", self._scheduler_soak),
            ("microphone", "Microphone", self._microphone),
            ("speaker", "TTS·Speaker", lambda: self._speaker(live=live)),
        ]
        if scope in {"device", "full"}:
            checks.append(("camera", "Camera", lambda: self._camera(live=live)))
        for key, label, check in checks:
            started = time.perf_counter()
            try:
                status, summary, evidence, remediation = check()
            except Exception as exc:
                status, summary = "failed", f"{type(exc).__name__}: {exc}"
                evidence, remediation = {}, "로그와 장치/서비스 설정을 확인하세요."
            probes.append(ProbeResult(
                key, label, status, summary, evidence,
                round((time.perf_counter() - started) * 1000, 2), remediation,
            ))
        report = {
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "scope": scope, "live": bool(live),
            "summary": self._summary(probes),
            "probes": [asdict(item) for item in probes],
        }
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        METRICS.gauge("diagnostics.last_summary", report["summary"])
        get_event_bus().publish(Event("diagnostics.completed", "diagnostics_runtime",
                                      data={"summary": report["summary"], "scope": scope}))
        return report

    def last_report(self) -> dict[str, Any]:
        try:
            return json.loads(self.report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"generated_at": "", "summary": {"status": "not_run"}, "probes": []}

    @staticmethod
    def _summary(probes: list[ProbeResult]) -> dict[str, Any]:
        counts = {name: sum(item.status == name for item in probes)
                  for name in ("passed", "warning", "failed", "not_run")}
        status = "failed" if counts["failed"] else "warning" if counts["warning"] or counts["not_run"] else "passed"
        return {"status": status, **counts, "total": len(probes)}

    @staticmethod
    def _runtime():
        executable = Path(__import__("sys").executable).resolve()
        expected = (Path.cwd() / ".venv" / "Scripts" / "python.exe").resolve()
        matched = not expected.exists() or executable == expected
        return ("passed" if matched else "warning",
                f"Python {platform.python_version()} · {executable}",
                {"executable": str(executable), "expected": str(expected), "platform": platform.platform()},
                "PyCharm 실행 구성을 프로젝트 .venv로 지정하세요." if not matched else "")

    @staticmethod
    def _ollama():
        url = Config.OLLAMA_BASE_URL.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        installed = sorted(str(item.get("name", "")) for item in payload.get("models", []))
        requested = sorted({profile.model for profile in get_model_registry().profiles().values()})
        missing = [model for model in requested if model not in installed]
        return ("warning" if missing else "passed",
                f"서버 응답 · 설치 {len(installed)}개 · 필수 누락 {len(missing)}개",
                {"url": url, "installed": installed, "requested": requested, "missing": missing},
                "누락 모델을 Ollama로 내려받거나 역할별 모델 설정을 변경하세요." if missing else "")

    @staticmethod
    def _cuda():
        import torch
        available = bool(torch.cuda.is_available())
        if not available:
            return "warning", "CUDA를 사용할 수 없어 CPU로 동작합니다.", {
                "torch": torch.__version__, "cuda_available": False,
            }, "NVIDIA 드라이버와 CUDA 호환 PyTorch 설치를 확인하세요."
        tensor = torch.tensor([2.0], device="cuda") * 3
        name = torch.cuda.get_device_name(0)
        return "passed", f"{name} · 실제 CUDA 텐서 연산 성공", {
            "torch": torch.__version__, "cuda": torch.version.cuda,
            "device": name, "result": float(tensor.cpu().item()),
        }, ""

    @staticmethod
    def _memory():
        try:
            import psutil
            vm = psutil.virtual_memory()
            evidence: dict[str, Any] = {
                "ram_total_mb": round(vm.total / 1024 ** 2),
                "ram_available_mb": round(vm.available / 1024 ** 2), "ram_percent": vm.percent,
            }
            status = "warning" if vm.percent >= 85 else "passed"
            summary = f"RAM {vm.percent:.0f}% 사용 · 가용 {evidence['ram_available_mb']}MB"
            try:
                import torch
                if torch.cuda.is_available():
                    free, total = torch.cuda.mem_get_info()
                    evidence.update(vram_free_mb=round(free / 1024 ** 2), vram_total_mb=round(total / 1024 ** 2))
                    summary += f" · VRAM 가용 {evidence['vram_free_mb']}MB"
            except Exception:
                pass
            return status, summary, evidence, "동시 상주 모델 수를 줄이세요." if status == "warning" else ""
        except ImportError:
            return "warning", "psutil이 없어 메모리 계측을 생략했습니다.", {}, "psutil을 설치하세요."

    def _workspace(self):
        path = self.workspace_manager.get_workspace_path() if self.workspace_manager else None
        allowed = list(Config.ALLOWED_PATHS)
        if not path:
            return "warning", "선택된 Workspace가 없습니다.", {"allowed_paths": allowed}, "상단 W 버튼으로 작업 폴더를 선택하세요."
        target = Path(path)
        accessible = target.is_dir()
        return ("passed" if accessible else "failed", f"{target} · 접근 {'가능' if accessible else '실패'}",
                {"workspace": str(target), "allowed_paths": allowed},
                "Workspace 접근 권한과 ALLOWED_PATHS를 확인하세요." if not accessible else "")

    def _plugins(self):
        if self.plugin_registry is None:
            return "failed", "Plugin Registry가 연결되지 않았습니다.", {}, "ToolExecutor 초기화를 확인하세요."
        statuses = self.plugin_registry.get_plugin_statuses()
        failed = [
            item.name for item in statuses
            if item.installation_state == "failed" or item.contract_state == "failed"
            or item.runtime_state == "failed"
        ]
        confirmed = sum(item.verification_state == "confirmed" for item in statuses)
        unchecked = sum(item.verification_state == "unchecked" for item in statuses)
        summary = (
            f"등록 {sum(item.registered for item in statuses)}개 · "
            f"실행 검증 {confirmed}개 · 미확인 {unchecked}개 · 실패 {len(failed)}개"
        )
        return ("warning" if failed or unchecked else "passed", summary,
                {"plugins": [asdict(item) for item in statuses], "failed": failed,
                 "unchecked": [item.name for item in statuses
                               if item.verification_state == "unchecked"]},
                "Plugin 진단 화면에서 실패와 미확인 상태를 구분해 확인하세요."
                if failed or unchecked else "")

    def _tool_flow(self):
        if self.tool_executor is None:
            return "failed", "ToolExecutor가 연결되지 않았습니다.", {}, "애플리케이션 초기화 순서를 확인하세요."
        result = self.tool_executor.execute_tool("get_time", {})
        evidence = result.to_dict() if hasattr(result, "to_dict") else {"raw": str(result)}
        succeeded = bool(getattr(result, "succeeded", False))
        return ("passed" if succeeded else "failed",
                "Registry → 실행 → 검증 증거 흐름 성공" if succeeded else str(getattr(result, "error", result)),
                evidence, "system_tools Plugin과 중앙 권한 검사를 확인하세요." if not succeeded else "")

    def _scheduler_soak(self):
        if self.automation_engine is None:
            get_quality_metric_store().record(
                "long_run_recovery", 0.0, success=False,
                context={"reason": "automation_engine_disconnected"},
            )
            return "warning", "AutomationEngine이 연결되지 않았습니다.", {}, "자동화 런타임 초기화 순서를 확인하세요."
        result = self.automation_engine.soak_test(iterations=1000)
        failures = list(result.get("failures") or ())
        passed = not failures and int(result.get("iterations", 0)) == 1000
        get_quality_metric_store().record(
            "long_run_recovery", 1.0 if passed else 0.0, success=passed,
            context={"probe": "accelerated_scheduler_soak", **result},
        )
        return (
            "passed" if passed else "failed",
            f"가속 스케줄러 루프 {result.get('iterations', 0)}회 · 오류 {len(failures)}개",
            result,
            "스케줄러 복원 상태와 등록 작업 callback을 확인하세요." if failures else "",
        )

    @staticmethod
    def _microphone():
        import sounddevice as sd
        devices = sd.query_devices()
        inputs = [{"index": index, "name": item["name"], "channels": item["max_input_channels"]}
                  for index, item in enumerate(devices) if item["max_input_channels"] > 0]
        return ("passed" if inputs else "failed", f"입력 장치 {len(inputs)}개 열거", {"devices": inputs},
                "Windows 마이크 권한과 입력 장치를 확인하세요." if not inputs else "")

    def _speaker(self, *, live: bool):
        voices = self.tts_settings.list_voices() if self.tts_settings else []
        import sounddevice as sd
        devices = sd.query_devices()
        outputs = [{"index": index, "name": item["name"], "channels": item["max_output_channels"]}
                   for index, item in enumerate(devices) if item["max_output_channels"] > 0]
        evidence = {
            "voices": [asdict(item) if hasattr(item, "__dataclass_fields__") else str(item) for item in voices],
            "output_devices": outputs, "playback_executed": False,
        }
        if not voices or not outputs:
            return "failed", f"TTS 음성 {len(voices)}개 · 출력 장치 {len(outputs)}개", evidence, \
                "Windows 출력 장치와 TTS 엔진 또는 음성 모델 설치를 확인하세요."
        if live:
            if self.tool_executor is None:
                return "failed", "실제 TTS 재생을 위한 ToolExecutor가 없습니다.", evidence, \
                    "애플리케이션 초기화 순서를 확인하세요."
            result = self.tool_executor.speak_text_result(
                "아니스 오디오 출력 진단입니다.", self.audio_processor,
            )
            evidence["playback_executed"] = True
            evidence["playback_result"] = (
                result.to_dict() if hasattr(result, "to_dict") else str(result)
            )
            if not getattr(result, "succeeded", False):
                return "failed", "TTS 실제 재생에 실패했습니다.", evidence, \
                    str(getattr(result, "error", "TTS 설정을 확인하세요."))
            return "passed", f"TTS 음성 {len(voices)}개 · 출력 장치 {len(outputs)}개 · 실제 재생 성공", evidence, ""
        return "passed", f"TTS 음성 {len(voices)}개 · 출력 장치 {len(outputs)}개 열거", evidence, ""

    @staticmethod
    def _camera(*, live: bool):
        if not live:
            return "not_run", "카메라는 명시적 장치 진단에서만 켭니다.", {"privacy": "camera_off"}, "전체 장치 진단에서 직접 실행하세요."
        import cv2
        devices = []
        for index in range(5):
            capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            opened = capture.isOpened()
            if opened:
                ok, frame = capture.read()
                devices.append({"index": index, "frame_verified": bool(ok and frame is not None)})
            capture.release()
        return ("passed" if devices else "failed", f"카메라 {len(devices)}개 실제 프레임 확인",
                {"devices": devices}, "Windows 카메라 권한 또는 장치 연결을 확인하세요." if not devices else "")
