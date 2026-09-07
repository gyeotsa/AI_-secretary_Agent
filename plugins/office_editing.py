"""Template-preserving Office editing and render QA tools."""
import hashlib
from pathlib import Path
from typing import Any, Dict, List

from core.artifact_transaction import commit_artifact_bundle
from core.harness import SafetyLayer
from core.office_runtime import AtomicOfficeEditor, OfficeComAdapter, OfficeRenderer
from core.plugin import BasePlugin, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult


class OfficeEditingPlugin(BasePlugin):
    supports_process_isolation = True

    def __init__(self):
        super().__init__(); self.name = "office_editing"; self.version = "1.0.0"
        self.description = "Office 템플릿 보존 편집·PDF 렌더링 QA·COM 라이브 제어"
        self.dependencies = ["win32com"] ; self.supported_os = ["Windows"]

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("office_replace_template_text", "기존 서식·테마·미디어를 보존하며 텍스트를 치환합니다", {
                "type":"object","properties":{"path":{"type":"string"},"replacements":{"type":"object","additionalProperties":{"type":"string"}}},
                "required":["path","replacements"],"additionalProperties":False}, ["filesystem_write"], side_effect="change"),
            ToolSchema("office_render_visual_qa", "Office 문서를 PDF와 페이지 PNG로 렌더링해 빈 페이지 여부를 검증합니다", {
                "type":"object","properties":{"path":{"type":"string"},"output_pdf":{"type":"string"}},
                "required":["path","output_pdf"],"additionalProperties":False}, ["filesystem_read","filesystem_write"],
                side_effect="change", timeout_seconds=180, cancellable=True, execution_isolation="process"),
            ToolSchema("office_com_status", "Office·HWP COM 설치와 연결 가능 상태를 확인합니다", {
                "type":"object","properties":{},"additionalProperties":False}, ["windows_api"], side_effect="read"),
            ToolSchema("office_com_open", "설치된 Office/HWP 앱에서 문서를 실제로 엽니다", {
                "type":"object","properties":{"app":{"enum":["word","excel","powerpoint","hwp"]},"path":{"type":"string"}},
                "required":["app","path"],"additionalProperties":False}, ["windows_api","filesystem_read"], side_effect="execute"),
        ]

    @staticmethod
    def _safe(path):
        target=Path(path).expanduser().resolve(); ok,error=SafetyLayer.validate_path(str(target))
        if not ok: raise ValueError(error)
        return target

    def prepare_isolated_input(self, tool_name, tool_input, staging_directory):
        if tool_name != "office_render_visual_qa":
            return super().prepare_isolated_input(tool_name, tool_input, staging_directory)
        source, destination = self._safe(tool_input["path"]), self._safe(tool_input["output_pdf"])
        if not source.is_file() or source.suffix.casefold() not in OfficeRenderer.PROG_IDS:
            raise ValueError("실제 DOCX/XLSX/PPTX 파일이 필요합니다.")
        if destination.suffix.casefold() != ".pdf" or destination == source:
            raise ValueError("렌더링 결과는 원본과 다른 PDF 경로여야 합니다.")
        stage = Path(staging_directory).resolve() / "artifacts"
        stage.mkdir()
        snapshot_dir = stage.parent / "source"
        snapshot_dir.mkdir()
        snapshot = snapshot_dir / source.name
        context = self.get_execution_context()
        copied = 0
        with source.open("rb") as incoming, snapshot.open("wb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                if context is not None:
                    context.raise_if_cancelled()
                copied += len(chunk)
                if copied > 256 * 1024 * 1024:
                    raise ValueError("Office 렌더링 원본의 안전 처리 크기를 초과했습니다.")
                outgoing.write(chunk)
        # The COM worker is never given the user's final output path. A late
        # COM response can at most write to this private staging directory.
        # A uniquely named input snapshot also prevents Documents.Open from
        # reusing (and our cleanup closing) a user's already-open document.
        return {"path": str(snapshot), "output_pdf": str(stage / destination.name)}

    def finalize_isolated_result(self, tool_name, original_input, isolated_input, result):
        if tool_name != "office_render_visual_qa" or not isinstance(result, ToolRunResult) or not result.succeeded:
            return result
        context = self.get_execution_context()
        if context is None or not context.staging_directory:
            raise ValueError("격리 실행의 산출물 컨텍스트가 없습니다.")
        stage = (Path(context.staging_directory) / "artifacts").resolve()
        staged_pdf = Path(isolated_input["output_pdf"]).resolve()
        destination = self._safe(original_input["output_pdf"])
        evidence = next((item for item in result.evidence if item.kind == "rendered_office"), None)
        if evidence is None:
            raise ValueError("렌더링 검증 증거가 없습니다.")
        details = dict(evidence.data)
        previews = details.get("previews")
        pages, nonblank = details.get("pages"), details.get("nonblank_pages")
        if (not isinstance(previews, list) or not previews or len(previews) > 10
                or type(pages) is not int or type(nonblank) is not int
                or pages < 1 or nonblank < 1 or len(previews) != min(pages, 10)):
            raise ValueError("렌더링 페이지 검증 증거가 올바르지 않습니다.")
        if Path(str(details.get("pdf", ""))).resolve() != staged_pdf:
            raise ValueError("PDF 검증 대상과 요청한 산출물이 다릅니다.")
        files = [staged_pdf, *[Path(item).resolve() for item in previews if isinstance(item, str)]]
        if len(files) != len(previews) + 1 or len(set(files)) != len(files):
            raise ValueError("렌더링 산출물 목록에 중복 또는 잘못된 경로가 있습니다.")
        if any(path.parent != stage or not path.is_file() for path in files):
            raise ValueError("격리 임시 폴더 밖의 산출물은 저장할 수 없습니다.")
        if {Path(item.uri).resolve() for item in result.artifacts} != set(files):
            raise ValueError("검증 증거와 실제 산출물 목록이 다릅니다.")
        hashes = details.get("preview_sha256", {})
        if not isinstance(hashes, dict):
            raise ValueError("페이지 이미지 검증 해시가 없습니다.")
        expected_hashes = {staged_pdf: str(details.get("sha256", ""))}
        for index, preview in enumerate(files[1:], 1):
            if preview.name != f"{staged_pdf.stem}-page-{index}.png":
                raise ValueError("페이지 이미지 파일명이 검증 순서와 일치하지 않습니다.")
            expected_hashes[preview] = str(hashes.get(str(preview), ""))

        mapping = {
            path: self._safe(destination if path == staged_pdf else destination.with_name(path.name))
            for path in files
        }
        # Finish format, size and worker-provided hash validation before the
        # first visible destination change.  The worker files remain private
        # stages; commit_artifact_bundle seals them again, prepares same-volume
        # copies and rolls back transaction-owned replacements on failure.
        for source in files[1:] + files[:1]:
            context.raise_if_cancelled()
            target = mapping[source]
            limit = 128 * 1024 * 1024 if source == staged_pdf else 32 * 1024 * 1024
            if source.stat().st_size > limit:
                raise ValueError("렌더링 산출물의 안전 저장 크기를 초과했습니다.")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            first = True
            copied = 0
            with source.open("rb") as incoming:
                while chunk := incoming.read(1024 * 1024):
                    context.raise_if_cancelled()
                    copied += len(chunk)
                    if copied > limit:
                        raise ValueError("렌더링 산출물의 안전 저장 크기를 초과했습니다.")
                    if first:
                        signature = b"%PDF-" if source == staged_pdf else b"\x89PNG\r\n\x1a\n"
                        if not chunk.startswith(signature):
                            raise ValueError("렌더링 산출물의 파일 형식이 올바르지 않습니다.")
                        first = False
                    digest.update(chunk)
            if first or digest.hexdigest() != expected_hashes[source]:
                raise ValueError("렌더링 완료 이후 산출물이 변경됐거나 검증 해시가 일치하지 않습니다.")

        # Publish previews first and the PDF last as a useful reader hint, but
        # consistency does not rely on that ordering.  A cancellation before
        # this guard changes nothing.  Once publication starts it is allowed to
        # complete; stopping between per-file replaces would recreate a mixed
        # revision.  This is rollback-backed process atomicity, not filesystem
        # or power-loss whole-bundle atomicity.
        publication_mapping = {
            source: mapping[source] for source in files[1:] + files[:1]
        }
        with context.publication_guard():
            publication = commit_artifact_bundle(publication_mapping)

        uri_mapping = {str(source): str(target) for source, target in mapping.items()}
        details["pdf"] = str(destination)
        details["previews"] = [uri_mapping[str(item)] for item in files[1:]]
        details["preview_sha256"] = {uri_mapping[str(item)]: expected_hashes[item] for item in files[1:]}
        details["publication"] = {
            str(target): dict(publication[target]) for target in publication_mapping.values()
        }
        details["publication_contract"] = {
            "prevalidated_bundle": True,
            "rollback_on_process_failure": True,
            "atomicity": "per_file_with_bundle_rollback",
            "crash_or_power_loss_atomicity": False,
        }
        result.evidence = [Evidence(item.kind, item.summary, details if item is evidence else dict(item.data))
                           for item in result.evidence]
        result.artifacts = [Artifact(item.kind, uri_mapping[str(Path(item.uri).resolve())], dict(item.metadata))
                            for item in result.artifacts]
        return result

    def execute_tool(self, name: str, data: Dict[str, Any]):
        try:
            if name == "office_replace_template_text":
                path=self._safe(data["path"]); details=AtomicOfficeEditor.replace_text(path, dict(data["replacements"]))
                if details["replacement_count"] < 1: return ToolRunResult.failed(tool_name=name,error="치환할 원문을 찾지 못했습니다.")
                return ToolRunResult.successful(tool_name=name,raw_output="Office 템플릿 텍스트를 수정했습니다.",
                    evidence=[Evidence("office_package_preservation","서식·테마·미디어 part와 결과 해시를 검증했습니다.",details)],
                    artifacts=[Artifact("office_document",str(path),details)])
            if name == "office_render_visual_qa":
                source=self._safe(data["path"]); output=self._safe(data["output_pdf"])
                details=OfficeRenderer.render_pdf(source, output, protect_existing_application=True)
                details["preview_sha256"] = {item: hashlib.sha256(Path(item).read_bytes()).hexdigest()
                                             for item in details["previews"]}
                return ToolRunResult.successful(tool_name=name,raw_output="Office 렌더링 시각 QA를 통과했습니다.",
                    evidence=[Evidence("rendered_office","PDF 페이지 수와 비어 있지 않은 Preview를 확인했습니다.",details)],
                    artifacts=[Artifact("pdf",details["pdf"]),*[Artifact("image",item) for item in details["previews"]]])
            if name == "office_com_status":
                status={app:OfficeComAdapter.available(app) for app in OfficeComAdapter.PROG_IDS}
                return ToolRunResult.successful(tool_name=name,raw_output=str(status),
                    evidence=[Evidence("office_com_registry","각 앱 COM ProgID 연결 가능 여부를 확인했습니다.",status)])
            if name == "office_com_open":
                path=self._safe(data["path"]); details=OfficeComAdapter.open_document(data["app"],str(path))
                return ToolRunResult.successful(tool_name=name,raw_output=f"{data['app']}에서 문서를 열었습니다.",
                    evidence=[Evidence("office_com_document","COM 문서 객체 생성을 확인했습니다.",details)],
                    artifacts=[Artifact("office_document",str(path),{"app":data["app"]})])
            return ToolRunResult.failed(tool_name=name,error="지원하지 않는 Office 도구입니다.")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=name,error=str(exc))
