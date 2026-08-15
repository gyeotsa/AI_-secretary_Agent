"""Professional, versioned layer graph used between design planning and rendering."""
from __future__ import annotations

from copy import deepcopy
import base64
import html
import mimetypes
import os
from pathlib import Path


LAYER_GRAPH_VERSION = 1


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
            "mask": {"type": asset.get("shape", "rectangle"), "feather": asset.get("mask_feather", 0)},
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
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            clip_id = f"clip-{layer_id}"
            mask_type = layer.get("mask", {}).get("type", "rectangle")
            if mask_type == "ellipse":
                defs.append(f'<clipPath id="{clip_id}"><ellipse cx="{x+w/2}" cy="{y+h/2}" rx="{w/2}" ry="{h/2}"/></clipPath>')
            elif mask_type == "rounded":
                defs.append(f'<clipPath id="{clip_id}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{min(w,h)/12}"/></clipPath>')
            else:
                defs.append(f'<clipPath id="{clip_id}"><rect x="{x}" y="{y}" width="{w}" height="{h}"/></clipPath>')
            aspect = "xMidYMid meet" if transform.get("fit") == "contain" else "xMidYMid slice"
            body.append(f'<image id="{layer_id}" x="{x}" y="{y}" width="{w}" height="{h}" opacity="{opacity}" '
                        f'preserveAspectRatio="{aspect}" clip-path="url(#{clip_id})" href="data:{mime};base64,{encoded}"/>')
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
            font_size = typo.get("font_size", .065)*min(width,height)
            background = layer.get("background", "transparent")
            if background != "transparent": body.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{background}"/>')
            content = html.escape(str(layer.get("content", "")))
            body.append(f'<text id="{layer_id}" x="{tx}" y="{y+h/2}" dominant-baseline="middle" text-anchor="{anchor}" '
                        f'font-family="{html.escape(str(typo.get("font_family","Malgun Gothic")))}" font-size="{font_size}" '
                        f'font-weight="{typo.get("font_weight","bold")}" fill="{typo.get("color","#111111")}" '
                        f'stroke="{typo.get("stroke","none")}" stroke-width="{typo.get("stroke_width",0)*min(width,height)}" '
                        f'letter-spacing="{typo.get("letter_spacing",0)*min(width,height)}" opacity="{opacity}">{content}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{width}" height="{height}" viewBox="0 0 {width} {height}"><defs>{"".join(defs)}</defs>{"".join(body)}</svg>')


def render_svg_with_qt(svg: str, width: int, height: int):
    """Rasterize SVG through Qt's vector engine and return a PIL RGBA image."""
    from PyQt6.QtCore import QByteArray, QCoreApplication
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer
    from PIL import Image
    global _qt_application
    if QCoreApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        _qt_application = QApplication([])
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
