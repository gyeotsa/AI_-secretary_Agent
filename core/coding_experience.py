"""Scoped coding attempts; a user's success report is not execution evidence."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid

from config import Config
from core.answer_verification import _blocks


def normalize_workspace(workspace_path: str) -> str:
    value = str(workspace_path or "").strip()
    return os.path.normcase(os.path.realpath(os.path.expanduser(value))) if value else ""


def _code_text(answer: str) -> str:
    blocks = _blocks(str(answer))
    return "\n\n".join(block.body.strip() for block in blocks) if blocks else str(answer).strip()


class CodingExperienceStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or Config.DB_PATH
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS coding_experiences (
                id TEXT PRIMARY KEY, session_id TEXT, problem TEXT, failure TEXT,
                corrected_code TEXT, verified INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, metadata TEXT)""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(coding_experiences)")}
            additions = {
                "workspace_path": "TEXT NOT NULL DEFAULT ''",
                "failed_code": "TEXT NOT NULL DEFAULT ''",
                "failure_cause": "TEXT NOT NULL DEFAULT ''",
                "tests": "TEXT NOT NULL DEFAULT '[]'",
                "test_results": "TEXT NOT NULL DEFAULT '[]'",
                "previous_attempt_id": "TEXT NOT NULL DEFAULT ''",
                "verification_source": "TEXT NOT NULL DEFAULT 'none'",
                "user_report": "TEXT NOT NULL DEFAULT ''",
                "user_feedback": "TEXT NOT NULL DEFAULT ''",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE coding_experiences ADD COLUMN {name} {declaration}")
            if "verification_source" not in columns:
                # Old keyword-based approvals lack test evidence. Preserve the
                # records but never publish them as verified repair examples.
                conn.execute("UPDATE coding_experiences SET verified=0, "
                             "verification_source='legacy_unverified'")
            conn.execute("CREATE INDEX IF NOT EXISTS coding_experiences_scope "
                         "ON coding_experiences(session_id, workspace_path, created_at)")

    @staticmethod
    def _scope(session_id: str, workspace_path: str) -> tuple[str, str]:
        return str(session_id or "default"), normalize_workspace(workspace_path)

    @staticmethod
    def _record(row) -> dict | None:
        if row is None:
            return None
        record = dict(row)
        for name, default in (("metadata", {}), ("tests", []), ("test_results", [])):
            try:
                record[name] = json.loads(record.get(name) or json.dumps(default))
            except (ValueError, TypeError):
                record[name] = default
            if not isinstance(record[name], type(default)):
                record[name] = default
        record["verified"] = bool(record["verified"])
        record["correction"] = record["corrected_code"]
        return record

    def record_attempt(self, session_id: str, failure: str, correction: str, *,
                       workspace_path: str = "", problem: str = "", failed_code: str = "",
                       failure_cause: str = "", tests: list | None = None,
                       test_results: list | None = None, previous_attempt_id: str = "",
                       metadata: dict | None = None) -> str:
        session, workspace = self._scope(session_id, workspace_path)
        experience_id = uuid.uuid4().hex
        with sqlite3.connect(self.db_path) as conn:
            if previous_attempt_id and not conn.execute(
                "SELECT 1 FROM coding_experiences WHERE id=? AND session_id=? AND workspace_path=?",
                (previous_attempt_id, session, workspace),
            ).fetchone():
                raise ValueError("Previous attempt does not belong to this conversation/workspace")
            conn.execute("""INSERT INTO coding_experiences
                (id, session_id, problem, failure, corrected_code, verified, created_at,
                 metadata, workspace_path, failed_code, failure_cause, tests, test_results,
                 previous_attempt_id) VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?,?)""",
                (experience_id, session, str(problem), str(failure or ""), str(correction),
                 time.time(), json.dumps({"source": "conversation", **(metadata or {})}, ensure_ascii=False),
                 workspace, str(failed_code or ""), str(failure_cause or ""),
                 json.dumps(tests or [], ensure_ascii=False),
                 json.dumps(test_results or [], ensure_ascii=False), previous_attempt_id))
        return experience_id

    def get_attempt(self, attempt_id: str, *, session_id: str, workspace_path: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return self._record(conn.execute(
                "SELECT * FROM coding_experiences WHERE id=? AND session_id=? AND workspace_path=?",
                (attempt_id, *self._scope(session_id, workspace_path)),
            ).fetchone())

    def latest_attempt(self, session_id: str, workspace_path: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return self._record(conn.execute(
                "SELECT * FROM coding_experiences WHERE session_id=? AND workspace_path=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                self._scope(session_id, workspace_path),
            ).fetchone())

    @staticmethod
    def is_success_report(text: str) -> bool:
        text = str(text or "").strip()
        if re.search(r"[?？]|아니|않|못|오답|틀|오류|에러|\b(?:not|wrong|incorrect|failed)\b", text, re.I):
            return False
        text = re.sub(r"실패\s*0\s*개", "", text).strip()
        if re.search(r"실패|성공\s*0\s*개", text):
            return False
        return bool(re.fullmatch(
            r"(?:(?:이번엔|이번에는|이제|드디어|응|네)[,\s]*)*"
            r"(?:(?:(?:전체|모든|전부|모두)\s*)?(?:테스트|케이스|예제)\s*(?:\d+\s*개)?\s*(?:가|는|를)?\s*)?"
            r"(?:(?:모두|전부|다)\s*)?"
            r"(?:통과(?:했어|했어요|했습니다|했네|됐어)?|정답(?:이야|이에요|입니다)?|"
            r"성공(?:했어|했어요|했습니다|이야|\s*[1-9]\d*\s*개)?|맞았(?:어|어요|습니다)|"
            r"잘\s*(?:됐어|돼|되네)|accepted|(?:all\s+tests\s+)?passed)"
            r"[.!。\s]*", text, re.I,
        ))

    def record_user_feedback(self, attempt_id: str, *, session_id: str,
                             workspace_path: str, feedback: str) -> dict | None:
        """Caller binds feedback to the adjacent code answer, never an older task."""
        success = self.is_success_report(feedback)
        failure = bool(re.search(
            r"아니|않|못|오답|틀|실패|오류|에러|이상|다시\s*(?:봐|보|확인|검토)|"
            r"\b(?:fail(?:ed)?|wrong|incorrect|not\s+(?:accepted|correct))\b",
            str(feedback or ""), re.I,
        ))
        if not success and not failure:
            return None
        report = "success" if success else "failure"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM coding_experiences WHERE id=? AND session_id=? AND workspace_path=?",
                (attempt_id, *self._scope(session_id, workspace_path)),
            ).fetchone()
            if row is None:
                return None
            # A later rejection revokes confidence in this exact attempt. A
            # repeated approval never walks backwards through older attempts.
            conn.execute("UPDATE coding_experiences SET user_report=?, user_feedback=?, "
                         "verified=?, verification_source=? WHERE id=?",
                         (report, feedback, int(bool(row["verified"]) and success),
                          row["verification_source"] if row["verified"] and success else "user_report",
                          attempt_id))
            return self._record(conn.execute(
                "SELECT * FROM coding_experiences WHERE id=?", (attempt_id,),
            ).fetchone())

    def record_test_result(self, attempt_id: str, *, session_id: str, workspace_path: str,
                           tested_code: str, test_results: list[dict]) -> dict | None:
        """Accept trusted runner evidence, never model review or user prose.

        Each executed result carries input, expected, actual and a boolean
        passed flag. A passing subset is evidence for those tests only.
        """
        if not test_results or not all(
            isinstance(item, dict) and item.get("executed") is True
            and type(item.get("passed")) is bool
            and all(key in item for key in ("input", "expected", "actual"))
            for item in test_results
        ):
            return None
        evidence = json.dumps(test_results, ensure_ascii=False)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM coding_experiences WHERE id=? AND session_id=? AND workspace_path=?",
                (attempt_id, *self._scope(session_id, workspace_path)),
            ).fetchone()
            if (row is None or not row["problem"] or not str(tested_code).strip()
                    or str(tested_code).strip() != _code_text(row["corrected_code"])):
                return None
            passed = all(item["passed"] and item["expected"] == item["actual"] for item in test_results)
            conn.execute("UPDATE coding_experiences SET verified=?, verification_source='executed_tests', "
                         "test_results=? WHERE id=?", (int(passed), evidence, attempt_id))
            return self._record(conn.execute(
                "SELECT * FROM coding_experiences WHERE id=?", (attempt_id,),
            ).fetchone())

    def verified_documents(self, *, session_id: str, workspace_path: str,
                           attempt_id: str | None = None, limit: int = 20) -> list[dict]:
        query = ("SELECT * FROM coding_experiences WHERE verified=1 "
                 "AND verification_source='executed_tests' AND session_id=? AND workspace_path=?")
        params = list(self._scope(session_id, workspace_path))
        if attempt_id is not None:
            query += " AND id=?"
            params.append(attempt_id)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(max(0, min(int(limit), 100)))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [self._record(row) for row in conn.execute(query, params)]

    def rag_document(self, attempt_id: str, *, session_id: str, workspace_path: str,
                     namespace: str) -> dict | None:
        records = self.verified_documents(
            session_id=session_id, workspace_path=workspace_path, attempt_id=attempt_id,
        )
        if not records:
            return None
        record = records[0]
        expected_namespace = record["metadata"].get("memory_namespace")
        if not expected_namespace:
            from core.workspace import WorkspaceManager
            expected_namespace = (WorkspaceManager.namespace_for(record["workspace_path"])
                                  if record["workspace_path"] else "global")
        if namespace != expected_namespace:
            return None
        text = (f"문제: {record['problem']}\n이전 실패 코드:\n{record['failed_code']}\n"
                f"실패 피드백: {record['failure']}\n확인된 원인: {record['failure_cause']}\n"
                f"수정 답변:\n{record['correction']}\n"
                "실행한 테스트 범위에서 확인된 결과 (전체 정답 보장 아님):\n"
                + json.dumps(record["test_results"], ensure_ascii=False))
        return {
            "text": text, "doc_id": f"coding-experience-{attempt_id}", "namespace": namespace,
            "metadata": {
                "source_type": "verified_coding_experience", "verified": True,
                "verification_source": "executed_tests", "coding_attempt_id": attempt_id,
                "coding_session_id": record["session_id"],
                "coding_workspace_path": record["workspace_path"],
                "coding_memory_namespace": namespace,
            },
        }


_store: CodingExperienceStore | None = None


def get_coding_experience_store() -> CodingExperienceStore:
    global _store
    if _store is None:
        _store = CodingExperienceStore()
    return _store
