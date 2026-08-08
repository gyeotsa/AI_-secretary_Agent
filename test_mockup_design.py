import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageDraw
from PyQt6.QtWidgets import QApplication, QListWidgetItem

from core.mockup_design import MockupDesignRuntime
from core.model_registry import ModelRegistry, ModelRoleRouter
from core.specialist_workspaces import get_specialist_workspace_registry
from plugins.mockup_design import MockupDesignPlugin
from ui.specialist_workspaces import MockupWorkspaceWindow


class FakeVision:
    def analyze(self, paths, prompt, mode="general"):
        return {"analysis": "큰 제목, 짙은 배경, 밝은 강조색과 둥근 사진 카드가 반복됩니다."}


class FakeGenerationBackend:
    def __init__(self, ready=False):
        self.ready = ready

    def status(self):
        return {"ready": self.ready, "base_ready": self.ready, "adapter_ready": self.ready,
                "image_encoder_ready": self.ready, "cuda": True, "vram_mb": 8192}

    def prepare(self, progress=None):
        if progress: progress("준비 중")
        self.ready = True
        return self.status()

    def generate_background(self, **_kwargs):
        return Image.new("RGB", (512, 768), (30, 80, 120))


class FailingGenerationBackend(FakeGenerationBackend):
    def generate_background(self, **_kwargs):
        raise RuntimeError("테스트 생성 실패")


def _image(path: Path, color, size=(400, 500), accent=(255, 255, 255)):
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((35, 35, size[0] - 35, size[1] - 35), outline=accent, width=12)
    image.save(path)
    return str(path)


def test_learned_style_profile_and_rendered_output_are_persistent(tmp_path):
    refs = [_image(tmp_path / "ref1.png", (10, 25, 55)), _image(tmp_path / "ref2.png", (20, 35, 70))]
    products = [_image(tmp_path / "product1.png", (190, 80, 70), accent=(240, 220, 100))]
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend())
    profile = runtime.learn_style(refs, name="네이비 카드형")
    result = runtime.render(profile.profile_id, products, instruction="여름 신제품\n시원한 분위기", output_dir=tmp_path / "out", backend="local")
    output = Path(result["output"])
    assert output.is_file() and output.stat().st_size > 1000
    assert output.with_suffix(".json").is_file()
    assert len(profile.reference_hashes) == 2 and profile.palette
    assert runtime.load_profile(profile.profile_id).name == "네이비 카드형"


def test_reference_and_production_inputs_stay_separate_in_ui(tmp_path):
    app = QApplication.instance() or QApplication([])
    spec = get_specialist_workspace_registry().get("mockup")
    window = MockupWorkspaceWindow(spec, runtime=MockupDesignRuntime(
        tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend()))
    ref, product = str(tmp_path / "ref.png"), str(tmp_path / "product.png")
    _image(Path(ref), (1, 2, 3)); _image(Path(product), (4, 5, 6))
    window.reference_paths.append(ref); window.reference_list.addItem(QListWidgetItem("ref.png"))
    window.production_paths.append(product); window.production_list.addItem(QListWidgetItem("product.png"))
    assert window.reference_paths == [ref]
    assert window.production_paths == [product]
    assert window.reference_list is not window.production_list
    window.close(); assert app is not None


def test_mockup_workspace_and_model_role_are_registered():
    registry = get_specialist_workspace_registry()
    assert registry.match_open_command("시안 제작 전문가 작업공간 열어줘").key == "mockup"
    assert ModelRegistry().resolve("mockup").role == "mockup_design"
    assert ModelRoleRouter().route(allowed_tools=["mockup_learn_style"], modalities=["image"]) == "mockup_design"


def test_mockup_plugin_exposes_two_stage_contract():
    names = {tool.name for tool in MockupDesignPlugin().get_tools()}
    assert names == {"mockup_learn_style", "mockup_render", "mockup_list_styles",
                     "mockup_delete_style", "mockup_generation_status", "mockup_prepare_generation"}


