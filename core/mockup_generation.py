"""Lazy SD1.5 + IP-Adapter Plus backend sized for an 8 GB NVIDIA GPU."""
from __future__ import annotations

import gc
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


class SDXLGenerationBackend:
    """Optional high-quality background generator; exact text stays in SVG layers."""
    MODEL_ID = "stabilityai/sdxl-turbo"

    def __init__(self, model_root: str | Path = "data/models/mockup_generation/sdxl-turbo"):
        self.model_root = Path(model_root).resolve()
        self._pipe = None

    def status(self) -> dict:
        try:
            import torch
            cuda = torch.cuda.is_available()
            vram_mb = int(torch.cuda.get_device_properties(0).total_memory / 1024**2) if cuda else 0
        except Exception:
            cuda, vram_mb = False, 0
        return {"ready": (self.model_root / "model_index.json").is_file() and cuda,
                "model": self.MODEL_ID, "cuda": cuda, "vram_mb": vram_mb,
                "model_root": str(self.model_root)}

    def prepare(self, progress=None) -> dict:
        from huggingface_hub import snapshot_download
        if progress: progress("SDXL Turbo 모델을 준비하고 있습니다.")
        self.model_root.mkdir(parents=True, exist_ok=True)
        snapshot_download(self.MODEL_ID, local_dir=str(self.model_root))
        return self.status()

    def _load(self):
        if self._pipe is not None: return self._pipe
        if not self.status()["ready"]: raise RuntimeError("SDXL Turbo 모델 준비가 필요합니다.")
        import torch
        from diffusers import AutoPipelineForText2Image
        pipe = AutoPipelineForText2Image.from_pretrained(
            str(self.model_root), torch_dtype=torch.float16, variant="fp16",
            local_files_only=True, safety_checker=None)
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
