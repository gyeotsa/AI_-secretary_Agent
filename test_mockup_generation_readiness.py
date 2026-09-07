"""SDXL installation contracts using tiny synthetic snapshots only.

No Hugging Face request, real model load, CUDA inference or user file is used.
The tensors below only exercise container integrity, not SDXL model quality.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from core.mockup_generation import SDXLGenerationBackend, _SDXLSnapshotValidator


COMPONENTS = {
    "unet": ["diffusers", "UNet2DConditionModel"],
    "vae": ["diffusers", "AutoencoderKL"],
    "text_encoder": ["transformers", "CLIPTextModel"],
    "text_encoder_2": ["transformers", "CLIPTextModelWithProjection"],
    "tokenizer": ["transformers", "CLIPTokenizer"],
    "tokenizer_2": ["transformers", "CLIPTokenizer"],
    "scheduler": ["diffusers", "EulerAncestralDiscreteScheduler"],
}
WEIGHTS = {
    name: f"{name}/{'model' if name.startswith('text_encoder') else 'diffusion_pytorch_model'}.fp16.safetensors"
    for name in ("unet", "vae", "text_encoder", "text_encoder_2")
}
REQUIRED_FILES = [
    "model_index.json", "scheduler/scheduler_config.json",
    *[f"{name}/{part}" for name in ("tokenizer", "tokenizer_2")
      for part in ("tokenizer_config.json", "vocab.json", "merges.txt")],
    *[f"{name}/config.json" for name in WEIGHTS], *WEIGHTS.values(),
]


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _tensor_file(path, header=None, payload=b"\x00\x00\x00\x00"):
    if header is None:
        header = {"__metadata__": {"format": "pt"},
                  "weight": {"dtype": "F16", "shape": [2], "data_offsets": [0, 4]}}
    raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
    raw += b" " * (-len(raw) % 8)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(len(raw).to_bytes(8, "little") + raw + payload)


def _snapshot(root):
    _json(root / "model_index.json", {"_class_name": "StableDiffusionXLPipeline", **COMPONENTS,
                                     "image_encoder": [None, None], "feature_extractor": [None, None]})
    _json(root / "scheduler/scheduler_config.json", {"_class_name": "EulerAncestralDiscreteScheduler",
                                                    "num_train_timesteps": 1000})
    for name in ("tokenizer", "tokenizer_2"):
        _json(root / name / "tokenizer_config.json", {"tokenizer_class": "CLIPTokenizer", "model_max_length": 77})
        _json(root / name / "vocab.json", {"<|startoftext|>": 0, "<|endoftext|>": 1, "a": 2, "b": 3, "ab": 4})
        (root / name / "merges.txt").write_text("#version: 0.2\na b\n", encoding="utf-8")
    for name, (_library, class_name) in COMPONENTS.items():
        if name not in WEIGHTS:
            continue
        config = ({"architectures": [class_name], "model_type": "clip_text_model", "vocab_size": 5,
                   "hidden_size": 4, "num_hidden_layers": 1, "num_attention_heads": 1, "max_position_embeddings": 77}
                  if name.startswith("text_encoder") else
                  {"_class_name": class_name, "sample_size": 64, "in_channels": 4 if name == "unet" else 3,
                   "out_channels": 4 if name == "unet" else 3, "latent_channels": 4,
                   "block_out_channels": [32], "down_block_types": ["DownBlock2D"], "up_block_types": ["UpBlock2D"]})
        _json(root / name / "config.json", config)
        _tensor_file(root / WEIGHTS[name])
    return root


def _backend(root, monkeypatch):
    backend = SDXLGenerationBackend(root)
    monkeypatch.setattr(backend, "_device_status", lambda: (True, 8192))
    monkeypatch.setattr(backend, "_package_status", lambda: {
        name: "discoverable" for name in ("torch", "diffusers", "accelerate", "transformers", "safetensors")
    })
    return backend


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = _snapshot(tmp_path / "sdxl")
    return _backend(root, monkeypatch), root


def _failed(backend, component=None):
    status = backend.status()
    assert status["ready"] is False
    assert status["files_ready"] is False
    assert status["inference_verified"] is False
    assert status["issues"]
    if component:
        assert status["components"][component]["ready"] is False
    return status


def test_structurally_complete_snapshot_is_not_an_inference_or_weight_value_validation(installed):
    backend, _root = installed
    status = backend.status()
    assert status["ready"] is True and status["files_ready"] is True
    assert status["packages_ready"] is True and status["cuda"] is True
    assert set(status["packages"].values()) == {"discoverable"}
    assert status["packages_validation"] == "module_discovery_only"
    assert status["readiness_scope"] == "managed_local_fp16_snapshot_dependencies_and_cuda"
    assert status["checked_files"] == sorted(REQUIRED_FILES)
    assert status["model_loaded"] is False and status["inference_verified"] is False
    assert status["package_imports_verified"] is False
    assert status["weight_values_verified"] is False
    assert status["checksums_verified"] is False and status["source_revision_verified"] is False
    assert status["weights_validation"] == "safetensors_header_payload_length_and_shard_index"
    assert status["issues"] == []


def test_manifest_and_cuda_alone_are_not_a_prepared_model(tmp_path, monkeypatch):
    root = tmp_path / "manifest-only"
    _json(root / "model_index.json", {"_class_name": "StableDiffusionXLPipeline", **COMPONENTS})
    result = _failed(_backend(root, monkeypatch))
    assert result["components"]["manifest"]["ready"] is True
    assert result["components"]["unet"]["ready"] is False


def test_manifest_matches_the_official_sdxl_turbo_diffusers_scheduler(installed):
    """Regression for the upstream model_index.json, not a synthetic choice."""
    backend, root = installed
    manifest = json.loads((root / "model_index.json").read_text(encoding="utf-8"))
    assert manifest["scheduler"] == ["diffusers", "EulerAncestralDiscreteScheduler"]
    assert backend.status()["components"]["manifest"]["ready"] is True


@pytest.mark.parametrize("relative", REQUIRED_FILES)
def test_every_required_component_file_is_checked(installed, relative):
    backend, root = installed
    (root / relative).unlink()
    _failed(backend)


@pytest.mark.parametrize("name", list(WEIGHTS))
@pytest.mark.parametrize("data", [b"", b"version https://git-lfs.github.com/spec/v1\noid sha256:not-a-weight\nsize 1000000000\n"])
def test_empty_or_lfs_pointer_weights_cannot_claim_ready(installed, name, data):
    backend, root = installed
    (root / WEIGHTS[name]).write_bytes(data)
    _failed(backend, name)


@pytest.mark.parametrize("manifest", [
    [], {}, {"_class_name": "StableDiffusionXLInpaintPipeline", **COMPONENTS},
    {"_class_name": ["custom.py", "CustomPipeline"], **COMPONENTS},
    {"_class_name": "StableDiffusionXLPipeline", **COMPONENTS, "unet": [None, None]},
    {"_class_name": "StableDiffusionXLPipeline", **COMPONENTS, "unet": ["foreign_module", "UNet2DConditionModel"]},
    {"_class_name": "StableDiffusionXLPipeline", **COMPONENTS, "auto_map": {"pipeline": "code.py"}},
    {"_class_name": "StableDiffusionXLPipeline", **COMPONENTS, "image_encoder": ["transformers", "CLIPVisionModelWithProjection"]},
])
def test_invalid_or_unsupported_pipeline_manifest_is_not_ready(installed, manifest):
    backend, root = installed
    _json(root / "model_index.json", manifest)
    _failed(backend, "manifest")


@pytest.mark.parametrize("payload", [b"{broken json", b'{"_class_name":"StableDiffusionXLPipeline","_class_name":"bad"}',
                                     b'{"bad":NaN}', b'{"bad":' + b'[' * 1100 + b'0' + b']' * 1100 + b'}'])
def test_malformed_duplicate_or_unbounded_json_fails_as_status_not_an_exception(installed, payload):
    backend, root = installed
    (root / "model_index.json").write_bytes(payload)
    _failed(backend, "manifest")


@pytest.mark.parametrize("relative,field,value", [
    ("unet/config.json", "_class_name", "NotAUNet"),
    ("scheduler/scheduler_config.json", "_class_name", "EulerDiscreteScheduler"),
    ("vae/config.json", "block_out_channels", []),
    ("unet/config.json", "down_block_types", ["one", "two"]),
    ("text_encoder/config.json", "model_type", "wrong"),
    ("text_encoder_2/config.json", "architectures", ["CLIPTextModel"]),
    ("text_encoder/config.json", "hidden_size", True),
    ("text_encoder/config.json", "num_attention_heads", 0),
    ("scheduler/scheduler_config.json", "num_train_timesteps", "1000"),
    ("tokenizer/tokenizer_config.json", "tokenizer_class", []),
    ("tokenizer_2/tokenizer_config.json", "auto_map", {"AutoTokenizer": "remote.py"}),
])
def test_malformed_component_config_is_explained(installed, relative, field, value):
    backend, root = installed
    path = root / relative
    config = json.loads(path.read_text(encoding="utf-8"))
    config[field] = value
    _json(path, config)
    _failed(backend, relative.split("/")[0])


@pytest.mark.parametrize("vocab", [{"a": 0}, {"<|startoftext|>": 0, "<|endoftext|>": 0},
                                  {"<|startoftext|>": True, "<|endoftext|>": 1}])
def test_invalid_tokenizer_vocabulary_is_not_ready(installed, vocab):
    backend, root = installed
    _json(root / "tokenizer_2/vocab.json", vocab)
    _failed(backend, "tokenizer_2")


@pytest.mark.parametrize("merges", ["#version: 0.2\n", "bad\n", "a b c\n", ""])
def test_incomplete_bpe_merges_are_not_ready(installed, merges):
    backend, root = installed
    (root / "tokenizer/merges.txt").write_text(merges, encoding="utf-8")
    _failed(backend, "tokenizer")


@pytest.mark.parametrize("header,payload", [
    ({"weight": {"dtype": "F16", "shape": [4], "data_offsets": [0, 8]}}, b"1234"),
    ({"weight": {"dtype": "F16", "shape": [2], "data_offsets": [0, 4]}}, b"123456"),
    ({"weight": {"dtype": "F16", "shape": [True], "data_offsets": [0, 2]}}, b"12"),
    ({"weight": {"dtype": "F16", "shape": [-1], "data_offsets": [0, 2]}}, b"12"),
    ({"weight": {"dtype": [], "shape": [2], "data_offsets": [0, 4]}}, b"1234"),
    ({"weight": {"dtype": "F16", "shape": [2], "data_offsets": [-1, 3]}}, b"1234"),
    ({"weight": {"dtype": "F16", "shape": [1], "data_offsets": [2, 4]}}, b"1234"),
    ({"a": {"dtype": "F16", "shape": [2], "data_offsets": [0, 4]},
      "b": {"dtype": "F16", "shape": [2], "data_offsets": [0, 4]}}, b"1234"),
    ({"__metadata__": {"format": "pt"}}, b"1234"),
    ({"__metadata__": {"format": True}, "weight": {"dtype": "F16", "shape": [2], "data_offsets": [0, 4]}}, b"1234"),
])
def test_safetensors_header_payload_and_offsets_must_be_complete(installed, header, payload):
    backend, root = installed
    _tensor_file(root / WEIGHTS["unet"], header, payload)
    _failed(backend, "unet")


def test_header_size_is_bounded_before_any_large_read(installed, monkeypatch):
    backend, root = installed
    (root / WEIGHTS["unet"]).write_bytes((2**64 - 1).to_bytes(8, "little") + b"bad")
    _failed(backend, "unet")
    monkeypatch.setattr(_SDXLSnapshotValidator, "JSON_LIMIT", 16)
    _failed(backend)


def test_impossibly_large_tensor_dimension_is_rejected_without_large_integer_products(installed):
    backend, root = installed
    _tensor_file(
        root / WEIGHTS["unet"],
        {"weight": {"dtype": "F16", "shape": [10**1000, 10**1000], "data_offsets": [0, 4]}},
        b"1234",
    )
    _failed(backend, "unet")


def _sharded(root, component="unet", legacy=False):
    single = root / WEIGHTS[component]
    single.unlink()
    stem = "model" if component.startswith("text_encoder") else "diffusion_pytorch_model"
    names = [(f"{stem}-{number:05d}-of-00002.fp16.safetensors" if legacy else
              f"{stem}.fp16-{number:05d}-of-00002.safetensors") for number in (1, 2)]
    weight_map = {}
    for number, name in enumerate(names, 1):
        tensor = f"tensor{number}"
        _tensor_file(root / component / name, {tensor: {"dtype": "F16", "shape": [2], "data_offsets": [0, 4]}})
        weight_map[tensor] = name
    index = root / component / (f"{stem}.fp16.safetensors.index.json" if legacy else
                                f"{stem}.safetensors.index.fp16.json")
    _json(index, {"metadata": {"total_size": 8}, "weight_map": weight_map})
    return index, names


@pytest.mark.parametrize("component", list(WEIGHTS))
@pytest.mark.parametrize("legacy", [False, True])
def test_complete_fp16_weight_shards_are_accepted(installed, component, legacy):
    backend, root = installed
    _sharded(root, component, legacy)
    assert backend.status()["ready"] is True


@pytest.mark.parametrize("fault", ["missing_shard", "truncated_shard", "missing_tensor", "undeclared_tensor",
                                   "missing_index_entry", "wrong_total", "invalid_map", "outside", "absolute"])
def test_incomplete_or_escaping_weight_index_is_not_ready(installed, fault):
    backend, root = installed
    index, names = _sharded(root)
    data = json.loads(index.read_text(encoding="utf-8"))
    if fault == "missing_shard":
        (root / "unet" / names[1]).unlink()
    elif fault == "truncated_shard":
        shard = root / "unet" / names[1]
        shard.write_bytes(shard.read_bytes()[:-1])
    elif fault == "missing_tensor":
        data["weight_map"]["absent"] = names[1]
    elif fault == "undeclared_tensor":
        _tensor_file(root / "unet" / names[1], {"other": {"dtype": "F16", "shape": [2], "data_offsets": [0, 4]}})
    elif fault == "missing_index_entry":
        del data["weight_map"]["tensor2"]
        data["metadata"]["total_size"] = 4
    elif fault == "wrong_total":
        data["metadata"]["total_size"] = 10
    elif fault == "invalid_map":
        data["weight_map"] = []
    elif fault == "outside":
        data["weight_map"]["tensor2"] = "../../outside.safetensors"
    else:
        data["weight_map"]["tensor2"] = "C:\\outside.safetensors"
    _json(index, data)
    _failed(backend, "unet")


def test_pickle_or_wrong_variant_does_not_substitute_for_required_fp16_weights(installed):
    backend, root = installed
    weight = root / WEIGHTS["unet"]
    weight.rename(weight.with_name("diffusion_pytorch_model.safetensors"))
    (root / "unet/diffusion_pytorch_model.fp16.bin").write_bytes(b"not-safe-pickle")
    _failed(backend, "unet")


@pytest.mark.parametrize("outside", [False, True])
def test_huggingface_style_linked_weight_or_config_file_is_content_validated(installed, tmp_path, outside):
    backend, root = installed
    path = root / "unet/config.json"
    target = (tmp_path if outside else root) / "linked-config.json"
    path.rename(target)
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"This Windows account cannot create symbolic links: {exc}")
    assert path.is_file()
    assert backend.status()["ready"] is True


def test_huggingface_style_linked_snapshot_root_is_supported(tmp_path, monkeypatch):
    real = _snapshot(tmp_path / "real-snapshot")
    linked = tmp_path / "snapshot-link"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"This Windows account cannot create symbolic links: {exc}")
    backend = _backend(linked, monkeypatch)
    assert backend.model_root == linked.absolute()
    assert backend.status()["ready"] is True


def test_broken_model_file_link_is_not_treated_as_a_downloaded_weight(installed):
    backend, root = installed
    path = root / WEIGHTS["unet"]
    path.unlink()
    try:
        path.symlink_to(root / "missing-weight.safetensors")
    except OSError as exc:
        pytest.skip(f"This Windows account cannot create symbolic links: {exc}")
    _failed(backend, "unet")


@pytest.mark.parametrize("missing", ["torch", "diffusers", "accelerate", "transformers", "safetensors"])
def test_missing_dependency_prevents_ready_without_importing_or_loading_models(installed, monkeypatch, missing):
    backend, _root = installed
    monkeypatch.setattr("core.mockup_generation.importlib.util.find_spec", lambda name: None if name == missing else object())
    monkeypatch.setattr(backend, "_package_status", SDXLGenerationBackend._package_status)
    status = backend.status()
    assert status["files_ready"] is True and status["ready"] is False
    assert status["packages"][missing] == "missing" and status["packages_ready"] is False


def test_missing_cuda_keeps_model_installation_truth_separate(installed, monkeypatch):
    backend, _root = installed
    monkeypatch.setattr(backend, "_device_status", lambda: (False, 0))
    status = backend.status()
    assert status["files_ready"] is True and status["packages_ready"] is True
    assert status["ready"] is False and status["cuda"] is False


def test_prepare_rechecks_downloaded_snapshot_and_preserves_existing_download_contract(tmp_path, monkeypatch):
    backend = _backend(tmp_path / "downloaded", monkeypatch)
    calls, progress = [], []

    def fake_download(model_id, **options):
        calls.append((model_id, options))
        _snapshot(Path(options["local_dir"]))

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=fake_download))
    assert backend.status()["ready"] is False
    status = backend.prepare(progress.append)
    assert calls == [(backend.MODEL_ID, {
        "local_dir": str(backend.model_root),
        "allow_patterns": list(backend.DOWNLOAD_ALLOW_PATTERNS),
    })]
    assert progress and status["ready"] is True and status["inference_verified"] is False


def test_prepare_download_filter_excludes_non_runtime_exports_and_full_precision_weights():
    patterns = SDXLGenerationBackend.DOWNLOAD_ALLOW_PATTERNS
    joined = "\n".join(patterns)
    assert "*.onnx" not in joined and "*.bin" not in joined
    assert "sd_xl_turbo_1.0" not in joined
    assert "model.fp16.safetensors" in joined
    assert "diffusion_pytorch_model.fp16.safetensors" in joined
    assert "model-*.fp16.safetensors" in joined
    assert "diffusion_pytorch_model-*.fp16.safetensors" in joined


def test_partial_download_returns_unready_instead_of_success(tmp_path, monkeypatch):
    backend = _backend(tmp_path / "partial", monkeypatch)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        snapshot_download=lambda *_args, **_kwargs: _json(backend.model_root / "model_index.json",
                                                         {"_class_name": "StableDiffusionXLPipeline", **COMPONENTS})))
    status = backend.prepare()
    assert status["ready"] is False and status["issues"]


def test_incomplete_snapshot_is_blocked_before_any_pipeline_constructor(installed, monkeypatch):
    backend, root = installed
    (root / WEIGHTS["unet"]).unlink()
    calls = []
    monkeypatch.setitem(sys.modules, "diffusers", SimpleNamespace(AutoPipelineForText2Image=SimpleNamespace(
        from_pretrained=lambda *_args, **_kwargs: calls.append("unexpected-load"))))
    with pytest.raises(RuntimeError, match="SDXL Turbo 모델 준비"):
        backend._load()
    assert calls == [] and backend._pipe is None


def test_loader_uses_the_validated_local_fp16_safetensors_contract(installed, monkeypatch):
    backend, root = installed
    calls = []
    pipe = SimpleNamespace(enable_model_cpu_offload=lambda: calls.append("offload"),
                           vae=SimpleNamespace(enable_slicing=lambda: calls.append("slice")))

    def fake_load(*args, **kwargs):
        calls.append((args, kwargs))
        return pipe

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(float16="fake-f16"))
    monkeypatch.setitem(sys.modules, "diffusers", SimpleNamespace(AutoPipelineForText2Image=SimpleNamespace(from_pretrained=fake_load)))
    assert backend._load() is pipe
    assert calls == [((str(root),), {"torch_dtype": "fake-f16", "variant": "fp16", "local_files_only": True,
                                     "use_safetensors": True, "safety_checker": None}), "offload", "slice"]
    assert backend._load() is pipe and len(calls) == 3
    loaded_status = backend.status()
    assert loaded_status["model_loaded"] is True
    assert loaded_status["package_imports_verified"] is True
    # Deserializing the pipeline still does not prove that a generated image
    # completed successfully or that tensor values match an upstream checksum.
    assert loaded_status["inference_verified"] is False
    assert loaded_status["weight_values_verified"] is False


def test_mutation_after_a_successful_status_is_not_hidden_by_a_readiness_cache(installed):
    backend, root = installed
    assert backend.status()["ready"] is True
    path = root / WEIGHTS["vae"]
    data = path.read_bytes()
    path.write_bytes(data[:-1])
    _failed(backend, "vae")
    path.write_bytes(data)
    assert backend.status()["ready"] is True


def test_status_evidence_is_a_snapshot_not_mutable_shared_state(installed):
    backend, _root = installed
    status = backend.status()
    original = deepcopy(status)
    status["components"]["unet"]["ready"] = False
    status["checked_files"].clear()
    assert backend.status() == original
