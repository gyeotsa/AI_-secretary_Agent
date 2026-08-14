import os
import hashlib
import time
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image, ImageDraw
from PyQt6.QtWidgets import QApplication, QListWidgetItem

from core.mockup_design import MockupDesignRuntime
from core.mockup_scene import (build_evidence_fallback_plan, enforce_explicit_user_constraints,
                               enforce_measured_style_evidence, merge_scoped_scene_edit,
                               normalize_scene_plan, ScenePlanError, filter_scene_edit_patch,
                               validate_patch_against_instruction)
from core.model_registry import ModelRegistry, ModelRoleRouter
from core.specialist_workspaces import get_specialist_workspace_registry
from plugins.mockup_design import MockupDesignPlugin
from ui.specialist_workspaces import MockupWorkspaceWindow


def test_windows_localized_font_family_resolves_to_real_file():
    gungsuh = MockupDesignRuntime._font(40, bold=True, family="궁서")
    human_round = MockupDesignRuntime._font(40, bold=True, family="휴먼둥근헤드라인")
    assert Path(gungsuh.path).name.casefold() == "h2gsrb.ttf"
    assert Path(human_round.path).name.casefold() == "hmkmrhd.ttf"


def test_face_visibility_is_grounded_in_detected_source_coordinates(tmp_path, monkeypatch):
    source = tmp_path / "portrait.jpg"
    Image.new("RGB", (800, 1200), "gray").save(source)
    runtime = MockupDesignRuntime(profile_dir=tmp_path / "profiles")
    monkeypatch.setattr(runtime, "_detect_primary_face", lambda _path: {
        "center_x": .52, "center_y": .14, "width": .13, "height": .09,
        "source_ratio": 800 / 1200,
    })
    plan = normalize_scene_plan({
        "canvas": {"aspect_ratio": 1, "background": "#fff"},
        "assets": [{"index": 0, "x": .1, "y": .1, "width": .8, "height": .8,
                    "shape": "ellipse", "fit": "cover", "zoom": 1,
                    "focal_x": .5, "focal_y": .5}],
        "texts": [], "decorations": [],
    }, asset_count=1)
    face_only, fields = runtime._enforce_detected_subject_visibility(
        plan, [source], "스티커 안에는 얼굴만 나오게 해줘"
    )
    assert face_only["assets"][0]["focal_y"] == .14
    assert face_only["assets"][0]["zoom"] > 1
    assert "assets[0].focal_y" in fields
    visible, _ = runtime._enforce_detected_subject_visibility(
        plan, [source], "얼굴이 보이게 수정해줘"
    )
    assert visible["assets"][0]["fit"] == "contain"


class FakeVision:
    def analyze(self, paths, prompt, mode="general"):
        if "이미지 편집 결과 의미 검증기" in prompt:
            import json
            return {"analysis": json.dumps({"fulfilled": True, "reason": "요청한 배경만 변경됨",
                "missing_requirements": [], "unintended_changes": []}, ensure_ascii=False)}
        if "비파괴 이미지 편집 명령 해석기" in prompt:
            import json
            background = "#dde5ea" if "#eef2f5" in prompt else "#eef2f5"
            return {"analysis": json.dumps({
                "intent_summary": "요청한 분위기에 맞춰 배경만 조정",
                "canvas": {"background": background},
                "assets": [], "texts": [], "replace_decorations": False,
                "decorations": [], "visible_copy": "", "remove_visible_copy": False,
                "success_criteria": ["배경색이 차분한 회청색으로 바뀐다"],
            }, ensure_ascii=False)}
        if ('필수 최상위 키는 canvas, assets' in prompt or
                'canvas, assets, decorations, texts, rationale 키를 가진 JSON' in prompt or
                '전체 JSON 설계도만 반환' in prompt or
                '최상위 키는 canvas, assets, decorations, texts, rationale' in prompt):
            import json, re
            if "1차 설계도:" in prompt:
                start = prompt.index("1차 설계도:") + len("1차 설계도:")
                end = prompt.index("\n사용자 지시:", start)
                return {"analysis": prompt[start:end].strip()}
            match = re.search(r"마지막 (\d+)장은 제작용", prompt)
            count = int(match.group(1)) if match else 1
            changed = "기존 설계도를 사용자의 수정 명령" in prompt
            copy_match = (re.search(r"이미지에 실제 표시할 문구: (.+)", prompt) or
                          re.search(r"표시 문구: (.+)", prompt))
            copy = copy_match.group(1).strip() if copy_match and copy_match.group(1).strip() != "없음" else ""
            return {"analysis": json.dumps({
                "canvas": {"aspect_ratio": 1, "background": "#ffffff"},
                "assets": [{"index": i, "x": .08, "y": .08, "width": .84, "height": .76,
                            "shape": "rounded" if changed else "ellipse", "fit": "contain",
                            "focal_x": .5, "focal_y": .42, "rotation": 0, "z": i} for i in range(count)],
                "decorations": [],
                "texts": ([{"content": copy, "x": .18, "y": .72, "width": .64, "height": .12,
                             "font_size": .055, "color": "#111111", "background": "#ffffffcc",
                             "align": "center", "padding": .015, "z": 10}] if copy else []),
                "rationale": "reference-driven plan"}, ensure_ascii=False)}
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


