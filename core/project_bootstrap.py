"""Declarative and recoverable new-project bootstrap workflow."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


PROJECT_TEMPLATES: Dict[str, Dict[str, str]] = {
    "empty": {"README.md": "# {name}\n"},
    "python-basic": {
        "README.md": "# {name}\n\nPython project.\n",
        ".gitignore": ".venv/\n__pycache__/\n*.py[cod]\n.pytest_cache/\n",
        "pyproject.toml": (
            "[project]\nname = \"{package}\"\nversion = \"0.1.0\"\n"
            "requires-python = \">=3.12\"\n\n[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n"
        ),
        "src/{package}/__init__.py": "\"\"\"{name} package.\"\"\"\n",
        "tests/test_smoke.py": "def test_smoke():\n    assert True\n",
    },
    "python-cli": {
        "README.md": "# {name}\n\nPython command-line application.\n",
        ".gitignore": ".venv/\n__pycache__/\n*.py[cod]\n.pytest_cache/\n",
        "pyproject.toml": (
            "[project]\nname = \"{package}\"\nversion = \"0.1.0\"\n"
            "requires-python = \">=3.12\"\n[project.scripts]\n{name} = \"{package}.main:main\"\n"
        ),
        "src/{package}/__init__.py": "",
        "src/{package}/main.py": (
            "def main() -> None:\n    print(\"Hello from {name}\")\n\n"
            "if __name__ == \"__main__\":\n    main()\n"
        ),
        "tests/test_main.py": (
            "from {package}.main import main\n\ndef test_main(capsys):\n"
            "    main()\n    assert \"Hello\" in capsys.readouterr().out\n"
        ),
    },
}


@dataclass
class BootstrapResult:
    path: str
    template: str
    created_files: List[str] = field(default_factory=list)
    virtual_environment: bool = False
    git_initialized: bool = False


class ProjectBootstrapper:
    @staticmethod
    def available_templates() -> List[str]:
        return sorted(PROJECT_TEMPLATES)

    def create(
        self, parent: str, name: str, template: str = "python-basic",
        create_venv: bool = True, init_git: bool = True,
    ) -> BootstrapResult:
        if template not in PROJECT_TEMPLATES:
            raise ValueError(f"지원하지 않는 템플릿입니다: {template}")
        if not re.fullmatch(r"[A-Za-z0-9가-힣][A-Za-z0-9가-힣._-]{0,79}", name):
            raise ValueError("프로젝트 이름에는 문자, 숫자, 점, 밑줄, 하이픈만 사용할 수 있습니다.")
        parent_path = Path(parent).expanduser().resolve()
        if not parent_path.is_dir():
            raise ValueError("상위 폴더가 존재하지 않습니다.")
        target = (parent_path / name).resolve()
        if not target.is_relative_to(parent_path):
            raise ValueError("상위 폴더 밖에는 프로젝트를 만들 수 없습니다.")
        if target.exists():
            raise FileExistsError(f"이미 존재하는 경로입니다: {target}")

        package = re.sub(r"\W+", "_", name, flags=re.UNICODE).strip("_").lower() or "app"
        result = BootstrapResult(str(target), template)
        target.mkdir()
        try:
            for relative, content in PROJECT_TEMPLATES[template].items():
                rendered_path = relative.format(name=name, package=package)
                rendered = content.format(name=name, package=package)
                destination = target / rendered_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(rendered, encoding="utf-8")
                result.created_files.append(rendered_path)
            if create_venv:
                process = subprocess.run(
                    [sys.executable, "-m", "venv", ".venv"], cwd=target,
                    capture_output=True, text=True, timeout=180, check=False,
                )
                if process.returncode != 0:
                    raise RuntimeError(process.stderr.strip() or "가상환경 생성에 실패했습니다.")
                result.virtual_environment = True
            if init_git:
                process = subprocess.run(
                    ["git", "init"], cwd=target, capture_output=True, text=True,
                    timeout=30, check=False,
                )
                if process.returncode != 0:
                    raise RuntimeError(process.stderr.strip() or "Git 초기화에 실패했습니다.")
                result.git_initialized = True
            (target / ".jarvis-project.json").write_text(json.dumps({
                "schema_version": 1, "name": name, "template": template,
                "python_executable": str(target / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
                if create_venv else "",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            result.created_files.append(".jarvis-project.json")
            return result
        except Exception:
            # The target did not exist before this transaction; rollback is safe and bounded.
            import shutil
            shutil.rmtree(target, ignore_errors=True)
            raise
