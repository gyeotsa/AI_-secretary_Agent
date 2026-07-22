"""Ollama local-model Tool-call compatibility tests."""
from core.llm import OllamaClient


def _client_with_tools():
    client = OllamaClient()
    client.tools = [
        {"name": "get_date", "description": "date", "input_schema": {"type": "object", "properties": {}}},
        {"name": "read_file", "description": "read", "input_schema": {"type": "object", "properties": {}}},
    ]
    return client


def test_extracts_plain_legacy_tool_json():
    block = _client_with_tools()._extract_legacy_tool_call('{"name":"get_date","arguments":{}}')
    assert block["name"] == "get_date"
    assert block["input"] == {}


def test_extracts_markdown_wrapped_tool_json():
    block = _client_with_tools()._extract_legacy_tool_call(
        '```json\n{"name":"read_file","parameters":{"path":"README.md"}}\n```'
    )
    assert block["name"] == "read_file"
    assert block["input"] == {"path": "README.md"}


def test_rejects_unknown_tool_and_non_object_arguments():
    client = _client_with_tools()
    assert client._extract_legacy_tool_call('{"name":"delete_everything","arguments":{}}') is None
    assert client._extract_legacy_tool_call('{"name":"get_date","arguments":[]}') is None


def test_accepts_json_encoded_arguments():
    block = _client_with_tools()._extract_legacy_tool_call(
        '{"name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}'
    )
    assert block["input"] == {"path": "README.md"}
