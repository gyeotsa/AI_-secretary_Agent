"""Lazy SD1.5 + IP-Adapter Plus backend sized for an 8 GB NVIDIA GPU."""
from __future__ import annotations

import gc
import importlib.util
import json
import re
import stat
from pathlib import Path

from PIL import Image, ImageOps


class IPAdapterGenerationBackend:
    BASE_MODEL_ID = "stable-diffusion-v1-5/stable-diffusion-v1-5"
    ADAPTER_MODEL_ID = "h94/IP-Adapter"
    ADAPTER_WEIGHT = "ip-adapter-plus_sd15.safetensors"

    def __init__(self, model_root: str | Path = "data/models/mockup_generation"):
        self.model_root = Path(model_root).resolve()
        self.base_dir = self.model_root / "stable-diffusion-v1-5"
        self.adapter_dir = self.model_root / "ip-adapter"
        self._pipe = None

    def status(self) -> dict:
        packages = {}
        for name in ("torch", "diffusers", "accelerate", "transformers", "safetensors"):
            try:
                module = __import__(name)
                packages[name] = getattr(module, "__version__", "installed")
            except Exception:
                packages[name] = "missing"
        required_base_files = [
            "model_index.json",
            "text_encoder/model.fp16.safetensors",
            "unet/diffusion_pytorch_model.fp16.safetensors",
            "vae/diffusion_pytorch_model.fp16.safetensors",
            "tokenizer/vocab.json",
            "scheduler/scheduler_config.json",
        ]
        base_ready = all((self.base_dir / relative).is_file() for relative in required_base_files)
        adapter_ready = (self.adapter_dir / "models" / self.ADAPTER_WEIGHT).is_file()
        encoder_ready = (self.adapter_dir / "models" / "image_encoder" / "config.json").is_file()
        try:
            import torch
            cuda = torch.cuda.is_available()
            vram_mb = int(torch.cuda.get_device_properties(0).total_memory / 1024**2) if cuda else 0
        except Exception:
            cuda, vram_mb = False, 0
        return {
            "ready": base_ready and adapter_ready and encoder_ready and cuda,
            "base_model": self.BASE_MODEL_ID,
            "adapter_model": self.ADAPTER_MODEL_ID,
            "base_ready": base_ready,
            "adapter_ready": adapter_ready,
            "image_encoder_ready": encoder_ready,
            "cuda": cuda,
            "vram_mb": vram_mb,
            "packages": packages,
            "model_root": str(self.model_root),
        }

    def prepare(self, progress=None) -> dict:
        """Download only inference files into the application-owned model directory."""
        from huggingface_hub import snapshot_download

        self.model_root.mkdir(parents=True, exist_ok=True)
        if progress:
            progress("Stable Diffusion 1.5 모델을 준비하고 있습니다.")
        snapshot_download(
            self.BASE_MODEL_ID,
            local_dir=str(self.base_dir),
            allow_patterns=[
                "model_index.json",
                "scheduler/scheduler_config.json",
                "tokenizer/*.json", "tokenizer/*.txt",
                "text_encoder/config.json", "text_encoder/model.fp16.safetensors",
                "unet/config.json", "unet/diffusion_pytorch_model.fp16.safetensors",
                "vae/config.json", "vae/diffusion_pytorch_model.fp16.safetensors",
                "feature_extractor/preprocessor_config.json",
            ],
        )
        if progress:
            progress("IP-Adapter Plus와 이미지 인코더를 준비하고 있습니다.")
        snapshot_download(
            self.ADAPTER_MODEL_ID,
            local_dir=str(self.adapter_dir),
            allow_patterns=[
                f"models/{self.ADAPTER_WEIGHT}",
                "models/image_encoder/*.json",
                "models/image_encoder/*.safetensors",
            ],
        )
        return self.status()

    def _load(self):
        if self._pipe is not None:
            return self._pipe
        if not self.status()["ready"]:
            raise RuntimeError("생성형 시안 모델이 준비되지 않았습니다. UI에서 모델 준비를 먼저 실행하세요.")
        import torch
        from diffusers import StableDiffusionPipeline

        pipe = StableDiffusionPipeline.from_pretrained(
            str(self.base_dir),
            torch_dtype=torch.float16,
            use_safetensors=True,
            variant="fp16",
            safety_checker=None,
            local_files_only=True,
        )
        pipe.load_ip_adapter(
            str(self.adapter_dir),
            subfolder="models",
            weight_name=self.ADAPTER_WEIGHT,
            local_files_only=True,
        )
        pipe.set_ip_adapter_scale(0.72)
        # Do not call enable_attention_slicing after load_ip_adapter: it replaces
        # the IP-Adapter-aware attention processors with generic sliced ones.
        pipe.vae.enable_slicing()
        pipe.enable_model_cpu_offload()
        self._pipe = pipe
        return pipe

    @staticmethod
    def reference_sheet(paths: list[str], size=(768, 768)) -> Image.Image:
        images = []
        for value in paths[:9]:
            with Image.open(value) as source:
                images.append(source.convert("RGB").copy())
        if not images:
            raise ValueError("스타일 참고 이미지가 없습니다.")
        columns = 1 if len(images) == 1 else 2 if len(images) <= 4 else 3
        rows = (len(images) + columns - 1) // columns
        cell_w, cell_h = size[0] // columns, size[1] // rows
        sheet = Image.new("RGB", size, "white")
        for index, source in enumerate(images):
            tile = ImageOps.fit(source, (cell_w, cell_h), method=Image.Resampling.LANCZOS)
            sheet.paste(tile, ((index % columns) * cell_w, (index // columns) * cell_h))
        return sheet

    def generate_background(self, *, reference_paths: list[str], prompt: str,
                            orientation: str, seed: int = 42) -> Image.Image:
        import torch

        pipe = self._load()
        width, height = (
            (768, 512) if orientation == "landscape" else
            (512, 768) if orientation == "portrait" else (640, 640)
        )
        style_image = self.reference_sheet(reference_paths)
        final_prompt = (
            "professional commercial graphic design background, polished visual hierarchy, "
            "premium advertising layout, no readable text, no letters, no logos, "
            "empty areas for product photos, " + str(prompt or "modern editorial design")
        )
        negative = (
            "text, letters, watermark, logo, signature, malformed objects, duplicated objects, "
            "low quality, blurry, noisy, oversaturated"
        )
        generator = torch.Generator(device="cpu").manual_seed(int(seed))
        with torch.inference_mode():
            image = pipe(
                prompt=final_prompt,
                negative_prompt=negative,
                ip_adapter_image=style_image,
                width=width,
                height=height,
                num_inference_steps=24,
                guidance_scale=7.0,
                generator=generator,
            ).images[0]
        return image.convert("RGB")

    def unload(self):
        self._pipe = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


class _SDXLSnapshotValidator:
    """Inspect a local FP16 diffusers snapshot without loading tensor values.

    This is a structural installation check, not a model checksum or inference
    test. Bounded JSON/safetensors header reads keep status polling lightweight.
    """

    JSON_LIMIT = 8 * 1024 * 1024
    COMPONENTS = {
        "unet": ("diffusers", "UNet2DConditionModel"),
        "vae": ("diffusers", "AutoencoderKL"),
        "text_encoder": ("transformers", "CLIPTextModel"),
        "text_encoder_2": ("transformers", "CLIPTextModelWithProjection"),
        "tokenizer": ("transformers", "CLIPTokenizer"),
        "tokenizer_2": ("transformers", "CLIPTokenizer"),
        # stabilityai/sdxl-turbo publishes the ancestral scheduler.  Keeping
        # this declaration aligned with the upstream model_index.json matters:
        # the previous EulerDiscreteScheduler value rejected a complete,
        # official snapshot before Diffusers ever had a chance to load it.
        "scheduler": ("diffusers", "EulerAncestralDiscreteScheduler"),
    }
    DTYPE_BYTES = {"F64": 8, "F32": 4, "F16": 2, "BF16": 2, "I64": 8,
                   "I32": 4, "I16": 2, "I8": 1, "U64": 8, "U32": 4,
                   "U16": 2, "U8": 1, "BOOL": 1, "F8_E4M3": 1, "F8_E5M2": 1}

    def __init__(self, root: Path):
        self.root = root
        self.checked_files: set[str] = set()

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"중복 JSON 키: {key}")
            result[key] = value
        return result

    @classmethod
    def _decode_json(cls, raw):
        def invalid_constant(value):
            raise ValueError(f"유효하지 않은 JSON 상수: {value}")
        data = json.loads(raw, object_pairs_hook=cls._unique_object, parse_constant=invalid_constant)
        if not isinstance(data, dict) or not data:
            raise ValueError("비어 있지 않은 JSON 객체가 필요합니다.")
        return data

    def file(self, relative: str) -> Path:
        parts = relative.split("/")
        if any(not part or part in {".", ".."} or "\\" in part or ":" in part for part in parts):
            raise ValueError(f"모델 폴더 밖의 경로는 허용되지 않습니다: {relative}")
        # Hugging Face's normal cache snapshots use links from a revision
        # directory into the content-addressed blob store.  Follow those links
        # to the final file instead of rejecting an otherwise official cache
        # snapshot.  The requested relative path itself is still constrained
        # above, and the resolved target must be a non-empty regular file whose
        # JSON/safetensors contents are validated before it is trusted.
        path = (self.root.joinpath(*parts)).resolve(strict=True)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise ValueError(f"비어 있거나 일반 파일이 아닙니다: {relative}")
        self.checked_files.add(relative)
        return path

    def json_file(self, relative: str) -> dict:
        path = self.file(relative)
        with path.open("rb") as handle:
            raw = handle.read(self.JSON_LIMIT + 1)
        if len(raw) > self.JSON_LIMIT:
            raise ValueError(f"JSON 크기 제한을 초과했습니다: {relative}")
        try:
            return self._decode_json(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"{relative}: {exc}") from exc

    def manifest(self):
        data = self.json_file("model_index.json")
        if data.get("_class_name") != "StableDiffusionXLPipeline":
            raise ValueError("model_index.json: SDXL 텍스트→이미지 파이프라인이 아닙니다.")
        if any(key in data for key in ("auto_map", "custom_pipeline", "_module")):
            raise ValueError("model_index.json: 사용자 지정 실행 코드가 포함되어 있습니다.")
        for name, expected in self.COMPONENTS.items():
            if data.get(name) != list(expected):
                raise ValueError(f"model_index.json: 필수 구성요소 {name} 선언이 올바르지 않습니다.")
        for name in ("image_encoder", "feature_extractor"):
            if data.get(name) not in (None, [None, None]):
                raise ValueError(f"model_index.json: 지원하지 않는 선택 구성요소 {name}입니다.")

    def config(self, name: str):
        relative = f"{name}/" + ("scheduler_config.json" if name == "scheduler" else "config.json")
        data = self.json_file(relative)
        if any(key in data for key in ("auto_map", "custom_pipeline", "_module")):
            raise ValueError(f"{relative}: 사용자 지정 실행 코드는 지원하지 않습니다.")
        library, class_name = self.COMPONENTS[name]
        if library == "diffusers" and data.get("_class_name") != class_name:
            raise ValueError(f"{relative}: 구성요소 클래스가 일치하지 않습니다.")
        if library == "transformers" and (
            data.get("model_type") != "clip_text_model" or
            not isinstance(data.get("architectures"), list) or class_name not in data["architectures"]
        ):
            raise ValueError(f"{relative}: CLIP 텍스트 인코더 구성이 아닙니다.")
        numeric_fields = (("vocab_size", "hidden_size", "num_hidden_layers", "num_attention_heads",
                           "max_position_embeddings") if name.startswith("text_encoder") else
                          ("num_train_timesteps",) if name == "scheduler" else
                          ("sample_size", "in_channels", "out_channels") if name == "unet" else
                          ("in_channels", "out_channels", "latent_channels"))
        for field in numeric_fields:
            if type(data.get(field)) is not int or data[field] <= 0:
                raise ValueError(f"{relative}: 필수 양의 정수 설정 {field}이 잘못되었습니다.")
        if name in {"unet", "vae"}:
            channels = data.get("block_out_channels")
            if (not isinstance(channels, list) or not channels or
                any(type(value) is not int or value <= 0 for value in channels)):
                raise ValueError(f"{relative}: block_out_channels 구성이 잘못되었습니다.")
            for field in ("down_block_types", "up_block_types"):
                blocks = data.get(field)
                if (not isinstance(blocks, list) or len(blocks) != len(channels) or
                    any(not isinstance(value, str) or not value for value in blocks)):
                    raise ValueError(f"{relative}: {field} 구성이 잘못되었습니다.")

    def tokenizer(self, name: str):
        config = self.json_file(f"{name}/tokenizer_config.json")
        tokenizer_class = config.get("tokenizer_class")
        if (not isinstance(tokenizer_class, str) or
            tokenizer_class not in {"CLIPTokenizer", "CLIPTokenizerFast"} or "auto_map" in config):
            raise ValueError(f"{name}: CLIP 토크나이저 구성이 아닙니다.")
        vocab = self.json_file(f"{name}/vocab.json")
        if (not {"<|startoftext|>", "<|endoftext|>"}.issubset(vocab) or
            any(type(value) is not int or value < 0 for value in vocab.values()) or
            len(set(vocab.values())) != len(vocab)):
            raise ValueError(f"{name}/vocab.json: 토큰 사전이 유효하지 않습니다.")
        path = self.file(f"{name}/merges.txt")
        with path.open("rb") as handle:
            raw = handle.read(self.JSON_LIMIT + 1)
        if len(raw) > self.JSON_LIMIT:
            raise ValueError(f"{name}/merges.txt: 크기 제한을 초과했습니다.")
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip() and not line.startswith("#")]
        if not lines or any(len(line.split()) != 2 for line in lines):
            raise ValueError(f"{name}/merges.txt: BPE 병합 규칙이 누락되었거나 잘못되었습니다.")

    def safetensors(self, relative: str) -> tuple[set[str], int]:
        path = self.file(relative)
        size = path.stat().st_size
        with path.open("rb") as handle:
            prefix = handle.read(8)
            header_size = int.from_bytes(prefix, "little")
            if len(prefix) != 8 or not 1 <= header_size <= self.JSON_LIMIT or header_size + 8 >= size:
                raise ValueError(f"{relative}: 가중치 헤더/파일 크기가 잘못되었습니다(부분 다운로드 또는 LFS 포인터).")
            header = self._decode_json(handle.read(header_size))
        metadata = header.pop("__metadata__", {})
        if not isinstance(metadata, dict) or any(not isinstance(value, str) for value in metadata.values()):
            raise ValueError(f"{relative}: 가중치 메타데이터가 잘못되었습니다.")
        if not header:
            raise ValueError(f"{relative}: 텐서가 없습니다.")
        spans = []
        payload_length = size - 8 - header_size
        for name, tensor in header.items():
            if not name or not isinstance(tensor, dict):
                raise ValueError(f"{relative}: 텐서 선언이 잘못되었습니다.")
            shape, offsets, dtype = tensor.get("shape"), tensor.get("data_offsets"), tensor.get("dtype")
            if (not isinstance(dtype, str) or dtype not in self.DTYPE_BYTES or
                not isinstance(shape, list) or len(shape) > 32 or
                any(type(value) is not int or value < 0 for value in shape) or
                not isinstance(offsets, list) or len(offsets) != 2 or
                any(type(value) is not int or value < 0 for value in offsets)):
                raise ValueError(f"{relative}: 텐서 형식/차원/오프셋이 유효하지 않습니다.")
            start, end = offsets
            dtype_bytes = self.DTYPE_BYTES[dtype]
            # Calculate the element count with a payload-derived ceiling.  A
            # malicious but JSON-valid header must not make status polling
            # spend time multiplying enormous arbitrary-precision integers.
            elements = 1
            for dimension in shape:
                if dimension and elements > payload_length // dtype_bytes // dimension:
                    raise ValueError(f"{relative}: 텐서 차원이 실제 payload보다 큽니다.")
                elements *= dimension
            if end - start != elements * dtype_bytes or end > payload_length:
                raise ValueError(f"{relative}: 텐서 크기와 실제 가중치 파일 길이가 일치하지 않습니다.")
            spans.append((start, end))
        cursor = 0
        for start, end in sorted(spans):
            if start != cursor or end < start:
                raise ValueError(f"{relative}: 텐서 데이터가 겹치거나 누락되었습니다.")
            cursor = end
        if cursor != payload_length:
            raise ValueError(f"{relative}: 가중치 payload 길이가 헤더와 일치하지 않습니다.")
        return set(header), cursor

    def weights(self, name: str):
        stem = "model" if name.startswith("text_encoder") else "diffusion_pytorch_model"
        single = f"{name}/{stem}.fp16.safetensors"
        indexes = [f"{name}/{stem}.safetensors.index.fp16.json", f"{name}/{stem}.fp16.safetensors.index.json"]
        # lstat also detects broken links, which must not silently select another file.
        existing = []
        for relative in indexes:
            try:
                (self.root / relative).lstat()
                existing.append(relative)
            except FileNotFoundError:
                pass
        if not existing:
            self.safetensors(single)
            return
        for relative in existing:
            index = self.json_file(relative)
            weight_map = index.get("weight_map")
            if not isinstance(weight_map, dict) or not weight_map:
                raise ValueError(f"{relative}: weight_map이 누락되었습니다.")
            shards = {}
            shard_numbers = {}
            for tensor, filename in weight_map.items():
                match = (re.fullmatch(re.escape(stem) +
                         r"(?:\.fp16-(\d{5})-of-(\d{5})|-(\d{5})-of-(\d{5})\.fp16)\.safetensors", filename)
                         if isinstance(filename, str) else None)
                if (not isinstance(tensor, str) or not tensor or not isinstance(filename, str) or
                    "/" in filename or "\\" in filename or ":" in filename or
                    match is None):
                    raise ValueError(f"{relative}: 샤드 파일 경로가 유효하지 않습니다.")
                number, count = map(int, (match.group(1), match.group(2)) if match.group(1) else
                                    (match.group(3), match.group(4)))
                if not 1 <= number <= count <= 10000:
                    raise ValueError(f"{relative}: 샤드 번호/총개수가 올바르지 않습니다.")
                shard_numbers[filename] = (number, count)
                shards.setdefault(filename, set()).add(tensor)
            counts = {item[1] for item in shard_numbers.values()}
            if (len(counts) != 1 or len(shards) != next(iter(counts)) or
                {item[0] for item in shard_numbers.values()} != set(range(1, len(shards) + 1))):
                raise ValueError(f"{relative}: 전체 샤드 목록이 완전하지 않습니다.")
            total_size = 0
            for filename, expected in shards.items():
                actual, payload_size = self.safetensors(f"{name}/{filename}")
                if actual != expected:
                    raise ValueError(f"{relative}: 샤드의 텐서 목록이 weight_map과 일치하지 않습니다.")
                total_size += payload_size
            metadata = index.get("metadata", {})
            if not isinstance(metadata, dict) or (
                "total_size" in metadata and
                (type(metadata["total_size"]) is not int or metadata["total_size"] != total_size)
            ):
                raise ValueError(f"{relative}: 샤드 전체 가중치 크기가 일치하지 않습니다.")

    def inspect(self) -> dict:
        components, issues = {}, []
        checks = {"manifest": self.manifest, "scheduler": lambda: self.config("scheduler")}
        checks.update({name: lambda name=name: self.tokenizer(name) for name in ("tokenizer", "tokenizer_2")})
        checks.update({name: lambda name=name: (self.config(name), self.weights(name))
                       for name in ("text_encoder", "text_encoder_2", "unet", "vae")})
        for name, check in checks.items():
            before = set(self.checked_files)
            try:
                check()
                components[name] = {"ready": True, "checked_files": sorted(self.checked_files - before)}
            except (OSError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
                components[name] = {"ready": False, "reason": str(exc)}
                issues.append({"component": name, "reason": str(exc)})
        return {"files_ready": not issues, "components": components, "issues": issues,
                "checked_files": sorted(self.checked_files)}


class SDXLGenerationBackend:
    """Optional high-quality background generator; exact text stays in SVG layers."""
    MODEL_ID = "stabilityai/sdxl-turbo"
    DOWNLOAD_ALLOW_PATTERNS = (
        "model_index.json",
        "scheduler/scheduler_config.json",
        "tokenizer/merges.txt",
        "tokenizer/*.json",
        "tokenizer_2/merges.txt",
        "tokenizer_2/*.json",
        "text_encoder/config.json",
        "text_encoder/model.fp16.safetensors",
        "text_encoder/model.safetensors.index.fp16.json",
        "text_encoder/model.fp16.safetensors.index.json",
        "text_encoder/model.fp16-*.safetensors",
        "text_encoder/model-*.fp16.safetensors",
        "text_encoder_2/config.json",
        "text_encoder_2/model.fp16.safetensors",
        "text_encoder_2/model.safetensors.index.fp16.json",
        "text_encoder_2/model.fp16.safetensors.index.json",
        "text_encoder_2/model.fp16-*.safetensors",
        "text_encoder_2/model-*.fp16.safetensors",
        "unet/config.json",
        "unet/diffusion_pytorch_model.fp16.safetensors",
        "unet/diffusion_pytorch_model.safetensors.index.fp16.json",
        "unet/diffusion_pytorch_model.fp16.safetensors.index.json",
        "unet/diffusion_pytorch_model.fp16-*.safetensors",
        "unet/diffusion_pytorch_model-*.fp16.safetensors",
        "vae/config.json",
        "vae/diffusion_pytorch_model.fp16.safetensors",
        "vae/diffusion_pytorch_model.safetensors.index.fp16.json",
        "vae/diffusion_pytorch_model.fp16.safetensors.index.json",
        "vae/diffusion_pytorch_model.fp16-*.safetensors",
        "vae/diffusion_pytorch_model-*.fp16.safetensors",
    )

    def __init__(self, model_root: str | Path = "data/models/mockup_generation/sdxl-turbo"):
        self.model_root = Path(model_root).absolute()
        self._pipe = None

    @staticmethod
    def _device_status():
        try:
            import torch
            cuda = torch.cuda.is_available()
            vram_mb = int(torch.cuda.get_device_properties(0).total_memory / 1024**2) if cuda else 0
        except Exception:
            cuda, vram_mb = False, 0
        return bool(cuda), vram_mb

    @staticmethod
    def _package_status():
        packages = {}
        for name in ("torch", "diffusers", "accelerate", "transformers", "safetensors"):
            try:
                packages[name] = "discoverable" if importlib.util.find_spec(name) is not None else "missing"
            except (ImportError, ValueError, AttributeError):
                packages[name] = "missing"
        return packages

    def status(self) -> dict:
        snapshot = _SDXLSnapshotValidator(self.model_root).inspect()
        packages = self._package_status()
        packages_ready = all(value == "discoverable" for value in packages.values())
        cuda, vram_mb = self._device_status()
        return {**snapshot, "ready": snapshot["files_ready"] and packages_ready and cuda,
                "model": self.MODEL_ID, "cuda": cuda, "vram_mb": vram_mb,
                "packages": packages, "packages_ready": packages_ready,
                "packages_validation": "module_discovery_only",
                "package_imports_verified": self._pipe is not None,
                "model_root": str(self.model_root), "model_loaded": self._pipe is not None,
                "readiness_scope": "managed_local_fp16_snapshot_dependencies_and_cuda",
                "linked_cache_snapshot_support": "resolved_regular_files",
                "weights_validation": "safetensors_header_payload_length_and_shard_index",
                "weight_values_verified": False, "checksums_verified": False,
                "source_revision_verified": False, "inference_verified": False}

    def prepare(self, progress=None) -> dict:
        from huggingface_hub import snapshot_download
        if progress: progress("SDXL Turbo 모델을 준비하고 있습니다.")
        self.model_root.mkdir(parents=True, exist_ok=True)
        # Do not fetch the repository's full-precision checkpoint, ONNX exports,
        # sample media or standalone checkpoint.  The local loader below asks
        # specifically for the Diffusers FP16 variant, so downloading anything
        # else only wastes disk/network resources and can mask a partial FP16
        # installation.
        snapshot_download(
            self.MODEL_ID,
            local_dir=str(self.model_root),
            allow_patterns=list(self.DOWNLOAD_ALLOW_PATTERNS),
        )
        return self.status()

    def _load(self):
        if self._pipe is not None: return self._pipe
        status = self.status()
        if not status["ready"]:
            reasons = [item["reason"] for item in status.get("issues", [])]
            if not status["packages_ready"]: reasons.append("필수 Python 패키지가 누락되었습니다.")
            if not status["cuda"]: reasons.append("CUDA 장치를 사용할 수 없습니다.")
            raise RuntimeError("SDXL Turbo 모델 준비가 필요합니다. " + "; ".join(reasons[:3]))
        import torch
        from diffusers import AutoPipelineForText2Image
        pipe = AutoPipelineForText2Image.from_pretrained(
            str(self.model_root), torch_dtype=torch.float16, variant="fp16",
            local_files_only=True, use_safetensors=True, safety_checker=None)
        pipe.enable_model_cpu_offload(); pipe.vae.enable_slicing(); self._pipe = pipe
        return pipe

    def generate_background(self, *, reference_paths: list[str], prompt: str,
                            orientation: str, seed: int = 42) -> Image.Image:
        import torch
        pipe = self._load()
        width, height = ((768, 512) if orientation == "landscape" else
                         (512, 768) if orientation == "portrait" else (640, 640))
        generator = torch.Generator(device="cpu").manual_seed(int(seed))
        exact = ("professional graphic design background, no text, no letters, no logo, "
                 "reserved negative space for exact raster photo and vector typography, " + str(prompt))
        with torch.inference_mode():
            return pipe(prompt=exact, width=width, height=height, num_inference_steps=4,
                        guidance_scale=0.0, generator=generator).images[0].convert("RGB")

    def unload(self):
        self._pipe = None; gc.collect()
        try:
            import torch
            if torch.cuda.is_available(): torch.cuda.empty_cache()
        except Exception: pass
