"""Manual live LLM client diagnostic; pytest collection performs no call."""

from __future__ import annotations

import argparse


def run_simple_llm_diagnostic(prompt: str = "안녕하세요!") -> tuple[str, list]:
    from config import Config
    from core.llm import get_llm_client

    print(f"[DIAGNOSTIC] LLM Provider: {Config.LLM_PROVIDER}")
    print(f"[DIAGNOSTIC] Ollama Model: {Config.OLLAMA_MODEL}")
    llm = get_llm_client()
    response_text, tool_uses = llm.chat_with_tools(
        [{"role": "user", "content": prompt}]
    )
    print(f"[DIAGNOSTIC] Response text: {response_text}")
    print(f"[DIAGNOSTIC] Tool uses: {tool_uses}")
    return response_text, tool_uses


def main() -> int:
    parser = argparse.ArgumentParser(description="실제 LLM 한 턴 수동 진단")
    parser.add_argument("--prompt", default="안녕하세요!")
    args = parser.parse_args()
    run_simple_llm_diagnostic(args.prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
