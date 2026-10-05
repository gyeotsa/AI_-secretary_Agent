import json
from types import SimpleNamespace

import pytest

from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from plugins.filesystem import FilesystemPlugin


def surface(tmp_path):
    plugin = FilesystemPlugin()
    plugin.workspace = SimpleNamespace(get_workspace_path=lambda: str(tmp_path))
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    return plugin, registry


@pytest.mark.parametrize('utterance', [
    'Agent 인수인계.txt 파일에 작성되어 있는 첫 번째 줄 내용을 알려줘',
    'Agent 인수인계 파일의 첫 줄을 보여줘',
    'Agent 인수인계.txt에 적혀 있는 내용을 읽어줘',
])
def test_spaced_file_is_read_and_never_written(tmp_path, utterance):
    original = 'x = [1, 2] 🙂\r\n두 번째 줄\r\n'.encode('utf-8')
    target = tmp_path / 'Agent 인수인계.txt'
    target.write_bytes(original)
    plugin, registry = surface(tmp_path)
    try:
        result = IntentRouter(registry).resolve(utterance)
        assert result.intent_name == 'filesystem.read_file'
        assert result.slots['filename'] == target.name
        run = registry.execute_tool(result.tool_name, result.slots)
        assert run.succeeded
        value = json.loads(run.raw_output)
        expected = original.decode('utf-8') if '적혀' in utterance else 'x = [1, 2] 🙂\r\n'
        assert value['content'] == expected
        assert expected in plugin.present_result(result.tool_name, run.raw_output)
        assert target.read_bytes() == original
    finally:
        registry.shutdown()


def test_same_basename_in_two_directories_requires_target(tmp_path):
    for folder in ('a', 'b'):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / '회의 기록.txt').write_text(folder, encoding='utf-8')
    plugin, registry = surface(tmp_path)
    try:
        assert plugin.resolve_file_candidates('회의 기록.txt를 읽어줘') == ['a/회의 기록.txt', 'b/회의 기록.txt']
        result = IntentRouter(registry).resolve('회의 기록.txt 파일 내용을 읽어줘')
        assert result.question and not result.ready
    finally:
        registry.shutdown()


@pytest.mark.parametrize('filename', ['../outside.txt', 'missing.txt'])
def test_read_fails_closed_without_target(tmp_path, filename):
    plugin, registry = surface(tmp_path)
    try:
        result = plugin.execute_tool('filesystem_read_file', {'filename': filename})
        assert not result.succeeded
        assert not result.artifacts
    finally:
        registry.shutdown()


def test_requested_line_range_and_display_limit_are_explicit(tmp_path):
    (tmp_path / 'note.txt').write_bytes('1\n2\n3\n4\n'.encode())
    plugin, registry = surface(tmp_path)
    try:
        result = plugin.execute_tool('filesystem_read_file', {
            'filename': 'note.txt', 'start_line': 2, 'end_line': 3, 'max_chars': 2,
        })
        data = json.loads(result.raw_output)
        assert data['content'] == '2\n' and data['truncated']
        assert '일부만' in plugin.present_result('filesystem_read_file', result.raw_output)
        missing = plugin.execute_tool('filesystem_read_file', {'filename': 'note.txt', 'start_line': 9, 'end_line': 9})
        assert '요청한 줄이 없습니다' in plugin.present_result('filesystem_read_file', missing.raw_output)
    finally:
        registry.shutdown()


@pytest.mark.parametrize('bounds,expected', [
    ({}, '1\n2\n3\n'),
    ({'start_line': 2}, '2\n3\n'),
    ({'start_line': 2, 'end_line': 2}, '2\n'),
])
def test_optional_range_contract_matches_real_reading(tmp_path, bounds, expected):
    (tmp_path / 'note.txt').write_bytes(b'1\n2\n3\n')
    plugin, registry = surface(tmp_path)
    try:
        contract = registry.get_capability('filesystem_read_file').input_schema['properties']
        assert all(field.get('description') for field in contract.values())
        assert '파일 끝까지' in contract['end_line']['description']
        assert '같은 번호' in contract['end_line']['description']
        result = registry.execute_tool('filesystem_read_file', {'filename': 'note.txt', **bounds})
        assert result.succeeded
        assert json.loads(result.raw_output)['content'] == expected
    finally:
        registry.shutdown()
