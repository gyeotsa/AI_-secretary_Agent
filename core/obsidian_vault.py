"""Obsidian-compatible, auditable knowledge vault for Anis.

Obsidian is only the editor/viewer.  This module owns classification, atomic notes,
wikilinks, bounded graph traversal and RAG synchronization.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote
import hashlib
import json
import os
import re
import sqlite3
import threading
import time

from config import Config
from core.harness import SafetyLayer
from core.knowledge_memory import KnowledgeRecord, MemoryKind, SensitiveMemoryPolicy


class ObsidianVaultError(ValueError):
    pass


class ObsidianVault:
    WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
    FOLDERS = (
        "raw/conversations", "wiki/facts", "wiki/preferences", "wiki/projects",
        "wiki/settings", "wiki/tasks", "wiki/cases", "wiki/questions",
        "wiki/chitchat", "wiki/patterns", "wiki/topics", "wiki/actions", "derived", "prompts",
    )

    def __init__(self, root: str | Path | None = None, *, rag=None,
                 settings_path: str | Path | None = None):
        self.settings_path = Path(settings_path or Path(Config.DB_PATH).with_name("obsidian_settings.json"))
        self._lock = threading.RLock()
        configured = root or self._load_settings().get("vault_path") or Config.OBSIDIAN_VAULT_PATH
        self.root = Path(configured).expanduser().resolve()
        self.rag = rag
        self._ensure_structure()

    def _load_settings(self) -> dict:
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def configure(self, root: str | Path) -> Path:
        target = Path(root).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise ObsidianVaultError("Vault 경로는 폴더여야 합니다.")
        with self._lock:
            self.root = target
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.settings_path.with_suffix(".tmp")
            temp.write_text(json.dumps({"vault_path": str(target)}, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.settings_path)
            self._ensure_structure()
        return target

    def _ensure_structure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in self.FOLDERS:
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        index = self.root / "wiki" / "index.md"
        if not index.exists():
            self._atomic_write(index, """---
type: index
managed_by: anis
---
# Anis Knowledge Vault