class MissingAssetOnceVision(FakeVision):
    def __init__(self):
        self.failed_once = False

    def analyze(self, paths, prompt, mode="general"):
        if '최상위 키는 canvas, assets' in prompt and not self.failed_once:
            self.failed_once = True
            return {"analysis": '{"canvas":{"aspect_ratio":1,"background":"#ffffff"},"assets":[],"decorations":[],"texts":[],"rationale":"invalid"}'}
        if "검증에 실패했습니다" in prompt:
            return {"analysis": '{"canvas":{"aspect_ratio":1,"background":"#ffffff"},"assets":[{"index":0,"x":0.1,"y":0.1,"width":0.8,"height":0.8,"shape":"rounded","fit":"contain","focal_x":0.5,"focal_y":0.5,"rotation":0,"z":0}],"decorations":[],"texts":[],"rationale":"repaired"}'}
        return super().analyze(paths, prompt, mode)


class ReviewDropsAssetVision(FakeVision):
    def analyze(self, paths, prompt, mode="general"):
        if "1차 설계도:" in prompt:
            return {"analysis": '{"canvas":{"aspect_ratio":1,"background":"#ddeeff"},"assets":[],"decorations":[],"texts":[],"rationale":"review dropped source"}'}
        return super().analyze(paths, prompt, mode)


class InvalidReviewVision(FakeVision):
    def analyze(self, paths, prompt, mode="general"):
        if "1차 설계도" in prompt or "품질 검토" in prompt:
            return {"analysis": "JSON이 아닌 검토 의견"}
        return super().analyze(paths, prompt, mode)


class AlwaysInvalidVision:
    def analyze(self, paths, prompt, mode="general"):
        return {"analysis": "장면을 설명한 일반 문장만 반환"}


def _image(path: Path, color, size=(400, 500), accent=(255, 255, 255)):
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((35, 35, size[0] - 35, size[1] - 35), outline=accent, width=12)
    image.save(path)
    return str(path)


def _circular_sticker(path: Path, background, portrait_color, size=500):
    image = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((10, 10, size - 10, size - 10), fill=background)
    draw.ellipse((110, 55, size - 110, size - 90), fill=portrait_color)
    draw.arc((35, 35, size - 35, size - 35), 0, 360, fill="white", width=7)
    draw.rectangle((55, 335, size - 55, 440), fill=(255, 255, 255))
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


def test_active_style_card_and_render_button_make_selection_explicit(tmp_path):
    app = QApplication.instance() or QApplication([])
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend())
    profile = runtime.learn_style([_image(tmp_path / "ref.png", (10, 20, 30))], name="선택 테스트")
    window = MockupWorkspaceWindow(get_specialist_workspace_registry().get("mockup"), runtime=runtime)
    assert window.active_profile_id == ""
    assert not window.render_button.isEnabled()
    window.profile_list.setCurrentRow(0)
    assert window.active_profile_id == profile.profile_id
    assert window.render_button.isEnabled()
    assert "선택 테스트" in window.active_profile_label.text()
    assert "참고 이미지 1장" in window.active_profile_meta.text()
    window.close(); assert app is not None


def test_missing_production_asset_in_ai_plan_is_repaired_automatically(tmp_path):
    vision = MissingAssetOnceVision()
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=vision, generation_backend=FakeGenerationBackend())
    profile = runtime.learn_style([_image(tmp_path / "ref.png", (10, 20, 30))])
    result = runtime.render(profile.profile_id, [_image(tmp_path / "product.png", (80, 90, 100))],
                            backend="local", output_dir=tmp_path / "out")
    assert vision.failed_once is True
    assert [item["index"] for item in result["scene_plan"]["assets"]] == [0]


def test_quality_review_cannot_delete_valid_production_assets(tmp_path):
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=ReviewDropsAssetVision(),
                                  generation_backend=FakeGenerationBackend())
    profile = runtime.learn_style([_image(tmp_path / "ref.png", (10, 20, 30))])
    result = runtime.render(profile.profile_id, [_image(tmp_path / "product.png", (90, 100, 110))],
                            backend="local", output_dir=tmp_path / "out")
    assert [item["index"] for item in result["scene_plan"]["assets"]] == [0]
    assert result["scene_plan"]["restored_required_elements"] == ["asset:0"]
    assert result["scene_plan"]["canvas"]["background"] == "#ddeeff"


def test_invalid_quality_review_keeps_valid_initial_plan(tmp_path):
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=InvalidReviewVision(),
                                  generation_backend=FakeGenerationBackend())
    profile = runtime.learn_style([_image(tmp_path / "ref.png", (10, 20, 30))])
    result = runtime.render(profile.profile_id, [_image(tmp_path / "product.png", (90, 100, 110))],
                            backend="local", output_dir=tmp_path / "out")
    assert result["scene_plan"]["assets"][0]["index"] == 0


