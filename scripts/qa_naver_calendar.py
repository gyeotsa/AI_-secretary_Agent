"""Explicit live QA of ANIS's own extension adapter (no automatic writes)."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plugins.naver_calendar import NaverCalendarPlugin
from core.plugin import PluginRegistry
from core.verifier import ToolVerifier


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect-editor", action="store_true")
    parser.add_argument("--inspect-timezone", action="store_true")
    parser.add_argument("--interactive", action="store_true",
                        help="Keep one ANIS session for read/editor/timezone probes until quit or EOF; never save")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    plugin = NaverCalendarPlugin()
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    payload = {"connected":False, "remote_save_attempted":False}
    print('ANIS Calendar: select the calendar tab within 5 minutes. Old/finished requests are expired.', flush=True)
    try:
        status = plugin.service.connect()
        payload["connected"] = status
        command = "timezone" if args.inspect_timezone else "editor" if args.inspect_editor else "read"
        while True:
            verification = None
            if command in {"editor", "timezone"}:
                value = plugin.service.inspect_editor(show_timezone=command == "timezone")
            else:
                result = registry.execute_tool("naver_calendar_read_view", {})
                verdict = ToolVerifier().verify("naver_calendar_read_view", {}, result)
                value = result.to_dict()
                verification = {"passed":verdict.success, "message":verdict.message}
                payload.update(result=value, verification=verification)
                if not verdict.success and not args.interactive:
                    raise ValueError("아니스 캘린더 조회 검수를 통과하지 못했습니다.")
            payload = {"connected":plugin.service.status(), "result":value,
                       "verification":verification, "remote_save_attempted":False}
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"connected":True, "probe":command, "remote_save_attempted":False,
                              "verified":verification and verification["passed"],
                              "report":str(args.report)}), flush=True)
            if not args.interactive:
                break
            if not plugin.service.status()["connected"]:
                raise ValueError("화면 검사 실패로 연결 증거가 폐기되었습니다. 자동 재연결하지 않습니다.")
            print('Session stays connected. Commands: read / editor / timezone / quit (no Save).', flush=True)
            command = sys.stdin.readline().strip()
            if command in {"", "quit"}:
                break
            if command not in {"read", "editor", "timezone"}:
                raise ValueError("알 수 없는 읽기 검사 명령입니다. 저장 명령은 지원하지 않습니다.")
    except Exception as exc:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        payload.update(connected=plugin.service.status(), error_type=type(exc).__name__)
        from core.naver_calendar import CalendarScreenError
        if isinstance(exc, CalendarScreenError):
            payload.update(error_stage=exc.stage, screen=exc.screen)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"connected":False, "error_type":type(exc).__name__,
                          "error_stage":payload.get("error_stage"), "screen":payload.get("screen"),
                          "remote_save_attempted":False}))
        raise
    finally:
        plugin.service.disconnect()
        registry.shutdown()


if __name__ == "__main__":
    main()
