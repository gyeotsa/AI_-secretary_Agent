import os
import re
import json
import csv
import hashlib
import inspect
import time
import threading
from io import StringIO
from config import Config
from core.knowledge_memory import FreshnessPolicy


class VectorRAGManager:
    """Vector DB + Embedding + Reranker 기반 RAG (없으면 Simple RAG로 fallback)"""

    _store_lock_initialization_lock = threading.Lock()

    def __init__(self):
        self.data_dir = os.path.dirname(Config.DB_PATH)
        self.rag_file = os.path.join(self.data_dir, "simple_rag.json")
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 문서 저장소: {doc_id: {chunks: [text, ...], source: file_path}}
        self.documents = {}
        self.namespace = "global"
        self._store_lock = threading.RLock()
        self.usage_tracker = None
        self.last_retrieval_trace = ""
        self._retrieval_local = threading.local()
        self._load()
        
        # Vector DB, Embedding, Reranker 초기화 (try-except로 fallback)
        self.use_vector_rag = False
        self.vector_db = None
        self.embedding_model = None
        self.reranker = None
        self._init_vector_rag()

    def _get_store_lock(self) -> threading.RLock:
        """Return the instance lock, creating it for restored legacy objects.

        A few compatibility paths reconstruct ``VectorRAGManager`` instances
        without calling ``__init__`` (for example, old pickles and focused
        test doubles).  Persisting memory must remain safe in those paths too,
        so storage operations resolve the lock lazily instead of assuming the
        newest constructor has run.
        """
        lock = getattr(self, "_store_lock", None)
        if lock is not None:
            return lock
        with self._store_lock_initialization_lock:
            lock = getattr(self, "_store_lock", None)
            if lock is None:
                lock = threading.RLock()
                self._store_lock = lock
        return lock

    def set_namespace(self, namespace: str) -> None:
        """Set the legacy default namespace.

        New request paths should pass ``namespace=`` to each operation instead.
        This mutable default remains only for older integrations that have not
        yet been migrated.
        """
        self.namespace = str(namespace or "global")

    def _resolve_namespace(self, namespace: str | None = None) -> str:
        """Snapshot one operation's namespace without mutating shared state."""
        return str(namespace or self.namespace or "global")

    @staticmethod
    def _search_namespaces(namespace: str) -> set[str]:
        """Workspace retrieval may also use explicitly global knowledge."""
        return {"global", str(namespace or "global")}

    def set_usage_tracker(self, tracker) -> None:
        self.usage_tracker = tracker

    def get_last_retrieval_trace(self) -> str:
        """Return the trace for this execution thread, not another GUI worker."""
        return str(getattr(self._retrieval_local, "trace_id", "") or "")

    def _document_key(self, doc_id: str, namespace: str | None = None) -> str:
        return f"{self._resolve_namespace(namespace)}::{doc_id}"
    
    def _init_vector_rag(self):
        try:
            # 1. Chroma DB 초기화
            import chromadb
            from chromadb.utils import embedding_functions
            import torch
            
            self.vector_db_dir = os.path.join(self.data_dir, "chroma_db")
            self.chroma_client = chromadb.PersistentClient(path=self.vector_db_dir)
            
            # 2. Embedding 모델 초기화 (로컬 고정, 암묵적 다운로드 금지)
            embedding_model_path = os.path.abspath(Config.RAG_EMBEDDING_MODEL_PATH)
            if not os.path.isdir(embedding_model_path):
                raise FileNotFoundError(
                    f"로컬 임베딩 모델이 없습니다: {embedding_model_path}"
                )
            requested_device = Config.RAG_DEVICE
            if requested_device == "auto":
                requested_device = "cuda" if torch.cuda.is_available() else "cpu"
            if requested_device == "cuda" and not torch.cuda.is_available():
                print("[RAG] CUDA를 사용할 수 없어 CPU로 fallback합니다.")
                requested_device = "cpu"
            self.embedding_model = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=embedding_model_path,
                device=requested_device,
            )
            print(f"[RAG] 임베딩 장치: {requested_device}")
            self.collection = self.chroma_client.get_or_create_collection(
                name="jarvis_rag_bge_m3",
                embedding_function=self.embedding_model,
            )
            
            # 3. Reranker 초기화 (BAAI/bge-reranker-v2-m3)
            try:
                from sentence_transformers import CrossEncoder
                reranker_path = Config.RAG_RERANKER_MODEL_PATH
                self.reranker = CrossEncoder(os.path.abspath(reranker_path)) if reranker_path else None
            except ImportError:
                print("[RAG] Reranker 모듈이 없어서 키워드 기반 리랭킹을 사용합니다.")
                self.reranker = None
            
            self.use_vector_rag = True
            print("[RAG] Vector RAG 초기화 완료")
            
        except ImportError as e:
            print(f"[RAG] Vector RAG 라이브러리가 없어 Simple RAG로 fallback합니다: {e}")
            self.use_vector_rag = False
        except Exception as e:
            print(f"[RAG] Vector RAG 초기화 오류: {e}, Simple RAG로 fallback합니다.")
            self.use_vector_rag = False
    
    def _load(self):
        if os.path.exists(self.rag_file):
            try:
                with open(self.rag_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                with self._get_store_lock():
                    self.documents = loaded
            except Exception as e:
                print(f"[RAG] 파일 로드 오류: {e}")
                with self._get_store_lock():
                    self.documents = {}
    
    def _save(self):
        try:
            # A GUI turn and a specialist workspace can persist memories at the
            # same time.  Serialize one immutable snapshot and atomically swap
            # it into place so neither thread can truncate the other's JSON.
            with self._get_store_lock():
                snapshot = json.loads(json.dumps(self.documents, ensure_ascii=False))
                temp_path = (
                    f"{self.rag_file}.tmp.{os.getpid()}.{threading.get_ident()}"
                )
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f, ensure_ascii=False, indent=2)
                os.replace(temp_path, self.rag_file)
        except Exception as e:
            print(f"[RAG] 파일 저장 오류: {e}")

    def _documents_snapshot(self) -> tuple[dict, ...]:
        """Return a stable view for retrieval while another thread writes."""
        with self._get_store_lock():
            return tuple(self.documents.values())

    @staticmethod
    def _chunk_id(namespace: str, doc_id: str, index: int, content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        return f"{namespace}:{doc_id}:{index}:{digest}"
    
    def chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> list:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        chunks = []
        current_chunk = ""
        
        for sentence in sentences:
            if len(current_chunk) + len(sentence) > chunk_size:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = current_chunk[-overlap:] + sentence
            else:
                current_chunk += " " + sentence
        
        if current_chunk:
            chunks.append(current_chunk.strip())
        
        return chunks

    def chunk_document(self, text: str, file_path: str = "", chunk_size: int = 700) -> list[dict]:
        """Preserve useful Markdown, code, JSON and CSV boundaries in chunk metadata."""
        suffix = os.path.splitext(file_path)[1].casefold()
        blocks = []
        if suffix in {".md", ".markdown"}:
            section, current, start = "문서", [], 1
            for line_no, line in enumerate(text.splitlines(), 1):
                if re.match(r"^#{1,6}\s+", line) and current:
                    blocks.append((section, "\n".join(current), start, line_no - 1))
                    current, start = [], line_no
                if re.match(r"^#{1,6}\s+", line): section = line.lstrip("#").strip()
                current.append(line)
            if current: blocks.append((section, "\n".join(current), start, len(text.splitlines())))
        elif suffix == ".json":
            try:
                value = json.loads(text)
                items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else [("value", value)]
                blocks = [(str(key), json.dumps(item, ensure_ascii=False, indent=2), 0, 0) for key, item in items]
            except json.JSONDecodeError:
                blocks = [("문서", text, 1, len(text.splitlines()))]
        elif suffix == ".csv":
            rows = list(csv.reader(StringIO(text)))
            header = rows[0] if rows else []
            blocks = [("CSV", ",".join(header) + "\n" + ",".join(row), index + 2, index + 2)
                      for index, row in enumerate(rows[1:])]
        elif suffix in {".py", ".js", ".ts", ".java", ".go", ".rs"}:
            lines = text.splitlines()
            starts = [i for i, line in enumerate(lines) if re.match(r"^\s*(?:async\s+def|def|class|function|export\s+(?:async\s+)?function)\s+", line)]
            if not starts: starts = [0]
            for index, start_index in enumerate(starts):
                end_index = starts[index + 1] if index + 1 < len(starts) else len(lines)
                title = lines[start_index].strip()[:120] if lines else "code"
                blocks.append((title, "\n".join(lines[start_index:end_index]), start_index + 1, end_index))
        else:
            blocks = [("문서", chunk, 0, 0) for chunk in self.chunk_text(text, chunk_size, min(80, chunk_size // 5))]

        chunks = []
        for section, block, start_line, end_line in blocks:
            for part in self.chunk_text(block, chunk_size, min(80, chunk_size // 5)) or [block.strip()]:
                if part.strip():
                    chunks.append({"content": part.strip(), "section": section,
                                   "start_line": start_line, "end_line": end_line})
        return chunks
    
    def add_document(self, file_path: str, metadata: dict | None = None, *,
                     namespace: str | None = None) -> str:
        if not os.path.exists(file_path):
            return f"오류: 파일 '{file_path}'가 존재하지 않습니다."
        target_namespace = self._resolve_namespace(namespace)
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
            
            chunk_records = self.chunk_document(text, file_path)
            if not chunk_records:
                return "오류: 파일 내용이 비어있습니다."
            chunks = [item["content"] for item in chunk_records]
            doc_id = os.path.basename(file_path)
            now = time.time()
            base_metadata = dict(metadata or {})
            base_metadata.setdefault("source_type", "file")
            base_metadata.setdefault("recorded_at", now)
            if base_metadata.get("source_type") == "web" and not base_metadata.get("expires_at"):
                base_metadata["expires_at"] = FreshnessPolicy.expires_at(
                    "web", str(base_metadata.get("topic", "general")), now
                )
            for index, item in enumerate(chunk_records):
                item.update(base_metadata)
                item["chunk_id"] = self._chunk_id(target_namespace, doc_id, index, item["content"])
            with self._get_store_lock():
                self.documents[self._document_key(doc_id, target_namespace)] = {
                    "chunks": chunks,
                    "chunk_metadata": chunk_records,
                    "source": file_path,
                    "namespace": target_namespace,
                    "doc_id": doc_id,
                    "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "recorded_at": now,
                    "metadata": base_metadata,
                }
                self._save()
            
            # Vector DB에 추가 (사용 가능하면)
            if self.use_vector_rag:
                try:
                    # 기존 문서 삭제
                    existing_ids = self.collection.get(where={
                        "$and": [{"doc_id": doc_id}, {"namespace": target_namespace}]
                    })["ids"]
                    if existing_ids:
                        self.collection.delete(ids=existing_ids)
                    
                    # 새로운 문서 추가
                    ids = [item["chunk_id"] for item in chunk_records]
                    metadatas = [{"doc_id": doc_id, "source": file_path, "namespace": target_namespace,
                                  "chunk_id": item["chunk_id"], "section": str(item.get("section", "")),
                                  "recorded_at": float(item.get("recorded_at", now)),
                                  "source_type": str(item.get("source_type", "file"))}
                                 for item in chunk_records]
                    self.collection.add(
                        ids=ids,
                        documents=chunks,
                        metadatas=metadatas
                    )
                    print(f"[RAG] Vector DB에 문서 '{doc_id}' 추가 완료")
                except Exception as e:
                    print(f"[RAG] Vector DB 추가 오류: {e}")
            
            return f"문서 '{doc_id}'가 성공적으로 추가되었습니다! ({len(chunks)}개 청크)"
        
        except Exception as e:
            return f"문서 추가 오류: {str(e)}"

    def add_text_document(self, text: str, *, doc_id: str, namespace: str | None = None,
                          metadata: dict | None = None, source_uri: str | None = None) -> str:
        """Index trusted in-memory text such as a consolidated Memory record."""
        content = str(text or "").strip()
        if not content:
            raise ValueError("RAG에 추가할 텍스트가 비어 있습니다.")
        target_namespace = self._resolve_namespace(namespace)
        source = str(source_uri or f"memory://{doc_id}")
        chunks = self.chunk_text(content)
        now = time.time()
        base_metadata = {
            "source_type": "memory", "recorded_at": now,
            **dict(metadata or {}),
        }
        records = []
        for index, chunk in enumerate(chunks):
            records.append({
                "content": chunk, "section": "memory", "start_line": 0, "end_line": 0,
                "chunk_id": self._chunk_id(target_namespace, doc_id, index, chunk),
                **base_metadata,
            })
        key = f"{target_namespace}::{doc_id}"
        with self._get_store_lock():
            self.documents[key] = {
                "chunks": chunks, "chunk_metadata": records, "source": source,
                "namespace": target_namespace, "doc_id": doc_id,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "recorded_at": now, "metadata": base_metadata,
            }
            self._save()
        if self.use_vector_rag:
            existing_ids = self.collection.get(where={"$and": [
                {"doc_id": doc_id}, {"namespace": target_namespace},
            ]})["ids"]
            if existing_ids:
                self.collection.delete(ids=existing_ids)
            ids = [item["chunk_id"] for item in records]
            self.collection.add(
                ids=ids, documents=chunks,
                metadatas=[{
                    "doc_id": doc_id, "source": source,
                    "namespace": target_namespace, "chunk_id": item["chunk_id"],
                    "section": "memory", "recorded_at": now,
                    "source_type": str(item.get("source_type", "memory")),
                } for item in records],
            )
        return doc_id

    def remove_text_document(self, doc_id: str, *, namespace: str = "global") -> bool:
        key = f"{namespace}::{doc_id}"
        with self._get_store_lock():
            removed = self.documents.pop(key, None)
            if removed is None:
                return False
            self._save()
        if self.use_vector_rag:
            ids = self.collection.get(where={"$and": [
                {"doc_id": doc_id}, {"namespace": namespace},
            ]})["ids"]
            if ids:
                self.collection.delete(ids=ids)
        return True

    def remove_document(self, doc_id: str, *, namespace: str | None = None) -> bool:
        target_namespace = self._resolve_namespace(namespace)
        key = self._document_key(os.path.basename(doc_id), target_namespace)
        with self._get_store_lock():
            document = self.documents.pop(key, None)
            if document is None: return False
            self._save()
        if self.use_vector_rag:
            try:
                ids = self.collection.get(where={"$and": [
                    {"doc_id": document["doc_id"]}, {"namespace": target_namespace}
                ]})["ids"]
                if ids: self.collection.delete(ids=ids)
            except Exception as exc:
                print(f"[RAG] Vector DB 삭제 오류: {exc}")
        return True

    def sync_document(self, file_path: str, metadata: dict | None = None, *,
                      namespace: str | None = None) -> str:
        target_namespace = self._resolve_namespace(namespace)
        doc_id = os.path.basename(file_path)
        key = self._document_key(doc_id, target_namespace)
        if not os.path.exists(file_path):
            return (
                "삭제 동기화 완료"
                if self.remove_document(doc_id, namespace=target_namespace)
                else "문서가 이미 없습니다."
            )
        content = open(file_path, "r", encoding="utf-8").read()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._get_store_lock():
            current_digest = self.documents.get(key, {}).get("content_sha256")
        if current_digest == digest:
            return "문서 변경 없음"
        return self.add_document(file_path, metadata, namespace=target_namespace)

    def sync_index(self, *, namespace: str | None = None) -> dict:
        """Synchronize changed/deleted local sources without touching web freshness state."""
        target_namespace = self._resolve_namespace(namespace)
        changed, deleted = [], []
        with self._get_store_lock():
            scoped = list(self.documents.items())
        for key, document in scoped:
            if document.get("namespace", "global") != target_namespace: continue
            if document.get("metadata", {}).get("source_type", "file") != "file": continue
            source = document.get("source", "")
            if not os.path.exists(source):
                if self.remove_document(
                    document.get("doc_id", os.path.basename(source)),
                    namespace=target_namespace,
                ):
                    deleted.append(source)
                continue
            try:
                digest = hashlib.sha256(open(source, "rb").read()).hexdigest()
            except OSError:
                continue
            if digest != document.get("content_sha256"):
                self.add_document(
                    source, document.get("metadata"), namespace=target_namespace,
                )
                changed.append(source)
        return {"changed": changed, "deleted": deleted}

    def revalidate_document(self, doc_id: str, *, verified_at: float | None = None,
                            ttl_seconds: float | None = None,
                            namespace: str | None = None) -> bool:
        target_namespace = self._resolve_namespace(namespace)
        with self._get_store_lock():
            document = self.documents.get(
                self._document_key(os.path.basename(doc_id), target_namespace)
            )
            if not document or document.get("metadata", {}).get("source_type") != "web": return False
            verified_at = float(verified_at or time.time())
            metadata = document["metadata"]
            metadata["recorded_at"] = verified_at
            metadata["expires_at"] = verified_at + float(ttl_seconds or FreshnessPolicy.WEB_TTL_SECONDS.get(
                str(metadata.get("topic", "general")), FreshnessPolicy.WEB_TTL_SECONDS["general"]
            ))
            document["recorded_at"] = verified_at
            for chunk in document.get("chunk_metadata", []):
                chunk["recorded_at"] = metadata["recorded_at"]
                chunk["expires_at"] = metadata["expires_at"]
            self._save()
        return True
    
    def search_docs(self, query: str, top_k: int = 3, metadata_filter: dict | None = None,
                    include_stale: bool = False,
                    namespace: str | None = None) -> list:
        """Hybrid retrieval: vector + lexical RRF, optional cross-encoder reranking."""
        target_namespace = self._resolve_namespace(namespace)
        self.sync_index(namespace=target_namespace)
        if not self.documents:
            return []
        
        if self.use_vector_rag:
            results = self._hybrid_search(
                query, top_k, metadata_filter, include_stale,
                namespace=target_namespace,
            )
        else:
            results = self._simple_search(
                query, top_k, metadata_filter, include_stale,
                namespace=target_namespace,
            )
            results = self._attach_confidence(results, query)
        tracker = getattr(self, "usage_tracker", None)
        if tracker is not None:
            try:
                trace_id = tracker.record_retrieval(query, results)
                self.last_retrieval_trace = trace_id
                retrieval_local = getattr(self, "_retrieval_local", None)
                if retrieval_local is not None:
                    retrieval_local.trace_id = trace_id
            except Exception:
                self.last_retrieval_trace = ""
                retrieval_local = getattr(self, "_retrieval_local", None)
                if retrieval_local is not None:
                    retrieval_local.trace_id = ""
        return results

    def search_with_confidence(self, query: str, top_k: int = 3,
                               metadata_filter: dict | None = None,
                               include_stale: bool = False,
                               threshold: float = 0.42,
                               namespace: str | None = None) -> dict:
        results = self.search_docs(
            query, top_k, metadata_filter, include_stale,
            namespace=namespace,
        )
        confidence = float(results[0].get("retrieval_confidence", 0.0)) if results else 0.0
        tracker = getattr(self, "usage_tracker", None)
        if results and confidence >= threshold and tracker is not None:
            try:
                chunk_ids = [str(item.get("chunk_id") or item.get("doc_id") or "")
                             for item in results[:top_k]]
                retrieval_local = getattr(self, "_retrieval_local", None)
                trace_id = getattr(retrieval_local, "trace_id", getattr(self, "last_retrieval_trace", ""))
                tracker.mark_included(trace_id,
                                                 [item for item in chunk_ids if item])
            except Exception:
                pass
        answerable = bool(results and confidence >= threshold)
        return {
            "results": results,
            "confidence": confidence,
            "answerable": answerable,
            "reason": "근거가 충분합니다." if results and confidence >= threshold
                      else "검색 근거가 부족하여 추측하지 않습니다.",
        }

    def _all_lexical_candidates(self, query: str, *,
                                namespace: str | None = None) -> list[dict]:
        target_namespace = self._resolve_namespace(namespace)
        tokens = set(re.findall(r"[0-9a-zA-Z가-힣]+", query.casefold()))
        candidates = []
        for document in self._documents_snapshot():
            if document.get("namespace", "global") not in self._search_namespaces(target_namespace):
                continue
            for record in document.get("chunk_metadata", []):
                words = set(re.findall(r"[0-9a-zA-Z가-힣]+", str(record.get("content", "")).casefold()))
                exact = len(tokens & words)
                partial = sum(1 for token in tokens if len(token) >= 2 and any(token in word for word in words))
                if exact or partial:
                    candidates.append({**record, "source": document.get("source", ""),
                                       "lexical_score": exact * 2 + partial})
        return sorted(candidates, key=lambda item: item["lexical_score"], reverse=True)

    @staticmethod
    def _accepts_keyword(callback, keyword: str) -> bool:
        """Keep old test doubles/adapters compatible as contracts gain keywords."""
        try:
            parameters = inspect.signature(callback).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            or parameter.name == keyword
            for parameter in parameters
        )

    def _hybrid_search(self, query: str, top_k: int, metadata_filter=None,
                       include_stale=False, *, namespace: str | None = None) -> list:
        """Merge independent vector and lexical rankings with reciprocal-rank fusion."""
        target_namespace = self._resolve_namespace(namespace)
        fetch_k = max(top_k * 5, 20)
        namespaces = self._search_namespaces(target_namespace)
        namespace_filter = ({"namespace": target_namespace} if len(namespaces) == 1 else {
            "$or": [{"namespace": value} for value in sorted(namespaces)]
        })
        try:
            raw = self.collection.query(query_texts=[query], n_results=fetch_k, where=namespace_filter)
            vector = [
                {"content": content, "source": meta.get("source", ""), **meta,
                 "vector_distance": float(distance)}
                for content, meta, distance in zip(
                    raw.get("documents", [[]])[0], raw.get("metadatas", [[]])[0],
                    raw.get("distances", [[1.0] * fetch_k])[0],
                )
            ]
        except Exception as exc:
            print(f"[RAG] Vector 후보 검색 오류: {exc}")
            vector = []
        lexical_search = self._all_lexical_candidates
        if self._accepts_keyword(lexical_search, "namespace"):
            lexical = lexical_search(query, namespace=target_namespace)[:fetch_k]
        else:
            lexical = lexical_search(query)[:fetch_k]
        fused: dict[str, dict] = {}
        for channel, ranking in (("vector", vector), ("lexical", lexical)):
            for rank, item in enumerate(ranking, 1):
                key = str(item.get("chunk_id") or hashlib.sha256(
                    str(item.get("content", "")).encode("utf-8")
                ).hexdigest())
                merged = fused.setdefault(key, dict(item, rrf_score=0.0, retrieval_channels=[]))
                merged.update({k: v for k, v in item.items() if k not in merged})
                merged["rrf_score"] += 1.0 / (60 + rank)
                merged["retrieval_channels"].append(channel)
        candidates = list(fused.values())
        if self.reranker and candidates:
            scores = self.reranker.predict([(query, item.get("content", "")) for item in candidates])
            for item, score in zip(candidates, scores):
                item["reranker_score"] = float(score)
            candidates.sort(key=lambda item: (item["reranker_score"], item["rrf_score"]), reverse=True)
        else:
            candidates.sort(key=lambda item: item["rrf_score"], reverse=True)
        for item in candidates:
            item["score"] = float(item.get("reranker_score", 0.0)) + float(item["rrf_score"] * 100)
        filter_and_rerank = self._filter_and_rerank
        filter_arguments = (
            query, candidates, max(top_k * 2, top_k), metadata_filter,
            include_stale,
        )
        if self._accepts_keyword(filter_and_rerank, "namespace"):
            filtered = filter_and_rerank(
                *filter_arguments, namespace=target_namespace,
            )
        else:
            filtered = filter_and_rerank(*filter_arguments)
        return self._attach_confidence(filtered[:top_k], query)

    @staticmethod
    def _attach_confidence(results: list[dict], query: str) -> list[dict]:
        if not results:
            return results
        query_tokens = set(re.findall(r"[0-9a-zA-Z가-힣]+", query.casefold()))
        for index, item in enumerate(results):
            content_tokens = set(re.findall(r"[0-9a-zA-Z가-힣]+", str(item.get("content", "")).casefold()))
            coverage = len(query_tokens & content_tokens) / max(1, len(query_tokens))
            channels = len(set(item.get("retrieval_channels", []))) / 2
            rank_prior = 1 / (index + 1)
            item["retrieval_confidence"] = round(min(1.0, coverage * 0.55 + channels * 0.3 + rank_prior * 0.15), 4)
        return results
    
    def _vector_search(self, query: str, top_k: int, metadata_filter=None,
                       include_stale=False, *, namespace: str | None = None) -> list:
        """Vector DB 기반 검색 + Reranker"""
        target_namespace = self._resolve_namespace(namespace)
        try:
            # 1. Vector DB로 초기 검색 (top_k * 2개)
            namespaces = self._search_namespaces(target_namespace)
            namespace_filter = ({"namespace": target_namespace} if len(namespaces) == 1 else {
                "$or": [{"namespace": value} for value in sorted(namespaces)]
            })
            initial_results = self.collection.query(
                query_texts=[query],
                n_results=top_k * 2,
                where=namespace_filter,
            )
            
            if not initial_results["documents"] or not initial_results["documents"][0]:
                return []
            
            # 2. Reranker로 재정렬 (있으면)
            chunks = initial_results["documents"][0]
            metadatas = initial_results["metadatas"][0]
            
            if self.reranker:
                # Reranker로 점수 계산
                pairs = [(query, chunk) for chunk in chunks]
                scores = self.reranker.predict(pairs)
                
                # 점수와 함께 정렬
                scored_results = list(zip(scores, chunks, metadatas))
                scored_results.sort(reverse=True, key=lambda x: x[0])
                
                # Top-k 반환
                top_results = scored_results[:top_k]
                candidates = [{"content": chunk, "source": meta["source"], **meta, "score": float(score)}
                              for score, chunk, meta in top_results]
            else:
                candidates = [{"content": chunk, "source": meta["source"], **meta}
                              for chunk, meta in zip(chunks, metadatas)]
            return self._filter_and_rerank(
                query, candidates, top_k, metadata_filter, include_stale,
                namespace=target_namespace,
            )
        
        except Exception as e:
            print(f"[RAG] Vector 검색 오류: {e}, Simple 검색으로 fallback합니다.")
            return self._simple_search(
                query, top_k, metadata_filter, include_stale,
                namespace=target_namespace,
            )
    
    def _simple_search(self, query: str, top_k: int, metadata_filter=None,
                       include_stale=False, *, namespace: str | None = None) -> list:
        """기존 Simple 키워드 기반 검색"""
        target_namespace = self._resolve_namespace(namespace)
        query_words = {word for word in re.split(r'\W+', query.lower()) if word}
        results = []
        
        allowed_namespaces = self._search_namespaces(target_namespace)
        with self._get_store_lock():
            document_items = tuple(self.documents.items())
        for doc_id, doc_data in document_items:
            if doc_data.get("namespace", "global") not in allowed_namespaces:
                continue
            records = doc_data.get("chunk_metadata") or [
                {"content": chunk, "chunk_id": self._chunk_id(
                    str(doc_data.get("namespace", target_namespace)),
                    doc_data.get("doc_id", doc_id), i, chunk,
                )}
                for i, chunk in enumerate(doc_data["chunks"])
            ]
            for record in records:
                chunk = record["content"]
                chunk_words = {word for word in re.split(r'\W+', chunk.lower()) if word}
                overlap = sum(
                    1 for query_word in query_words
                    if any(query_word == word or (len(query_word) >= 2 and query_word in word)
                           for word in chunk_words)
                )
                if overlap > 0:
                    results.append({"score": float(overlap), "content": chunk,
                                    "source": doc_data["source"], **record})
        
        if not results:
            return []
        
        # 점수 높은 순으로 정렬
        return self._filter_and_rerank(
            query, results, top_k, metadata_filter, include_stale,
            namespace=target_namespace,
        )

    def _filter_and_rerank(self, query, candidates, top_k, metadata_filter,
                           include_stale, *, namespace: str | None = None):
        target_namespace = self._resolve_namespace(namespace)
        now, tokens = time.time(), set(re.findall(r"\w+", query.casefold()))
        catalog = {}
        allowed_namespaces = self._search_namespaces(target_namespace)
        for document in self._documents_snapshot():
            if document.get("namespace", "global") not in allowed_namespaces: continue
            for chunk in document.get("chunk_metadata", []):
                catalog[chunk.get("chunk_id", "")] = {"source": document.get("source", ""), **chunk}
        filtered = []
        for item in candidates:
            item = {**catalog.get(item.get("chunk_id", ""), {}), **item}
            expires_at = item.get("expires_at")
            stale = bool(expires_at and float(expires_at) <= now)
            if stale and not include_stale: continue
            if metadata_filter and not all(item.get(key) == value for key, value in metadata_filter.items()): continue
            content_tokens = set(re.findall(r"\w+", str(item.get("content", "")).casefold()))
            lexical = sum(1 for token in tokens if any(
                token == word or (len(token) >= 2 and token in word) for word in content_tokens
            ))
            item = dict(item)
            item["score"] = float(item.get("score", 0.0)) + lexical
            item["stale"] = stale
            item["needs_revalidation"] = stale and item.get("source_type") == "web"
            item["citation"] = {
                "chunk_id": item.get("chunk_id", ""), "source": item.get("source", ""),
                "section": item.get("section", ""), "start_line": item.get("start_line", 0),
                "end_line": item.get("end_line", 0),
            }
            filtered.append(item)
        filtered.sort(key=lambda item: (item["score"], float(item.get("recorded_at", 0))), reverse=True)
        return filtered[:top_k]
    
    def list_documents(self, *, namespace: str | None = None) -> str:
        target_namespace = self._resolve_namespace(namespace)
        scoped = [doc for doc in self._documents_snapshot()
                  if doc.get("namespace", "global") == target_namespace]
        if not scoped:
            return "저장된 문서가 없습니다."
        
        result = ["저장된 문서 목록:"]
        for document in scoped:
            result.append(f"- {document.get('doc_id') or os.path.basename(document['source'])}")
        
        return "\n".join(result)


# Singleton instance
_rag_manager = None


def get_rag_manager() -> VectorRAGManager:
    global _rag_manager
    if _rag_manager is None:
        _rag_manager = VectorRAGManager()
    return _rag_manager

# 기존 함수명 유지 (호환성)
def get_rag() -> VectorRAGManager:
    return get_rag_manager()