def test_invalid_initial_json_uses_measured_evidence_fallback(tmp_path):
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=AlwaysInvalidVision(),
                                  generation_backend=FakeGenerationBackend())
    refs = [_circular_sticker(tmp_path / f"ref{i}.png", (130, 220, 210), (70, 80, 90))
            for i in range(4)]
    profile = runtime.learn_style(refs)
    result = runtime.render(profile.profile_id, [_image(tmp_path / "product.png", (90, 100, 110))],
                            visible_copy="응원합니다", backend="auto", output_dir=tmp_path / "out")
    assert result["scene_plan"]["initial_plan_fallback"] is True
    assert result["scene_plan"]["assets"][0]["shape"] == "ellipse"
    assert result["scene_plan"]["texts"][0]["content"] == "응원합니다"
    assert result["generation_backend"] == "model_planned_local"


def test_evidence_fallback_supports_multiple_production_assets_without_omission():
    plan = build_evidence_fallback_plan({}, asset_count=5)
    assert [asset["index"] for asset in plan["assets"]] == [0, 1, 2, 3, 4]


def test_explicit_visibility_and_center_requests_override_learned_defaults():
    plan = {"assets": [{"index": 0, "x": .11, "y": .06, "width": .78, "height": .78,
                        "fit": "cover", "focal_x": .3, "focal_y": .8}],
            "texts": [{"content": "테스트", "x": .2, "y": .72, "width": .5, "height": .18}]}
    result, applied = enforce_explicit_user_constraints(
        plan, "사진 속 인물의 얼굴이 모두 보이게 하고 문구는 스티커 중앙에 작성해줘"
    )
    assert result["assets"][0]["fit"] == "contain"
    assert (result["assets"][0]["focal_x"], result["assets"][0]["focal_y"]) == (.5, .5)
    assert (result["texts"][0]["x"], result["texts"][0]["y"]) == (.25, .36)
    assert result["texts"][0]["background"] == "transparent"
    assert applied


def test_visual_change_detection_ignores_internal_provenance_only():
    before = {"canvas": {"aspect_ratio": 1}, "assets": [], "texts": [],
              "enforced_user_constraints": ["old"]}
    after = {"canvas": {"aspect_ratio": 1}, "assets": [], "texts": [],
             "quality_review_fallback": "internal"}
    from core.mockup_scene import scene_changed
    assert scene_changed(before, after) is False


def test_explicit_text_size_and_color_constraints_are_applied():
    plan = {"assets": [], "texts": [{"content": "테스트", "x": .4, "y": .4, "width": .1,
                                      "height": .04, "font_size": .04, "color": "#111111"}]}
    result, applied = enforce_explicit_user_constraints(plan, "문구를 더 크게 하고 파란색으로 바꿔줘")
    assert result["texts"][0]["color"] == "#2878d0"
    assert result["texts"][0]["font_size"] > .04
    assert applied


def test_sequential_edit_preserves_previous_text_change_when_only_frame_is_targeted():
    previous = {
        "canvas": {"aspect_ratio": 1, "background": "#ffffff"},
        "assets": [{"index": 0, "x": .1, "y": .1, "width": .8, "height": .7,
                    "shape": "rounded", "fit": "contain", "focal_x": .5,
                    "focal_y": .5, "rotation": 0, "z": 1}],
        "decorations": [],
        "texts": [{"content": "테스트", "x": .2, "y": .3, "width": .6,
                   "height": .2, "font_size": .1, "color": "#111111",
                   "background": "transparent", "align": "center", "padding": 0, "z": 2}],
        "rationale": "previous",
    }
    candidate = deepcopy(previous)
    candidate["assets"][0]["shape"] = "ellipse"
    candidate["texts"][0]["font_size"] = .2
    command = "현재 양쪽이 잘려 원 형태가 아니야. 잘리지 않게 원형으로 만들어줘."
    merged, scopes = merge_scoped_scene_edit(previous, candidate, command)
    merged, applied = enforce_explicit_user_constraints(merged, command)
    assert scopes == {"assets"}
    assert merged["texts"][0]["font_size"] == .1
    assert merged["assets"][0]["shape"] == "ellipse"
    assert merged["assets"][0]["width"] == merged["assets"][0]["height"]
    assert merged["assets"][0]["fit"] == "cover"
    assert applied


def test_half_text_size_is_applied_without_model_replanning():
    plan = {"assets": [], "texts": [{"content": "테스트", "font_size": .2}]}
    result, applied = enforce_explicit_user_constraints(plan, "글씨 크기를 절반으로 줄여줘")
    assert result["texts"][0]["font_size"] == .1
    assert "texts[0].font_size" in applied


