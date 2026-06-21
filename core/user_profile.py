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


# Singleton instance
_user_profile = None


def get_user_profile() -> UserProfile:
    global _user_profile
    if _user_profile is None:
        _user_profile = UserProfile()
    return _user_profile
