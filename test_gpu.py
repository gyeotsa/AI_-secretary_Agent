"""Manual GPU environment diagnostic; import and pytest collection are inert."""

from __future__ import annotations


def run_gpu_diagnostic() -> dict:
    import torch

    from core.hardware import get_hardware_manager

    cuda_available = torch.cuda.is_available()
    details = {
        "cuda_available": cuda_available,
        "device_count": torch.cuda.device_count() if cuda_available else 0,
        "device_name": torch.cuda.get_device_name(0) if cuda_available else None,
        "cuda_version": torch.version.cuda,
        "selected_device": str(get_hardware_manager().device),
    }
    print("=== PyTorch GPU 수동 진단 ===")
    for key, value in details.items():
        print(f"{key}: {value}")
    return details


def main() -> int:
    run_gpu_diagnostic()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
