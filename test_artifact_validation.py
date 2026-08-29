import json
import zipfile

from PIL import Image

from core.artifact_validation import validate_local_artifact
from core.specialist_team import SpecialistTeamRuntime


def _office(path, marker: str):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(marker, "<root/>")


def test_validates_real_image_json_svg_and_office_packages(tmp_path):
    image = tmp_path / "preview.png"
    Image.new("RGB", (8, 8), "red").save(image)
    data = tmp_path / "result.json"
    data.write_text(json.dumps({"ok": True}), encoding="utf-8")
    svg = tmp_path / "vector.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    document = tmp_path / "report.docx"
    _office(document, "word/document.xml")

    for path in (image, data, svg, document):
        assert validate_local_artifact(path)[0], path
        assert SpecialistTeamRuntime._artifact_exists({"kind": "file", "uri": str(path)})


def test_rejects_mislabeled_and_incomplete_deliverables(tmp_path):
    broken = {
        "fake.png": b"not an image",
        "fake.pdf": b"%PDF-1.7\nnot complete",
        "fake.json": b"{broken",
        "fake.svg": b"<html/>",
    }
    for name, payload in broken.items():
        path = tmp_path / name
        path.write_bytes(payload)
        assert not validate_local_artifact(path)[0], name
        assert not SpecialistTeamRuntime._artifact_exists({"kind": "file", "uri": str(path)})

    incomplete = tmp_path / "fake.xlsx"
    _office(incomplete, "word/document.xml")
    assert not validate_local_artifact(incomplete)[0]


def test_remote_and_memory_artifact_contracts_remain_non_file_resources():
    assert SpecialistTeamRuntime._artifact_exists(
        {"kind": "url", "uri": "https://example.com/result"}
    )
    assert SpecialistTeamRuntime._artifact_exists(
        {"kind": "semantic_memory", "uri": "memory-1"}
    )
