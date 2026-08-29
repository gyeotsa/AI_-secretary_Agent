"""Content-aware local artifact validation shared by specialist workspaces."""
from __future__ import annotations

from pathlib import Path
import json
import wave
import zipfile
import xml.etree.ElementTree as ET


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
_OFFICE_ZIP_MARKERS = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml",
    ".hwpx": "Contents/content.hpf",
}


def validate_local_artifact(path: str | Path) -> tuple[bool, str]:
    """Reject empty, corrupt, mislabeled, or structurally incomplete deliverables."""
    target = Path(path)
    try:
        if not target.is_file():
            return False, "일반 파일이 아닙니다."
        if target.stat().st_size <= 0:
            return False, "파일이 비어 있습니다."
        suffix = target.suffix.casefold()
        if suffix in _IMAGE_SUFFIXES:
            from PIL import Image
            with Image.open(target) as image:
                image.verify()
            with Image.open(target) as image:
                image.load()
                if image.width < 1 or image.height < 1:
                    return False, "이미지 크기가 유효하지 않습니다."
            return True, "이미지 디코딩과 픽셀 로드를 확인했습니다."
        if suffix == ".pdf":
            payload = target.read_bytes()
            if not payload.startswith(b"%PDF-") or not payload.rstrip().endswith(b"%%EOF"):
                return False, "PDF 파일 봉투가 손상되었습니다."
            try:
                from pypdf import PdfReader
                if len(PdfReader(str(target)).pages) < 1:
                    return False, "PDF에 페이지가 없습니다."
            except ImportError:
                pass
            return True, "PDF 봉투와 페이지 구조를 확인했습니다."
        if suffix in _OFFICE_ZIP_MARKERS:
            with zipfile.ZipFile(target) as archive:
                if archive.testzip() is not None:
                    return False, "압축 패키지 CRC가 손상되었습니다."
                names = set(archive.namelist())
                marker = _OFFICE_ZIP_MARKERS[suffix]
                if "[Content_Types].xml" not in names or marker not in names:
                    return False, f"{suffix} 필수 문서 항목이 없습니다."
            return True, f"{suffix} 패키지와 필수 문서 항목을 확인했습니다."
        if suffix == ".svg":
            root = ET.parse(target).getroot()
            if not root.tag.casefold().endswith("svg"):
                return False, "SVG 루트 요소가 없습니다."
            return True, "SVG XML 구조를 확인했습니다."
        if suffix == ".json":
            json.loads(target.read_text(encoding="utf-8-sig"))
            return True, "JSON 구문을 확인했습니다."
        if suffix == ".wav":
            with wave.open(str(target), "rb") as audio:
                if audio.getnframes() <= 0 or audio.getframerate() <= 0:
                    return False, "WAV 오디오 프레임이 없습니다."
            return True, "WAV 헤더와 오디오 프레임을 확인했습니다."
        return True, "비어 있지 않은 로컬 파일을 확인했습니다."
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile,
            ET.ParseError, wave.Error) as exc:
        return False, f"파일 형식을 읽지 못했습니다: {exc}"
    except Exception as exc:
        return False, f"파일 검증에 실패했습니다: {exc}"
