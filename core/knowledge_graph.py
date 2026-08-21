import sqlite3
import json
from datetime import datetime
from typing import List, Dict, Optional, Any
from config import Config
import os


class Entity:
    """지식 그래프의 엔티티"""
    def __init__(self, name: str, entity_type: str, metadata: Optional[Dict] = None):
        self.id: Optional[int] = None
        self.name = name
        self.entity_type = entity_type
        self.metadata = metadata or {}
        self.created_at = datetime.now().isoformat()
        self.updated_at = datetime.now().isoformat()
        
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.entity_type,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }


class Relation:
    """지식 그래프의 관계"""
    def __init__(self, from_entity: str, relation_type: str, to_entity: str, metadata: Optional[Dict] = None):
        self.id: Optional[int] = None
        self.from_entity = from_entity
        self.relation_type = relation_type
        self.to_entity = to_entity
        self.metadata = metadata or {}
        self.created_at = datetime.now().isoformat()
        
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "from": self.from_entity,
            "relation": self.relation_type,
            "to": self.to_entity,
            "metadata": self.metadata,
            "created_at": self.created_at
        }


class KnowledgeGraph:
    """로컬 지식 그래프"""
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.join(os.path.dirname(Config.DB_PATH), "knowledge_graph.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        
    def _init_db(self):
        """데이터베이스 초기화"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 엔티티 테이블
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                metadata TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(name, type)
            )
        """)
        
        # 관계 테이블
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_entity TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                to_entity TEXT NOT NULL,
                metadata TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(from_entity, relation_type, to_entity)
            )
        """)
        
        # 인덱스
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_entity)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_entity)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_relations_type ON relations(relation_type)")
        
        conn.commit()
        conn.close()
        
    def add_entity(self, entity: Entity) -> int:
        """엔티티 추가 또는 업데이트"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            metadata_json = json.dumps(entity.metadata, ensure_ascii=False)
            cursor.execute("""
                INSERT OR REPLACE INTO entities (name, type, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                entity.name,
                entity.entity_type,
                metadata_json,
                entity.created_at,
                entity.updated_at
            ))
            entity_id = cursor.lastrowid
            
            # 기존 ID가 있으면 업데이트
            if entity.id:
                entity_id = entity.id
                
            conn.commit()
            return entity_id
            
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
            
    def get_entity(self, name: str, entity_type: Optional[str] = None) -> Optional[Entity]:
        """엔티티 조회"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            if entity_type:
                cursor.execute("""
                    SELECT id, name, type, metadata, created_at, updated_at 
                    FROM entities 
                    WHERE name = ? AND type = ?
                """, (name, entity_type))
            else:
                cursor.execute("""
                    SELECT id, name, type, metadata, created_at, updated_at 
                    FROM entities 
                    WHERE name = ?
                """, (name,))
                
            row = cursor.fetchone()
            if not row:
                return None
                
            entity = Entity(name=row[1], entity_type=row[2], metadata=json.loads(row[3]) if row[3] else {})
            entity.id = row[0]
            entity.created_at = row[4]
            entity.updated_at = row[5]
            return entity
            
        finally:
            conn.close()
            
    def search_entities(self, query: str, entity_type: Optional[str] = None, limit: int = 20) -> List[Entity]:
        """엔티티 검색"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            if entity_type:
                cursor.execute("""
                    SELECT id, name, type, metadata, created_at, updated_at 
                    FROM entities 
                    WHERE (name LIKE ? OR metadata LIKE ?) AND type = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                """, (f"%{query}%", f"%{query}%", entity_type, limit))
            else:
                cursor.execute("""
                    SELECT id, name, type, metadata, created_at, updated_at 
                    FROM entities 
                    WHERE name LIKE ? OR metadata LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                """, (f"%{query}%", f"%{query}%", limit))
                
            rows = cursor.fetchall()
            entities = []
            
            for row in rows:
                entity = Entity(name=row[1], entity_type=row[2], metadata=json.loads(row[3]) if row[3] else {})
                entity.id = row[0]
                entity.created_at = row[4]
                entity.updated_at = row[5]
                entities.append(entity)
                
            return entities
            
        finally:
            conn.close()
            
    def delete_entity(self, name: str, entity_type: Optional[str] = None) -> bool:
        """엔티티 삭제"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            if entity_type:
                cursor.execute("DELETE FROM entities WHERE name = ? AND type = ?", (name, entity_type))
            else:
                cursor.execute("DELETE FROM entities WHERE name = ?", (name,))
                
            # 관련 관계도 삭제
            cursor.execute("DELETE FROM relations WHERE from_entity = ? OR to_entity = ?", (name, name))
            
            conn.commit()
            return cursor.rowcount > 0
            
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
            
    def add_relation(self, relation: Relation) -> int:
        """관계 추가"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            metadata_json = json.dumps(relation.metadata, ensure_ascii=False)
            cursor.execute("""
                INSERT OR REPLACE INTO relations (from_entity, relation_type, to_entity, metadata, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                relation.from_entity,
                relation.relation_type,
                relation.to_entity,
                metadata_json,
                relation.created_at
            ))
            relation_id = cursor.lastrowid
            conn.commit()
            return relation_id
            
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
            
    def get_relations(self, entity_name: str, direction: str = "both") -> List[Relation]:
        """엔티티의 관계 조회"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            if direction == "from":
                cursor.execute("""
                    SELECT id, from_entity, relation_type, to_entity, metadata, created_at 
                    FROM relations 
                    WHERE from_entity = ?
                    ORDER BY created_at DESC
                """, (entity_name,))
            elif direction == "to":
                cursor.execute("""
                    SELECT id, from_entity, relation_type, to_entity, metadata, created_at 
                    FROM relations 
                    WHERE to_entity = ?
                    ORDER BY created_at DESC
                """, (entity_name,))
            else:
                cursor.execute("""
                    SELECT id, from_entity, relation_type, to_entity, metadata, created_at 
                    FROM relations 
                    WHERE from_entity = ? OR to_entity = ?
                    ORDER BY created_at DESC
                """, (entity_name, entity_name))
                
            rows = cursor.fetchall()
            relations = []
            
            for row in rows:
                relation = Relation(
                    from_entity=row[1],
                    relation_type=row[2],
                    to_entity=row[3],
                    metadata=json.loads(row[4]) if row[4] else {}
                )
                relation.id = row[0]
                relation.created_at = row[5]
                relations.append(relation)
                
            return relations
            
        finally:
            conn.close()
            
    def delete_relation(self, from_entity: str, relation_type: str, to_entity: str) -> bool:
        """관계 삭제"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                DELETE FROM relations 
                WHERE from_entity = ? AND relation_type = ? AND to_entity = ?
            """, (from_entity, relation_type, to_entity))
            
            conn.commit()
            return cursor.rowcount > 0
            
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
            
    def get_subgraph(self, entity_name: str, depth: int = 2) -> Dict:
        """서브그래프 조회 (엔티티 + 관계)"""
        entities: Dict[str, Entity] = {}
        relations: List[Relation] = []
        
        # BFS로 그래프 탐색
        queue = [(entity_name, 0)]
        visited = set()
        
        while queue:
            current_name, current_depth = queue.pop(0)
            
            if current_name in visited or current_depth > depth:
                continue
                
            visited.add(current_name)
            
            # 엔티티 조회
            entity = self.get_entity(current_name)
            if entity:
                entities[current_name] = entity
                
            # 관계 조회
            entity_relations = self.get_relations(current_name)
            relations.extend(entity_relations)
            
            # 다음 레벨 엔티티 추가
            for rel in entity_relations:
                if rel.from_entity == current_name and rel.to_entity not in visited:
                    queue.append((rel.to_entity, current_depth + 1))
                if rel.to_entity == current_name and rel.from_entity not in visited:
                    queue.append((rel.from_entity, current_depth + 1))
                    
        return {
            "entities": [e.to_dict() for e in entities.values()],
            "relations": [r.to_dict() for r in relations]
        }
        
    def add_triple(self, subject: str, predicate: str, object_: str, 
                   subject_type: str = "thing", object_type: str = "thing",
                   metadata: Optional[Dict] = None) -> int:
        """트리플 추가 (편의 메서드)"""
        # 주어 엔티티 추가
        subject_entity = self.get_entity(subject, subject_type)
        if not subject_entity:
            subject_entity = Entity(name=subject, entity_type=subject_type)
            self.add_entity(subject_entity)
            
        # 목적어 엔티티 추가
        object_entity = self.get_entity(object_, object_type)
        if not object_entity:
            object_entity = Entity(name=object_, entity_type=object_type)
            self.add_entity(object_entity)
            
        # 관계 추가
        relation = Relation(from_entity=subject, relation_type=predicate, to_entity=object_, metadata=metadata)
        return self.add_relation(relation)
        
    def get_context_for_llm(self, query: str, max_entities: int = 10, max_depth: int = 2) -> str:
        """LLM용 컨텍스트 생성"""
        # 쿼리와 관련된 엔티티 검색
        entities = self.search_entities(query, limit=max_entities)
        
        if not entities:
            return ""
            
        context_parts = ["### 관련 지식 그래프 ###"]
        
        for entity in entities:
            subgraph = self.get_subgraph(entity.name, depth=max_depth)
            
            context_parts.append(f"\n- 엔티티: {entity.name} ({entity.entity_type})")
            if entity.metadata:
                context_parts.append(f"  메타데이터: {json.dumps(entity.metadata, ensure_ascii=False)}")
                
            # 관계 추가
            for rel in subgraph["relations"]:
                if rel["from"] == entity.name:
                    context_parts.append(f"  → {rel['relation']} → {rel['to']}")
                else:
                    context_parts.append(f"  ← {rel['relation']} ← {rel['from']}")
                    
        return "\n".join(context_parts)


# 싱글톤 인스턴스
_knowledge_graph = None


def get_knowledge_graph() -> KnowledgeGraph:
    global _knowledge_graph
    if _knowledge_graph is None:
        _knowledge_graph = KnowledgeGraph()
    return _knowledge_graph