def test_compound_sticker_and_text_request_binds_directions_to_the_sticker():
    plan = {
        "assets": [{"x": .11, "y": .06, "width": .78, "height": .78,
                    "shape": "ellipse", "fit": "contain"}],
        "texts": [{"content": "정지원 테스트", "x": 0, "y": .2, "width": 1,
                   "height": .5, "font_size": .2, "font_weight": "bold",
                   "color": "#111111", "background": "transparent",
                   "align": "center", "padding": .1}],
    }
    command = ("글씨 크기가 너무 크고 스티커 오른쪽 왼쪽이 잘렸어. "
               "스티커가 원형이 되도록 변경해줘. 그리고 글씨가 스티커 안에 "
               "들어오도록 글씨크기 수정해줘. 글씨 색은 빨간색으로 바꿔줘.")
    result, applied = enforce_explicit_user_constraints(plan, command)
    text = result["texts"][0]
    frame = result["assets"][0]
    assert frame["shape"] == "ellipse"
    assert text["align"] == "center"
    assert text["font_size"] < .2
    assert text["color"] == "#e5484d"
    assert frame["x"] <= text["x"]
    assert text["x"] + text["width"] <= frame["x"] + frame["width"]
    assert "texts[0].x" in applied


def test_text_center_and_bold_are_persistent_scene_properties():
    plan = {
        "assets": [{"x": .11, "y": .06, "width": .78, "height": .78}],
        "texts": [{"content": "정지원 테스트", "x": .7, "y": .4, "width": .2,
                   "height": .1, "font_size": .04, "font_weight": "normal",
                   "color": "#e5484d", "background": "#ffffffcc",
                   "align": "right", "padding": .1}],
    }
    centered, _ = enforce_explicit_user_constraints(
        plan, "글씨를 흰색으로 바꿔줘. 글씨 위치가 스티커 중앙에 위치하게 수정해줘."
    )
    text = centered["texts"][0]
    assert text["align"] == "center"
    assert text["color"] == "#ffffff"
    assert text["background"] == "transparent"
    assert text["padding"] <= .025
    bold, fields = enforce_explicit_user_constraints(centered, "글씨 크기를 좀 더 키워주고 볼드체로 해줘.")
    assert bold["texts"][0]["font_weight"] == "bold"
    assert bold["texts"][0]["font_size"] > text["font_size"]
    assert "texts[0].font_weight" in fields


def test_scene_normalization_clamps_boxes_and_text_padding_to_canvas():
    raw = {
        "canvas": {"aspect_ratio": 1, "background": "#ffffff"},
        "assets": [{"index": 0, "x": .8, "y": .9, "width": .5, "height": .4,
                    "shape": "ellipse", "fit": "contain"}],
        "decorations": [],
        "texts": [{"content": "정지원", "x": .9, "y": .9, "width": .4,
                   "height": .2, "font_size": .06, "padding": .1}],
    }
    result = normalize_scene_plan(raw, asset_count=1, visible_copy="정지원")
    assert result["assets"][0]["x"] + result["assets"][0]["width"] <= 1
    assert result["assets"][0]["y"] + result["assets"][0]["height"] <= 1
    text = result["texts"][0]
    assert text["x"] + text["width"] <= 1
    assert text["y"] + text["height"] <= 1
    assert text["padding"] <= text["height"] * .2


def test_upper_body_instruction_uses_real_source_zoom_not_only_cover():
    plan = {
        "assets": [{"index": 0, "x": .11, "y": .06, "width": .78, "height": .78,
                    "shape": "ellipse", "fit": "contain", "zoom": 1,
                    "focal_x": .5, "focal_y": .5}],
        "texts": [],
    }
    result, fields = enforce_explicit_user_constraints(
        plan, "사진 속 인물이 상반신 위로만 스티커에 나오게 해줘."
    )
    asset = result["assets"][0]
    assert asset["fit"] == "cover"
    assert asset["zoom"] >= 1.5
    assert asset["focal_y"] < .5
    assert "assets[0].zoom" in fields


def test_textbox_removal_accepts_an_already_satisfied_text_color():
    before = {
        "assets": [{"x": .11, "y": .06, "width": .78, "height": .78}],
        "texts": [{"content": "정지원", "x": .15, "y": .66, "width": .7,
                   "height": .15, "font_size": .06, "color": "#2878d0",
                   "background": "#ffffffcc"}],
    }
    command = ("글씨를 하얀색으로 만들라는게 아니라 글씨 뒤 배경인 텍스트박스 "
               "색상을 없애달라는거야. 글씨는 파란색으로 해줘.")
    after, fields = enforce_explicit_user_constraints(before, command)
    assert after["texts"][0]["background"] == "transparent"
    assert after["texts"][0]["color"] == "#2878d0"
    assert fields == ["texts[0].background"]
    validate_patch_against_instruction(command, fields, before=before, after=after)


