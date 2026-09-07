"""COM-shaped tests only: never launches Photoshop or edits user documents.

Pillow is an independent fixture/oracle here, not a production edit fallback.
Native Photoshop acceptance remains a separately authorized live test.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont
import pytest

from core.photoshop_runtime import (
    EDIT_INPUT_SCHEMA, PhotoshopRuntime, PhotoshopUnavailable,
    PhotoshopVerificationError, file_sha256, verify_edit_evidence,
)
from core.tool_result import ToolRunStatus
from plugins.photoshop import PhotoshopPlugin


class Collection:
    def __init__(self, items=None):
        self.items = list(items or [])

    @property
    def Count(self):
        return len(self.items)

    def Item(self, key):
        if isinstance(key, int):
            return self.items[key - 1]
        return next(item for item in self.items if item.Name == key)


class TextItem:
    def __init__(self):
        self.Contents = ""
        self.Font = "Fixture-Regular"
        self.Size = 10.0
        self.Position = (10.0, 20.0)
        self.Justification = 1
        self.Color = SimpleNamespace(RGB=SimpleNamespace(Red=0, Green=0, Blue=0))


class Layer:
    def __init__(self, name="Background"):
        self.Name = name
        self.Kind = 1
        self.Visible = True
        self.Opacity = 100
        self.TextItem = TextItem()

    @property
    def Bounds(self):
        item = self.TextItem
        lines = item.Contents.split("\r")
        width = max(len(line) for line in lines) * item.Size * .6
        x, y = item.Position
        x -= width * {1: 0, 2: .5, 3: 1}[item.Justification]
        return (x, y - item.Size, x + width, y + (len(lines) - 1) * item.Size)


class ArtLayers(Collection):
    def __init__(self, document):
        self.document = document
        super().__init__()

    def Add(self):
        layer = Layer("new layer")
        self.items.append(layer)
        self.document.layers.append(layer)
        self.document.Saved = False
        return layer


class Document:
    def __init__(self, app, image, path=None, name="fixture"):
        self.app, self.image = app, image.convert("RGBA")
        self.path = Path(path).resolve() if path else None
        self.Name = name
        self.Mode = 2
        self.Saved = True
        self.layers = [Layer()]
        self.ArtLayers = ArtLayers(self)
        self.ArtLayers.items = self.layers.copy()
        self.closed = False

    @property
    def Width(self):
        return self.image.width

    @property
    def Height(self):
        return self.image.height

    @property
    def FullName(self):
        if self.path is None:
            raise ValueError("not yet saved")
        return str(self.path)

    @property
    def Layers(self):
        return Collection(self.layers)

    def Duplicate(self, name, merge_layers):
        assert merge_layers is False
        if self.app.fault == "duplicate_source":
            return self
        other = Document(self.app, self.image.copy(), name=name)
        other.layers = deepcopy(self.layers)
        other.ArtLayers.items = other.layers.copy()
        self.app.Documents.items.append(other)
        self.app.ActiveDocument = other
        return other

    def _flatten(self):
        image = self.render()
        self.image = image
        self.layers = [Layer()]
        self.ArtLayers.items = self.layers.copy()

    def ResizeImage(self, width, height):
        if self.app.fault == "ignore_resize":
            return
        self._flatten()
        self.image = self.image.resize((int(width), int(height)))
        self.Saved = False

    def Crop(self, bounds):
        self._flatten()
        left, top, right, bottom = map(int, bounds)
        if self.app.fault == "wrong_crop":
            left, right = left + 1, right + 1
        self.image = self.image.crop((left, top, right, bottom))
        self.Saved = False

    def RotateCanvas(self, angle):
        self._flatten()
        angle = int(angle)
        if self.app.fault == "wrong_rotation":
            angle = -angle
        transpose = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                     270: Image.Transpose.ROTATE_90}[angle % 360]
        self.image = self.image.transpose(transpose)
        self.Saved = False

    def render(self):
        image = self.image.copy()
        draw = ImageDraw.Draw(image)
        for layer in self.layers:
            if layer.Kind != 2 or not layer.Visible or self.app.fault == "ignore_text":
                continue
            text = layer.TextItem
            if self.app.fault == "wrong_font":
                text.Font = "FallbackFont"
            left, top, _, _ = layer.Bounds
            color = tuple(round(getattr(text.Color.RGB, channel)) for channel in ("Red", "Green", "Blue"))
            font = ImageFont.load_default(size=max(1, round(text.Size)))
            draw.multiline_text((left, top), text.Contents.replace("\r", "\n"), font=font, fill=(*color, 255))
        return image

    def SaveAs(self, path, options, as_copy):
        assert as_copy is True
        path = Path(path)
        self.app.saved.append((self, path, deepcopy(options)))
        if self.app.fault == "missing_save":
            return
        if self.app.fault == "corrupt_save":
            path.write_bytes(b"not-a-valid-image")
            return
        rendered = self.render()
        if path.suffix == ".psd":
            assert options.Layers and options.EmbedColorProfile
            # Test-only PSD envelope plus explicit fake reopen state; not a real PSD writer.
            path.write_bytes(b"8BPS\x00\x01" + rendered.tobytes())
            self.app.saved_psds[str(path.resolve())] = (self.image.copy(), deepcopy(self.layers))
        else:
            assert options.Interlaced is False and options.Compression == 6
            rendered.save(path, "PNG")

    def Close(self, save_changes):
        assert save_changes == 2
        if self.app.fault == "close_failure" and self.path is None:
            raise RuntimeError("fixture close failed")
        self.closed = True
        self.app.closed.append(self)
        if self in self.app.Documents.items:
            self.app.Documents.items.remove(self)
        self.app.ActiveDocument = self.app.Documents.items[-1] if self.app.Documents.items else None


class FakePhotoshop:
    def __init__(self, fault=""):
        self.fault = fault
        self.Preferences = SimpleNamespace(RulerUnits=3, TypeUnits=2)
        self.DisplayDialogs = 1
        self.Documents = Collection()
        self.ActiveDocument = None
        self.Fonts = Collection([
            SimpleNamespace(PostScriptName="Fixture-Regular", Name="Fixture Regular", Family="Fixture", Style="Regular"),
            SimpleNamespace(PostScriptName="Fixture-Bold", Name="Fixture Bold", Family="Fixture", Style="Bold"),
            SimpleNamespace(PostScriptName="Other-Regular", Name="Other", Family="Other", Style="Regular"),
        ])
        self.opened, self.saved, self.closed, self.saved_psds = [], [], [], {}
        self.on_output_open = None

    def Open(self, path):
        path = Path(path).resolve()
        self.opened.append(path)
        if str(path) in self.saved_psds:
            image, layers = self.saved_psds[str(path)]
            document = Document(self, image.copy(), path, path.name)
            document.layers = deepcopy(layers)
            document.ArtLayers.items = document.layers.copy()
            if self.fault == "drop_psd_layer":
                document.layers = document.layers[:1]
                document.ArtLayers.items = document.layers.copy()
            if self.fault == "mutate_psd_text":
                for layer in document.layers:
                    if layer.Kind == 2:
                        layer.TextItem.Contents = "MUTATED"
        else:
            with Image.open(path) as opened:
                document = Document(self, opened.copy(), path, path.name)
        if path.name.startswith("edited"):
            if self.fault == "reopen_corrupt_pixels":
                document.image.putpixel((0, 0), (1, 2, 3, 4))
            if self.on_output_open:
                self.on_output_open(path)
        self.Documents.items.append(document)
        self.ActiveDocument = document
        return document

    @staticmethod
    def create_object(name):
        if name == "Photoshop.SolidColor":
            return SimpleNamespace(RGB=SimpleNamespace(Red=0, Green=0, Blue=0))
        assert name in {"Photoshop.PNGSaveOptions", "Photoshop.PhotoshopSaveOptions"}
        return SimpleNamespace()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "input.png"
    image = Image.new("RGBA", (160, 120))
    image.putdata([(x % 256, y % 256, (x * 3 + y) % 256, 255)
                   for y in range(120) for x in range(160)])
    image.save(path)
    return path


def runtime_for(app):
    return PhotoshopRuntime(connector=lambda: app, object_factory=app.create_object)


def request(source, operation, *, suffix=".png"):
    return {"source_path": str(source), "output_path": str(source.parent / ("result" + suffix)),
            "operations": operation if isinstance(operation, list) else [operation]}


def text_operation(**updates):
    return {"op": "text", "content": "QA", "font": "Fixture-Regular", "font_size": 12,
            "x": 40, "y": 40, "color": "#ff0033", **updates}


def result_artifact(details):
    return {"kind": "image" if details["output_format"] == "png" else "photoshop_document",
            "uri": details["output_path"], "metadata": {"role": "output", "verification_id": details["verification_id"]}}


def evidence(details):
    return [{"kind": "photoshop_edit_verification", "summary": "fixture COM re-open and pixel verification", "data": details}]


@pytest.mark.parametrize("operation,expected_size", [
    ({"op": "resize", "width": 100, "height": 80}, (100, 80)),
    ({"op": "crop", "left": 10, "top": 5, "right": 140, "bottom": 110}, (130, 105)),
    *[({"op": "rotate", "angle": angle}, (120, 160) if abs(angle) % 180 else (160, 120))
      for angle in (-270, -180, -90, 90, 180, 270)],
    (text_operation(), (160, 120)),
    (text_operation(align="center"), (160, 120)),
    (text_operation(align="right"), (160, 120)),
])
@pytest.mark.parametrize("suffix", [".png", ".psd"])
def test_supported_operations_produce_verified_non_destructive_copy(source, operation, expected_size, suffix):
    app = FakePhotoshop()
    original_hash = file_sha256(source)
    data = request(source, operation, suffix=suffix)
    details = runtime_for(app).edit_copy(data)
    assert (details["after"]["width"], details["after"]["height"]) == expected_size
    assert details["source_before_sha256"] == details["source_after_sha256"] == original_hash == file_sha256(source)
    assert Path(details["output_path"]).is_file()
    assert details["operations"][0]["pixel_verified"] is True
    assert details["operations"][0]["verified"] is True
    assert all(check["verified"] for check in verify_edit_evidence(evidence(details), [result_artifact(details)]).values())
    assert app.Documents.Count == 0
    assert app.Preferences.RulerUnits == 3 and app.Preferences.TypeUnits == 2 and app.DisplayDialogs == 1
    assert not list(source.parent.glob("anis-photoshop-*"))


def test_multi_operation_order_and_existing_user_documents_preserved(source, tmp_path):
    app = FakePhotoshop()
    source_document = app.Open(str(source))
    other_path = tmp_path / "other.png"
    Image.new("RGB", (20, 20), "red").save(other_path)
    previous_active = app.Open(str(other_path))
    operations = [{"op": "crop", "left": 10, "top": 20, "right": 140, "bottom": 110},
                  {"op": "rotate", "angle": 90}, {"op": "resize", "width": 120, "height": 100}, text_operation()]
    details = runtime_for(app).edit_copy(request(source, operations, suffix=".psd"))
    assert [item["operation"]["op"] for item in details["operations"]] == ["crop", "rotate", "resize", "text"]
    assert app.Documents.items == [source_document, previous_active]
    assert app.ActiveDocument is previous_active
    assert source_document.Saved and not source_document.closed and source_document.image.size == (160, 120)
    assert all(document is not source_document and document is not previous_active for document in app.closed)
    assert all(document is not source_document and document is not previous_active for document, _, _ in app.saved)
    assert details["text_layers"][0]["font"] == "Fixture-Regular"


@pytest.mark.parametrize("operation", [
    {"op": "resize", "width": True, "height": 80},
    {"op": "resize", "width": [80], "height": 80},
    {"op": "resize", "width": 0, "height": 80},
    {"op": "resize", "width": 16385, "height": 80},
    {"op": "resize", "width": 80, "height": 80, "code": "alert(1)"},
    {"op": "rotate", "angle": 45},
    {"op": "script", "code": "app.activeDocument.close()"},
    text_operation(font_size=float("nan")), text_operation(font_size=float("inf")),
    text_operation(content=" "), text_operation(color="red"), text_operation(font=""),
])
def test_unsupported_or_malformed_operations_rejected_before_com(source, operation):
    app = FakePhotoshop()
    with pytest.raises(ValueError):
        runtime_for(app).edit_copy(request(source, operation))
    assert app.opened == []
    assert not (source.parent / "result.png").exists()


@pytest.mark.parametrize("change", ["same_source", "existing_output", "missing_source", "missing_parent", "output_jpg", "input_wrong_extension", "empty_input", "no_operations", "extra_input"])
def test_paths_and_request_shape_fail_before_com(source, change):
    app = FakePhotoshop()
    data = request(source, {"op": "rotate", "angle": 90})
    if change == "same_source": data["output_path"] = str(source)
    elif change == "existing_output": Path(data["output_path"]).write_bytes(b"keep-me")
    elif change == "missing_source": data["source_path"] = str(source.parent / "absent.png")
    elif change == "missing_parent": data["output_path"] = str(source.parent / "absent" / "result.png")
    elif change == "output_jpg": data["output_path"] = str(source.parent / "result.jpg")
    elif change == "input_wrong_extension":
        bad = source.parent / "input.exe"; bad.write_bytes(source.read_bytes()); data["source_path"] = str(bad)
    elif change == "empty_input": source.write_bytes(b"")
    elif change == "no_operations": data["operations"] = []
    elif change == "extra_input": data["javascript"] = "arbitrary code"
    with pytest.raises((ValueError, OSError)):
        runtime_for(app).edit_copy(data)
    assert app.opened == []
    if change == "existing_output": assert Path(data["output_path"]).read_bytes() == b"keep-me"


@pytest.mark.parametrize("fault,operation,suffix", [
    ("ignore_resize", {"op": "resize", "width": 100, "height": 80}, ".png"),
    ("wrong_crop", {"op": "crop", "left": 10, "top": 5, "right": 140, "bottom": 100}, ".png"),
    ("wrong_rotation", {"op": "rotate", "angle": 90}, ".png"),
    ("ignore_text", text_operation(), ".png"),
    ("wrong_font", text_operation(), ".png"),
    ("duplicate_source", {"op": "rotate", "angle": 90}, ".png"),
    ("missing_save", {"op": "rotate", "angle": 90}, ".png"),
    ("corrupt_save", {"op": "rotate", "angle": 90}, ".png"),
    ("reopen_corrupt_pixels", {"op": "rotate", "angle": 90}, ".png"),
    ("drop_psd_layer", text_operation(), ".psd"),
    ("mutate_psd_text", text_operation(), ".psd"),
    ("close_failure", text_operation(), ".png"),
])
def test_ignored_corrupt_or_mismatched_native_results_never_publish(source, fault, operation, suffix):
    app = FakePhotoshop(fault)
    before_hash = file_sha256(source)
    data = request(source, operation, suffix=suffix)
    with pytest.raises((PhotoshopVerificationError, OSError)):
        runtime_for(app).edit_copy(data)
    assert not Path(data["output_path"]).exists()
    assert file_sha256(source) == before_hash
    assert app.Preferences.RulerUnits == 3 and app.Preferences.TypeUnits == 2 and app.DisplayDialogs == 1


@pytest.mark.parametrize("operation", [
    {"op": "crop", "left": 100, "top": 5, "right": 90, "bottom": 100},
    {"op": "crop", "left": 0, "top": 0, "right": 200, "bottom": 100},
    {"op": "resize", "width": 10000, "height": 10000},
    text_operation(font="NotInstalled"), text_operation(font="Fixture"),
    text_operation(x=159), text_operation(y=0), text_operation(y=121),
])
def test_bounds_memory_and_font_constraints_do_not_silently_fallback(source, operation):
    app = FakePhotoshop()
    with pytest.raises((ValueError, PhotoshopVerificationError)):
        runtime_for(app).edit_copy(request(source, operation))
    assert not (source.parent / "result.png").exists()
    assert app.Documents.Count == 0


def test_unsaved_source_document_is_not_edited_saved_or_closed(source):
    app = FakePhotoshop()
    document = app.Open(str(source))
    document.Saved = False
    with pytest.raises(PhotoshopVerificationError, match="저장하지 않은"):
        runtime_for(app).edit_copy(request(source, {"op": "rotate", "angle": 90}))
    assert app.Documents.items == [document] and app.ActiveDocument is document
    assert not document.Saved and not document.closed and app.saved == []


def test_no_visual_change_and_inverse_operations_are_not_completed(source):
    app = FakePhotoshop()
    for operations in ([{"op": "resize", "width": 160, "height": 120}],
                       [{"op": "rotate", "angle": 90}, {"op": "rotate", "angle": -90}]):
        with pytest.raises(PhotoshopVerificationError, match="픽셀 변경"):
            runtime_for(app).edit_copy(request(source, operations))
        assert not (source.parent / "result.png").exists()


def test_publish_race_does_not_overwrite_new_existing_output(source):
    app = FakePhotoshop()
    data = request(source, {"op": "rotate", "angle": 90})
    output = Path(data["output_path"])
    app.on_output_open = lambda _path: output.write_bytes(b"other-writer")
    with pytest.raises(FileExistsError):
        runtime_for(app).edit_copy(data)
    assert output.read_bytes() == b"other-writer"
    assert app.Documents.Count == 0


def test_source_change_during_processing_is_not_published(source):
    app = FakePhotoshop()
    app.on_output_open = lambda _path: source.write_bytes(b"external-writer-change")
    with pytest.raises(PhotoshopVerificationError, match="원본이 변경"):
        runtime_for(app).edit_copy(request(source, {"op": "rotate", "angle": 90}))
    assert not (source.parent / "result.png").exists()


def test_actual_font_identifiers_and_query_are_exposed_without_fallback():
    runtime = runtime_for(FakePhotoshop())
    fonts = runtime.fonts(query="fixture", limit=1)
    assert fonts == [{"postscript_name": "Fixture-Regular", "name": "Fixture Regular", "family": "Fixture", "style": "Regular"}]
    assert runtime.fonts(query="absent") == []
    for limit in (0, 101, True, "1"):
        with pytest.raises(ValueError): runtime.fonts(limit=limit)


def test_plugin_edit_returns_verified_output_only_and_source_is_input(source, monkeypatch):
    monkeypatch.setattr("plugins.photoshop.SafetyLayer.validate_path", lambda _path: (True, ""))
    app = FakePhotoshop()
    plugin = PhotoshopPlugin(runtime_for(app))
    opened = plugin.execute_tool("photoshop_open_document", {"path": str(source)})
    assert opened.status == ToolRunStatus.SUCCEEDED
    assert opened.artifacts[0].metadata["role"] == "input"
    result = plugin.execute_tool("photoshop_edit_document", request(source, text_operation()))
    assert result.status == ToolRunStatus.SUCCEEDED
    assert result.evidence[0].kind == "photoshop_edit_verification"
    assert result.artifacts[0].metadata["role"] == "output"
    assert result.artifacts[0].uri != str(source)
    assert result.evidence[0].data["verification_id"] == result.artifacts[0].metadata["verification_id"]


def test_plugin_failures_are_not_successful_shells(source, monkeypatch):
    monkeypatch.setattr("plugins.photoshop.SafetyLayer.validate_path", lambda _path: (True, ""))
    broken = PhotoshopPlugin(runtime_for(FakePhotoshop("ignore_resize")))
    result = broken.execute_tool("photoshop_edit_document", request(source, {"op": "resize", "width": 100, "height": 80}))
    assert result.status == ToolRunStatus.UNVERIFIED and result.artifacts == []
    assert result.evidence[0].kind == "photoshop_verification_failed"
    def unavailable(): raise PhotoshopUnavailable("Photoshop is not installed")
    missing = PhotoshopPlugin(PhotoshopRuntime(connector=unavailable, object_factory=lambda _name: None))
    result = missing.execute_tool("photoshop_edit_document", request(source, text_operation()))
    assert result.status == ToolRunStatus.FAILED and result.artifacts == []
    assert result.evidence[0].kind == "photoshop_unavailable"


def test_plugin_rejects_unsafe_paths_before_native_session(source, monkeypatch):
    monkeypatch.setattr("plugins.photoshop.SafetyLayer.validate_path", lambda _path: (False, "path forbidden"))
    app = FakePhotoshop()
    result = PhotoshopPlugin(runtime_for(app)).execute_tool("photoshop_edit_document", request(source, text_operation()))
    assert result.status == ToolRunStatus.FAILED and "path forbidden" in result.error
    assert app.opened == []


@pytest.mark.parametrize("mutation", ["no_report", "wrong_kind", "original_output", "wrong_id", "output_changed", "source_changed", "no_operations", "unverified_operation", "same_pixels", "not_reopened", "invalid_operation", "invalid_parameters", "broken_pixel_chain", "wrong_dimensions", "unbound_pixel_hash"])
def test_criterion_verification_requires_bound_edit_evidence(source, mutation):
    app = FakePhotoshop()
    details = runtime_for(app).edit_copy(request(source, {"op": "rotate", "angle": 90}))
    proof, artifacts = evidence(details), [result_artifact(details)]
    if mutation == "no_report": proof = []
    elif mutation == "wrong_kind": proof[0]["kind"] = "photoshop_document"
    elif mutation == "original_output": details["output_path"] = str(source); artifacts[0]["uri"] = str(source)
    elif mutation == "wrong_id": artifacts[0]["metadata"]["verification_id"] = "another-edit"
    elif mutation == "output_changed": Path(details["output_path"]).write_bytes(b"changed")
    elif mutation == "source_changed": source.write_bytes(b"changed")
    elif mutation == "no_operations": details["operations"] = []
    elif mutation == "unverified_operation": details["operations"][0]["pixel_verified"] = False
    elif mutation == "same_pixels": details["after"]["pixel_sha256"] = details["before"]["pixel_sha256"]
    elif mutation == "not_reopened": details["output_reopened"] = False
    elif mutation == "invalid_operation": details["operations"][0]["operation"] = {"op": "script"}
    elif mutation == "invalid_parameters": details["operations"][0]["operation"] = {"op": "rotate", "angle": 45}
    elif mutation == "broken_pixel_chain": details["operations"][0]["before_pixel_sha256"] = "0" * 64
    elif mutation == "wrong_dimensions": details["operations"][0]["after_size"] = [1, 1]
    elif mutation == "unbound_pixel_hash":
        details["after"]["pixel_sha256"] = "0" * 64
        details["operations"][0]["after_pixel_sha256"] = "0" * 64
    assert not all(item["verified"] for item in verify_edit_evidence(proof, artifacts).values())


def test_edit_tool_schema_is_strict_and_never_auto_retries():
    tool = next(tool for tool in PhotoshopPlugin().get_tools() if tool.name == "photoshop_edit_document")
    assert tool.input_schema is EDIT_INPUT_SCHEMA
    assert tool.max_retries == 0 and tool.cancellable is False and tool.side_effect == "change"
    assert {"filesystem_read", "filesystem_write", "windows_api"} <= set(tool.required_permissions)


@pytest.mark.parametrize("suffix,format_name", [(".jpg", "JPEG"), (".jpeg", "JPEG"), (".bmp", "BMP"), (".tif", "TIFF"), (".tiff", "TIFF")])
def test_supported_raster_sources_are_opened_and_saved_as_separate_output(source, suffix, format_name):
    input_path = source.with_suffix(suffix)
    with Image.open(source) as image:
        image.convert("RGB").save(input_path, format_name)
    original_hash = file_sha256(input_path)
    result = runtime_for(FakePhotoshop()).edit_copy(request(input_path, {"op": "rotate", "angle": 90}))
    assert result["source_before_sha256"] == original_hash == file_sha256(input_path)
    assert Path(result["output_path"]).suffix == ".png"


def test_psd_source_preserves_existing_layers_on_copy(source):
    app = FakePhotoshop()
    original_document = app.Open(str(source))
    psd_path = source.with_suffix(".psd")
    PhotoshopRuntime._save(original_document, psd_path, app.create_object)
    original_hash = file_sha256(psd_path)
    result = runtime_for(app).edit_copy(request(psd_path, text_operation(), suffix=".psd"))
    assert result["source_before_sha256"] == original_hash == file_sha256(psd_path)
    assert result["text_layers"][0]["content"] == "QA"


def test_native_com_unavailable_is_explicit_and_thread_initialization_is_balanced(source, monkeypatch):
    import sys
    import core.photoshop_runtime as module
    if module.os.name != "nt":
        with pytest.raises(PhotoshopUnavailable, match="Windows"):
            PhotoshopRuntime().edit_copy(request(source, {"op": "rotate", "angle": 90}))
        return
    calls = []
    pythoncom = SimpleNamespace(CoInitialize=lambda: calls.append("initialize"), CoUninitialize=lambda: calls.append("uninitialize"))
    def no_com(_name):
        calls.append("dispatch")
        raise RuntimeError("class not registered")
    win32client = SimpleNamespace(Dispatch=no_com)
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=win32client))
    monkeypatch.setitem(sys.modules, "win32com.client", win32client)
    with pytest.raises(PhotoshopUnavailable, match="연결하지 못"):
        PhotoshopRuntime().edit_copy(request(source, {"op": "rotate", "angle": 90}))
    assert calls == ["initialize", "dispatch", "uninitialize"]


def test_workspace_criterion_results_distinguish_saved_visual_and_source(source):
    from core.specialist_team import SpecialistTeamRuntime
    from core.specialist_workspaces import get_specialist_workspace_registry
    details = runtime_for(FakePhotoshop()).edit_copy(request(source, {"op": "rotate", "angle": 90}))
    contract = get_specialist_workspace_registry().get("photoshop").execution_contract()
    payload = {"status": "completed", "tool_status": "succeeded", "tool_name": "photoshop_edit_document",
               "evidence": evidence(details), "artifacts": [result_artifact(details)]}
    review = SpecialistTeamRuntime._review_execution(payload, contract)
    assert review["passed"] and all(item["verified"] for item in review["criteria_results"])
    source.write_bytes(b"externally changed source")
    review = SpecialistTeamRuntime._review_execution(payload, contract)
    assert not review["passed"]
    results = {item["verification"]: item["verified"] for item in review["criteria_results"]}
    assert results == {"photoshop_saved_copy": True, "photoshop_visual_change": True, "photoshop_source_preserved": False}


@pytest.mark.parametrize("utterance", [
    "포토샵에서 사진 크기를 400x300으로 바꿔줘",
    "포토샵에서 사진을 오른쪽으로 90도 회전해줘",
    "포토샵으로 사진에서 왼쪽 10픽셀부터 오른쪽 100픽셀까지 잘라줘",
    "포토샵 사진에 빨간색 QA 문구를 추가해줘",
    "Resize this photo in Photoshop and save a separate PNG copy",
])
def test_natural_edit_requests_reach_planner_tool_not_open_shortcut(utterance):
    from core.plugin import PluginRegistry
    from core.intent_router import IntentRouter
    from core.tool_loadout import ToolLoadoutSelector
    registry = PluginRegistry()
    registry.register_plugin(PhotoshopPlugin())
    resolution = IntentRouter(registry).resolve(utterance)
    assert resolution.intent_name != "photoshop.open"
    loadout = ToolLoadoutSelector(registry).select(utterance, resolution)
    assert "photoshop_edit_document" in loadout.tool_names
