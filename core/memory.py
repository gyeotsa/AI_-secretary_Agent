import sqlite3
import json
from datetime import datetime
from typing import List, Dict, Optional, Any
from config import Config
import os
from dataclasses import dataclass, asdict


@dataclass
class MemoryItem:
    """기본 메모리 아이템"""
    id: Optional[int] = None
    content: str = ""
    metadata: Dict[str, Any] = None
    timestamp: float = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if self.timestamp is None:
            self.timestamp = datetime.now().timestamp()


@dataclass
class Episode(MemoryItem):
    """에피소드 메모리: 대화 세션, 이벤트 기록"""
    session_id: str = ""
    role: str = ""  # user, assistant, system
    summary: str = ""
    importance: float = 0.0  # 중요도 (0.0 ~ 1.0)


@dataclass
class SemanticMemory(MemoryItem):
    """시맨틱 메모리: 의미 기반 지식, 사실, 개념"""
    key: str = ""  # 검색 키
    category: str = ""  # 카테고리 (예: 사용자_프로필, 프로젝트_정보, 기술_지식)
    embedding: Optional[List[float]] = None  # 벡터 임베딩 (나중에 사용)


class EpisodeMemoryManager:
    """에피소드 메모리 관리자: 대화 기록, 이벤트 로그"""
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or Config.DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT,
                importance REAL DEFAULT 0.0,
                metadata TEXT,
                timestamp REAL NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes(timestamp)")
        conn.commit()
        conn.close()
        
    def add_episode(self, episode: Episode) -> int:
        """에피소드 추가"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO episodes (session_id, role, content, summary, importance, metadata, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            episode.session_id,
            episode.role,
            episode.content,
            episode.summary,
            episode.importance,
            json.dumps(episode.metadata, ensure_ascii=False),
            episode.timestamp
        ))
        episode_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return episode_id
        
    def get_session_episodes(self, session_id: str, limit: Optional[int] = None) -> List[Episode]:
        """세션의 에피소드 가져오기"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        query = "SELECT id, session_id, role, content, summary, importance, metadata, timestamp FROM episodes WHERE session_id = ? ORDER BY timestamp ASC"
        if limit:
            query += f" LIMIT {limit}"
        cursor.execute(query, (session_id,))
        rows = cursor.fetchall()
        conn.close()
        
        episodes = []
        for row in rows:
            episodes.append(Episode(
                id=row[0],
                session_id=row[1],
                role=row[2],
                content=row[3],
                summary=row[4],
                importance=row[5],
                metadata=json.loads(row[6]) if row[6] else {},
                timestamp=row[7]
            ))
        return episodes
        
    def get_recent_episodes(self, limit: int = 50) -> List[Episode]:
        """최근 에피소드 가져오기"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, session_id, role, content, summary, importance, metadata, timestamp 
            FROM episodes 
            ORDER BY timestamp DESC 
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        conn.close()
        
        episodes = []
        for row in rows:
            episodes.append(Episode(
                id=row[0],
                session_id=row[1],
                role=row[2],
                content=row[3],
                summary=row[4],
                importance=row[5],
                metadata=json.loads(row[6]) if row[6] else {},
                timestamp=row[7]
            ))
        return episodes[::-1]  # 시간순으로 정렬
        
    def search_episodes(self, query: str, limit: int = 10) -> List[Episode]:
        """에피소드 검색 (간단한 텍스트 매치)"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, session_id, role, content, summary, importance, metadata, timestamp 
            FROM episodes 
            WHERE content LIKE ? OR summary LIKE ?
            ORDER BY timestamp DESC 
            LIMIT ?
        """, (f"%{query}%", f"%{query}%", limit))
        rows = cursor.fetchall()
        conn.close()
        
        episodes = []
        for row in rows:
            episodes.append(Episode(
                id=row[0],
                session_id=row[1],
                role=row[2],
                content=row[3],
                summary=row[4],
                importance=row[5],
                metadata=json.loads(row[6]) if row[6] else {},
                timestamp=row[7]
            ))
        return episodes
        
    def list_sessions(self) -> List[Dict[str, Any]]:
        """모든 세션 목록 가져오기"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT session_id, MIN(timestamp) as start_time, MAX(timestamp) as end_time, COUNT(*) as message_count
            FROM episodes
            GROUP BY session_id
            ORDER BY start_time DESC
        """)
        rows = cursor.fetchall()
        conn.close()
        
        sessions = []
        for row in rows:
            sessions.append({
                "session_id": row[0],
                "start_time": datetime.fromtimestamp(row[1]).isoformat() if row[1] else "",
                "end_time": datetime.fromtimestamp(row[2]).isoformat() if row[2] else "",
                "message_count": row[3]
            })
        return sessions


class SemanticMemoryManager:
    """시맨틱 메모리 관리자: 의미 기반 지식 저장"""
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or Config.DB_PATH.replace(".db", "_semantic.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS semantic_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT,
                timestamp REAL NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_semantic_key ON semantic_memory(key)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_semantic_category ON semantic_memory(category)")
        conn.commit()
        conn.close()
        
    def add_memory(self, memory: SemanticMemory) -> int:
        """시맨틱 메모리 추가 또는 업데이트"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT OR REPLACE INTO semantic_memory (key, category, content, metadata, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (
                memory.key,
                memory.category,
                memory.content,
                json.dumps(memory.metadata, ensure_ascii=False),
                memory.timestamp
            ))
            memory_id = cursor.lastrowid
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
        return memory_id
        
    def get_memory(self, key: str) -> Optional[SemanticMemory]:
        """키로 메모리 가져오기"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, key, category, content, metadata, timestamp 
            FROM semantic_memory 
            WHERE key = ?
        """, (key,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return SemanticMemory(
                id=row[0],
                key=row[1],
                category=row[2],
                content=row[3],
                metadata=json.loads(row[4]) if row[4] else {},
                timestamp=row[5]
            )
        return None
        
    def get_memories_by_category(self, category: str) -> List[SemanticMemory]:
        """카테고리별 메모리 가져오기"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, key, category, content, metadata, timestamp 
            FROM semantic_memory 
            WHERE category = ?
            ORDER BY timestamp DESC
        """, (category,))
        rows = cursor.fetchall()
        conn.close()
        
        memories = []
        for row in rows:
            memories.append(SemanticMemory(
                id=row[0],
                key=row[1],
                category=row[2],
                content=row[3],
                metadata=json.loads(row[4]) if row[4] else {},
                timestamp=row[5]
            ))
        return memories
        
    def search_memories(self, query: str, category: Optional[str] = None, limit: int = 10) -> List[SemanticMemory]:
        """메모리 검색"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if category:
            cursor.execute("""
                SELECT id, key, category, content, metadata, timestamp 
                FROM semantic_memory 
                WHERE category = ? AND (content LIKE ? OR key LIKE ?)
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (category, f"%{query}%", f"%{query}%", limit))
        else:
            cursor.execute("""
                SELECT id, key, category, content, metadata, timestamp 
                FROM semantic_memory 
                WHERE content LIKE ? OR key LIKE ?
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (f"%{query}%", f"%{query}%", limit))
        
        rows = cursor.fetchall()
        conn.close()
        
        memories = []
        for row in rows:
            memories.append(SemanticMemory(
                id=row[0],
                key=row[1],
                category=row[2],
                content=row[3],
                metadata=json.loads(row[4]) if row[4] else {},
                timestamp=row[5]
            ))
        return memories
        
    def delete_memory(self, key: str) -> bool:
        """메모리 삭제"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM semantic_memory WHERE key = ?", (key,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted


# 기존 ConversationMemory 유지 (하위 호환성)
class ConversationMemory:
    def __init__(self):
        self.episode_manager = EpisodeMemoryManager()
        
    def init_db(self):
        # episode_manager가 이미 초기화함
        pass
        
    def save_message(self, session_id: str, role: str, content: str):
        episode = Episode(
            session_id=session_id,
            role=role,
            content=content
        )
        self.episode_manager.add_episode(episode)
        
    def load_session(self, session_id: str) -> List[Dict[str, str]]:
        episodes = self.episode_manager.get_session_episodes(session_id)
        return [{"role": e.role, "content": e.content} for e in episodes]
        
    def list_sessions(self) -> List[tuple]:
        sessions = self.episode_manager.list_sessions()
        return [(s["session_id"], s["start_time"]) for s in sessions]


# 싱글톤 인스턴스
_episode_manager = None
_semantic_manager = None
_memory = None


def get_episode_memory() -> EpisodeMemoryManager:
    global _episode_manager
    if _episode_manager is None:
        _episode_manager = EpisodeMemoryManager()
    return _episode_manager


def get_semantic_memory() -> SemanticMemoryManager:
    global _semantic_manager
    if _semantic_manager is None:
        _semantic_manager = SemanticMemoryManager()
    return _semantic_manager


def get_memory() -> ConversationMemory:
    global _memory
    if _memory is None:
        _memory = ConversationMemory()
    return _memory


def build_memory_context(session_id: str, max_episodes: int = 20, include_semantic: bool = True) -> str:
    """LLM 컨텍스트용 메모리 빌더"""
    context_parts = []
    
    # 1. 최근 대화 에피소드
    episode_manager = get_episode_memory()
    episodes = episode_manager.get_session_episodes(session_id, limit=max_episodes)
    
    if episodes:
        context_parts.append("## 최근 대화 기록")
        for ep in episodes:
            time_str = datetime.fromtimestamp(ep.timestamp).strftime("%H:%M:%S")
            context_parts.append(f"[{time_str}] {ep.role}: {ep.content}")
        context_parts.append("")
    
    # 2. 시맨틱 메모리 (사용자 프로필, 프로젝트 정보 등)
    if include_semantic:
        semantic_manager = get_semantic_memory()
        
        # 사용자 프로필
        user_memories = semantic_manager.get_memories_by_category("사용자_프로필")
        if user_memories:
            context_parts.append("## 사용자 정보")
            for mem in user_memories:
                context_parts.append(f"- {mem.key}: {mem.content}")
            context_parts.append("")
        
        # 프로젝트 정보
        project_memories = semantic_manager.get_memories_by_category("프로젝트_정보")
        if project_memories:
            context_parts.append("## 프로젝트 정보")
            for mem in project_memories:
                context_parts.append(f"- {mem.key}: {mem.content}")
            context_parts.append("")
    
    return "\n".join(context_parts)
