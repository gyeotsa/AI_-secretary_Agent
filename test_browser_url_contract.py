"""Literal URL extraction contracts; browser/network actions are faked."""
import pytest

from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from plugins.browser import BrowserPlugin


@pytest.fixture
def plugin():
    return BrowserPlugin()


@pytest.mark.parametrize("wrapped", [
    '"https://example.com/path"', "'https://example.com/path'",
    "“https://example.com/path”", "‘https://example.com/path’",
    "`https://example.com/path`", "<https://example.com/path>",
    "(https://example.com/path)", "[https://example.com/path]",
    "{https://example.com/path}", "（https://example.com/path）",
    "「https://example.com/path」", "[주소](https://example.com/path)",
    '"https://example.com/path",', "(https://example.com/path).",
    "https://example.com/path.", "https://example.com/path,",
    "https://example.com/path!", "https://example.com/path).",
])
def test_url_surrounding_delimiters_are_not_part_of_target(plugin, wrapped):
    slots = plugin.extract_slots("web.open_url", f"{wrapped} 열어줘", {})
    assert slots["url"] == "https://example.com/path"


@pytest.mark.parametrize("url", [
    "https://example.com/?q=hello!",
    "https://example.com/?q=hello,",
    "https://example.com/?q=hello.",
    "https://example.com/?q=why?",
    "https://example.com/?q=)",
    "https://example.com/?q=]",
    "https://example.com/?q='quoted'",
    "https://example.com/?q=%22quoted%22&x=%27value%27",
    "https://example.com/?q=a+b&key=a%2Fb%3Fc%3Dd#section!",
    "https://example.com/?next=https://other.example/path?x=1&y=2",
    "https://example.com/#part,",
    "https://example.com/O'Reilly",
    "https://example.com/wiki/Function_(mathematics)",
])
def test_bare_url_legal_query_fragment_and_path_data_is_not_rstripped(plugin, url):
    assert plugin.extract_slots("web.open_url", f"{url} 열어줘", {})["url"] == url


@pytest.mark.parametrize("wrapped, url", [
    ('"https://example.com/?q=)"', "https://example.com/?q=)"),
    ("(https://example.com/wiki/Foo_(bar))", "https://example.com/wiki/Foo_(bar)"),
    ("[주소](https://example.com/?q=(abc))", "https://example.com/?q=(abc)"),
    ("<https://example.com/?q=a+b&x=%22data%22>", "https://example.com/?q=a+b&x=%22data%22"),
    ('"https://example.com/path."', "https://example.com/path."),
    ('"https://example.com/?q=\'quoted\'"', "https://example.com/?q='quoted'"),
    ("'https://example.com/O'Reilly'", "https://example.com/O'Reilly"),
    ("'https://example.com/?q='value'&x=1'", "https://example.com/?q='value'&x=1"),
    ("'https://example.com/?q=a%27b'을", "https://example.com/?q=a%27b"),
])
def test_explicit_wrapper_is_the_boundary_not_legal_url_punctuation(plugin, wrapped, url):
    assert plugin.extract_slots("web.open_url", f"{wrapped} 열어줘", {})["url"] == url


@pytest.mark.parametrize("utterance", [
    "https://one.example https://two.example 열어줘",
    '"https://one.example", "https://two.example" 중 링크 열어줘',
    "<https://one.example>,<https://two.example> 열어줘",
    "https://one.example,https://two.example 열어줘",
])
def test_multiple_distinct_urls_require_one_target_without_reusing_old_url(plugin, utterance):
    slots = plugin.extract_slots("web.open_url", utterance, {"url": "https://old.example"})
    assert not slots.get("url")
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    resolution = IntentRouter(registry).resolve(utterance)
    assert resolution.matched
    assert not resolution.ready
    assert "하나" in resolution.question


def test_repeated_same_url_is_not_ambiguous(plugin):
    slots = plugin.extract_slots(
        "web.open_url", '"https://example.com" 이 주소 https://example.com 열어줘', {},
    )
    assert slots["url"] == "https://example.com"


def test_no_new_url_keeps_explicitly_selected_current_target(plugin):
    current = {"url": "https://example.com/?q=a!"}
    assert plugin.extract_slots("web.open_url", "다시 열어줘", current) == current


@pytest.mark.parametrize("wrapper", [('"', '"'), ("'", "'"), ("(", ")"), ("<", ">")])
def test_video_learning_uses_the_same_literal_url_boundary(plugin, wrapper):
    url = "https://www.youtube.com/watch?v=xyz&list=abc&t=17&query=%22value%22"
    utterance = f"{wrapper[0]}{url}{wrapper[1]} 이 영상에 아니스가 나오는 말투를 학습해줘"
    slots = plugin.extract_slots("web.video_learning", utterance, {})
    assert slots["url"] == url


def test_multiple_video_urls_cannot_silently_pick_first_or_stale_target(plugin):
    slots = plugin.extract_slots(
        "web.video_learning", "https://youtu.be/one https://youtu.be/two 학습해줘",
        {"url": "https://youtu.be/old"},
    )
    assert not slots.get("url")


@pytest.mark.parametrize("url", [
    "https://youtu.be.evil.example/video", "https://youtube.com.evil.example/watch?v=test",
    "https://youtube.com@evil.example/watch?v=test",
])
def test_non_youtube_url_does_not_preserve_stale_learning_target(plugin, url):
    slots = plugin.extract_slots(
        "web.video_learning", f"{url} 영상 말투를 학습해줘", {"url": "https://youtu.be/old"},
    )
    assert not slots.get("url")


def test_uri_case_unicode_and_percent_encoding_remain_literal(plugin):
    url = "HTTPS://Example.COM/자료?이름=정지원&data=%2f%22%27%2B"
    assert plugin.extract_slots("web.open_url", f'"{url}" 열어줘', {})["url"] == url


def test_quoted_url_router_and_fake_dispatch_preserve_exact_target(plugin, monkeypatch):
    url = "https://example.com/?next=https://other.example/a&text=%22quoted%22&x=)!"
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    resolution = IntentRouter(registry).resolve(f'"{url}" 열어줘')
    opened = []
    monkeypatch.setattr(plugin, "_validate_url", lambda value: value)
    monkeypatch.setattr(plugin, "_open_external_url", opened.append)
    assert resolution.ready
    assert resolution.slots["url"] == url
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.succeeded
    assert opened == [url]
    assert result.evidence[0].data["url"] == url
    assert result.evidence[0].data["page_loaded_verified"] is False
