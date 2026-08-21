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
    if expected is None:
        assert not resolution.matched
        return
    assert resolution.intent_name == expected
    assert resolution.request_type == case["request_type"]
    if case.get("freshness"):
        assert resolution.freshness == case["freshness"]
        assert resolution.requires_sources
