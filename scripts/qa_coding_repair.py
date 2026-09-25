"""Local-only coding dialogue acceptance, with capability-free WASI example checks.

Run from the repository with .venv/Scripts/python.exe scripts/qa_coding_repair.py.
All application stores/configuration live in a temporary directory. The report
contains only this synthetic archery problem and model answers, never user logs.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time


PROBLEM = """문제 설명
카카오배 양궁대회에서 어피치가 n발을 쏜 뒤 라이언이 n발을 쏩니다.
info[i]는 어피치가 10-i점에 맞힌 화살 개수입니다.
각 점수에서 라이언이 어피치보다 많은 화살을 맞히면 라이언만 그 점수를 얻고,
같거나 적으면 어피치가 얻습니다. 단 둘 다 0발이면 아무도 점수를 얻지 않습니다.
총점이 같으면 라이언은 패배합니다. 라이언이 가장 큰 점수 차로 이기는 배치를 구하세요.
제한사항
1 <= n <= 10, len(info) == 11, sum(info) == n.
답은 길이 11인 10점~0점 화살 배열이며 반드시 합이 n이어야 합니다.
최대 점수 차의 배치가 여러 개이면 0점부터 낮은 점수 순으로 더 많은 화살을 둔 배치를 택합니다.
이길 수 없으면 [-1]을 반환합니다.
입출력 예
5, [2,1,1,1,0,0,0,0,0,0,0] -> [0,2,2,0,1,0,0,0,0,0,0]
1, [1,0,0,0,0,0,0,0,0,0,0] -> [-1]
9, [0,0,1,2,0,1,1,1,1,1,1] -> [1,1,2,0,1,2,2,0,0,0,0]
10, [0,0,0,0,0,0,0,0,3,4,3] -> [1,1,1,1,1,1,1,1,0,0,2]
이 문제를 풀어줘. 완전한 Python solution(n, info) 코드와 설명을 제공해줘.
"""
BAD_ANSWER = "```python\ndef solution(n, info):\n    return [-1]\n```"
EXAMPLES = [
    (5, [2,1,1,1,0,0,0,0,0,0,0], [0,2,2,0,1,0,0,0,0,0,0]),
    (1, [1,0,0,0,0,0,0,0,0,0,0], [-1]),
    (9, [0,0,1,2,0,1,1,1,1,1,1], [1,1,2,0,1,2,2,0,0,0,0]),
    (10, [0,0,0,0,0,0,0,0,3,4,3], [1,1,1,1,1,1,1,1,0,0,2]),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--case", choices=("problem", "short_followup", "failure_feedback", "general_function"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report_path = args.report.resolve()
    sys.path.insert(0, str(root))
    report = []
    with tempfile.TemporaryDirectory(prefix="anis-coding-qa-") as directory:
        sandbox = Path(directory)
        (sandbox / "data").mkdir()
        os.environ.update({
            "DB_PATH": str(sandbox / "data/assistant.db"),
            "LEARNING_DB_PATH": str(sandbox / "data/learning.db"),
            "OBSIDIAN_VAULT_PATH": str(sandbox / "vault"), "ALLOWED_PATHS": str(sandbox),
            "LLM_PROVIDER": "ollama", "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "ANTHROPIC_API_KEY": "", "HYBRID_CLAUDE_ROLES": "", "PYTHON_DOTENV_DISABLED": "1",
            "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1",
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "QT_QPA_PLATFORM": "offscreen",
            "JARVIS_DISABLE_MIC_AUTOSTART": "1", "JARVIS_DISABLE_CAMERA_AUTOSTART": "1",
        })
        os.chdir(sandbox)
        with redirect_stdout(io.StringIO()):
            from core.agent_services import ConversationService
            from core.answer_verification import AnswerVerificationService, _blocks
            from core.executor import _local_answer_draft_client
            from core.llm import OllamaClient
            from core.plugin import PluginRegistry
            from core.semantic_request import SemanticRequestInterpreter
            from core.turn_context import TurnExecutionContext, bind_turn_context
            interpreter = SemanticRequestInterpreter(OllamaClient("reasoning"), PluginRegistry(),
                                                     classify_response_mode=True)
            service = ConversationService(OllamaClient("conversation"),
                answer_verifier=AnswerVerificationService(),
                generation_client_factory=_local_answer_draft_client)
        cases = [
            ("general_function", 'Python def normalize_name(name): 함수를 작성해줘. 입력 문자열 양끝 공백을 제거하고 소문자로 반환해줘.\n" A " -> "a"\n"" -> ""\n" Hello World " -> "hello world"', []),
            ("problem", PROBLEM, []),
            ("short_followup", "풀어줘", [{"role": "user", "content": PROBLEM},
                {"role": "assistant", "content": "조건을 확인했어. 이제 풀어볼게."}]),
            ("failure_feedback", "이상한데", [{"role": "user", "content": PROBLEM},
                {"role": "assistant", "content": BAD_ANSWER}]),
        ]
        for name, request, history in cases:
            if args.case and args.case != name:
                continue
            start = time.monotonic()
            print(json.dumps({"case": name, "event": "started"}), flush=True)
            with redirect_stdout(io.StringIO()), bind_turn_context(TurnExecutionContext(name, "qa", directory)):
                decision = interpreter.interpret(request, history=history)
                response = service.respond(request, history, answer_kind=decision.answer_kind)
                record = {"case": name, "route": decision.answer_kind,
                    "grounded": decision.is_grounded_conversation,
                    "seconds": round(time.monotonic()-start, 2),
                    "truncated": response.truncated,
                    "code_present": any(b.closed and b.body.strip() for b in _blocks(response)),
                    "review": response.answer_review.to_dict() if response.answer_review else None,
                    "response": str(response)}
            report.append(record)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            review = record["review"] or {}
            print(json.dumps({"case": name, "seconds": record["seconds"], "route": record["route"],
                "code_present": record["code_present"], "review_status": review.get("status"),
                "execution_status": review.get("execution_status"), "repair_calls": review.get("repair_calls"),
                "example_passes": sum(r["passed"] for r in review.get("test_results", [])),
                "example_count": len(review.get("test_results", []))}, ensure_ascii=False), flush=True)
        os.chdir(root)
        logging.shutdown()
    return 0 if all(r["grounded"] and r["code_present"] and not r["truncated"]
                   and (r["review"] or {}).get("execution_status") == "passed" for r in report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
