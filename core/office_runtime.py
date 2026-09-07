"""Template-preserving Office edits, COM live control and render-based QA."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class PackageSnapshot:
    path: str
    parts: Dict[str, str]


class OfficePackageInspector:
    PRESERVED_PREFIXES = (
        "word/styles", "word/theme/", "word/media/", "word/fontTable",
        "xl/styles", "xl/theme/", "xl/media/", "xl/drawings/",
        "ppt/theme/", "ppt/media/", "ppt/slideMasters/", "ppt/slideLayouts/",
        "Contents/header.xml", "BinData/", "Preview/", "settings.xml",
    )

    @staticmethod
    def snapshot(path: str | Path) -> PackageSnapshot:
        target = Path(path).resolve()
        with zipfile.ZipFile(target) as archive:
            parts = {
                name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in archive.namelist() if not name.endswith("/")
            }
        return PackageSnapshot(str(target), parts)

    @classmethod
    def verify_preserved(cls, before: PackageSnapshot, after: PackageSnapshot) -> Dict[str, Any]:
        protected = {name for name in before.parts if name.startswith(cls.PRESERVED_PREFIXES)}
        missing = sorted(name for name in protected if name not in after.parts)
        changed = sorted(name for name in protected if name in after.parts and before.parts[name] != after.parts[name])
        if missing or changed:
            raise ValueError(f"서식·테마·미디어 보존 검증 실패: missing={missing}, changed={changed}")
        return {"protected_parts": len(protected), "missing": missing, "changed": changed}


class AtomicOfficeEditor:
    @staticmethod
    def edit(path: str | Path, callback: Callable[[Path], Any]) -> tuple[Any, Dict[str, Any]]:
        target = Path(path).resolve()
        if not target.is_file(): raise FileNotFoundError(target)
        before = OfficePackageInspector.snapshot(target)
        temp_dir = Path(tempfile.mkdtemp(prefix="jarvis-office-", dir=str(target.parent)))
        temporary = temp_dir / target.name
        try:
            shutil.copy2(target, temporary)
            result = callback(temporary)
            after = OfficePackageInspector.snapshot(temporary)
            preservation = OfficePackageInspector.verify_preserved(before, after)
            with open(temporary, "r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            return result, {
                **preservation, "path": str(target), "size": target.stat().st_size,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @classmethod
    def replace_text(cls, path: str | Path, replacements: Dict[str, str]) -> Dict[str, Any]:
        suffix = Path(path).suffix.casefold()
        if suffix == ".docx": return cls._replace_docx(path, replacements)
        if suffix == ".pptx": return cls._replace_pptx(path, replacements)
        if suffix == ".xlsx": return cls._replace_xlsx(path, replacements)
        if suffix == ".hwpx": return cls._replace_hwpx(path, replacements)
        raise ValueError(f"지원하지 않는 Office 형식: {suffix}")

    @classmethod
    def _replace_docx(cls, path, replacements):
        from docx import Document
        def edit(temp):
            document, count = Document(temp), 0
            paragraphs = list(document.paragraphs)
            for table in document.tables:
                for row in table.rows:
                    for cell in row.cells: paragraphs.extend(cell.paragraphs)
            for paragraph in paragraphs:
                count += cls._replace_runs(paragraph.runs, replacements)
            document.save(temp)
            return count
        count, evidence = cls.edit(path, edit)
        return {**evidence, "replacement_count": count, "format": "docx"}

    @classmethod
    def _replace_pptx(cls, path, replacements):
        from pptx import Presentation
        def edit(temp):
            presentation, count = Presentation(temp), 0
            for slide in presentation.slides:
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        for paragraph in shape.text_frame.paragraphs:
                            count += cls._replace_runs(paragraph.runs, replacements)
                    if getattr(shape, "has_table", False):
                        for row in shape.table.rows:
                            for cell in row.cells:
                                for paragraph in cell.text_frame.paragraphs:
                                    count += cls._replace_runs(paragraph.runs, replacements)
            presentation.save(temp)
            return count
        count, evidence = cls.edit(path, edit)
        return {**evidence, "replacement_count": count, "format": "pptx"}

    @classmethod
    def _replace_xlsx(cls, path, replacements):
        from openpyxl import load_workbook
        def edit(temp):
            workbook, count = load_workbook(temp), 0
            for sheet in workbook.worksheets:
                for row in sheet.iter_rows():
                    for cell in row:
                        if isinstance(cell.value, str):
                            value = cell.value
                            for old, new in replacements.items():
                                if old in value:
                                    occurrences = value.count(old); value = value.replace(old, new); count += occurrences
                            cell.value = value
            workbook.save(temp)
            return count
        count, evidence = cls.edit(path, edit)
        return {**evidence, "replacement_count": count, "format": "xlsx"}

    @classmethod
    def _replace_hwpx(cls, path, replacements):
        def edit(temp):
            output = temp.with_suffix(".new.hwpx"); count = 0
            with zipfile.ZipFile(temp) as source, zipfile.ZipFile(output, "w") as target:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename.startswith("Contents/") and info.filename.endswith(".xml"):
                        text = data.decode("utf-8")
                        for old, new in replacements.items():
                            occurrences = text.count(old); text = text.replace(old, new); count += occurrences
                        data = text.encode("utf-8")
                    target.writestr(info, data)
            os.replace(output, temp)
            return count
        count, evidence = cls.edit(path, edit)
        return {**evidence, "replacement_count": count, "format": "hwpx"}

    @staticmethod
    def _replace_runs(runs: Iterable[Any], replacements: Dict[str, str]) -> int:
        runs = list(runs)
        if not runs: return 0
        count = 0
        for run in runs:
            value = run.text
            for old, new in replacements.items():
                occurrences = value.count(old)
                if occurrences:
                    value, count = value.replace(old, new), count + occurrences
            run.text = value
        if count:
            return count
        original = "".join(run.text for run in runs)
        updated = original
        for old, new in replacements.items():
            occurrences = updated.count(old)
            if occurrences: updated, count = updated.replace(old, new), count + occurrences
        if updated == original: return 0
        runs[0].text = updated
        for run in runs[1:]: run.text = ""
        return count


class OfficeRenderer:
    PROG_IDS = {".docx": "Word.Application", ".xlsx": "Excel.Application", ".pptx": "PowerPoint.Application"}

    @classmethod
    def render_pdf(
        cls, source: str | Path, output_pdf: str | Path, *, protect_existing_application: bool = False,
    ) -> Dict[str, Any]:
        source, output = Path(source).resolve(), Path(output_pdf).resolve()
        if source.suffix.casefold() not in cls.PROG_IDS: raise ValueError("DOCX/XLSX/PPTX 렌더링만 지원합니다.")
        output.parent.mkdir(parents=True, exist_ok=True)
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        application = document = None
        try:
            suffix = source.suffix.casefold()
            application = win32com.client.DispatchEx(cls.PROG_IDS[suffix])
            if not protect_existing_application:
                application.Visible = False
            if suffix == ".docx":
                document = application.Documents.Open(str(source), ReadOnly=True, AddToRecentFiles=False, Visible=False)
                document.ExportAsFixedFormat(str(output), 17)
            elif suffix == ".xlsx":
                document = application.Workbooks.Open(str(source), ReadOnly=True, UpdateLinks=0)
                document.ExportAsFixedFormat(0, str(output))
            else:
                document = application.Presentations.Open(str(source), ReadOnly=True, WithWindow=False)
                document.SaveAs(str(output), 32)
            return cls.verify_pdf(output)
        finally:
            if document is not None:
                try:
                    if source.suffix.casefold() == ".pptx":
                        document.Close()
                    else:
                        document.Close(False)
                except Exception: pass
            # DispatchEx is not proof of a private server (PowerPoint can
            # share one). In isolated rendering only our unique snapshot
            # document may be closed; never quit/hide an existing user app.
            if application is not None and not protect_existing_application:
                try: application.Quit()
                except Exception: pass
            pythoncom.CoUninitialize()

    @staticmethod
    def verify_pdf(path: str | Path) -> Dict[str, Any]:
        import fitz
        target = Path(path).resolve()
        document = fitz.open(target)
        try:
            if document.page_count < 1: raise ValueError("렌더링 PDF에 페이지가 없습니다.")
            previews, nonblank = [], 0
            for index in range(min(document.page_count, 10)):
                page = document[index]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), alpha=False)
                preview = target.with_name(f"{target.stem}-page-{index + 1}.png")
                pixmap.save(preview); previews.append(str(preview))
                samples = pixmap.samples
                if samples and min(samples) < 245: nonblank += 1
            if nonblank == 0: raise ValueError("렌더링 결과가 모두 빈 페이지입니다.")
            return {"pdf": str(target), "pages": document.page_count, "nonblank_pages": nonblank,
                    "previews": previews, "size": target.stat().st_size,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
        finally:
            document.close()


class OfficeComAdapter:
    PROG_IDS = {"word": "Word.Application", "excel": "Excel.Application",
                "powerpoint": "PowerPoint.Application", "hwp": "HWPFrame.HwpObject"}

    @classmethod
    def available(cls, app: str) -> bool:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, cls.PROG_IDS[app] + r"\CLSID") as key:
                class_id = winreg.QueryValueEx(key, "")[0]
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{class_id}\LocalServer32") as key:
                command = str(winreg.QueryValueEx(key, "")[0]).strip()
            executable = command.split('"')[1] if command.startswith('"') else command.split()[0]
            return Path(os.path.expandvars(executable)).is_file()
        except OSError:
            return False

    @classmethod
    def open_document(cls, app: str, path: str) -> Dict[str, Any]:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        target = Path(path).resolve()
        application = win32com.client.Dispatch(cls.PROG_IDS[app])
        if app == "word": document = application.Documents.Open(str(target))
        elif app == "excel": document = application.Workbooks.Open(str(target))
        elif app == "powerpoint": document = application.Presentations.Open(str(target))
        else:
            application.RegisterModule("FilePathCheckDLL", "FilePathCheckerModuleExample")
            application.Open(str(target)); document = application
        try: application.Visible = True
        except Exception: pass
        pythoncom.CoUninitialize()
        return {"app": app, "path": str(target), "opened": document is not None}
