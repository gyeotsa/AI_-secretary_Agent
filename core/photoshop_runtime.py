"""Bounded Photoshop COM editing with non-destructive, verified copy outputs.

Only the typed operations below are accepted; scripts/actions/JavaScript are
never executed. COM methods and numeric enums follow Adobe's 2020 VBScript
Scripting Reference (Document, TextItem, Preferences, SaveOptions):
https://community.adobe.com/havfw69955/attachments/havfw69955/photoshop/556207/1/photoshop-vbs-ref-2020_unlocked.pdf

The native adapter is optional. Tests inject a COM-shaped adapter, never a
silent production Pillow fallback. Real Photoshop acceptance is separate.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Iterator
import uuid

from jsonschema import Draft202012Validator


MAX_DIMENSION = 16384
MAX_PIXELS = 36_000_000
MAX_INPUT_BYTES = 256 * 1024 * 1024
SOURCE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".psd"}
OUTPUT_EXTENSIONS = {".png", ".psd"}
_EDIT_LOCK = threading.Lock()
_DIMENSION = {"type": "integer", "minimum": 1, "maximum": MAX_DIMENSION}
_COORDINATE = {"type": "integer", "minimum": 0, "maximum": MAX_DIMENSION}


def _operation(kind: str, properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": {"op": {"const": kind}, **properties},
            "required": ["op", *required], "additionalProperties": False}


EDIT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "source_path": {"type": "string", "minLength": 1,
                        "description": "저장된 입력 PNG/JPEG/BMP/TIFF/PSD의 경로"},
        "output_path": {"type": "string", "minLength": 1,
                        "description": "아직 존재하지 않는 별도 PNG 또는 PSD 파일 경로. 덮어쓰기 금지"},
        "operations": {"type": "array", "minItems": 1, "maxItems": 24, "items": {"oneOf": [
            _operation("resize", {"width": _DIMENSION, "height": _DIMENSION}, ["width", "height"]),
            _operation("crop", {"left": _COORDINATE, "top": _COORDINATE,
                                "right": _DIMENSION, "bottom": _DIMENSION},
                       ["left", "top", "right", "bottom"]),
            _operation("rotate", {"angle": {"enum": [-270, -180, -90, 90, 180, 270]}}, ["angle"]),
            _operation("text", {
                "content": {"type": "string", "minLength": 1, "maxLength": 2000},
                "font": {"type": "string", "minLength": 1, "maxLength": 200,
                         "description": "Photoshop 글꼴 목록의 정확한 PostScriptName 또는 유일한 이름"},
                "font_size": {"type": "number", "minimum": 1, "maximum": 1200,
                              "description": "픽셀 단위 글자 크기"},
                "x": _COORDINATE, "y": _COORDINATE,
                "color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
                "align": {"enum": ["left", "center", "right"], "default": "left"},
            }, ["content", "font", "font_size", "x", "y", "color"]),
        ]}},
    },
    "required": ["source_path", "output_path", "operations"],
    "additionalProperties": False,
}


class PhotoshopUnavailable(RuntimeError):
    """Photoshop/COM cannot currently execute the requested operation."""


class PhotoshopVerificationError(RuntimeError):
    """The request was attempted on a duplicate but its result was not proven."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image_facts(path: Path) -> dict[str, Any]:
    from PIL import Image
    with Image.open(path) as image:
        if image.format != "PNG":
            raise PhotoshopVerificationError("검증용 렌더링이 PNG 형식이 아닙니다.")
        _check_dimensions(image.width, image.height)
        image.load()
        rgba = image.convert("RGBA")
        digest = hashlib.sha256(f"{rgba.width}x{rgba.height}:RGBA:".encode("ascii"))
        digest.update(rgba.tobytes())
        return {"width": image.width, "height": image.height, "pixel_sha256": digest.hexdigest()}


