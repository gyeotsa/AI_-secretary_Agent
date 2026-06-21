import sqlite3
import json
from datetime import datetime
from config import Config
import os


class ConversationMemory:
    def __init__(self):
        os.makedirs(os.path.dirname(Config.DB_PATH), exist_ok=True)
        self.db_path = Config.DB_PATH
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT,
                  role TEXT,
                  content TEXT,
                  timestamp TEXT)"""
        )
        conn.commit()
        conn.close()

    def save_message(self, session_id: str, role: str, content: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO conversations VALUES (NULL, ?, ?, ?, ?)",
            (session_id, role, content, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

    def load_session(self, session_id: str) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT role, content FROM conversations WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in rows]

    def list_sessions(self) -> list[tuple[str, str]]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT DISTINCT session_id, 
                      MIN(timestamp) as start_time
               FROM conversations 
               GROUP BY session_id 
               ORDER BY start_time DESC"""
        ).fetchall()
        conn.close()
        return rows


# Singleton instance
_memory = None


def get_memory() -> ConversationMemory:
    global _memory
    if _memory is None:
        _memory = ConversationMemory()
    return _memory
