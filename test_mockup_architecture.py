from pathlib import Path
import json
import sqlite3
import time

from PIL import Image

from core.mockup_design import MockupDesignRuntime
from core.mockup_layer_graph import (layer_graph_to_svg, render_svg_with_qt,
                                      scene_plan_to_layer_graph, validate_layer_graph)
from core.mockup_style_index import VisualStyleIndex
from core.mockup_subject_runtime import SubjectAnalysisRuntime
from core.mockup_document import assert_scene_document, scene_digest, stamp_scene_document
from core.mockup_pipeline_policy import route_mockup_request
from core.mockup_scene import (enforce_exact_user_copy, enforce_explicit_user_constraints,
                               filter_scene_edit_patch, infer_edit_scopes,
                               parse_explicit_colored_copy, requests_circular_shape,
                               validate_patch_against_instruction)
from core.specialist_team import SpecialistTeamRuntime, TeamRun


def _plan():
    return {
        "canvas": {"aspect_ratio": 1.0, "background": "#ffffff"},
        "assets": [{"index": 0, "x": .1, "y": .1, "width": .8, "height": .8,
                    "shape": "ellipse", "fit": "cover", "zoom": 1, "focal_x": .5,
                    "focal_y": .5, "rotation": 0, "z": 0}],
        "decorations": [{"type": "ellipse", "x": .13, "y": .13, "width": .74,
                         "height": .74, "fill": "transparent", "stroke": "#ffffff",
                         "stroke_width": .01, "dash": True, "dash_length": .03,
                         "gap_length": .02, "z": 2}],
        "texts": [{"content": "테스트", "x": .2, "y": .7, "width": .6, "height": .15,
                   "font_size": .08, "font_family": "Malgun Gothic", "font_weight": "bold",
                   "color": "#111111", "background": "transparent", "align": "center",
                   "padding": .01, "z": 3, "letter_spacing": .01, "line_height": 1.2,
                   "stroke": "#ffffff", "stroke_width": .002}],
    }


def test_layer_graph_svg_is_editable_and_qt_renderable(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (200, 240), "#7d96b3").save(source)
    graph = scene_plan_to_layer_graph(_plan())
    validate_layer_graph(graph, asset_count=1)
    svg = layer_graph_to_svg(graph, [source], 600, 600)
    assert "stroke-dasharray" in svg
    assert "letter-spacing" in svg
    rendered = render_svg_with_qt(svg, 600, 600)
    assert rendered.size == (600, 600)


def test_svg_arc_text_uses_real_text_path(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (200, 240), "#7d96b3").save(source)
    plan = _plan()
    plan["texts"][0]["path"] = {
        "type": "arc", "radius": .3, "start_angle": 200, "end_angle": 340,
    }
    svg = layer_graph_to_svg(scene_plan_to_layer_graph(plan), [source], 600, 600)
    assert "<textPath" in svg
    assert 'id="text-path-text-0"' in svg
    assert render_svg_with_qt(svg, 600, 600).size == (600, 600)


def test_design_document_detects_stale_or_mutated_preview():
    stamped = stamp_scene_document(_plan(), revision=4)
    assert stamped["document"]["revision"] == 4
    digest = scene_digest(stamped)
    assert assert_scene_document(stamped, expected_digest=digest)["scene_digest"] == digest
    stale_digest = "0" * 64
    try:
        assert_scene_document(stamped, expected_digest=stale_digest)
    except ValueError as exc:
        assert "현재 미리보기" in str(exc)
    else:
        raise AssertionError("mutated design document was accepted")


def test_capability_router_activates_only_required_heavy_stage():
    vector = route_mockup_request("문구를 아래로 내리고 점선을 흰색으로 바꿔줘")
    assert vector.image_generation == "disabled"
    assert vector.subject_processing == "none"
    segmented = route_mockup_request("인물 누끼를 따고 배경을 투명하게 해줘", segmentation_ready=True)
    assert segmented.subject_processing == "birefnet-or-grabcut"
    generated = route_mockup_request("새로운 야경 배경을 생성해줘")
    assert generated.image_generation == "diffusion"


def test_render_contract_checks_actual_circle_alpha():
    plan = _plan()
    opaque = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
    violations = MockupDesignRuntime._render_contract_violations(
        opaque, plan, "스티커를 원형으로 만들어줘",
    )
    assert any("모서리" in item for item in violations)


