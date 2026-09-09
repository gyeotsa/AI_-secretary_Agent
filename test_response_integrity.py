import pytest

from core.agent_services import _apply_requested_style, _sanitize_response
from core.korean_naturalizer import light_polish_korean
from core.response_presenter import present_channels
from core.response_integrity import preserves_sources


@pytest.mark.parametrize("payload", [
    '```python\nx = [1, 2]\nprint("a*b")\n```',
    '"학교에서 만나 주세요."',
    '내용: 학교에서 만나 주세요.',
    '```json\n{"번역": "你好", "ok": true}\n```',
    '`rate = 50%`',
    'https://example.com/?a=1&b=2',
    '“정확히 읽어 주세요!”',
])
def test_sources_survive_all_presentation_layers(payload):
    source = "확인했습니다.\n" + payload
    styled = _apply_requested_style(source, "반말")
    sanitized = _sanitize_response(styled)
    polished = light_polish_korean(sanitized)
    assert payload in polished
    assert payload in present_channels(polished).screen_text


def test_style_changes_only_prose():
    result = _apply_requested_style('말씀해 주세요. 원문: "말씀해 주세요."', '반말')
    assert result == '말해 줘. 원문: "말씀해 주세요."'


def test_source_guard_rejects_dropped_or_changed_literal():
    assert not preserves_sources('본문: 학교에서 만나', '본문: 학교 만나')
    assert not preserves_sources('`a=1` 그리고 `b=2`', '`b=2` 그리고 `a=1`')


def test_screen_and_speech_are_separate():
    source = '코드입니다.\n```python\nprint("안녕")\n```'
    result = present_channels(source)
    assert result.screen_text == source
    assert 'print' not in result.speech_text
