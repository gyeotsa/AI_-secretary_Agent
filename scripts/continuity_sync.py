"""Connect or backfill local AI transcripts without loading the GUI or models."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description="아니스 통합 리마인더 로컬 로그 연결/동기화")
    parser.add_argument("--connect", nargs=2, metavar=("PROVIDER", "DIRECTORY"),
                        help="codex 또는 claude_code와 세션 로그 폴더")
    parser.add_argument("--max-seconds", type=float, default=15,
                        help="한 번의 초기 수집 시간 상한 (기본 15초)")
    parser.add_argument("--now", action="store_true", help="수집 후 다음 할 일 표시")
    args = parser.parse_args()
    os.chdir(ROOT)
    from core.continuity import ContinuityService
    service = ContinuityService()
    try:
        if args.connect:
            service.add_source(*args.connect)
        started, total, previous = time.monotonic(), 0, None
        while True:
            report = service.sync()
            total += report["events"]
            pending = report.get("pending_bytes", 0)
            if not pending or time.monotonic() - started >= max(0, args.max_seconds):
                break
            if previous == pending and not report["events"] and all(s.get("scan_complete", True) for s in report["sources"]):
                break
            previous = pending
        summary = {"status": report["status"], "imported_events": total,
                   "pending_bytes": pending, "skipped_records": report.get("skipped_records", 0),
                   "sessions": len(service.list_sessions(limit=10000)),
                   "stats": service.dashboard()["stats"], "errors": report["errors"][:5]}
        print(json.dumps(summary, ensure_ascii=True, indent=2))
        if args.now:
            print(service.answer_now())
        return 1 if report["errors"] and not total and not report.get("skipped_records") else 0
    finally:
        service.stop()
        service.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