def test_visual_style_index_does_not_use_text_embedding(tmp_path):
    db = VisualStyleIndex(tmp_path / "styles.db", tmp_path / "missing-clip")
    blue = tmp_path / "blue.png"; red = tmp_path / "red.png"
    Image.new("RGB", (100, 100), "blue").save(blue)
    Image.new("RGB", (100, 100), "red").save(red)
    db.add("blue-profile", blue)
    db.add("red-profile", red)
    matches = db.search(blue)
    assert matches[0]["profile_id"] == "blue-profile"
    assert matches[0]["backend"] == "visual-descriptor-v1"


def test_visual_style_index_reads_legacy_nested_clip_vectors(tmp_path, monkeypatch):
    db_path = tmp_path / "styles.db"
    index = VisualStyleIndex(db_path, tmp_path / "missing-clip")
    source = tmp_path / "source.png"
    Image.new("RGB", (32, 32), "blue").save(source)
    monkeypatch.setattr(index, "embed", lambda _path: ([.6, .8], "clip-vit-base-patch32"))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO visual_styles VALUES(?,?,?,?,?,?,?,?,?)",
            ("legacy", "profile", str(source), "reference", "clip-vit-base-patch32",
             json.dumps([[[.6, .8]]]), "{}", 0, time.time()),
        )
    matches = index.search(source)
    assert matches[0]["item_id"] == "legacy"
    assert matches[0]["score"] == 1.0


def test_specialist_team_enforces_artifact_contract_and_records_events():
    team = SpecialistTeamRuntime()
    run = TeamRun("mockup", "시안 제작", {
        "references": ["ref.png"], "production_assets": ["source.png"],
        "style_evidence": {}, "subject_evidence": {}, "layer_graph": {},
        "rendered_image": "preview.png", "quality_verdict": {},
    })
    value = team.execute_role(run, "design_director", lambda state: {"version": 1})
    assert value == {"version": 1}
    assert run.events[-1]["role"] == "design_director"
    assert run.events[-1]["status"] == "completed"


def test_subject_analysis_exposes_safe_regions(tmp_path):
    source = tmp_path / "subject.png"
    Image.new("RGB", (320, 480), "gray").save(source)
    result = SubjectAnalysisRuntime(tmp_path / "missing").analyze(source)
    assert result["width"] == 320
    assert len(result["safe_text_regions"]) == 4
    assert result["segmentation"]["backend"] == "opencv-grabcut"


def test_exact_multicolor_copy_preserves_user_spelling():
    plan = _plan()
    copy, spans = parse_explicit_colored_copy(
        "스티커는 원형으로 제작해줘. 문구는 정지언은 빨간색, 테스트는 파란색으로 해줘."
    )
    assert copy == "정지언 테스트"
    assert spans == [
        {"content": "정지언", "color": "#e5484d"},
        {"content": "테스트", "color": "#2878d0"},
    ]
    revised, revised_copy, fields = enforce_exact_user_copy(plan, "정지언은 빨간색, 테스트는 파란색", "")
    assert revised_copy == "정지언 테스트"
    assert revised["texts"][0]["spans"] == spans
    assert "texts[0].spans" in fields


def test_svg_multicolor_text_is_clipped_and_auto_fitted(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (400, 700), "#777777").save(source)
    plan = _plan()
    plan["canvas"]["background"] = "transparent"
    plan["texts"][0].update({
        "content": "정지언 테스트", "font_size": .2, "width": .2,
        "spans": [{"content": "정지언", "color": "#e5484d"},
                  {"content": "테스트", "color": "#2878d0"}],
    })
    svg = layer_graph_to_svg(scene_plan_to_layer_graph(plan), [source], 600, 600)
    assert 'id="text-0-span-0"' in svg and 'fill="#e5484d"' in svg and '>정지언</text>' in svg
    assert 'id="text-0-span-1"' in svg and 'fill="#2878d0"' in svg and '> 테스트</text>' in svg
    assert 'clip-path="url(#text-clip-text-0)"' in svg
    rendered = render_svg_with_qt(svg, 600, 600)
    assert rendered.mode == "RGBA"
    assert rendered.getpixel((0, 0))[3] == 0


