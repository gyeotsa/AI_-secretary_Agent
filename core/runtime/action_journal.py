"""
Action Journal for Jarvis Runtime
- 모든 행동 기록
- 감사·롤백 용도
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import json
import sqlite3

@dataclass
class ActionRecord:
    id: str
    action_type: str
    description: str
    source: str
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    success: bool = True
    error: Optional[str] = None

class ActionJournal:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or "data/action_journal.db"
        Path(self.db_path).parent.mkdir(exist_ok=True)
        self._init_db()
        
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY,
                action_type TEXT NOT NULL,
                description TEXT NOT NULL,
                source TEXT NOT NULL,
                data TEXT,
                timestamp TEXT NOT NULL,
                success INTEGER DEFAULT 1,
                error TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON actions(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_type ON actions(action_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_source ON actions(source)")
        conn.commit()
        conn.close()
        
    def record(self, action_type: str, description: str, source: str, 
               data: Optional[Dict[str, Any]] = None, success: bool = True, 
               error: Optional[str] = None) -> str:
        """행동 기록"""
        import uuid
        action_id = str(uuid.uuid4())
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO actions (id, action_type, description, source, data, timestamp, success, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            action_id,
            action_type,
            description,
            source,
            json.dumps(data or {}, ensure_ascii=False),
            datetime.now().isoformat(),
            1 if success else 0,
            error
        ))
        conn.commit()
        conn.close()
        # Event Bus로 발행
        try:
            from core.runtime.event_bus import get_event_bus, Event
            bus = get_event_bus()
            bus.publish(Event(
                type="action_recorded",
                source="action_journal",
                data={
                    "action_id": action_id,
                    "action_type": action_type,
                    "description": description,
                    "source": source,
                    "success": success
                }
            ))
        except Exception as e:
            pass
        return action_id
        
    def get_recent(self, limit: int = 100) -> List[ActionRecord]:
        """최근 기록 조회"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, action_type, description, source, data, timestamp, success, error
            FROM actions ORDER BY timestamp DESC LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        conn.close()
        records = []
        for row in rows:
            records.append(ActionRecord(
                id=row[0],
                action_type=row[1],
                description=row[2],
                source=row[3],
                data=json.loads(row[4]) if row[4] else {},
                timestamp=datetime.fromisoformat(row[5]),
                success=bool(row[6]),
                error=row[7]
            ))
        return records
        
    def get_by_type(self, action_type: str, limit: int = 100) -> List[ActionRecord]:
        """타입별 기록 조회"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, action_type, description, source, data, timestamp, success, error
            FROM actions WHERE action_type = ? ORDER BY timestamp DESC LIMIT ?
        """, (action_type, limit))
        rows = cursor.fetchall()
        conn.close()
        records = []
        for row in rows:
            records.append(ActionRecord(
                id=row[0],
                action_type=row[1],
                description=row[2],
                source=row[3],
                data=json.loads(row[4]) if row[4] else {},
                timestamp=datetime.fromisoformat(row[5]),
                success=bool(row[6]),
                error=row[7]
            ))
        return records

# Singleton
_action_journal = None

def get_action_journal() -> ActionJournal:
    global _action_journal
    if _action_journal is None:
        _action_journal = ActionJournal()
    return _action_journal