def test_patch_validation_rejects_partial_compound_edit_and_unrequested_group():
    import pytest
    command = "문구를 오른쪽 아래로 옮기고 파란색으로 바꿔줘"
    with pytest.raises(ScenePlanError, match="명령하지 않은 영역"):
        validate_patch_against_instruction(command, ["texts[0].color", "decorations.replace"])
    with pytest.raises(ScenePlanError, match="좌표 변경"):
        validate_patch_against_instruction(command, ["texts[0].color"])
    validate_patch_against_instruction(command, ["texts[0].x", "texts[0].y", "texts[0].color"])


def test_edit_patch_write_mask_keeps_requested_asset_change_and_drops_text_noise():
    patch = {
        "assets": [{"index": 0, "action": "update", "zoom": 1.4}],
        "texts": [{"index": 0, "action": "update", "font_size": .1}],
        "intent_summary": "사진을 확대",
        "success_criteria": ["사진이 더 크게 보임"],
    }
    filtered, removed = filter_scene_edit_patch(patch, "사진 속 인물을 더 크게 확대해줘")
    assert filtered["assets"][0]["zoom"] == 1.4
    assert "texts" not in filtered
    assert removed == ["texts"]


def test_generic_text_position_constraints_complete_compound_direction_request():
    plan = {"assets": [{"x": .1, "y": .1, "width": .8, "height": .8}],
            "texts": [{"content": "테스트", "x": .2, "y": .2, "width": .3,
                       "height": .1, "font_size": .06, "color": "#111111"}]}
    command = "문구를 오른쪽 아래로 옮기고 파란색으로 바꿔줘"
    result, fields = enforce_explicit_user_constraints(plan, command)
    assert result["texts"][0]["x"] >= .49
    assert result["texts"][0]["y"] > .5
    assert result["texts"][0]["align"] == "right"
    assert result["texts"][0]["color"] == "#2878d0"
    validate_patch_against_instruction(command, fields)


def test_generic_compound_text_edit_removes_background_and_applies_style():
    plan = {"assets": [{"x": .1, "y": .1, "width": .8, "height": .8}],
            "texts": [{"content": "테스트", "x": .2, "y": .2, "width": .3,
                       "height": .1, "font_size": .06, "color": "#111111",
                       "background": "#ffffffcc", "font_weight": "normal"}]}
    command = "문구를 오른쪽 아래에 배치하고 배경 없이 굵은 흰색 글씨로 바꿔줘."
    result, fields = enforce_explicit_user_constraints(plan, command)
    text = result["texts"][0]
    assert text["x"] >= .49 and text["y"] > .5
    assert text["background"] == "transparent"
    assert text["color"] == "#ffffff"
    assert text["font_weight"] == "bold"
    validate_patch_against_instruction(command, fields, before=plan, after=result)


def test_font_gui_instruction_maps_to_editable_scene_text_properties():
    plan = {"assets": [{"x": .1, "y": .1, "width": .8, "height": .8}],
            "texts": [{"content": "테스트", "x": .2, "y": .2, "width": .5,
                       "height": .1, "font_size": .04, "font_family": "Arial",
                       "font_weight": "normal", "color": "#111111", "background": "transparent"}]}
    result, fields = enforce_explicit_user_constraints(
        plan, "문구 글꼴을 'Malgun Gothic'로 바꾸고 글자 크기를 80픽셀, 굵기는 굵게로 설정해줘."
    )
    text = result["texts"][0]
    assert text["font_family"] == "Malgun Gothic"
    assert text["font_size"] == .05
    assert text["font_weight"] == "bold"
    assert {"texts[0].font_family", "texts[0].font_size", "texts[0].font_weight"}.issubset(fields)


def test_photo_content_edit_requires_zoom_and_preserves_circular_frame():
    command = "사진 속 인물을 더 크게 하고 사진을 프레임 안에서 왼쪽으로 옮겨줘."
    before = {"assets": [{"shape": "ellipse", "width": .8, "height": .8,
                          "zoom": 1, "focal_x": .5, "focal_y": .5}], "texts": []}
    invalid = {"assets": [{"shape": "ellipse", "width": .9, "height": .8,
                           "zoom": 1, "focal_x": .5, "focal_y": .5}], "texts": []}
    import pytest
    with pytest.raises(ScenePlanError):
        validate_patch_against_instruction(command, ["assets[0].width"],
                                           before=before, after=invalid)
    valid = {"assets": [{"shape": "ellipse", "width": .8, "height": .8,
                         "zoom": 1.3, "focal_x": .45, "focal_y": .5}], "texts": []}
    validate_patch_against_instruction(command,
                                       ["assets[0].zoom", "assets[0].focal_x"],
                                       before=before, after=valid)


def test_learned_subject_scale_repairs_oversized_asset():
    plan = {"canvas": {"aspect_ratio": 1}, "assets": [{"index": 0, "x": 0, "y": 0,
            "width": 1, "height": 1, "shape": "ellipse", "fit": "contain"}], "texts": []}
    features = {"references": [{"aspect_ratio": 1}] * 4,
                "consensus": {"confidence": 1, "subject_scale": .78, "primary_frame": "circle",
                              "evidence": {"reference_count": 4}}}
    result, enforced = enforce_measured_style_evidence(plan, features)
    assert (result["assets"][0]["x"], result["assets"][0]["width"]) == (.11, .78)
    assert "assets[0].learned_subject_occupancy" in enforced


