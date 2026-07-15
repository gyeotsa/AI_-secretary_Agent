import torch
from core.hardware import get_hardware_manager

print("=== PyTorch GPU 테스트 ===")
print(f"CUDA 사용 가능: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"GPU 개수: {torch.cuda.device_count()}")
    print(f"GPU 이름: {torch.cuda.get_device_name(0)}")
    print(f"CUDA 버전: {torch.version.cuda}")

print("\n=== HardwareManager 테스트 ===")
manager = get_hardware_manager()
print(f"사용 중인 디바이스: {manager.device}")
print("테스트 완료!")