def _verify_operation_pixels(before_path: Path, after_path: Path, operation: dict) -> dict[str, Any]:
    """An independent pixel oracle catches ignored/wrong-direction COM calls."""
    from PIL import Image
    before_facts, after_facts = _image_facts(before_path), _image_facts(after_path)
    if before_facts["pixel_sha256"] == after_facts["pixel_sha256"]:
        raise PhotoshopVerificationError(f"{operation['op']} 작업에 실제 픽셀 변경이 없습니다.")
    if operation["op"] in {"crop", "rotate"}:
        with Image.open(before_path) as source, Image.open(after_path) as actual:
            expected = source.convert("RGBA")
            if operation["op"] == "crop":
                expected = expected.crop(tuple(operation[key] for key in ("left", "top", "right", "bottom")))
            else:
                # Photoshop rotates clockwise; Pillow's transpose names are CCW.
                transpose = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                             270: Image.Transpose.ROTATE_90}[operation["angle"] % 360]
                expected = expected.transpose(transpose)
            if expected.size != actual.size or expected.tobytes() != actual.convert("RGBA").tobytes():
                raise PhotoshopVerificationError(f"{operation['op']} 결과의 실제 픽셀이 요청 영역/회전 방향과 다릅니다.")
    return {"before_pixel_sha256": before_facts["pixel_sha256"],
            "after_pixel_sha256": after_facts["pixel_sha256"], "pixel_verified": True}


def _check_dimensions(width: int, height: int) -> None:
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise ValueError("이미지 크기는 각 변 1~16384픽셀이어야 합니다.")
    if width * height > MAX_PIXELS:
        raise ValueError("메모리 보호를 위해 3,600만 픽셀 이하만 편집합니다.")


def _numbers(value: Any) -> list[float]:
    if isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(value[0], (list, tuple)):
        value = value[0]
    result = [float(item) for item in value]
    if not all(math.isfinite(item) for item in result):
        raise PhotoshopVerificationError("Photoshop이 유효하지 않은 좌표를 반환했습니다.")
    return result


def _size(document: Any) -> tuple[int, int]:
    raw = [float(document.Width), float(document.Height)]
    if not all(math.isfinite(value) for value in raw):
        raise PhotoshopVerificationError("Photoshop 문서 크기가 유효하지 않습니다.")
    result = tuple(round(value) for value in raw)
    _check_dimensions(*result)
    return result


def _path_of(document: Any) -> Path | None:
    try:
        return Path(str(document.FullName)).resolve()
    except Exception:
        return None


def _snapshot_document(document: Any) -> dict[str, Any]:
    return {"size": list(_size(document)), "saved": bool(document.Saved),
            "layers": int(document.Layers.Count)}


def validate_edit_input(data: dict[str, Any]) -> tuple[Path, Path, list[dict[str, Any]]]:
    errors = sorted(Draft202012Validator(EDIT_INPUT_SCHEMA).iter_errors(data), key=lambda error: str(error.path))
    if errors:
        raise ValueError("Photoshop 편집 입력이 지원 schema와 다릅니다: " + errors[0].message)
    for operation in data["operations"]:
        for value in operation.values():
            if isinstance(value, (int, float)) and (isinstance(value, bool) or not math.isfinite(value)):
                raise ValueError("편집 값은 유한한 숫자여야 합니다.")
        if operation["op"] == "text" and not operation["content"].strip():
            raise ValueError("빈 문구는 추가하지 않습니다.")
    source = Path(data["source_path"]).expanduser().resolve(strict=True)
    output_candidate = Path(data["output_path"]).expanduser()
    if output_candidate.exists() or output_candidate.is_symlink():
        raise FileExistsError("출력 파일이 이미 있습니다. 새 이름으로 저장해야 합니다.")
    output = output_candidate.resolve()
    if not source.is_file() or source.suffix.casefold() not in SOURCE_EXTENSIONS:
        raise ValueError("입력은 저장된 PNG/JPEG/BMP/TIFF/PSD 파일이어야 합니다.")
    if source.stat().st_size <= 0 or source.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("입력 파일은 비어 있지 않은 256MB 이하 파일이어야 합니다.")
    if source == output or output.suffix.casefold() not in OUTPUT_EXTENSIONS:
        raise ValueError("원본과 다른 PNG 또는 PSD 출력 경로가 필요합니다.")
    if not output.parent.is_dir():
        raise ValueError("출력 폴더가 없습니다. 먼저 존재하는 작업 폴더를 선택해주세요.")
    return source, output, [dict(item) for item in data["operations"]]


