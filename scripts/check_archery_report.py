"""Explicit offline QA for previously inspected archery replies, never app runtime.

Accepts only functions and itertools imports, with no filesystem/network APIs;
each reply runs in a fresh Python process with a 20 second deadline. This is a
small acceptance helper for this fixture, not a general untrusted-code sandbox.
"""
import argparse
import ast
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.qa_coding_repair import EXAMPLES


def allocations(n, size=11):
    if size == 1:
        yield [n]
    else:
        for first in range(n + 1):
            for rest in allocations(n - first, size - 1):
                yield [first, *rest]


def oracle(n, info):
    best, difference = [-1], 0
    for candidate in allocations(n):
        score = sum(10-i if candidate[i] > info[i] else -(10-i) if info[i] else 0 for i in range(10))
        if score > difference or (score == difference and score > 0 and candidate[::-1] > best[::-1]):
            best, difference = candidate, score
    return best


def evaluate(code):
    tree = ast.parse(code)
    forbidden = (ast.ClassDef, ast.With, ast.AsyncWith, ast.AsyncFunctionDef, ast.Await)
    safe_attrs = {"append", "pop", "copy", "reverse", "sort", "extend", "count", "add"}
    for node in ast.walk(tree):
        if isinstance(node, forbidden) or (isinstance(node, ast.Name) and node.id.startswith("__")):
            raise ValueError("unsupported QA code")
        if isinstance(node, ast.Attribute) and node.attr not in safe_attrs:
            raise ValueError("unsupported attribute")
        if isinstance(node, ast.Import) or (isinstance(node, ast.ImportFrom) and node.module != "itertools"):
            raise ValueError("unsupported import")
    # Model print examples are not verification; run our independent cases.
    tree.body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ImportFrom))]
    def limited_import(name, *args):
        if name != "itertools":
            raise ValueError("unsupported import")
        return itertools
    names = {name: getattr(__import__("builtins"), name) for name in (
        "range", "len", "sum", "max", "min", "enumerate", "zip", "list", "tuple", "reversed",
        "sorted", "abs", "int", "float", "set", "any", "all", "bool")}
    namespace = {"__builtins__": {**names, "__import__": limited_import}}
    exec(compile(tree, "<inspected-archery-qa>", "exec"), namespace)
    solution = namespace.get("solution")
    if not callable(solution):
        return {"passed": False, "reason": "missing_solution_function", "example_passes": 0}
    results = []
    for n, info, expected in EXAMPLES:
        actual = solution(n, info[:])
        results.append({"n": n, "expected": expected, "actual": actual, "passed": actual == expected,
                        "arrow_count_valid": actual == [-1] or (len(actual) == 11
                            and all(type(x) is int and 0 <= x <= n for x in actual) and sum(actual) == n)})
    # Independent exhaustive allocations test tie-break, zero arrows and losses.
    rng = random.Random(947)
    checks = 0
    for n in range(1, 5):
        for _ in range(10):
            info = [0] * 11
            for _ in range(n):
                info[rng.randrange(11)] += 1
            checks += solution(n, info[:]) == oracle(n, info)
    return {"passed": all(r["passed"] for r in results) and checks == 40,
            "examples": results, "example_passes": sum(r["passed"] for r in results),
            "exhaustive_crosscheck_passes": checks, "crosscheck_total": 40}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        try:
            result = evaluate(json.loads(sys.stdin.read()))
        except Exception as exc:
            result = {"passed": False, "reason": type(exc).__name__}
        print(json.dumps(result))
        return
    records = json.loads(args.report.read_text(encoding="utf-8"))
    import re
    for record in records:
        blocks = re.findall(r"```(?:python|py)\s*\n(.*?)```", record["response"], re.S)
        try:
            proc = subprocess.run([sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--worker"],
                input=json.dumps("\n\n".join(blocks)), text=True, capture_output=True, timeout=20)
            result = json.loads(proc.stdout)
        except (subprocess.TimeoutExpired, ValueError):
            result = {"passed": False, "reason": "timeout_or_invalid_worker_output"}
        record["independent_test"] = result
        print(json.dumps({"case": record["case"], **result}, ensure_ascii=False))
    args.report.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(r["independent_test"]["passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
