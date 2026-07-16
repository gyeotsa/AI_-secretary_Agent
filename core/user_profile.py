import sqlite3
import json
from datetime import datetime
from config import Config
import os


class UserProfile:
    def __init__(self):
        os.makedirs(os.path.dirname(Config.DB_PATH), exist_ok=True)
        self.db_path = Config.DB_PATH
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_profile
                 (key TEXT PRIMARY KEY,
                  value TEXT,
                  updated_at TEXT)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_preferences
                 (pref_key TEXT PRIMARY KEY,
                  pref_value TEXT,
                  updated_at TEXT)"""
        )
        # Personal Memory 테이블 추가
        conn.execute(
            """CREATE TABLE IF NOT EXISTS activity_log
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  activity_type TEXT,
                  description TEXT,
                  details TEXT,
                  timestamp TEXT)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS favorite_paths
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  path TEXT UNIQUE,
                  name TEXT,
                  usage_count INTEGER DEFAULT 0,
                  last_used TEXT)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS favorite_commands
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  command TEXT UNIQUE,
                  description TEXT,
                  usage_count INTEGER DEFAULT 0,
                  last_used TEXT)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS project_settings
                 (project_path TEXT PRIMARY KEY,
                  settings TEXT,
                  last_used TEXT)"""
        )
        conn.commit()
        conn.close()

    def set(self, key: str, value: str) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "REPLACE INTO user_profile VALUES (?, ?, ?)",
            (key, value, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

    def get(self, key: str, default: str = "") -> str:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT value FROM user_profile WHERE key=?",
            (key,),
        ).fetchone()
        conn.close()
        return row[0] if row else default

    def get_all(self) -> dict:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT key, value FROM user_profile").fetchall()
        conn.close()
        return {row[0]: row[1] for row in rows}

    def delete(self, key: str) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM user_profile WHERE key=?", (key,))
        conn.commit()
        conn.close()

    def set_preference(self, pref_key: str, pref_value: str) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "REPLACE INTO user_preferences VALUES (?, ?, ?)",
            (pref_key, pref_value, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

    def get_preference(self, pref_key: str, default: str = "") -> str:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT pref_value FROM user_preferences WHERE pref_key=?",
            (pref_key,),
        ).fetchone()
        conn.close()
        return row[0] if row else default

    def get_all_preferences(self) -> dict:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT pref_key, pref_value FROM user_preferences").fetchall()
        conn.close()
        return {row[0]: row[1] for row in rows}

    def get_profile_summary(self) -> str:
        profile = self.get_all()
        preferences = self.get_all_preferences()

        summary = ["[사용자 프로필]"]
        for key, value in profile.items():
            summary.append(f"- {key}: {value}")

        if preferences:
            summary.append("\n[사용자 환경설정]")
            for key, value in preferences.items():
                summary.append(f"- {key}: {value}")

        return "\n".join(summary) if len(summary) > 1 else "저장된 사용자 프로필이 없습니다."

    # ------------------------------
    # Personal Memory: Activity Log
    # ------------------------------
    def log_activity(self, activity_type: str, description: str, details: str = "") -> None:
        """사용자 활동을 로그에 기록합니다."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO activity_log (activity_type, description, details, timestamp) VALUES (?, ?, ?, ?)",
            (activity_type, description, details, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

    def get_activity_log(self, limit: int = 50) -> list:
        """최근 활동 로그를 가져옵니다."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT id, activity_type, description, details, timestamp FROM activity_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [
            {
                "id": row[0],
                "activity_type": row[1],
                "description": row[2],
                "details": row[3],
                "timestamp": row[4]
            }
            for row in rows
        ]

    # ------------------------------
    # Personal Memory: Favorite Paths
    # ------------------------------
    def add_favorite_path(self, path: str, name: str = "") -> None:
        """자주 사용하는 경로를 추가하거나 업데이트합니다."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now().isoformat()
        # 기존 경로가 있는지 확인
        existing = conn.execute("SELECT id, usage_count FROM favorite_paths WHERE path=?", (path,)).fetchone()
        if existing:
            # 기존 경로 업데이트 (사용 횟수 증가)
            conn.execute(
                "UPDATE favorite_paths SET usage_count=?, last_used=? WHERE id=?",
                (existing[1] + 1, now, existing[0]),
            )
        else:
            # 새 경로 추가
            conn.execute(
                "INSERT INTO favorite_paths (path, name, usage_count, last_used) VALUES (?, ?, 1, ?)",
                (path, name if name else path, now),
            )
        conn.commit()
        conn.close()

    def get_favorite_paths(self) -> list:
        """자주 사용하는 경로 목록을 가져옵니다 (사용 횟수 순)."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT path, name, usage_count, last_used FROM favorite_paths ORDER BY usage_count DESC"
        ).fetchall()
        conn.close()
        return [
            {
                "path": row[0],
                "name": row[1],
                "usage_count": row[2],
                "last_used": row[3]
            }
            for row in rows
        ]

    # ------------------------------
    # Personal Memory: Favorite Commands
    # ------------------------------
    def add_favorite_command(self, command: str, description: str = "") -> None:
        """자주 사용하는 명령을 추가하거나 업데이트합니다."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now().isoformat()
        existing = conn.execute("SELECT id, usage_count FROM favorite_commands WHERE command=?", (command,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE favorite_commands SET usage_count=?, last_used=? WHERE id=?",
                (existing[1] + 1, now, existing[0]),
            )
        else:
            conn.execute(
                "INSERT INTO favorite_commands (command, description, usage_count, last_used) VALUES (?, ?, 1, ?)",
                (command, description, now),
            )
        conn.commit()
        conn.close()

    def get_favorite_commands(self) -> list:
        """자주 사용하는 명령 목록을 가져옵니다 (사용 횟수 순)."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT command, description, usage_count, last_used FROM favorite_commands ORDER BY usage_count DESC"
        ).fetchall()
        conn.close()
        return [
            {
                "command": row[0],
                "description": row[1],
                "usage_count": row[2],
                "last_used": row[3]
            }
            for row in rows
        ]

    # ------------------------------
    # Personal Memory: Project Settings
    # ------------------------------
    def set_project_settings(self, project_path: str, settings: dict) -> None:
        """프로젝트별 설정을 저장합니다."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now().isoformat()
        conn.execute(
            "REPLACE INTO project_settings (project_path, settings, last_used) VALUES (?, ?, ?)",
            (project_path, json.dumps(settings, ensure_ascii=False), now),
        )
        conn.commit()
        conn.close()

    def get_project_settings(self, project_path: str) -> dict:
        """프로젝트별 설정을 가져옵니다."""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT settings FROM project_settings WHERE project_path=?",
            (project_path,),
        ).fetchone()
        conn.close()
        return json.loads(row[0]) if row else {}

    def get_all_project_settings(self) -> dict:
        """모든 프로젝트 설정을 가져옵니다."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT project_path, settings FROM project_settings").fetchall()
        conn.close()
        return {row[0]: json.loads(row[1]) for row in rows}


# Singleton instance
_user_profile = None


def get_user_profile() -> UserProfile:
    global _user_profile
    if _user_profile is None:
        _user_profile = UserProfile()
    return _user_profile
