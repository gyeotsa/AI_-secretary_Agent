import threading
import time

import cv2
import numpy as np
from PIL import Image

from core.gpu_scheduler import GPUResourceQueue
from core.voice_runtime import (DeviceRecoveryPolicy, SpeechCandidate,
                                VoiceDuplexController, WakeWordCandidateEvaluator)
from core.vision_runtime import VisionRuntime
from core.windows_automation import WindowsAutomationRuntime
from plugins.multimodal_runtime import MultimodalRuntimePlugin
from plugins.windows_control import WindowsControlPlugin


class FakeLLM:
    def __init__(self):
        self.messages = None

    def chat(self, messages):
        self.messages = messages
        return "표에는 매출 증가가 보입니다."


def test_gpu_queue_enforces_budget_and_priority():
    queue = GPUResourceQueue(2048)
    first = queue.acquire("vision", 1536)
    admitted = []

    def wait_for_gpu():
        with queue.reserve("stt", 1024, priority=1, timeout=2) as item:
            admitted.append(item.role)

    worker = threading.Thread(target=wait_for_gpu)
    worker.start()
    time.sleep(0.05)
    assert queue.snapshot()["waiting"] == 1
    queue.release(first)
    worker.join(2)
    assert admitted == ["stt"] and queue.snapshot()["reserved_mb"] == 0


def test_gpu_queue_rejects_over_budget_request():
    queue = GPUResourceQueue(1024)
    try:
        queue.acquire("oversized", 2048)
        assert False
    except MemoryError:
        pass


def test_wake_word_candidates_are_re_evaluated():
    evaluator = WakeWordCandidateEvaluator("자비스")
    selected = evaluator.select([
        SpeechCandidate("서비스 디스코드 켜줘", acoustic_score=-0.1),
        SpeechCandidate("자비스 디스코드 켜줘", acoustic_score=-0.4),
    ], ["디스코드", "켜줘"])
    assert selected == "자비스 디스코드 켜줘"
    assert evaluator.select([SpeechCandidate("오늘 날씨 알려줘")]) is None


def test_duplex_ignores_echo_and_interrupts_sustained_near_end():
    interrupted = []
    duplex = VoiceDuplexController(lambda: interrupted.append(True))
    duplex.start_output()
    duplex.update_output(0.02)
    assert not duplex.observe_input(0.025, now=1.0)
    assert not duplex.observe_input(0.05, now=2.0)
    assert duplex.observe_input(0.05, now=2.3)
    assert interrupted and duplex.cancel_event.is_set()


def test_device_recovery_retries_with_bounded_backoff():
    attempts, sleeps = [], []

    def connect():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("device changed")
        return "connected"

    assert DeviceRecoveryPolicy(4, 0.1).run(connect, sleep=sleeps.append) == "connected"
    assert sleeps == [0.1, 0.2]


def test_multi_image_vision_preserves_order_and_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    first, second = tmp_path / "1.png", tmp_path / "2.png"
    Image.new("RGB", (30, 20), "white").save(first)
    Image.new("RGB", (40, 25), "blue").save(second)
    llm = FakeLLM()
    runtime = VisionRuntime(llm)
    runtime.gpu = GPUResourceQueue(4096)
    result = runtime.analyze([str(first), str(second)], "표를 읽어줘", mode="table")
    assert result["frame_count"] == 2 and len(result["frame_hashes"]) == 2
    assert len(llm.messages[-1]["images"]) == 2


def test_video_frames_have_monotonic_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    path = tmp_path / "sample.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 5, (32, 24))
    for value in range(10):
        writer.write(np.full((24, 32, 3), value * 20, dtype=np.uint8))
    writer.release()
    runtime = VisionRuntime(FakeLLM())
    frames = runtime.sample_video(str(path), 4)
    assert len(frames) == 4
    assert [item.timestamp_seconds for item in frames] == sorted(item.timestamp_seconds for item in frames)
    assert all(item.sha256 for item in frames)


def test_multimodal_plugin_contract_and_gpu_status():
    plugin = MultimodalRuntimePlugin()
    names = {tool.name for tool in plugin.get_tools()}
    assert names == {"screen_capture_region", "visual_analyze", "video_sample_analyze", "gpu_runtime_status"}
    assert plugin.execute_tool("gpu_runtime_status", {}).succeeded


def test_windows_policy_never_silently_uses_coordinates(monkeypatch):
    policy = WindowsAutomationRuntime.policy()
    assert policy["priority"][:4] == ["api", "cli", "com", "uia"]
    assert policy["coordinate_fallback"] == "explicit_user_approval_only"
    plugin = WindowsControlPlugin()
    result = plugin.execute_tool("windows_automation_policy", {})
    assert result.succeeded and not result.evidence[0].data.get("coordinate_fallback_used", False)
