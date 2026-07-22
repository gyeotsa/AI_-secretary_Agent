import pytest
import torch

from config import Config
from core.hardware import HardwareManager
from core.rag import VectorRAGManager


@pytest.mark.integration
def test_configured_gpu_is_used_by_torch_whisper_and_rag():
    if not torch.cuda.is_available():
        pytest.skip("CUDA GPU가 없는 환경")
    assert "RTX 4060" in torch.cuda.get_device_name(0)

    hardware = HardwareManager()
    assert hardware.device == "cuda"
    assert next(hardware.whisper_model.parameters()).device.type == "cuda"

    rag = VectorRAGManager()
    assert rag.use_vector_rag
    assert rag.embedding_model._model.device.type == "cuda"
