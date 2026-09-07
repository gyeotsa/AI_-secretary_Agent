"""Pixel-level contract for declared photo transforms in the SVG renderer.

The fixtures are synthetic; no application windows, models, source photos, or
workspace data are read or changed. Qt runs offscreen through the public renderer.
"""
from copy import deepcopy
import base64
import hashlib
import io
from pathlib import Path
import re

from PIL import Image, ImageChops, ImageDraw
import pytest

from core.mockup_design import MockupDesignRuntime
from core.mockup_layer_graph import (
    layer_graph_to_svg,
    render_svg_with_qt,
    scene_plan_to_layer_graph,
)


def _plan(**transform):
    asset = {
        "index": 0, "x": .1, "y": .1, "width": .8, "height": .8,
        "shape": "rectangle", "fit": "cover", "zoom": 1,
        "focal_x": .5, "focal_y": .5, "rotation": 0, "z": 0,
    }
    asset.update(transform)
    return {
        "canvas": {"aspect_ratio": 1.0, "background": "transparent"},
        "assets": [asset], "texts": [], "decorations": [],
    }


def _source(path: Path, size=(120, 120), *, alpha=255):
    """Four asymmetric color regions make angle and direction observable."""
    image = Image.new("RGBA", size, (220, 30, 40, alpha))
    painter = ImageDraw.Draw(image)
    width, height = size
    painter.rectangle((width // 2, 0, width - 1, height // 2 - 1),
                      fill=(30, 70, 230, alpha))
    painter.rectangle((0, height // 2, width // 2 - 1, height - 1),
                      fill=(25, 200, 60, alpha))
    painter.rectangle((width // 2, height // 2, width - 1, height - 1),
                      fill=(245, 205, 20, alpha))
    image.save(path)
    return path


def _render(plan, path, *, size=900):
    graph = scene_plan_to_layer_graph(plan)
    svg = layer_graph_to_svg(graph, [path], size, size)
    return render_svg_with_qt(svg, size, size), svg


def _pillow(plan, path, *, size=900):
    # Bypass persistent runtime setup: this method is a pure local renderer.
    runtime = object.__new__(MockupDesignRuntime)
    return runtime._render_scene_plan(plan, [path], long_edge=size)


def _assert_same_pixels(actual, expected, *, tolerance=2):
    assert actual.size == expected.size
    alpha_difference = ImageChops.difference(actual.getchannel("A"), expected.getchannel("A"))
    assert alpha_difference.getextrema()[1] <= tolerance
    # Hidden RGB is irrelevant, but both black/white composites expose actual
    # opacity or color errors. Qt's premultiplied conversion may round by 1.
    for color in ("black", "white"):
        background = Image.new("RGBA", actual.size, color)
        left = Image.alpha_composite(background, actual).convert("RGB")
        right = Image.alpha_composite(background, expected).convert("RGB")
        extrema = ImageChops.difference(left, right).getextrema()
        assert max(high for _, high in extrema) <= tolerance


@pytest.mark.parametrize("angle", [-30, 30])
@pytest.mark.parametrize("shape", ["rectangle", "ellipse", "rounded"])
@pytest.mark.parametrize("fit", ["cover", "contain"])
def test_qt_rotation_matches_fixed_frame_pillow_contract(tmp_path, angle, shape, fit):
    source = _source(tmp_path / "quadrants.png", (160, 120))
    plan = _plan(rotation=angle, shape=shape, fit=fit)
    rendered, _ = _render(plan, source)
    expected = _pillow(plan, source)
    baseline, _ = _render(_plan(shape=shape, fit=fit), source)

    assert ImageChops.difference(rendered.convert("RGB"), baseline.convert("RGB")).getbbox(), (
        "declaring photo rotation must change the rendered photo, not metadata only"
    )
    _assert_same_pixels(rendered, expected)
    # Rotation acts inside the declared frame; it must not move/expand its box.
    alpha_box = rendered.getchannel("A").getbbox()
    assert alpha_box is not None
    assert 90 <= alpha_box[0] < alpha_box[2] <= 810
    assert 90 <= alpha_box[1] < alpha_box[3] <= 810


@pytest.mark.parametrize("angle", [-30, 30])
def test_rotation_preserves_source_alpha_and_does_not_rotate_the_frame(tmp_path, angle):
    source = _source(tmp_path / "translucent.png", alpha=128)
    plan = _plan(rotation=angle, shape="ellipse", remove_background=True)
    rendered, _ = _render(plan, source)
    expected = _pillow(plan, source)

    _assert_same_pixels(rendered, expected)
    assert 127 <= rendered.getpixel((450, 450))[3] <= 128
    assert rendered.getpixel((90, 90))[3] == 0
    assert rendered.getpixel((0, 450))[3] == 0


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("endpoint", [0, 1])
@pytest.mark.parametrize("zoom", [1, 2])
def test_zero_and_one_focal_coordinates_select_source_edges(tmp_path, axis, endpoint, zoom):
    size = (360, 120) if axis == "x" else (120, 360)
    source = _source(tmp_path / "edge.png", size)
    plan = _plan(zoom=zoom, **{f"focal_{axis}": endpoint})
    rendered, _ = _render(plan, source)

    _assert_same_pixels(rendered, _pillow(plan, source))
    midpoint, _ = _render(_plan(zoom=zoom), source)
    assert ImageChops.difference(rendered.convert("RGB"), midpoint.convert("RGB")).getbbox()


@pytest.mark.parametrize("shape", ["rectangle", "ellipse", "rounded"])
@pytest.mark.parametrize("size", [(40, 80), (80, 40), (1600, 800)])
def test_contain_fits_small_and_large_sources_without_mask_dependent_scaling(tmp_path, shape, size):
    source = _source(tmp_path / "contain.png", size)
    plan = _plan(fit="contain", shape=shape)
    rendered, _ = _render(plan, source)
    _assert_same_pixels(rendered, _pillow(plan, source))


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("endpoint", [0, 1])
def test_contain_honors_zoom_and_focal_before_fitting(tmp_path, axis, endpoint):
    source = _source(tmp_path / "contain-zoom.png", (160, 120))
    plan = _plan(fit="contain", zoom=2, **{f"focal_{axis}": endpoint})
    rendered, _ = _render(plan, source)
    _assert_same_pixels(rendered, _pillow(plan, source))


@pytest.mark.parametrize("focal", [.2, .8])
@pytest.mark.parametrize("zoom", [1, 1.5, 3])
@pytest.mark.parametrize("fit", ["cover", "contain"])
def test_combined_transform_has_one_crop_coordinate_contract(tmp_path, focal, zoom, fit):
    source = _source(tmp_path / "combined.png", (200, 120))
    plan = _plan(focal_x=focal, focal_y=1 - focal, zoom=zoom, rotation=-15,
                 fit=fit, width=.6, height=.4, x=.2, y=.3, shape="rounded")
    rendered, _ = _render(plan, source)
    _assert_same_pixels(rendered, _pillow(plan, source))


def test_positive_rotation_is_counterclockwise_in_image_coordinates(tmp_path):
    source = _source(tmp_path / "direction.png")
    rendered, _ = _render(_plan(rotation=30), source)
    # A point from the top-right blue quadrant rotates across the old center
    # line into the top-left quadrant. SVG's native rotate() has the opposite
    # sign, so a metadata-only or clockwise implementation cannot pass this.
    red, green, blue, alpha = rendered.getpixel((418, 178))
    assert blue > 200 and red < 60 and green < 100 and alpha == 255
    baseline, _ = _render(_plan(rotation=0), source)
    assert baseline.getpixel((418, 178)) == (220, 30, 40, 255)


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_null_transform_values_use_defaults(tmp_path, missing):
    source = _source(tmp_path / "defaults.png", (160, 120))
    graph = scene_plan_to_layer_graph(_plan())
    for key in ("zoom", "rotation", "focal_x", "focal_y"):
        if missing:
            graph["layers"][0]["transform"].pop(key)
        else:
            graph["layers"][0]["transform"][key] = None
    rendered = render_svg_with_qt(layer_graph_to_svg(graph, [source], 900, 900), 900, 900)
    expected, _ = _render(_plan(), source)
    _assert_same_pixels(rendered, expected)


@pytest.mark.parametrize("key", ["zoom", "rotation", "focal_x", "focal_y"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), [], {"value": 30}])
def test_malformed_photo_transforms_fail_instead_of_ignoring_declared_edits(tmp_path, key, value):
    source = _source(tmp_path / "invalid.png")
    plan = _plan(**{key: value})
    with pytest.raises(ValueError, match=key):
        layer_graph_to_svg(scene_plan_to_layer_graph(plan), [source], 900, 900)


@pytest.mark.parametrize("fit", ["cover", "contain"])
def test_rotation_and_focus_do_not_mutate_source_plan_or_graph(tmp_path, fit):
    source = _source(tmp_path / "immutable.png", (240, 120))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    plan = _plan(fit=fit, zoom=2, focal_x=0, rotation=30, shape="ellipse")
    plan_before = deepcopy(plan)
    graph = scene_plan_to_layer_graph(plan)
    graph_before = deepcopy(graph)

    svg = layer_graph_to_svg(graph, [source], 900, 900)
    assert graph == graph_before
    assert plan == plan_before
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert graph["layers"][0]["transform"]["rotation"] == 30
    assert graph["layers"][0]["transform"]["focal_x"] == 0
    assert "file:" not in svg
    assert str(source) not in svg

    match = re.search(r'data:image/png;base64,([^"\s]+)', svg)
    assert match is not None
    with Image.open(io.BytesIO(base64.b64decode(match.group(1)))) as embedded:
        assert embedded.size == (720, 720)
        assert embedded.getpixel((0, 0))[3] == 0
    # Saved masters stay usable after temporary prepared source files disappear.
    expected = render_svg_with_qt(svg, 900, 900)
    source.unlink()
    _assert_same_pixels(render_svg_with_qt(svg, 900, 900), expected)


def test_masked_subject_coordinates_are_not_tightly_recropped(tmp_path):
    source = tmp_path / "cutout.png"
    image = Image.new("RGBA", (160, 120), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((15, 10, 60, 80), fill=(220, 30, 40, 128))
    image.save(source)
    plan = _plan(rotation=30, fit="contain", remove_background=True)
    rendered, _ = _render(plan, source)
    _assert_same_pixels(rendered, _pillow(plan, source))
