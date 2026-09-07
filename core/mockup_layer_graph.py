"""Professional, versioned layer graph used between design planning and rendering."""
from __future__ import annotations

from copy import deepcopy
import base64
import html
import io
import math
import os
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps


LAYER_GRAPH_VERSION = 2

_SVG_FONT_ALIASES = {
    "맑은고딕": "Malgun Gothic", "맑은 고딕": "Malgun Gothic",
    "궁서": "Gungsuh", "궁서체": "Gungsuh",
    "굴림": "Gulim", "굴림체": "GulimChe",
    "돋움": "Dotum", "돋움체": "DotumChe",
    "바탕": "Batang", "바탕체": "BatangChe",
}


def _svg_font_family(value) -> str:
    family = " ".join(str(value or "Malgun Gothic").split())
    return _SVG_FONT_ALIASES.get(family, family)


def scene_plan_to_layer_graph(plan: dict) -> dict:
    """Convert the compatibility scene plan to an extensible layer graph."""
    plan = deepcopy(plan)
    layers = []
    for asset in plan.get("assets", []):
        layers.append({
            "id": f"asset-{asset['index']}", "kind": "raster", "name": f"제작 이미지 {asset['index'] + 1}",
            "z": asset.get("z", 0), "visible": True, "opacity": asset.get("opacity", 1.0),
            "blend_mode": asset.get("blend_mode", "normal"), "source_index": asset["index"],
            "frame": {key: asset.get(key) for key in ("x", "y", "width", "height")},
            "transform": {key: asset.get(key) for key in ("zoom", "focal_x", "focal_y", "rotation", "fit")},
            "mask": {"type": asset.get("shape", "rectangle"), "feather": asset.get("mask_feather", 0),
                     "subject_background_removed": asset.get("remove_background", False)},
        })
    for index, decoration in enumerate(plan.get("decorations", [])):
        layers.append({
            "id": f"decoration-{index}", "kind": "vector", "name": f"장식 {index + 1}",
            "z": decoration.get("z", -1), "visible": True, "opacity": decoration.get("opacity", 1.0),
            "blend_mode": decoration.get("blend_mode", "normal"), "geometry": deepcopy(decoration),
        })
    for index, text in enumerate(plan.get("texts", [])):
        layers.append({
            "id": f"text-{index}", "kind": "text", "name": f"문구 {index + 1}",
            "z": text.get("z", 10), "visible": True, "opacity": text.get("opacity", 1.0),
            "blend_mode": text.get("blend_mode", "normal"), "content": text.get("content", ""),
            "spans": deepcopy(text.get("spans", [])),
            "frame": {key: text.get(key) for key in ("x", "y", "width", "height")},
            "typography": {
                "font_family": text.get("font_family", "Malgun Gothic"),
                "font_size": text.get("font_size", .065), "font_weight": text.get("font_weight", "bold"),
                "color": text.get("color", "#111111"), "align": text.get("align", "center"),
                "letter_spacing": text.get("letter_spacing", 0), "line_height": text.get("line_height", 1.2),
                "stroke": text.get("stroke", "transparent"), "stroke_width": text.get("stroke_width", 0),
                "shadow": deepcopy(text.get("shadow", {})), "path": deepcopy(text.get("path")),
            },
            "background": text.get("background", "transparent"), "padding": text.get("padding", .018),
        })
    return {
        "version": LAYER_GRAPH_VERSION, "canvas": deepcopy(plan.get("canvas", {})),
        "layers": sorted(layers, key=lambda layer: int(layer.get("z", 0))),
        "constraints": deepcopy(plan.get("constraints", [])),
        "decision_log": deepcopy(plan.get("decision_log", [])),
        "compatibility_scene_plan": plan,
    }


def validate_layer_graph(graph: dict, *, asset_count: int) -> None:
    if int(graph.get("version", 0)) != LAYER_GRAPH_VERSION:
        raise ValueError("지원하지 않는 레이어 그래프 버전입니다.")
    layers = graph.get("layers")
    if not isinstance(layers, list):
        raise ValueError("레이어 그래프의 layers가 배열이 아닙니다.")
    indexes = {int(layer["source_index"]) for layer in layers if layer.get("kind") == "raster"}
    missing = sorted(set(range(asset_count)) - indexes)
    if missing:
        raise ValueError(f"레이어 그래프에 제작 이미지가 누락되었습니다: {missing}")
    ids = [str(layer.get("id", "")) for layer in layers]
    if len(ids) != len(set(ids)) or any(not item for item in ids):
        raise ValueError("레이어 ID가 비어 있거나 중복되었습니다.")


