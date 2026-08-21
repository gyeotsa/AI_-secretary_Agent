"""Capability-based routing for the local mockup specialist team."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MockupRoute:
    planner: str
    renderer: str
    subject_processing: str
    image_generation: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


def route_mockup_request(instruction: str, *, backend: str = "auto",
                         segmentation_ready: bool = False) -> MockupRoute:
    """Choose capabilities, not named templates or one-off Korean sentences."""
    text = " ".join(str(instruction or "").casefold().split())
    subject_operation = bool(re.search(
        r"배경\s*(?:제거|삭제)|누끼|인물\s*(?:분리|추출)|머리카락|투명\s*배경", text
    ))
    generative_operation = backend in {"generative", "generative_sdxl"} or bool(re.search(
        r"(?:새로운|새)\s*[^.?!]{0,24}(?:배경|장면).{0,12}(?:생성|그려|만들)|"
        r"포즈\s*(?:변경|생성)|화풍|스타일로\s*(?:그려|생성)|인페인팅", text
    ))
    reasons = ["텍스트·도형·배치는 편집 가능한 SVG 레이어로 처리"]
    subject_processing = "none"
    if subject_operation:
        subject_processing = "birefnet-or-grabcut" if segmentation_ready else "grabcut-fallback"
        reasons.append("피사체 분리 요청이 있어 분할 런타임 활성화")
    image_generation = "disabled"
    if generative_operation:
        image_generation = "sdxl" if backend == "generative_sdxl" else "diffusion"
        reasons.append("기존 레이어 합성으로 표현할 수 없는 픽셀 생성 요청")
    return MockupRoute(
        planner="qwen-design-planner", renderer="qt-svg-layer-graph",
        subject_processing=subject_processing, image_generation=image_generation,
        reasons=tuple(reasons),
    )
