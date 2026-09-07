"""Side-effect-free configuration import smoke test."""

from __future__ import annotations

from config import Config


REQUIRED_CONFIG_FIELDS = (
    "LLM_PROVIDER",
    "OLLAMA_MODEL",
    "DB_PATH",
    "ALLOWED_PATHS",
)


def config_snapshot() -> dict[str, object]:
    return {name: getattr(Config, name) for name in REQUIRED_CONFIG_FIELDS}


def test_required_config_fields_are_accessible() -> None:
    snapshot = config_snapshot()
    assert tuple(snapshot) == REQUIRED_CONFIG_FIELDS


def main() -> int:
    for name, value in config_snapshot().items():
        print(f"Config.{name}: {value}")
    print("모든 필수 Config 접근 테스트 성공!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
