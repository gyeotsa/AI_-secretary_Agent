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


def _image(path: Path, color, size=(400, 500), accent=(255, 255, 255)):
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((35, 35, size[0] - 35, size[1] - 35), outline=accent, width=12)
    image.save(path)
    return str(path)


def test_learned_style_profile_and_rendered_output_are_persistent(tmp_path):
    refs = [_image(tmp_path / "ref1.png", (10, 25, 55)), _image(tmp_path / "ref2.png", (20, 35, 70))]
    products = [_image(tmp_path / "product1.png", (190, 80, 70), accent=(240, 220, 100))]
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision())
    profile = runtime.learn_style(refs, name="네이비 카드형")
    result = runtime.render(profile.profile_id, products, instruction="여름 신제품\n시원한 분위기", output_dir=tmp_path / "out")
    output = Path(result["output"])
    assert output.is_file() and output.stat().st_size > 1000
    assert output.with_suffix(".json").is_file()
    assert len(profile.reference_hashes) == 2 and profile.palette
    assert runtime.load_profile(profile.profile_id).name == "네이비 카드형"


def test_reference_and_production_inputs_stay_separate_in_ui(tmp_path):
    app = QApplication.instance() or QApplication([])
    spec = get_specialist_workspace_registry().get("mockup")
    window = MockupWorkspaceWindow(spec, runtime=MockupDesignRuntime(tmp_path / "styles", vision=FakeVision()))
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
    assert names == {"mockup_learn_style", "mockup_render", "mockup_list_styles"}