def test_invalid_ai_edit_can_apply_only_explicit_safe_constraints(tmp_path):
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=AlwaysInvalidVision(),
                                  generation_backend=FakeGenerationBackend())
    refs = [_circular_sticker(tmp_path / f"ref{i}.png", (130, 220, 210), (70, 80, 90))
            for i in range(4)]
    profile = runtime.learn_style(refs)
    original = runtime.render(profile.profile_id, [_image(tmp_path / "person.png", (90, 100, 110))],
                              visible_copy="테스트", backend="auto", preview_only=True)
    original["scene_plan"]["assets"][0]["fit"] = "cover"
    edited = runtime.edit_preview(original, "얼굴과 머리가 모두 보이고 문구는 중앙에 오게 해줘")
    assert edited["scene_plan"]["assets"][0]["fit"] == "contain"
    assert edited["renderer"] == "structured-scene-patch-v4"


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
    result = runtime.render(profile.profile_id, products, output_dir=tmp_path / "out", backend="generative")
    assert Path(result["output"]).is_file()
    assert result["generation_backend"] == "generative"
    assert result["reference_pixels_sent_to_generator"] is True
    assert result["renderer"] == "ai-scene-plan-renderer-v3"


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
    assert result["generation_backend"] == "model_planned_local"
    assert result["generation_fallback_reason"] == ""
    assert result["reference_pixels_sent_to_generator"] is False
    import pytest
    with pytest.raises(RuntimeError, match="테스트 생성 실패"):
        runtime.render(profile.profile_id, products, output_dir=tmp_path / "out", backend="generative")


def test_repeated_reference_ratio_overrides_unsupported_ai_canvas_ratio():
    plan = {"canvas": {"aspect_ratio": 1.9, "background": "#ffffff"}, "assets": [],
            "decorations": [], "texts": [], "rationale": "wide guess"}
    features = {"references": [{"aspect_ratio": value} for value in (1.0, 1.002, .998, 1.0)]}
    repaired, enforced = enforce_measured_style_evidence(plan, features)
    assert repaired["canvas"]["aspect_ratio"] == 1.0
    assert enforced == ["canvas.aspect_ratio"]


def test_high_confidence_learned_composition_repairs_tiny_off_center_subject_and_text():
    plan = {"canvas": {"aspect_ratio": 1.9, "background": "#ffffff"},
            "assets": [{"index": 0, "x": .77, "y": .72, "width": .34, "height": .37,
                        "shape": "rounded", "fit": "cover"}],
            "decorations": [],
            "texts": [{"content": "테스트", "x": .5, "y": .47, "width": .31, "height": .28,
                       "font_size": .2}], "rationale": "weak plan"}
    features = {"references": [{"aspect_ratio": 1.0}] * 4,
                "consensus": {"confidence": 1.0, "subject_scale": .78, "primary_frame": "circle",
                              "text_region": "lower_overlay", "evidence": {"reference_count": 4}}}
    repaired, enforced = enforce_measured_style_evidence(plan, features)
    assert {key: repaired["assets"][0][key] for key in ("x", "y", "width", "height", "shape")} == {
        "x": .11, "y": .06, "width": .78, "height": .78, "shape": "ellipse"}
    assert repaired["texts"][0]["x"] == .345
    assert repaired["texts"][0]["y"] == .525
    assert repaired["texts"][0]["font_size"] == .2
    assert "assets[0].learned_subject_occupancy" in enforced


def test_inconsistent_reference_ratios_do_not_force_a_template():
    plan = {"canvas": {"aspect_ratio": 1.6, "background": "#ffffff"}, "assets": [],
            "decorations": [], "texts": [], "rationale": "mixed references"}
    features = {"references": [{"aspect_ratio": value} for value in (.75, 1.0, 1.5, 1.8)]}
    repaired, enforced = enforce_measured_style_evidence(plan, features)
    assert repaired["canvas"]["aspect_ratio"] == 1.6
    assert enforced == []


