import json
from pathlib import Path

import pytest

from core.intent_router import IntentRouter
from core.plugin import PluginRegistry


CASES_PATH = Path(__file__).resolve().parent / "tests" / "data" / "intent_routing_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def router():
    registry = PluginRegistry()
    registry.load_plugins_from_directory()
    assert registry.validate_contracts() == {}
    return IntentRouter(registry)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_realistic_user_utterance_routing_dataset(router, case):
    resolution = router.resolve(case["utterance"])
    expected = case.get("intent")
    expected_any = case.get("intent_any") or []
    if expected is None:
        assert not resolution.matched
        return
    if expected_any:
        assert resolution.intent_name in expected_any
    else:
        assert resolution.intent_name == expected
    assert resolution.request_type == case["request_type"]
    if case.get("negated"):
        assert resolution.negated
        assert not resolution.ready
    if case.get("compound"):
        assert resolution.compound
        assert not resolution.ready
    if case.get("freshness"):
        assert resolution.freshness == case["freshness"]
        assert resolution.requires_sources


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"variant-{case['id']}")
def test_routing_is_stable_under_harmless_spacing_and_punctuation(router, case):
    expected = case.get("intent")
    if expected is None:
        pytest.skip("대화 발화는 의도적으로 Tool Intent가 없습니다.")
    utterance = case["utterance"]
    variants = (
        f"  {utterance}  ",
        utterance.replace(" ", "   "),
        utterance.rstrip(" .!?") + "!!!",
    )
    for variant in variants:
        resolution = router.resolve(variant)
        expected_any = case.get("intent_any") or []
        if expected_any:
            assert resolution.intent_name in expected_any, (variant, resolution)
        else:
            assert resolution.intent_name == expected, (variant, resolution)
        if case.get("negated"):
            assert resolution.negated and not resolution.ready
        if case.get("compound"):
            assert resolution.compound and not resolution.ready