def test_phrase_specific_font_and_true_circular_sticker_contract(tmp_path):
    copy, spans = parse_explicit_colored_copy(
        "'정지원'은 빨간색 맑은 고딕, '테스트'는 파란색 궁서체로 해줘."
    )
    assert copy == "정지원 테스트"
    assert spans[0]["font_family"] == "맑은 고딕"
    assert spans[1]["font_family"] == "궁서"
    plan, fields = enforce_explicit_user_constraints(_plan(), "스티커를 원형으로 만들어줘")
    assert plan["canvas"]["background"] == "transparent"
    assert plan["assets"][0]["shape"] == "ellipse"
    assert "canvas.background" in fields

    source = tmp_path / "portrait.png"
    Image.new("RGB", (400, 800), "gray").save(source)
    plan["assets"][0].update({"zoom": 3, "focal_x": .5, "focal_y": .2})
    plan["texts"][0].update({"content": copy, "spans": spans})
    svg = layer_graph_to_svg(scene_plan_to_layer_graph(plan), [source], 600, 600)
    assert 'font-family="Malgun Gothic"' in svg
    assert 'font-family="Gungsuh"' in svg
    assert 'href="data:image/png;base64,' in svg  # zoom/focal crop is embedded for Qt
    rendered = render_svg_with_qt(svg, 600, 600)
    assert rendered.getpixel((300, 250))[3] > 0
    assert rendered.getpixel((0, 0))[3] == 0


def test_visual_review_contract_uses_scene_facts_not_subjective_gaze():
    plan, _ = enforce_explicit_user_constraints(
        _plan(),
        "스티커를 원형으로 만들어줘. 문구는 스티커 안쪽 하단에 위치해줘. "
        "'정지원'은 빨간색 고딕체, 'ㅈㅈㅈ'은 파란색 궁서체로 해줘.",
    )
    plan["assets"][0]["zoom"] = 3
    plan["texts"][0].update({
        "content": "정지원 ㅈㅈㅈ", "x": .2, "y": .68, "width": .6, "height": .08,
        "spans": [
            {"content": "정지원", "color": "#e5484d", "font_family": "Malgun Gothic"},
            {"content": "ㅈㅈㅈ", "color": "#2878d0", "font_family": "Gungsuh"},
        ],
    })
    instruction = ("스티커를 원형으로 만들어줘. 문구는 스티커 안쪽 하단에 위치해줘. "
                   "'정지원'은 빨간색 고딕체, 'ㅈㅈㅈ'은 파란색 궁서체로 해줘.")
    assert MockupDesignRuntime._scene_contract_violations(
        plan, instruction, "정지원 ㅈㅈㅈ"
    ) == []

    plan["texts"][0]["spans"][1]["color"] = "#111111"
    assert "'ㅈㅈㅈ' 문구 색상이 요청과 다릅니다." in (
        MockupDesignRuntime._scene_contract_violations(plan, instruction, "정지원 ㅈㅈㅈ")
    )


def test_circular_sticker_edit_allows_required_canvas_alpha_change():
    instruction = "스티커를 원 형태로 만들어줘"
    assert infer_edit_scopes(instruction) == {"assets", "canvas"}
    filtered, removed = filter_scene_edit_patch(
        {"canvas": {"background": "transparent"},
         "assets": [{"index": 0, "shape": "ellipse"}]},
        instruction,
    )
    assert removed == []
    assert filtered["canvas"]["background"] == "transparent"

    before = _plan()
    after, fields = enforce_explicit_user_constraints(before, instruction)
    validate_patch_against_instruction(instruction, fields, before, after)
    assert after["canvas"]["background"] == "transparent"


def test_circular_sticker_constraint_does_not_move_existing_text():
    before = _plan()
    before["texts"][0]["x"] = 0.21
    before["texts"][0]["y"] = 0.45

    after, fields = enforce_explicit_user_constraints(
        before, "스티커를 원 형태로 만들어줘",
    )

    assert after["texts"] == before["texts"]
    assert "canvas.background" in fields
    assert "assets[0].shape" in fields


def test_plain_korean_circle_wording_creates_a_true_round_sticker(tmp_path):
    instruction = "스티커는 원으로 만들어줘."
    assert requests_circular_shape(instruction)
    assert infer_edit_scopes(instruction) == {"assets", "canvas"}

    plan, fields = enforce_explicit_user_constraints(_plan(), instruction)
    assert plan["canvas"]["background"] == "transparent"
    assert plan["assets"][0]["shape"] == "ellipse"
    assert plan["assets"][0]["fit"] == "cover"
    assert "assets[0].fit" in fields

    source = tmp_path / "portrait.png"
    Image.new("RGB", (500, 800), "gray").save(source)
    rendered = render_svg_with_qt(
        layer_graph_to_svg(scene_plan_to_layer_graph(plan), [source], 600, 600),
        600, 600,
    )
    # The corner of the asset frame must be transparent; checking only the
    # outer canvas would miss QtSvg silently ignoring a raster clipPath.
    assert rendered.getpixel((65, 65))[3] == 0
    assert rendered.getpixel((300, 65))[3] > 0