def _transform_number(transform: dict, key: str, default: float) -> float:
    """Default absent values, never valid zero-valued focal coordinates."""
    raw = transform.get(key)
    try:
        value = float(default if raw is None else raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"사진 레이어의 {key} 값이 유효한 숫자가 아닙니다.") from exc
    if not math.isfinite(value):
        raise ValueError(f"사진 레이어의 {key} 값이 유효한 숫자가 아닙니다.")
    return value


def _raster_for_frame(path: Path, transform: dict, cell: tuple[int, int]) -> Image.Image:
    """Apply the same fixed-frame photo transform order as the Pillow renderer.

    Zoom crops the source around its focal point, then contain/cover fits that
    crop to the frame. Rotation is positive counter-clockwise about the frame's
    center and never expands/moves the frame. The caller applies its mask last.
    Source alpha and coordinates are retained, including prepared cutout assets.
    """
    zoom = max(1.0, _transform_number(transform, "zoom", 1.0))
    focal_x = max(0.0, min(1.0, _transform_number(transform, "focal_x", .5)))
    focal_y = max(0.0, min(1.0, _transform_number(transform, "focal_y", .5)))
    rotation = _transform_number(transform, "rotation", 0.0)
    with Image.open(path) as opened:
        source = opened.convert("RGBA")
    if zoom > 1:
        crop_width = max(1, int(source.width / zoom))
        crop_height = max(1, int(source.height / zoom))
        center_x, center_y = int(focal_x * source.width), int(focal_y * source.height)
        left = min(max(0, center_x - crop_width // 2), source.width - crop_width)
        top = min(max(0, center_y - crop_height // 2), source.height - crop_height)
        source = source.crop((left, top, left + crop_width, top + crop_height))
    if transform.get("fit") == "contain":
        placed = ImageOps.contain(source, cell, Image.Resampling.LANCZOS)
        raster = Image.new("RGBA", cell, (0, 0, 0, 0))
        raster.alpha_composite(placed, ((cell[0] - placed.width) // 2,
                                       (cell[1] - placed.height) // 2))
    else:
        # After an explicit zoom crop the focal point has already been applied.
        centering = (.5, .5) if zoom > 1 else (focal_x, focal_y)
        raster = ImageOps.fit(source, cell, Image.Resampling.LANCZOS, centering=centering)
    if rotation:
        raster = raster.rotate(rotation, Image.Resampling.BICUBIC, expand=False)
    return raster


def layer_graph_to_svg(graph: dict, asset_paths: list[str | Path], width: int, height: int) -> str:
    """Serialize the editable graph to an SVG master document."""
    background = html.escape(str(graph.get("canvas", {}).get("background", "#ffffff")))
    defs, body = [], [f'<rect width="{width}" height="{height}" fill="{background}"/>']
    for layer in graph.get("layers", []):
        if not layer.get("visible", True):
            continue
        opacity = max(0.0, min(1.0, float(layer.get("opacity", 1))))
        kind, layer_id = layer.get("kind"), html.escape(str(layer.get("id")))
        if kind == "raster":
            frame, transform = layer["frame"], layer["transform"]
            x, y = frame["x"]*width, frame["y"]*height
            w, h = frame["width"]*width, frame["height"]*height
            path = Path(asset_paths[int(layer["source_index"])]).resolve()
            clip_id = f"clip-{layer_id}"
            mask_type = layer.get("mask", {}).get("type", "rectangle")
            if mask_type == "ellipse":
                defs.append(f'<clipPath id="{clip_id}"><ellipse cx="{x+w/2}" cy="{y+h/2}" rx="{w/2}" ry="{h/2}"/></clipPath>')
            elif mask_type == "rounded":
                defs.append(f'<clipPath id="{clip_id}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{min(w,h)/12}"/></clipPath>')
            else:
                defs.append(f'<clipPath id="{clip_id}"><rect x="{x}" y="{y}" width="{w}" height="{h}"/></clipPath>')
            raster = _raster_for_frame(path, transform, (max(1, round(w)), max(1, round(h))))

            # QtSvg does not consistently honor clipPath on embedded raster
            # images. Bake non-rectangular masks into the PNG alpha channel so
            # the saved preview and the editable SVG have identical geometry.
            if mask_type in {"ellipse", "rounded"}:
                mask = Image.new("L", raster.size, 0)
                painter = ImageDraw.Draw(mask)
                bounds = (0, 0, raster.width - 1, raster.height - 1)
                if mask_type == "ellipse":
                    painter.ellipse(bounds, fill=255)
                else:
                    painter.rounded_rectangle(bounds, radius=max(8, min(raster.size) // 12), fill=255)
                raster.putalpha(ImageChops.multiply(raster.getchannel("A"), mask))

            # Always embed the prepared frame. Re-embedding the original for
            # rectangular contain would silently discard zoom and rotation, and
            # delegate scaling to a different Qt path than the masked variants.
            payload = io.BytesIO()
            raster.save(payload, "PNG")
            encoded = base64.b64encode(payload.getvalue()).decode("ascii")
            body.append(f'<image id="{layer_id}" x="{x}" y="{y}" width="{w}" height="{h}" opacity="{opacity}" '
                        f'preserveAspectRatio="none" href="data:image/png;base64,{encoded}"/>')
        elif kind == "vector":
            item = layer["geometry"]
            x, y, w, h = item.get("x",0)*width, item.get("y",0)*height, item.get("width",0)*width, item.get("height",0)*height
            stroke_width = item.get("stroke_width", .005)*width
            dash = f' stroke-dasharray="{item.get("dash_length",.025)*width} {item.get("gap_length",.018)*width}"' if item.get("dash") else ""
            common = f'fill="{item.get("fill","none")}" stroke="{item.get("stroke","none")}" stroke-width="{stroke_width}" opacity="{opacity}"{dash}'
            if item.get("type") == "ellipse": body.append(f'<ellipse cx="{x+w/2}" cy="{y+h/2}" rx="{w/2}" ry="{h/2}" {common}/>')
            elif item.get("type") == "line": body.append(f'<line x1="{x}" y1="{y}" x2="{x+w}" y2="{y+h}" {common}/>')
            else: body.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" {common}/>')
        elif kind == "text":
            frame, typo = layer["frame"], layer["typography"]
            x, y, w, h = frame["x"]*width, frame["y"]*height, frame["width"]*width, frame["height"]*height
            anchor = {"left":"start", "center":"middle", "right":"end"}.get(typo.get("align"), "middle")
            tx = x if anchor == "start" else x+w if anchor == "end" else x+w/2
            requested_font_size = typo.get("font_size", .065)*min(width,height)
            padding_px = max(0.0, float(layer.get("padding", 0))) * min(width, height)
            available_width = max(8.0, w - padding_px * 2)
            available_height = max(8.0, h - padding_px * 2)
            content_value = str(layer.get("content", ""))
            # Conservative glyph measurement: Hangul/CJK occupies roughly one em,
            # Latin letters .62 em and spaces .35 em.  This prevents SVG text from
            # escaping its declared frame even when the planner requests 320 px.
            units = sum(.35 if ch.isspace() else 1.0 if ord(ch) >= 0x2E80 else .62 for ch in content_value) or 1
            font_size = min(float(requested_font_size), available_height * .82, available_width / units)
            font_size = max(8.0, font_size)
            background = layer.get("background", "transparent")
            if background != "transparent": body.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{background}"/>')
            content = html.escape(content_value)
            text_clip = f"text-clip-{layer_id}"
            defs.append(f'<clipPath id="{text_clip}"><rect x="{x}" y="{y}" width="{w}" height="{h}"/></clipPath>')
            spans = layer.get("spans") if isinstance(layer.get("spans"), list) else []
            text_path = typo.get("path") if isinstance(typo.get("path"), dict) else None
            if text_path and text_path.get("type") == "arc" and content_value:
                radius = max(font_size, float(text_path.get("radius", .25)) * min(width, height))
                cx, cy = x + w / 2, y + h / 2
                start_angle = float(text_path.get("start_angle", 200))
                end_angle = float(text_path.get("end_angle", 340))
                start = math.radians(start_angle)
                end = math.radians(end_angle)
                x1, y1 = cx + math.cos(start) * radius, cy + math.sin(start) * radius
                x2, y2 = cx + math.cos(end) * radius, cy + math.sin(end) * radius
                delta = (end_angle - start_angle) % 360
                large_arc = 1 if delta > 180 else 0
                sweep = 1 if end_angle >= start_angle else 0
                path_id = f"text-path-{layer_id}"
                defs.append(
                    f'<path id="{path_id}" d="M {x1} {y1} A {radius} {radius} 0 {large_arc} {sweep} {x2} {y2}"/>'
                )
                body.append(
                    f'<text id="{layer_id}" font-family="{html.escape(_svg_font_family(typo.get("font_family", "Malgun Gothic")))}" '
                    f'font-size="{font_size}" font-weight="{typo.get("font_weight","bold")}" '
                    f'fill="{typo.get("color","#111111")}" stroke="{typo.get("stroke","none")}" '
                    f'stroke-width="{typo.get("stroke_width",0)*min(width,height)}" opacity="{opacity}">'
                    f'<textPath href="#{path_id}" startOffset="50%" text-anchor="middle">{content}</textPath></text>'
                )
                continue
            if spans and " ".join(str(item.get("content", "")).strip() for item in spans) == content_value:
                # QtSvg does not reliably shape Hangul when a font-family is
                # changed on nested tspan nodes.  Render phrase spans as
                # independent text nodes so each phrase keeps its own Windows
                # font while remaining one centred copy line.
                raw_span_layout = []
                for index, span in enumerate(spans):
                    separator = " " if index else ""
                    span_content = separator + str(span.get("content", ""))
                    span_units = sum(.35 if ch.isspace() else 1.0 if ord(ch) >= 0x2E80 else .62
                                     for ch in span_content)
                    span_font_size = float(span.get("font_size", typo.get("font_size", .065))) * min(width, height)
                    span_font_size = max(8.0, span_font_size)
                    raw_span_layout.append((span, span_content, span_units, span_font_size))
                raw_width = sum(units * size for _span, _content, units, size in raw_span_layout)
                tallest = max((size for _span, _content, _units, size in raw_span_layout), default=font_size)
                scale = min(1.0, available_width / max(1.0, raw_width),
                            available_height * .82 / max(1.0, tallest))
                span_layout = [(span, span_content, max(8.0, size * scale),
                                units * max(8.0, size * scale))
                               for span, span_content, units, size in raw_span_layout]
                total_width = sum(part[2] for part in span_layout)
                # Width is the fourth tuple entry after adding per-run size.
                total_width = sum(part[3] for part in span_layout)
                cursor = x if anchor == "start" else x + w - total_width if anchor == "end" else x + (w - total_width) / 2
                for index, (span, span_content, span_font_size, span_width) in enumerate(span_layout):
                    body.append(
                        f'<text id="{layer_id}-span-{index}" x="{cursor}" y="{y+h/2}" '
                        f'dominant-baseline="middle" text-anchor="start" '
                        f'font-family="{html.escape(_svg_font_family(span.get("font_family", typo.get("font_family", "Malgun Gothic"))))}" '
                        f'font-size="{span_font_size}" font-weight="{span.get("font_weight", typo.get("font_weight","bold"))}" '
                        f'fill="{html.escape(str(span.get("color", typo.get("color", "#111111"))))}" '
                        f'stroke="{typo.get("stroke","none")}" stroke-width="{typo.get("stroke_width",0)*min(width,height)}" '
                        f'opacity="{opacity}" clip-path="url(#{text_clip})">{html.escape(span_content)}</text>'
                    )
                    cursor += span_width
                continue
            body.append(f'<text id="{layer_id}" x="{tx}" y="{y+h/2}" dominant-baseline="middle" text-anchor="{anchor}" '
                        f'font-family="{html.escape(_svg_font_family(typo.get("font_family", "Malgun Gothic")))}" font-size="{font_size}" '
                        f'font-weight="{typo.get("font_weight","bold")}" fill="{typo.get("color","#111111")}" '
                        f'stroke="{typo.get("stroke","none")}" stroke-width="{typo.get("stroke_width",0)*min(width,height)}" '
                        f'letter-spacing="{typo.get("letter_spacing",0)*min(width,height)}" opacity="{opacity}" '
                        f'clip-path="url(#{text_clip})">{content}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{width}" height="{height}" viewBox="0 0 {width} {height}"><defs>{"".join(defs)}</defs>{"".join(body)}</svg>')


def render_svg_with_qt(svg: str, width: int, height: int):
    """Rasterize SVG through Qt's vector engine and return a PIL RGBA image."""
    from PyQt6.QtCore import QByteArray, QCoreApplication
    from PyQt6.QtGui import QFontDatabase, QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer
    from PIL import Image
    global _qt_application
    if QCoreApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        _qt_application = QApplication([])
    global _qt_design_fonts_loaded
    if not _qt_design_fonts_loaded:
        # Headless Qt sessions may not enumerate Korean system fonts even when
        # they are installed. Register the concrete files used by supported
        # aliases so SVG text never degrades into missing-glyph boxes.
        for font_file in ("malgun.ttf", "malgunbd.ttf", "batang.ttc", "gulim.ttc"):
            path = Path("C:/Windows/Fonts") / font_file
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
        _qt_design_fonts_loaded = True
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise ValueError("생성된 SVG 레이어 그래프가 유효하지 않습니다.")
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    payload = bytes(image.constBits().asstring(image.sizeInBytes()))
    return Image.frombytes("RGBA", (width, height), payload)


_qt_application = None
_qt_design_fonts_loaded = False
