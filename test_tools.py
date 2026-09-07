"""Manual filesystem/Excel tool diagnostic with an explicit output target."""

from __future__ import annotations

import argparse
from pathlib import Path


def run_tools_diagnostic(output_dir: str) -> dict[str, object]:
    """Create diagnostic artifacts only in a new, explicitly named directory."""
    from dotenv import load_dotenv

    from core.tools import get_tool_executor

    load_dotenv()
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise FileExistsError(
            f"기존 사용자 파일 보호를 위해 새 경로만 허용합니다: {target}"
        )

    tool_executor = get_tool_executor()
    directory_result = tool_executor.create_directory(str(target))
    excel_path = target / "123.xlsx"
    data = [["이름", "나이"], ["철수", 30], ["영희", 25]]
    excel_result = tool_executor.create_excel_file(str(excel_path), data)
    print(f"폴더 결과: {directory_result}")
    print(f"Excel 결과: {excel_result}")
    return {
        "directory": directory_result,
        "excel": excel_result,
        "output_dir": str(target),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="새 격리 경로에 폴더와 Excel 파일을 만드는 수동 진단"
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="존재하지 않는 전용 진단 폴더 경로",
    )
    args = parser.parse_args()
    run_tools_diagnostic(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
