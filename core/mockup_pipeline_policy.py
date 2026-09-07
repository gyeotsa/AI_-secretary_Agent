"""Capability-based routing for the local mockup specialist team."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from core.utterance_scope import analyze_utterance_scope, mask_quoted_payloads


@dataclass(frozen=True)
class MockupRoute:
    planner: str
    renderer: str
    subject_processing: str
    image_generation: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SubjectBackgroundEdit:
    """One clause's operation and source selector; not a whole-request boolean."""

    remove: bool
    source_indexes: tuple[int, ...]
    all_sources: bool
    clause: str


_BACKGROUND_CLAUSES = re.compile(
    r"[.!?;\n]+|,(?!\s*\d)|\s+(?:그리고|하지만|대신)\s+|"
    r"(?<=고)\s+(?=(?:(?:\d+|첫|두|세|네|다섯)\s*(?:번째|번)\s*)?"
    r"(?:문구|글자|텍스트|글씨|사진|이미지|인물|피사체|캔버스))"
)
_BACKGROUND_REMOVE = re.compile(
    r"배경(?:을|은|도|만|에)?\s*(?:만\s*)?(?:제거|삭제|지워|지우|없애|빼\s*줘)|"
    r"누끼.{0,12}(?:따|추출|제거|처리)|(?:인물|피사체)\s*(?:만\s*)?(?:분리|추출)|"
    r"(?:배경(?:을|은|만)?\s*투명|투명\s*배경)"
)
_BACKGROUND_RESTORE = re.compile(
    r"배경.{0,20}(?:복원|되돌|살려|원래대로)|배경\s*제거.{0,8}취소|"
    r"(?:원본|원래)\s*배경.{0,15}(?:복원|되돌|돌려)|누끼.{0,8}취소"
)
_BACKGROUND_OWNER = re.compile(r"사진|이미지|인물|피사체|사람|누끼|문구|글자|텍스트|글씨|캔버스")
_NON_PHOTO_OWNERS = frozenset({"문구", "글자", "텍스트", "글씨", "캔버스"})


def subject_background_edits(instruction: str) -> tuple[SubjectBackgroundEdit, ...]:
    """Bind each positive photo-alpha clause to its own explicit source.

    Quoted text, explanations, conditions and negations are not write authority.
    The nearest owning noun distinguishes '사진 위 문구 배경' from '문구가 있는
    사진 배경'. Unknown/ambiguous multi-photo selectors are resolved fail-closed
    against the actual scene by the write barrier, never guessed as source zero.
    """
    text = mask_quoted_payloads(str(instruction or "")).casefold()
    edits = []
    for raw_clause in _BACKGROUND_CLAUSES.split(text):
        clause = " ".join(raw_clause.split())
        if not clause:
            continue
        scope = analyze_utterance_scope(clause)
        if scope.discussion or scope.negated or scope.conditional:
            continue
        action_clause = scope.action_text
        # Complement the shared speech-act guard for adjectival negations
        # ('투명하지 않게') and bare prohibitions. Neither can authorize alpha.
        if re.search(r"지\s*(?:않|말|마)|누끼\s*(?:없이|금지)|(?:안|금지)\s*(?:제거|삭제|복원|따)", action_clause):
            continue
        restoring = _BACKGROUND_RESTORE.search(action_clause)
        operation = restoring or _BACKGROUND_REMOVE.search(action_clause)
        if not operation:
            continue
        owners = list(_BACKGROUND_OWNER.finditer(action_clause[:operation.start() + 2]))
        if owners and owners[-1].group(0) in _NON_PHOTO_OWNERS:
            continue
        # A direct '누끼/인물 분리' is inherently a photo operation; other
        # background clauses without a named owner use the scene's sole photo.
        indexes = set()
        for match in re.finditer(r"(\d+(?:\s*[,·와과]\s*\d+)*)\s*(?:번째|번)", action_clause):
            indexes.update(int(value) - 1 for value in re.findall(r"\d+", match.group(1)))
        for ordinal, index in (("첫", 0), ("두", 1), ("세", 2), ("네", 3), ("다섯", 4)):
            if re.search(rf"{ordinal}\s*번째\s*(?:사진|이미지)", action_clause):
                indexes.add(index)
        all_sources = bool(re.search(
            r"(?:모든|전체|각)\s*(?:사진|이미지|인물|피사체)|(?:사진|이미지).{0,8}(?:모두|전부)|^전부\s", action_clause,
        ))
        edits.append(SubjectBackgroundEdit(not bool(restoring), tuple(sorted(indexes)), all_sources, clause))
    return tuple(edits)


def subject_background_action(instruction: str) -> bool | None:
    """Compatibility summary only; scene writes must use clause-bound edits."""
    edits = subject_background_edits(instruction)
    return edits[-1].remove if edits else None


def route_mockup_request(instruction: str, *, backend: str = "auto",
                         segmentation_ready: bool = False) -> MockupRoute:
    """Choose capabilities, not named templates or one-off Korean sentences."""
    text = " ".join(str(instruction or "").casefold().split())
    subject_operation = any(edit.remove for edit in subject_background_edits(instruction))
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