def test_reference_style_is_rendered_from_ai_scene_plan_not_named_template(tmp_path):
    refs = [
        _circular_sticker(tmp_path / f"ref{index}.png", color, (80 + index * 20, 70, 60))
        for index, color in enumerate(((130, 220, 210), (80, 100, 130), (220, 205, 190), (140, 235, 210)))
    ]
    product = _image(tmp_path / "person.png", (40, 80, 110), size=(700, 900), accent=(220, 180, 120))
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend(True))
    profile = runtime.learn_style(refs, name="원형 응원 스티커")
    assert profile.design_recipe["layout_family"] == "circular_sticker"
    assert profile.design_recipe["evidence"]["circle_votes"] >= 3

    result = runtime.render(
        profile.profile_id, [product], instruction="밝고 힘찬 분위기",
        visible_copy="배우님 화이팅!", output_dir=tmp_path / "out", backend="auto",
    )
    assert result["renderer"] == "ai-scene-plan-renderer-v3"
    assert result["visible_copy"] == "배우님 화이팅!"
    assert result["composition_plan"][0]["shape"] == "ellipse"
    assert result["scene_plan"]["texts"][0]["content"] == result["visible_copy"]
    with Image.open(result["output"]) as rendered:
        assert rendered.size == (1600, 1600)
        assert rendered.getpixel((0, 0)) == (255, 255, 255)
        assert rendered.getpixel((800, 300)) != (255, 255, 255)


def test_circular_sticker_does_not_render_instruction_as_copy(tmp_path):
    refs = [_circular_sticker(tmp_path / f"ref{index}.png", (130, 220, 210), (70, 80, 90))
            for index in range(3)]
    product = _image(tmp_path / "person.png", (40, 80, 110), size=(700, 900))
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend())
    profile = runtime.learn_style(refs)
    result = runtime.render(profile.profile_id, [product], instruction="더 발랄한 색감으로 만들어줘",
                            output_dir=tmp_path / "out", backend="local")
    assert result["visible_copy"] == ""
    assert result["scene_plan"]["texts"] == []


def test_ai_edits_revise_scene_plan_and_rerender_from_original_sources(tmp_path):
    refs = [_circular_sticker(tmp_path / f"ref{index}.png", (130, 220, 210), (70, 80, 90))
            for index in range(3)]
    product = _image(tmp_path / "person.png", (40, 80, 110), size=(700, 900))
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(),
                                  generation_backend=FailingGenerationBackend(ready=True))
    profile = runtime.learn_style(refs)
    original = runtime.render(profile.profile_id, [product], visible_copy="응원합니다!",
                              output_dir=tmp_path / "out", backend="auto", preview_only=True)
    first = runtime.edit_preview(original, "문구를 더 크게 하고 파란색으로 바꿔줘")
    assert first["renderer"] == "structured-scene-patch-v4"
    assert first["production_inputs"] == original["production_inputs"]
    assert first["production_sources"] == original["production_sources"]
    assert first["scene_plan"] != original["scene_plan"]
    assert first["revision"] == 1
    second = runtime.edit_preview(first, "조금 더 정돈된 느낌으로 수정해줘")
    assert second["revision"] == 2
    assert second["renderer"] == "ai-scene-patch-v5"
    assert second["scene_plan"]["texts"] == first["scene_plan"]["texts"]
    assert second["scene_plan"]["canvas"]["background"] == "#eef2f5"


def test_repeated_explicit_edit_is_successful_idempotent_operation(tmp_path):
    refs = [_circular_sticker(tmp_path / f"ref{index}.png", (130, 220, 210), (70, 80, 90))
            for index in range(3)]
    product = _image(tmp_path / "person.png", (40, 80, 110), size=(700, 900))
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(),
                                  generation_backend=FailingGenerationBackend(ready=True))
    profile = runtime.learn_style(refs)
    original = runtime.render(profile.profile_id, [product], visible_copy="응원합니다!",
                              output_dir=tmp_path / "out", backend="auto", preview_only=True)
    first = runtime.edit_preview(original, "글꼴을 '궁서'로 바꿔줘")
    repeated = runtime.edit_preview(first, "글꼴을 '궁서'로 바꿔줘")
    assert repeated["already_satisfied"] is True
    assert repeated["renderer"] == "verified-idempotent-edit-v1"
    assert repeated["output"] == first["output"]
    assert repeated["revision"] == first["revision"]


def test_white_dashed_inner_border_is_added_and_rendered(tmp_path):
    refs = [_circular_sticker(tmp_path / f"ref{index}.png", (130, 220, 210), (70, 80, 90))
            for index in range(3)]
    product = _image(tmp_path / "person.png", (40, 80, 110), size=(700, 900))
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(),
                                  generation_backend=FailingGenerationBackend(ready=True))
    profile = runtime.learn_style(refs)
    original = runtime.render(profile.profile_id, [product], output_dir=tmp_path / "out",
                              backend="auto", preview_only=True)
    edited = runtime.edit_preview(original, "하얀 점선으로 원형 테두리 안쪽에 그려줘")
    decoration = edited["scene_plan"]["decorations"][0]
    asset = edited["scene_plan"]["assets"][0]
    assert decoration["type"] == "ellipse"
    assert decoration["stroke"] == "#ffffff"
    assert decoration["dash"] is True
    assert decoration["x"] > asset["x"]
    assert decoration["width"] < asset["width"]
    with Image.open(edited["output"]) as rendered:
        assert rendered.size == (1600, 1600)


