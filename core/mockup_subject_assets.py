"""Non-destructive source preparation shared by every mockup renderer."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import tempfile

from PIL import Image

from core.mockup_scene import ScenePlanError
from core.mockup_subject_runtime import SubjectAnalysisRuntime


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def prepared_subject_sources(plan: dict, source_paths, runtime):
    """Yield alpha-prepared copies; never mutate or replace production sources.

    Temporary PNGs exist until both the raster preview and embedded SVG are
    rendered. Only original source paths belong in the editable document; the
    remove_background flag persists through undo/redo and subsequent edits.
    """
    paths = [Path(path).resolve() for path in source_paths]
    if any("remove_background" in asset and not isinstance(asset["remove_background"], bool)
           for asset in plan.get("assets", [])):
        raise ScenePlanError("사진 배경 제거 속성은 true/false여야 합니다.")
    requested = sorted({int(asset["index"]) for asset in plan.get("assets", [])
                        if asset.get("remove_background") is True})
    if not requested:
        yield paths, []
        return
    if any(index < 0 or index >= len(paths) for index in requested):
        raise ScenePlanError("배경을 제거할 제작 이미지 번호가 올바르지 않습니다.")
    evidence = []
    try:
        with tempfile.TemporaryDirectory(prefix="anis_mockup_subject_") as directory:
            effective = list(paths)
            for index in requested:
                original = paths[index]
                source_hash = _sha256(original)
                with Image.open(original) as source:
                    image = source.convert("RGBA")
                alpha = image.getchannel("A")
                # Transparent corners may be a circular photo frame, not a
                # segmented subject. Alpha structure alone is never evidence
                # that the requested background has already been removed.
                result = runtime.segment_with_evidence(original)
                if not result.success:
                    raise ScenePlanError("사진 배경 제거를 완료하지 못했습니다: " + result.error)
                # Independently reject a backend/adapter that claims success
                # while returning an empty, opaque, or unchanged bitmap.
                try:
                    mask, measured = SubjectAnalysisRuntime._validate_mask(result.mask, alpha)
                except ValueError as exc:
                    raise ScenePlanError(f"사진 배경 제거 결과 검증 실패: {exc}") from exc
                details = result.evidence()
                details["render_input_validation"] = measured
                if _sha256(original) != source_hash:
                    raise ScenePlanError("피사체 처리 중 원본 이미지가 변경되어 렌더링을 중단했습니다.")
                image.putalpha(mask)
                target = Path(directory) / f"subject_{index}.png"
                image.save(target, "PNG")
                effective[index] = target
                evidence.append({**details, "source_index": index,
                                 "source_sha256": source_hash,
                                 "mask_sha256": hashlib.sha256(mask.tobytes()).hexdigest()})
            yield effective, evidence
    finally:
        runtime.release()