def test_profile_can_be_deleted_without_deleting_reference(tmp_path):
    ref = Path(_image(tmp_path / "reference.png", (10, 20, 30)))
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend())
    profile = runtime.learn_style([str(ref)])
    assert runtime.delete_profile(profile.profile_id) is True
    assert runtime.delete_profile(profile.profile_id) is False
    assert ref.is_file()


def test_preview_is_not_finally_saved_until_user_confirms(tmp_path):
    ref = _image(tmp_path / "ref.png", (10, 20, 30))
    product = _image(tmp_path / "product.png", (180, 90, 50))
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend())
    profile = runtime.learn_style([ref])
    output_dir = tmp_path / "final"
    preview = runtime.render(profile.profile_id, [product], instruction="따뜻한 광고 시안",
                             output_dir=output_dir, backend="local", preview_only=True)
    assert Path(preview["output"]).is_file()
    assert not output_dir.exists()
    assert not Path(preview["output"]).with_suffix(".json").exists()
    saved = runtime.save_preview(preview["output"], output_dir / "confirmed.png", preview)
    assert Path(saved["output"]).is_file()
    assert Path(saved["output"]).with_suffix(".json").is_file()


def test_instruction_is_metadata_not_implicit_visible_title(tmp_path):
    ref = _image(tmp_path / "ref.png", (10, 20, 30))
    product = _image(tmp_path / "product.png", (180, 90, 50))
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend())
    profile = runtime.learn_style([ref])
    result = runtime.render(profile.profile_id, [product], instruction="이 문장을 이미지에 쓰지 말고 분위기만 반영",
                            output_dir=tmp_path / "out", backend="local")
    assert result["instruction"] == "이 문장을 이미지에 쓰지 말고 분위기만 반영"
    assert result["composition_plan"]


def test_manual_edit_creates_new_non_destructive_preview(tmp_path):
    source = Path(_image(tmp_path / "source.png", (10, 20, 30), size=(400, 500)))
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend())
    edited = runtime.transform_preview(source, "rotate_left")
    assert Path(edited["output"]).is_file()
    assert Image.open(edited["output"]).size == (500, 400)
    assert Image.open(source).size == (400, 500)


def test_generative_backend_preserves_production_layout_and_records_provenance(tmp_path):
    refs = [_image(tmp_path / "ref.png", (10, 20, 40))]
    products = [_image(tmp_path / "product.png", (200, 90, 70))]
    backend = FakeGenerationBackend(ready=True)
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=backend)
    profile = runtime.learn_style(refs, name="생성형 스타일")
    result = runtime.render(profile.profile_id, products, output_dir=tmp_path / "out", backend="auto")
    assert Path(result["output"]).is_file()
    assert result["generation_backend"] == "generative"
    assert "ip-adapter" in result["renderer"]


def test_generation_model_prepare_contract(tmp_path):
    backend = FakeGenerationBackend()
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=backend)
    messages = []
    assert runtime.prepare_generation_models(messages.append)["ready"] is True
    assert messages == ["준비 중"]


def test_auto_backend_falls_back_but_explicit_generative_reports_failure(tmp_path):
    refs = [_image(tmp_path / "ref.png", (10, 20, 40))]
    products = [_image(tmp_path / "product.png", (200, 90, 70))]
    runtime = MockupDesignRuntime(
        tmp_path / "styles", vision=FakeVision(), generation_backend=FailingGenerationBackend(ready=True))
    profile = runtime.learn_style(refs)
    result = runtime.render(profile.profile_id, products, output_dir=tmp_path / "out", backend="auto")
    assert result["generation_backend"] == "local"
    assert result["generation_fallback_reason"] == "테스트 생성 실패"
    import pytest
    with pytest.raises(RuntimeError, match="테스트 생성 실패"):
        runtime.render(profile.profile_id, products, output_dir=tmp_path / "out", backend="generative")
