"""Manual camera diagnostic.

This legacy ``test_`` file is intentionally not a pytest test: opening a real
camera is a user-visible hardware action and must never happen during test
collection.
"""

from __future__ import annotations

import argparse


def run_camera_diagnostic(save_path: str | None = None) -> str:
    """Capture one frame after an explicit command-line invocation."""
    from core.multimodal import get_multimodal_manager

    print("카메라 테스트를 시작합니다...")
    result = get_multimodal_manager().capture_camera_frame(save_path)
    print(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="실제 카메라 1프레임 수동 진단")
    parser.add_argument("--save-path", help="선택적인 프레임 저장 경로")
    args = parser.parse_args()
    run_camera_diagnostic(args.save_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