def test_exact_pixel_font_size_rejects_model_value_with_wrong_canvas_scale():
    before = {"assets": [], "decorations": [], "texts": [{"font_size": .05}]}
    after = {"assets": [], "decorations": [], "texts": [{"font_size": .2}]}
    with pytest.raises(ScenePlanError, match="실제 캔버스 기준"):
        validate_patch_against_instruction(
            "글자 크기를 200픽셀로 바꿔줘", ["texts[0].font_size"], before=before, after=after,
        )


def test_learned_lower_overlay_centers_copy_inside_primary_frame():
    plan = {
        "assets": [{"x": .1, "y": .05, "width": .8, "height": .8, "shape": "ellipse"}],
        "decorations": [],
        "texts": [{"x": .5, "y": .5, "width": .4, "height": .1, "align": "left"}],
    }
    enforced, fields = enforce_measured_style_evidence(plan, {
        "references": [{"aspect_ratio": 1.0}] * 4,
        "consensus": {"primary_frame": "circle", "subject_scale": .8,
                      "text_region": "lower_overlay", "confidence": 1.0,
                      "evidence": {"reference_count": 4}},
    })
    assert enforced["texts"][0]["x"] == pytest.approx(.3)
    assert enforced["texts"][0]["y"] == pytest.approx(.715)
    assert enforced["texts"][0]["align"] == "center"
    assert "texts[0].learned_lower_overlay" in fields


def test_save_uses_current_history_metadata_instead_of_stale_global_metadata(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    spec = get_specialist_workspace_registry().get("mockup")
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(),
                                  generation_backend=FakeGenerationBackend())
    window = MockupWorkspaceWindow(spec, runtime=runtime)
    preview_root = Path(__import__("tempfile").gettempdir()) / "jarvis_mockup_previews"
    preview_root.mkdir(parents=True, exist_ok=True)
    source = preview_root / "history-current.png"
    Image.new("RGB", (20, 20), "white").save(source)
    current = {"output": str(source), "edit_instruction": "마지막 명령", "scene_plan": {"texts": []}}
    window.preview_history = [current]; window.preview_index = 0
    window.preview_metadata = {"output": str(source), "edit_instruction": "이전 명령"}
    target = tmp_path / "saved.png"
    monkeypatch.setattr("ui.specialist_workspaces.QFileDialog.getSaveFileName",
                        lambda *_args, **_kwargs: (str(target), "PNG"))
    window._save_preview()
    saved = __import__("json").loads(target.with_suffix(".json").read_text(encoding="utf-8"))
    assert saved["edit_instruction"] == "마지막 명령"


def test_live_adjustment_is_repeatable_from_stable_base(tmp_path):
    source = Path(_image(tmp_path / "base.png", (80, 100, 120), size=(500, 500)))
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend())
    values = {"brightness": 1.4, "contrast": .8, "saturation": 1.2, "sharpness": 1.0}
    first = runtime.adjust_preview(source, values)
    second = runtime.adjust_preview(source, values)
    assert first["adjustment_base"] == second["adjustment_base"] == str(source.resolve())
    assert hashlib.sha256(Path(first["output"]).read_bytes()).digest() == hashlib.sha256(
        Path(second["output"]).read_bytes()).digest()


def test_adjustment_sliders_use_percent_range_and_keep_selected_value(tmp_path):
    app = QApplication.instance() or QApplication([])
    spec = get_specialist_workspace_registry().get("mockup")
    window = MockupWorkspaceWindow(spec, runtime=MockupDesignRuntime(
        tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend()))
    assert set(window.adjustment_sliders) == {"brightness", "contrast", "saturation", "sharpness"}
    for slider in window.adjustment_sliders.values():
        assert (slider.minimum(), slider.maximum(), slider.value()) == (0, 100, 50)
    window.adjustment_sliders["brightness"].setValue(73)
    assert window.adjustment_sliders["brightness"].value() == 73
    assert window.adjustment_labels["brightness"].text() == "73%"
    window.close(); assert app is not None


def test_slider_movement_applies_preview_without_button_or_reset(tmp_path):
    app = QApplication.instance() or QApplication([])
    spec = get_specialist_workspace_registry().get("mockup")
    runtime = MockupDesignRuntime(tmp_path / "styles", vision=FakeVision(), generation_backend=FakeGenerationBackend())
    window = MockupWorkspaceWindow(spec, runtime=runtime)
    source = _image(tmp_path / "preview.png", (80, 100, 120), size=(500, 500))
    window._push_preview({"output": source, "preview_only": True, "renderer": "test",
                          "width": 500, "height": 500, "generation_fallback_reason": ""})
    window.adjustment_sliders["contrast"].setValue(72)
    deadline = time.monotonic() + 3
    while window.preview_metadata.get("renderer") != "pillow-live-adjustment-v2" and time.monotonic() < deadline:
        app.processEvents(); time.sleep(.02)
    assert window.preview_metadata["renderer"] == "pillow-live-adjustment-v2"
    assert window.preview_metadata["adjustments"]["contrast"] == 1.44
    assert window.adjustment_sliders["contrast"].value() == 72
    window.close()