- [[topics/index|주제 지도]]
- [[actions/index|행동 지도]]
- `raw/`는 감사용 원문이며 기본 RAG 검색에서 제외됩니다.
- `wiki/`만 아니스의 활성 지식으로 사용됩니다.
""")
        for kind in ("topics", "actions"):
            path = self.root / "wiki" / kind / "index.md"
            if not path.exists():
                self._atomic_write(path, f"---\ntype: {kind}_index\nmanaged_by: anis\n---\n# {kind.title()} Index\n")

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ObsidianVaultError("Vault 밖의 경로에는 접근할 수 없습니다.") from exc
        return resolved

    def _atomic_write(self, path: Path, content: str) -> None:
        path = self._inside(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(content.rstrip() + "\n", encoding="utf-8")
        temp.replace(path)

    @staticmethod
    def _slug(value: str, fallback: str = "note") -> str:
        value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", " ", str(value))
        value = re.sub(r"\s+", "-", value.strip()).strip(".-")[:80]
        return value or fallback

    @staticmethod
    def _topics(record: KnowledgeRecord, maximum: int = 5) -> list[str]:
        stop = {"사용자", "프로젝트", "그리고", "하지만", "대한", "관련", "내용", "설정"}
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{2,}|[가-힣]{2,}", f"{record.subject} {record.content}")
        counts: dict[str, int] = {}
        for token in tokens:
            normalized = token.casefold()
            if normalized not in stop:
                counts[normalized] = counts.get(normalized, 0) + 1
        return [item[0] for item in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:maximum]]

    @staticmethod
    def _action(record: KnowledgeRecord) -> str:
        if record.kind == MemoryKind.PREFERENCE:
            return "preference"
        if record.kind == MemoryKind.PROJECT:
            return "decision"
        if record.kind == MemoryKind.TASK:
            return "action"
        if "설정" in record.subject or record.predicate in {"configures", "sets"}:
            return "setting"
        return "reference"

    @staticmethod
    def _importance(record: KnowledgeRecord) -> float:
        base = {
            MemoryKind.PREFERENCE: 0.9, MemoryKind.PROJECT: 0.88,
            MemoryKind.TASK: 0.84, MemoryKind.FACT: 0.78,
            MemoryKind.CASE: 0.72, MemoryKind.CONVERSATION: 0.35,
        }[record.kind]
        if re.search(r"기억|잊지|항상|앞으로|반드시|설정|정정", record.content):
            base += 0.08
        return round(min(1.0, base * 0.75 + record.confidence * 0.25), 3)

    @staticmethod
    def _folder(record: KnowledgeRecord, action: str) -> str:
        if action == "setting":
            return "settings"
        return {
            MemoryKind.PREFERENCE: "preferences", MemoryKind.PROJECT: "projects",
            MemoryKind.TASK: "tasks", MemoryKind.CASE: "cases",
            MemoryKind.CONVERSATION: "chitchat", MemoryKind.FACT: "facts",
        }[record.kind]

    @staticmethod
    def _frontmatter(metadata: dict[str, Any]) -> str:
        lines = ["---"]
        for key, value in metadata.items():
            if isinstance(value, (list, dict)):
                rendered = json.dumps(value, ensure_ascii=False)
            elif value is None:
                rendered = "null"
            else:
                rendered = json.dumps(value, ensure_ascii=False)
            lines.append(f"{key}: {rendered}")
        return "\n".join([*lines, "---"])

    def upsert_record(self, record: KnowledgeRecord) -> Path:
        reasons = SensitiveMemoryPolicy.detect(record.content, record.metadata)
        if reasons:
            raise ObsidianVaultError("민감정보가 포함된 기억은 Vault에 저장하지 않습니다.")
        action, topics = self._action(record), self._topics(record)
        folder = self._folder(record, action)
        filename = f"{self._slug(record.subject)}-{record.record_id[:8]}.md"
        path = self.root / "wiki" / folder / filename
        topic_links = [f"[[topics/{self._slug(topic)}|{topic}]]" for topic in topics]
        action_link = f"[[actions/{self._slug(action)}|{action}]]"
        metadata = {
            "id": record.record_id, "type": record.kind.value, "action": action,
            "importance": self._importance(record), "confidence": record.confidence,
            "epistemic_status": record.epistemic_status.value, "topics": topics,
            "source_uri": record.source_uri, "source_label": record.source_label,
            "workspace": record.workspace_namespace, "status": record.status,
            "created": datetime.fromtimestamp(record.recorded_at).isoformat(),
            "updated": datetime.now().isoformat(), "managed_by": "anis",
        }
        body = (
            f"{self._frontmatter(metadata)}\n# {record.subject}\n\n"
            f"## 기억\n{record.content.strip()}\n\n"
            f"## 관계\n- 행동: {action_link}\n"
            + "".join(f"- 주제: {link}\n" for link in topic_links)
            + f"\n## 출처\n- {record.source_label or record.source_uri or '사용자 발화'}\n"
        )
        with self._lock:
            self._atomic_write(path, body)
            self._update_map("actions", action, path, record.subject)
            for topic in topics:
                self._update_map("topics", topic, path, record.subject)
        return path

    def _update_map(self, map_type: str, name: str, note_path: Path, title: str) -> None:
        map_path = self.root / "wiki" / map_type / f"{self._slug(name)}.md"
        target = note_path.relative_to(self.root / "wiki").with_suffix("").as_posix()
        link = f"- [[{target}|{title}]]"
        if map_path.exists():
            content = map_path.read_text(encoding="utf-8")
            if link not in content:
                self._atomic_write(map_path, content.rstrip() + "\n" + link)
        else:
            self._atomic_write(map_path, f"---\ntype: {map_type}_map\nmanaged_by: anis\n---\n# {name}\n\n{link}")

    def archive_exchange(self, session_id: str, user_text: str, assistant_text: str) -> Path:
        """Append audit history outside active RAG. Text storage is cheap; retrieval is isolated."""
        safe_user, _ = SafetyLayer.isolate_untrusted_content(user_text, "conversation")
        safe_assistant = SafetyLayer.sanitize_input(assistant_text)
        month = datetime.now().strftime("%Y-%m")
        path = self.root / "raw" / "conversations" / month / f"{self._slug(session_id, 'default')}.md"
        timestamp = datetime.now().isoformat(timespec="seconds")
        entry = f"\n## {timestamp}\n\n**사용자**\n{safe_user}\n\n**아니스**\n{safe_assistant}\n"
        with self._lock:
            existing = path.read_text(encoding="utf-8") if path.exists() else (
                "---\ntype: raw_conversation\nrag_index: false\nmanaged_by: anis\n---\n# Conversation Archive\n"
            )
            self._atomic_write(path, existing.rstrip() + "\n" + entry)
            self._track_repeated_pattern(session_id, safe_user)
        return path

    @staticmethod
    def _pattern_signature(text: str) -> tuple[str, str]:
        normalized = text.casefold()
        normalized = re.sub(r"[\"'].*?[\"']", " <text> ", normalized)
        normalized = re.sub(r"\b\d+(?:\.\d+)?\b", " <number> ", normalized)
        normalized = re.sub(r"[^0-9a-z가-힣<>]+", " ", normalized).strip()
        kind = "action" if re.search(
            r"(?:해줘|해주세요|만들|수정|변경|실행|열어|닫아|켜|꺼|저장|검색|찾아)", text, re.I
        ) else "question" if "?" in text or re.search(r"(?:뭐|왜|어떻게|언제|어디|누구|알려)", text) else "chitchat"
        return normalized[:240], kind

    def _track_repeated_pattern(self, session_id: str, text: str) -> None:
        signature, kind = self._pattern_signature(text)
        if len(signature) < 4:
            return
        db_path = self.root / ".anis" / "vault_index.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        with sqlite3.connect(db_path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_patterns (
                signature TEXT PRIMARY KEY, kind TEXT NOT NULL, example TEXT NOT NULL,
                count INTEGER NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                sessions TEXT NOT NULL)""")
            row = db.execute("SELECT count,sessions,first_seen FROM conversation_patterns WHERE signature=?", (signature,)).fetchone()
            sessions = set(json.loads(row[1])) if row else set()
            sessions.add(session_id)
            count, first_seen = (int(row[0]) + 1, float(row[2])) if row else (1, now)
            db.execute("""INSERT INTO conversation_patterns VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(signature) DO UPDATE SET count=excluded.count,last_seen=excluded.last_seen,
                sessions=excluded.sessions,example=excluded.example""",
                (signature, kind, text[:500], count, first_seen, now,
                 json.dumps(sorted(sessions), ensure_ascii=False)))
        # A repeated pattern becomes active knowledge only after independent occurrences.
        if count < 3:
            return
        folder = "chitchat" if kind == "chitchat" else "patterns"
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10]
        note = self.root / "wiki" / folder / f"{kind}-{digest}.md"
        importance = min(0.82, 0.42 + count * 0.06 + min(len(sessions), 3) * 0.04)
        metadata = {
            "id": f"pattern-{digest}", "type": "conversation_pattern", "action": kind,
            "importance": round(importance, 3), "count": count,
            "session_count": len(sessions), "rag_index": True, "managed_by": "anis",
            "updated": datetime.now().isoformat(),
        }
        body = (
            f"{self._frontmatter(metadata)}\n# 반복 {kind} 패턴\n\n"
            f"## 대표 표현\n{text[:500]}\n\n## 정규화 패턴\n`{signature}`\n\n"
            f"## 관계\n- 행동: [[actions/{kind}|{kind}]]\n"
        )
        self._atomic_write(note, body)
        self._update_map("actions", kind, note, f"반복 {kind} 패턴")

    def _notes(self, include_raw: bool = False) -> list[Path]:
        roots = [self.root / "wiki"] + ([self.root / "raw"] if include_raw else [])
        return sorted(path for root in roots for path in root.rglob("*.md") if path.is_file())

    @staticmethod
    def _parse_note(content: str) -> tuple[dict[str, Any], str, str]:
        """Return JSON-compatible frontmatter, title and body without requiring YAML."""
        metadata: dict[str, Any] = {}
        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) == 3:
                body = parts[2].lstrip()
                for line in parts[1].splitlines():
                    if ":" not in line:
                        continue
                    key, raw = line.split(":", 1)
                    try:
                        metadata[key.strip()] = json.loads(raw.strip())
                    except (json.JSONDecodeError, TypeError):
                        metadata[key.strip()] = raw.strip()
        heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        return metadata, heading.group(1).strip() if heading else "", body

    def read_note(self, relative_path: str | Path) -> dict[str, Any]:
        relative = Path(str(relative_path).replace("\\", "/"))
        path = self._inside(self.root / relative)
        if path.suffix.casefold() != ".md" or not path.is_file():
            raise ObsidianVaultError("존재하는 Markdown 문서만 열 수 있습니다.")
        content = path.read_text(encoding="utf-8", errors="replace")
        metadata, title, body = self._parse_note(content)
        return {
            "path": str(path), "relative_path": path.relative_to(self.root).as_posix(),
            "title": title or path.stem, "metadata": metadata, "body": body,
            "content": content,
        }

    def open_note(self, relative_path: str | Path | None = None) -> str:
        target = self.root if not relative_path else self._inside(self.root / Path(relative_path))
        if target != self.root and not target.is_file():
            raise ObsidianVaultError("열려는 Vault 문서를 찾을 수 없습니다.")
        uri = "obsidian://open?path=" + quote(str(target))
        os.startfile(uri)
        return uri

    def build_graph(self, *, query: str = "", types: Iterable[str] = (),
                    actions: Iterable[str] = (), topics: Iterable[str] = (),
                    min_importance: float = 0.0, center: str | None = None,
                    depth: int = 2, max_nodes: int = 300,
                    include_raw: bool = False) -> dict[str, Any]:
        """Build a bounded Cytoscape-compatible graph from Vault notes and wikilinks."""
        max_nodes = max(1, min(int(max_nodes), 1000))
        depth = max(0, min(int(depth), 5))
        wanted_types = {str(item).casefold() for item in types if str(item).strip()}
        wanted_actions = {str(item).casefold() for item in actions if str(item).strip()}
        wanted_topics = {str(item).casefold() for item in topics if str(item).strip()}
        query_tokens = set(re.findall(r"[0-9A-Za-z가-힣]+", str(query).casefold()))
        note_data: dict[Path, dict[str, Any]] = {}
        aliases: dict[str, Path] = {}
        for path in self._notes(include_raw=include_raw):
            content = path.read_text(encoding="utf-8", errors="replace")
            metadata, title, body = self._parse_note(content)
            relative = path.relative_to(self.root).as_posix()
            topic_values = metadata.get("topics", [])
            if isinstance(topic_values, str):
                topic_values = [topic_values]
            item = {
                "path": path.resolve(), "relative_path": relative,
                "id": relative.removesuffix(".md"), "label": title or path.stem,
                "type": str(metadata.get("type", "note")),
                "action": str(metadata.get("action", "reference")),
                "importance": float(metadata.get("importance", 0.5) or 0.5),
                "confidence": float(metadata.get("confidence", 0.0) or 0.0),
                "topics": [str(value) for value in topic_values],
                "updated": str(metadata.get("updated", "")),
                "rag_index": metadata.get("rag_index", not relative.startswith("raw/")) is not False,
                "body": body, "links": list(dict.fromkeys(self.WIKILINK.findall(content))),
            }
            note_data[path.resolve()] = item
            aliases[item["id"].casefold()] = path.resolve()
            aliases[path.stem.casefold()] = path.resolve()

        def resolve(link: str) -> Path | None:
            normalized = link.strip().replace("\\", "/").removesuffix(".md").casefold()
            return aliases.get(normalized) or aliases.get(Path(normalized).name)

        adjacency: dict[Path, set[Path]] = {path: set() for path in note_data}
        all_edges: set[tuple[Path, Path]] = set()
        for source, item in note_data.items():
            for link in item["links"]:
                target = resolve(link)
                if target and target != source:
                    adjacency[source].add(target); adjacency[target].add(source)
                    all_edges.add((source, target))

        allowed = set(note_data)
        if center:
            center_key = str(center).replace("\\", "/").removesuffix(".md").casefold()
            seed = aliases.get(center_key) or aliases.get(Path(center_key).name)
            if seed:
                allowed, frontier = {seed}, [(seed, 0)]
                while frontier:
                    current, current_depth = frontier.pop(0)
                    if current_depth >= depth:
                        continue
                    for neighbor in adjacency[current]:
                        if neighbor not in allowed:
                            allowed.add(neighbor); frontier.append((neighbor, current_depth + 1))

        def visible(item: dict[str, Any]) -> bool:
            haystack = f"{item['label']} {item['body']} {' '.join(item['topics'])}".casefold()
            return (
                (not query_tokens or query_tokens <= set(re.findall(r"[0-9A-Za-z가-힣]+", haystack)))
                and (not wanted_types or item["type"].casefold() in wanted_types)
                and (not wanted_actions or item["action"].casefold() in wanted_actions)
                and (not wanted_topics or bool(wanted_topics & {x.casefold() for x in item["topics"]}))
                and item["importance"] >= float(min_importance)
            )

        selected = [path for path in allowed if visible(note_data[path])]
        selected.sort(key=lambda path: (-note_data[path]["importance"], -len(adjacency[path]), note_data[path]["label"]))
        selected = selected[:max_nodes]
        selected_set = set(selected)
        nodes = []
        for path in selected:
            item = note_data[path]
            nodes.append({key: item[key] for key in (
                "id", "label", "relative_path", "type", "action", "importance",
                "confidence", "topics", "updated", "rag_index"
            )} | {"degree": len(adjacency[path])})
        edges = [{"id": f"e{index}", "source": note_data[source]["id"],
                  "target": note_data[target]["id"], "type": "wikilink"}
                 for index, (source, target) in enumerate(sorted(all_edges, key=lambda pair: (str(pair[0]), str(pair[1]))))
                 if source in selected_set and target in selected_set]
        facets = {
            "types": sorted({item["type"] for item in note_data.values()}),
            "actions": sorted({item["action"] for item in note_data.values()}),
            "topics": sorted({topic for item in note_data.values() for topic in item["topics"]}),
        }
        return {"nodes": nodes, "edges": edges, "facets": facets,
                "stats": {"visible_nodes": len(nodes), "visible_edges": len(edges),
                          "total_notes": len(note_data)}, "center": center, "depth": depth}

    def _resolve_link(self, link: str) -> Optional[Path]:
        normalized = link.strip().replace("\\", "/").removesuffix(".md")
        direct = self.root / "wiki" / f"{normalized}.md"
        if direct.exists():
            return direct.resolve()
        candidates = [path for path in self._notes() if path.stem.casefold() == Path(normalized).name.casefold()]
        return candidates[0].resolve() if len(candidates) == 1 else None

    def explore(self, query: str, *, depth: int = 2, max_notes: int = 24) -> list[dict[str, Any]]:
        """Search seed notes and explicitly traverse wikilinks with a bounded BFS."""
        depth = max(0, min(int(depth), 3)); max_notes = max(1, min(int(max_notes), 50))
        tokens = set(re.findall(r"[0-9A-Za-z가-힣]+", query.casefold()))
        scored = []
        for path in self._notes():
            content = path.read_text(encoding="utf-8", errors="replace")
            words = set(re.findall(r"[0-9A-Za-z가-힣]+", f"{path.stem} {content}".casefold()))
            score = len(tokens & words)
            if score:
                scored.append((score, path.resolve()))
        queue = [(path, 0) for _, path in sorted(scored, key=lambda item: -item[0])[:8]]
        visited: set[Path] = set(); results = []
        while queue and len(results) < max_notes:
            path, current_depth = queue.pop(0)
            if path in visited or current_depth > depth:
                continue
            visited.add(path)
            content = path.read_text(encoding="utf-8", errors="replace")
            links = list(dict.fromkeys(self.WIKILINK.findall(content)))
            results.append({"path": str(path), "relative_path": str(path.relative_to(self.root)),
                            "depth": current_depth, "content": content, "links": links})
            if current_depth < depth:
                for link in links:
                    target = self._resolve_link(link)
                    if target and target not in visited:
                        queue.append((target, current_depth + 1))
        return results

    def sync_to_rag(self, rag=None) -> dict[str, Any]:
        target = rag or self.rag
        if target is None:
            raise ObsidianVaultError("RAG Manager가 연결되지 않았습니다.")
        indexed, errors, active_ids = [], [], set()
        for path in self._notes(include_raw=False):
            try:
                relative = path.relative_to(self.root).as_posix()
                doc_id = "obsidian-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]
                active_ids.add(doc_id)
                target.add_text_document(
                    path.read_text(encoding="utf-8", errors="replace"), doc_id=doc_id,
                    namespace=getattr(target, "namespace", "global"), source_uri=str(path),
                    metadata={"source_type": "obsidian", "vault": str(self.root), "relative_path": relative},
                )
                indexed.append(str(path))
            except Exception as exc:
                errors.append({"path": str(path), "error": str(exc)})
        removed = []
        for document in list(getattr(target, "documents", {}).values()):
            metadata = document.get("metadata", {})
            doc_id = str(document.get("doc_id", ""))
            if (metadata.get("source_type") == "obsidian"
                    and metadata.get("vault") == str(self.root)
                    and doc_id not in active_ids):
                namespace = str(document.get("namespace", getattr(target, "namespace", "global")))
                if target.remove_text_document(doc_id, namespace=namespace):
                    removed.append(doc_id)
        return {"indexed": len(indexed), "removed": len(removed), "errors": errors}

    def lint(self) -> dict[str, Any]:
        notes = self._notes(); stems = {path.stem.casefold() for path in notes}
        broken, thin, duplicates = [], [], {}
        hashes: dict[str, list[str]] = {}
        for path in notes:
            content = path.read_text(encoding="utf-8", errors="replace")
            if len(re.sub(r"---[\s\S]*?---", "", content, count=1).strip()) < 120:
                thin.append(str(path.relative_to(self.root)))
            for link in self.WIKILINK.findall(content):
                if not self._resolve_link(link):
                    broken.append({"source": str(path.relative_to(self.root)), "link": link})
            digest = hashlib.sha256(re.sub(r"\s+", " ", content).encode("utf-8")).hexdigest()
            hashes.setdefault(digest, []).append(str(path.relative_to(self.root)))
        duplicates = [items for items in hashes.values() if len(items) > 1]
        linked_targets = {Path(link).name.casefold() for path in notes
                          for link in self.WIKILINK.findall(path.read_text(encoding="utf-8", errors="replace"))}
        orphans = [str(path.relative_to(self.root)) for path in notes
                   if path.stem.casefold() not in linked_targets and path.name != "index.md"]
        return {"notes": len(notes), "broken_links": broken, "thin_pages": thin,
                "duplicate_groups": duplicates, "orphans": orphans}


_vault: ObsidianVault | None = None


def get_obsidian_vault(*, rag=None) -> ObsidianVault:
    global _vault
    if _vault is None:
        _vault = ObsidianVault(rag=rag)
    elif rag is not None:
        _vault.rag = rag
    return _vault