class PhotoshopRuntime:
    """A single serialized COM session; never closes or saves user-owned docs."""

    def __init__(self, *, connector: Callable[[], Any] | None = None,
                 object_factory: Callable[[str], Any] | None = None):
        self._connector = connector
        self._object_factory = object_factory

    @contextmanager
    def session(self) -> Iterator[tuple[Any, Callable[[str], Any]]]:
        if self._connector is not None:
            if self._object_factory is None:
                raise ValueError("테스트 COM 연결에는 object_factory도 필요합니다.")
            yield self._connector(), self._object_factory
            return
        if os.name != "nt":
            raise PhotoshopUnavailable("Photoshop COM 편집은 Windows에서만 사용할 수 있습니다.")
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise PhotoshopUnavailable("Photoshop COM에 필요한 pywin32가 설치되지 않았습니다.") from exc
        pythoncom.CoInitialize()
        try:
            try:
                app = win32com.client.Dispatch("Photoshop.Application")
            except Exception as exc:
                raise PhotoshopUnavailable("Photoshop을 연결하지 못했습니다. 정식 앱 설치·실행 상태를 확인해주세요.") from exc
            yield app, win32com.client.Dispatch
        finally:
            pythoncom.CoUninitialize()

    def fonts(self, *, query: str = "", limit: int = 100) -> list[dict[str, str]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("글꼴 조회 개수는 1~100이어야 합니다.")
        with self.session() as (app, _factory):
            return [font for font in self._fonts(app)
                    if query.casefold() in " ".join(font.values()).casefold()][:limit]

    @staticmethod
    def _fonts(app: Any) -> list[dict[str, str]]:
        count = int(app.Fonts.Count)
        if not 0 <= count <= 20000:
            raise PhotoshopVerificationError("Photoshop 글꼴 목록 크기가 유효하지 않습니다.")
        result = []
        for index in range(1, count + 1):
            font = app.Fonts.Item(index)
            result.append({"postscript_name": str(font.PostScriptName),
                           "name": str(font.Name), "family": str(font.Family), "style": str(font.Style)})
        return result

    @staticmethod
    def _resolve_font(requested: str, fonts: list[dict[str, str]]) -> str:
        key = requested.casefold()
        exact = [item["postscript_name"] for item in fonts if item["postscript_name"].casefold() == key]
        matches = exact or [item["postscript_name"] for item in fonts
                            if key in {item["name"].casefold(), item["family"].casefold()}]
        unique = set(matches)
        if len(unique) != 1:
            raise ValueError(f"글꼴 '{requested}'가 없거나 여러 스타일입니다. Photoshop 글꼴 목록에서 정확한 PostScriptName을 선택해주세요.")
        return unique.pop()

    @staticmethod
    def _save(document: Any, path: Path, factory: Callable[[str], Any]) -> None:
        if path.suffix.casefold() == ".psd":
            options = factory("Photoshop.PhotoshopSaveOptions")
            options.Layers = True
            options.EmbedColorProfile = True
        else:
            options = factory("Photoshop.PNGSaveOptions")
            options.Interlaced = False
            options.Compression = 6
        document.SaveAs(str(path), options, True)
        if not path.is_file() or path.stat().st_size <= 0:
            raise PhotoshopVerificationError("Photoshop이 실제 출력 파일을 저장하지 않았습니다.")
        if path.suffix.casefold() == ".psd":
            with path.open("rb") as handle:
                if handle.read(6) != b"8BPS\x00\x01":
                    raise PhotoshopVerificationError("저장 결과가 PSD 형식이 아닙니다.")
        else:
            _image_facts(path)

    @staticmethod
    def _text_facts(layer: Any) -> dict[str, Any]:
        text = layer.TextItem
        rgb = text.Color.RGB
        return {"name": str(layer.Name), "content": str(text.Contents), "font": str(text.Font),
                "font_size": float(text.Size), "position": _numbers(text.Position),
                "color": [round(float(rgb.Red)), round(float(rgb.Green)), round(float(rgb.Blue))],
                "align": int(text.Justification), "bounds": _numbers(layer.Bounds)}

    @staticmethod
    def _assert_text_equal(actual: dict, expected: dict) -> None:
        for name in ("name", "content", "font", "color", "align"):
            if actual[name] != expected[name]:
                raise PhotoshopVerificationError(f"저장된 텍스트의 {name} 값이 요청과 다릅니다.")
        if abs(actual["font_size"] - expected["font_size"]) > 0.1:
            raise PhotoshopVerificationError("텍스트 크기가 요청과 다릅니다.")
        if len(actual["position"]) != 2 or any(abs(a - b) > 0.1 for a, b in zip(actual["position"], expected["position"])):
            raise PhotoshopVerificationError("텍스트 위치가 요청과 다릅니다.")

    def _apply(self, document: Any, operation: dict, *, factory: Callable[[str], Any],
               fonts: list[dict[str, str]], index: int, verification_id: str) -> tuple[dict, Any | None]:
        width, height = _size(document)
        kind = operation["op"]
        text_layer = None
        expected_size = (width, height)
        if kind == "resize":
            expected_size = (operation["width"], operation["height"])
            _check_dimensions(*expected_size)
            document.ResizeImage(float(expected_size[0]), float(expected_size[1]))
        elif kind == "crop":
            left, top, right, bottom = (operation[key] for key in ("left", "top", "right", "bottom"))
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                raise ValueError("자르기 영역은 현재 이미지 안쪽의 유효한 left/top/right/bottom이어야 합니다.")
            document.Crop((float(left), float(top), float(right), float(bottom)))
            expected_size = (right - left, bottom - top)
        elif kind == "rotate":
            document.RotateCanvas(float(operation["angle"]))
            if abs(operation["angle"]) % 180 == 90:
                expected_size = (height, width)
        elif kind == "text":
            if operation["x"] >= width or operation["y"] >= height:
                raise ValueError("텍스트 원점(x/y)은 현재 이미지 내부여야 합니다. y는 텍스트 기준선 위치입니다.")
            font = self._resolve_font(operation["font"], fonts)
            text_layer = document.ArtLayers.Add()
            text_layer.Kind = 2  # PsLayerKind.psTextLayer
            text_layer.Name = f"ANIS-{verification_id}-{index}"
            text_layer.Visible = True
            text_layer.Opacity = 100
            text = text_layer.TextItem
            content = operation["content"].replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r")
            text.Contents = content
            text.Font = font
            text.Size = float(operation["font_size"])
            text.Position = (float(operation["x"]), float(operation["y"]))
            alignment = {"left": 1, "center": 2, "right": 3}[operation.get("align", "left")]
            text.Justification = alignment
            color = factory("Photoshop.SolidColor")
            channels = [int(operation["color"][start:start + 2], 16) for start in (1, 3, 5)]
            color.RGB.Red, color.RGB.Green, color.RGB.Blue = channels
            text.Color = color
            expected_text = {"name": f"ANIS-{verification_id}-{index}", "content": content, "font": font,
                             "font_size": operation["font_size"], "position": [operation["x"], operation["y"]],
                             "color": channels, "align": alignment}
            actual = self._text_facts(text_layer)
            self._assert_text_equal(actual, expected_text)
            bounds = actual["bounds"]
            if len(bounds) != 4 or not (0 <= bounds[0] < bounds[2] <= width and 0 <= bounds[1] < bounds[3] <= height):
                raise PhotoshopVerificationError("문구가 이미지 경계에서 잘립니다. 위치/크기를 조정해주세요. 임의로 축소하지 않았습니다.")
        else:
            raise ValueError("지원하지 않는 Photoshop 편집 동작입니다.")
        observed_size = _size(document)
        if observed_size != expected_size:
            raise PhotoshopVerificationError(f"{kind} 결과 크기가 요청과 다릅니다: {observed_size} != {expected_size}")
        observation = {"index": index, "operation": operation, "verified": True,
                       "before_size": [width, height], "after_size": list(observed_size)}
        if text_layer is not None:
            observation["text"] = actual
        return observation, text_layer

    def edit_copy(self, data: dict[str, Any]) -> dict[str, Any]:
        source, output, operations = validate_edit_input(data)
        if not _EDIT_LOCK.acquire(blocking=False):
            raise PhotoshopUnavailable("다른 Photoshop 편집이 실행 중입니다. 현재 작업이 끝난 뒤 다시 요청해주세요.")
        try:
            with self.session() as (app, factory):
                return self._edit_session(app, factory, source, output, operations)
        finally:
            _EDIT_LOCK.release()

    def _edit_session(self, app: Any, factory: Callable[[str], Any], source: Path,
                      output: Path, operations: list[dict]) -> dict[str, Any]:
        source_hash = file_sha256(source)
        verification_id = uuid.uuid4().hex
        previous_active = app.ActiveDocument if int(app.Documents.Count) else None
        preferences = app.Preferences
        settings = [(preferences, "RulerUnits", preferences.RulerUnits),
                    (preferences, "TypeUnits", preferences.TypeUnits),
                    (app, "DisplayDialogs", app.DisplayDialogs)]
        owned_documents: list[Any] = []
        result: dict[str, Any] | None = None
        with tempfile.TemporaryDirectory(prefix="anis-photoshop-", dir=str(output.parent)) as scratch:
            scratch_path = Path(scratch)
            stage = scratch_path / ("edited" + output.suffix.casefold())
            try:
                preferences.RulerUnits = 1  # pixels
                preferences.TypeUnits = 1  # pixels, independent of document DPI
                app.DisplayDialogs = 3  # no dialogs; never respond to prompts blindly
                source_document = None
                for index in range(1, int(app.Documents.Count) + 1):
                    candidate = app.Documents.Item(index)
                    if _path_of(candidate) == source:
                        source_document = candidate
                        break
                if source_document is None:
                    source_document = app.Open(str(source))
                    owned_documents.append(source_document)
                if _path_of(source_document) != source:
                    raise PhotoshopVerificationError("선택한 입력 파일과 열린 Photoshop 문서가 다릅니다.")
                original_state = _snapshot_document(source_document)
                if not original_state["saved"]:
                    raise PhotoshopVerificationError("이 원본 문서에 저장하지 않은 변경이 있습니다. 먼저 직접 저장하거나 별도 복사본을 선택해주세요.")
                app.ActiveDocument = source_document
                working = source_document.Duplicate(f"ANIS-{verification_id}", False)
                if working is source_document or _path_of(working) == source:
                    raise PhotoshopVerificationError("원본과 분리된 문서 복제에 실패했습니다.")
                owned_documents.append(working)
                app.ActiveDocument = working
                if _size(working) != tuple(original_state["size"]) or int(working.Layers.Count) != original_state["layers"]:
                    raise PhotoshopVerificationError("복제 문서의 크기/레이어가 원본과 다릅니다.")
                self._save(working, scratch_path / "before.png", factory)
                before = _image_facts(scratch_path / "before.png")
                fonts = self._fonts(app) if any(item["op"] == "text" for item in operations) else []
                observed = []
                added_text_layers = []
                prior_render = scratch_path / "before.png"
                for index, operation in enumerate(operations):
                    app.ActiveDocument = working
                    observation, text_layer = self._apply(working, operation, factory=factory,
                                                          fonts=fonts, index=index, verification_id=verification_id)
                    current_render = scratch_path / f"step-{index}.png"
                    self._save(working, current_render, factory)
                    observation.update(_verify_operation_pixels(prior_render, current_render, operation))
                    if text_layer is not None:
                        self._assert_text_equal(self._text_facts(text_layer), observation["text"])
                    prior_render = current_render
                    observed.append(observation)
                    if text_layer is not None:
                        added_text_layers.append(text_layer)
                final_text = [self._text_facts(layer) for layer in added_text_layers]
                final_layers = int(working.Layers.Count)
                self._save(working, scratch_path / "expected.png", factory)
                expected = _image_facts(scratch_path / "expected.png")
                if before["pixel_sha256"] == expected["pixel_sha256"]:
                    raise PhotoshopVerificationError("실제 픽셀 변경이 없습니다. 수정 완료로 처리하거나 중복 결과를 저장하지 않았습니다.")
                self._save(working, stage, factory)
                reopened = app.Open(str(stage))
                if _path_of(reopened) != stage.resolve():
                    raise PhotoshopVerificationError("저장된 결과 파일을 다시 연 문서가 아닙니다.")
                owned_documents.append(reopened)
                app.ActiveDocument = reopened
                if _size(reopened) != (expected["width"], expected["height"]):
                    raise PhotoshopVerificationError("저장 결과의 크기가 편집 결과와 다릅니다.")
                if output.suffix.casefold() == ".psd":
                    if int(reopened.Layers.Count) != final_layers:
                        raise PhotoshopVerificationError("PSD 저장 후 레이어 수가 보존되지 않았습니다.")
                    for expected_text in final_text:
                        saved_layer = reopened.ArtLayers.Item(expected_text["name"])
                        self._assert_text_equal(self._text_facts(saved_layer), expected_text)
                self._save(reopened, scratch_path / "reopened.png", factory)
                after = _image_facts(scratch_path / "reopened.png")
                if after != expected:
                    raise PhotoshopVerificationError("저장 후 다시 렌더링한 픽셀이 편집 결과와 다릅니다.")
                source_after = file_sha256(source)
                if source_hash != source_after or _snapshot_document(source_document) != original_state:
                    raise PhotoshopVerificationError("처리 도중 원본이 변경되었습니다. 결과를 게시하지 않았습니다.")
                result = {"verification_id": verification_id, "source_path": str(source),
                          "output_path": str(output), "source_before_sha256": source_hash,
                          "source_after_sha256": source_after, "output_sha256": file_sha256(stage),
                          "source_preserved": True, "output_reopened": True, "pixel_changed": True,
                          "before": before, "after": after, "operations": observed,
                          "output_format": output.suffix.casefold()[1:], "text_layers": final_text}
            finally:
                cleanup_errors = []
                for document in reversed(owned_documents):
                    try:
                        document.Close(2)  # psDoNotSaveChanges, owned duplicates/open handles only
                    except Exception as exc:
                        cleanup_errors.append(f"임시 문서 닫기: {exc}")
                for target, name, value in settings:
                    try:
                        setattr(target, name, value)
                    except Exception as exc:
                        cleanup_errors.append(f"설정 복원({name}): {exc}")
                if previous_active is not None:
                    try:
                        app.ActiveDocument = previous_active
                    except Exception as exc:
                        cleanup_errors.append(f"원래 활성 문서 복원: {exc}")
                if cleanup_errors:
                    raise PhotoshopVerificationError("Photoshop 세션을 완전히 복원하지 못했습니다: " + "; ".join(cleanup_errors))
            # Publish only after every operation, save/reopen, and cleanup passed.
            # Windows rename refuses an existing target, including a racing writer.
            if file_sha256(source) != source_hash:
                raise PhotoshopVerificationError("게시 전 원본 변경을 감지했습니다. 결과를 저장하지 않았습니다.")
            if os.name == "nt":
                stage.rename(output)
            else:  # Portable fake-adapter tests: no replacing POSIX rename.
                os.link(stage, output)
            assert result is not None
            return result


def verify_edit_evidence(evidence: list[dict], artifacts: list[dict]) -> dict[str, dict[str, Any]]:
    """Criterion-specific gate: opening a document can never satisfy editing."""
    keys = ("photoshop_saved_copy", "photoshop_visual_change", "photoshop_source_preserved")
    checks = {key: {"verified": False, "reason": "실제 Photoshop 편집·저장 검증 근거가 없습니다."} for key in keys}
    matches = [item.get("data") for item in evidence if item.get("kind") == "photoshop_edit_verification"]
    for details in matches:
        if not isinstance(details, dict):
            continue
        try:
            verification_id = details.get("verification_id")
            if not isinstance(verification_id, str) or len(verification_id) != 32 or any(
                char not in "0123456789abcdef" for char in verification_id
            ):
                continue
            source, output = Path(details["source_path"]).resolve(), Path(details["output_path"]).resolve()
            matching = [item for item in artifacts if Path(str(item.get("uri", ""))).resolve() == output
                        and item.get("metadata", {}).get("verification_id") == details.get("verification_id")
                        and item.get("metadata", {}).get("role") == "output"]
            if source == output or not matching or not source.is_file() or not output.is_file():
                continue
            digest_fields = ("source_before_sha256", "source_after_sha256", "output_sha256")
            if not all(isinstance(details.get(key), str) and len(details[key]) == 64
                       and all(char in "0123456789abcdef" for char in details[key]) for key in digest_fields):
                continue
            output_matches = file_sha256(output) == details["output_sha256"]
            operations = details.get("operations")
            operation_validator = Draft202012Validator(EDIT_INPUT_SCHEMA["properties"]["operations"])
            applied = bool(operations) and isinstance(operations, list) and all(
                isinstance(item, dict) and item.get("verified") is True
                and item.get("pixel_verified") is True
                for item in operations)
            if applied and not operation_validator.is_valid([item.get("operation") for item in operations]):
                applied = False
            saved = output_matches and details.get("output_reopened") is True and applied
            before, after = details.get("before", {}), details.get("after", {})
            pixel_hashes = [before.get("pixel_sha256", ""), after.get("pixel_sha256", "")]
            visual = saved and details.get("pixel_changed") is True and all(
                isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
                for value in pixel_hashes) and pixel_hashes[0] != pixel_hashes[1]
            if visual:
                previous_hash = pixel_hashes[0]
                previous_size = [before.get("width"), before.get("height")]
                for item in operations:
                    operation = item["operation"]
                    after_size = list(previous_size)
                    if operation["op"] == "resize":
                        after_size = [operation["width"], operation["height"]]
                    elif operation["op"] == "crop":
                        after_size = [operation["right"] - operation["left"], operation["bottom"] - operation["top"]]
                    elif operation["op"] == "rotate" and abs(operation["angle"]) % 180:
                        after_size.reverse()
                    actual_hash = item.get("after_pixel_sha256", "")
                    if (item.get("before_pixel_sha256") != previous_hash or actual_hash == previous_hash
                            or len(actual_hash) != 64 or any(char not in "0123456789abcdef" for char in actual_hash)
                            or item.get("before_size") != previous_size or item.get("after_size") != after_size):
                        visual = False
                        break
                    previous_hash, previous_size = actual_hash, after_size
                visual = visual and previous_hash == pixel_hashes[1] and previous_size == [after.get("width"), after.get("height")]
                if output.suffix.casefold() == ".png":
                    visual = visual and _image_facts(output) == after
                elif output.suffix.casefold() == ".psd":
                    with output.open("rb") as handle:
                        visual = visual and handle.read(6) == b"8BPS\x00\x01"
                else:
                    visual = False
            preserved = details.get("source_preserved") is True and (
                file_sha256(source) == details["source_before_sha256"] == details["source_after_sha256"])
            values = {"photoshop_saved_copy": saved, "photoshop_visual_change": visual,
                      "photoshop_source_preserved": preserved}
            for key, passed in values.items():
                checks[key] = {"verified": bool(passed), "reason": "해시·저장 재열기·타입 작업 검증으로 확인했습니다."
                               if passed else "편집 증거가 누락/변경되었거나 해당 수락 기준을 확인하지 못했습니다.",
                               "verification_id": details.get("verification_id"), "output_path": str(output)}
            if all(values.values()):
                return checks
        except (KeyError, TypeError, ValueError, OSError, AttributeError, PhotoshopVerificationError):
            continue
    return checks
