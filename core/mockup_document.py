"""Canonical, revisioned design documents for non-destructive mockup editing."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy


VOLATILE_SCENE_KEYS = {
    "document", "rationale", "quality_review_fallback", "quality_review_rejected",
    "initial_plan_fallback", "edit_plan_fallback",
}


def canonical_scene_payload(scene_plan: dict) -> dict:
    """Return the stable visual payload used for optimistic edit locking."""
    value = deepcopy(scene_plan) if isinstance(scene_plan, dict) else {}
    for key in VOLATILE_SCENE_KEYS:
        value.pop(key, None)
    return value


def scene_digest(scene_plan: dict) -> str:
    payload = json.dumps(
        canonical_scene_payload(scene_plan), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def stamp_scene_document(scene_plan: dict, *, revision: int = 0,
                         parent_digest: str = "") -> dict:
    """Attach immutable revision metadata without changing the visual payload."""
    result = deepcopy(scene_plan)
    digest = scene_digest(result)
    result["document"] = {
        "schema": "jarvis.design-document/v1",
        "revision": max(0, int(revision)),
        "scene_digest": digest,
        "parent_scene_digest": str(parent_digest or ""),
    }
    return result


def assert_scene_document(scene_plan: dict, *, expected_digest: str = "") -> dict:
    """Reject stale or externally mutated scene state before an edit transaction."""
    if not isinstance(scene_plan, dict):
        raise ValueError("편집할 디자인 문서가 없습니다.")
    document = scene_plan.get("document") if isinstance(scene_plan.get("document"), dict) else {}
    actual = scene_digest(scene_plan)
    declared = str(document.get("scene_digest", ""))
    # Every displayed revision is bound to the scene that produced its pixels.
    # UI controls must create a new rendered revision, never mutate this plan
    # behind the bitmap and pass the old optimistic token as authorization.
    if (declared and actual != declared) or (expected_digest and actual != expected_digest):
        raise ValueError("현재 미리보기와 편집 기준 문서가 다릅니다. 최신 미리보기를 다시 선택해 주세요.")
    return {"scene_digest": actual, "revision": int(document.get("revision", 0))}
