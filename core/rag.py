import os
import re
import json
from config import Config


class VectorRAGManager:
    """Vector DB + Embedding + Reranker 기반 RAG (없으면 Simple RAG로 fallback)"""
    def __init__(self):
        self.data_dir = os.path.dirname(Config.DB_PATH)
        self.rag_file = os.path.join(self.data_dir, "simple_rag.json")
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 문서 저장소: {doc_id: {chunks: [text, ...], source: file_path}}
        self.documents = {}
        self._load()
        
        # Vector DB, Embedding, Reranker 초기화 (try-except로 fallback)
        self.use_vector_rag = False
        self.vector_db = None
        self.embedding_model = None
        self.reranker = None
        self._init_vector_rag()
    
    def _init_vector_rag(self):
        try:
            # 1. Chroma DB 초기화
            import chromadb
            from chromadb.utils import embedding_functions
            
            self.vector_db_dir = os.path.join(self.data_dir, "chroma_db")
            self.chroma_client = chromadb.PersistentClient(path=self.vector_db_dir)
            self.collection = self.chroma_client.get_or_create_collection(name="jarvis_rag")
            
            # 2. Embedding 모델 초기화 (BAAI/bge-small-ko-v1.5)
            self.embedding_model = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="BAAI/bge-small-ko-v1.5"
            )
            
            # 3. Reranker 초기화 (BAAI/bge-reranker-v2-m3)
            try:
                from sentence_transformers import CrossEncoder
                self.reranker = CrossEncoder("BAAI/bge-reranker-v2-m3")
            except ImportError:
                print("⚠️ Reranker 모듈이 없어서 키워드 기반 리랭킹 사용합니다.")
                self.reranker = None
            
            self.use_vector_rag = True
            print("✅ Vector RAG 초기화 완료!")
            
        except ImportError as e:
            print(f"⚠️ Vector RAG 라이브러리가 없어서 Simple RAG로 fallback합니다: {e}")
            self.use_vector_rag = False
        except Exception as e:
            print(f"⚠️ Vector RAG 초기화 오류: {e}, Simple RAG로 fallback합니다.")
            self.use_vector_rag = False
    
    def _load(self):
        if os.path.exists(self.rag_file):
            try:
                with open(self.rag_file, "r", encoding="utf-8") as f:
                    self.documents = json.load(f)
            except Exception as e:
                print(f"⚠️ RAG 파일 로드 오류: {e}")
                self.documents = {}
    
    def _save(self):
        try:
            with open(self.rag_file, "w", encoding="utf-8") as f:
                json.dump(self.documents, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ RAG 파일 저장 오류: {e}")
    
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
    
    def add_document(self, file_path: str) -> str:
        if not os.path.exists(file_path):
            return f"⚠️ 파일 '{file_path}'가 존재하지 않습니다."
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
            
            chunks = self.chunk_text(text)
            if not chunks:
                return "⚠️ 파일 내용이 비어있습니다."
            
            doc_id = os.path.basename(file_path)
            self.documents[doc_id] = {
                "chunks": chunks,
                "source": file_path
            }
            self._save()
            
            # Vector DB에 추가 (사용 가능하면)
            if self.use_vector_rag:
                try:
                    # 기존 문서 삭제
                    existing_ids = self.collection.get(where={"doc_id": doc_id})["ids"]
                    if existing_ids:
                        self.collection.delete(ids=existing_ids)
                    
                    # 새로운 문서 추가
                    ids = [f"{doc_id}_chunk_{i}" for i in range(len(chunks))]
                    metadatas = [{"doc_id": doc_id, "source": file_path} for _ in chunks]
                    self.collection.add(
                        ids=ids,
                        documents=chunks,
                        metadatas=metadatas
                    )
                    print(f"✅ Vector DB에 문서 '{doc_id}' 추가 완료!")
                except Exception as e:
                    print(f"⚠️ Vector DB 추가 오류: {e}")
            
            return f"문서 '{doc_id}'가 성공적으로 추가되었습니다! ({len(chunks)}개 청크)"
        
        except Exception as e:
            return f"문서 추가 오류: {str(e)}"
    
    def search_docs(self, query: str, top_k: int = 3) -> list:
        """검색 결과를 dict 리스트로 반환 (기존 str 대신)"""
        if not self.documents:
            return []
        
        if self.use_vector_rag:
            return self._vector_search(query, top_k)
        else:
            return self._simple_search(query, top_k)
    
    def _vector_search(self, query: str, top_k: int) -> list:
        """Vector DB 기반 검색 + Reranker"""
        try:
            # 1. Vector DB로 초기 검색 (top_k * 2개)
            initial_results = self.collection.query(
                query_texts=[query],
                n_results=top_k * 2
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
                return [
                    {"content": chunk, "source": meta["source"]}
                    for _, chunk, meta in top_results
                ]
            else:
                # Reranker 없으면 초기 결과 반환
                return [
                    {"content": chunk, "source": meta["source"]}
                    for chunk, meta in zip(chunks[:top_k], metadatas[:top_k])
                ]
        
        except Exception as e:
            print(f"⚠️ Vector 검색 오류: {e}, Simple 검색으로 fallback합니다.")
            return self._simple_search(query, top_k)
    
    def _simple_search(self, query: str, top_k: int) -> list:
        """기존 Simple 키워드 기반 검색"""
        query_words = set(re.split(r'\W+', query.lower()))
        results = []
        
        for doc_id, doc_data in self.documents.items():
            for chunk in doc_data["chunks"]:
                chunk_words = set(re.split(r'\W+', chunk.lower()))
                overlap = len(query_words & chunk_words)
                if overlap > 0:
                    results.append((overlap, chunk, doc_data["source"]))
        
        if not results:
            return []
        
        # 점수 높은 순으로 정렬
        results.sort(reverse=True, key=lambda x: x[0])
        top_results = results[:top_k]
        
        return [
            {"content": chunk, "source": source}
            for _, chunk, source in top_results
        ]
    
    def list_documents(self) -> str:
        if not self.documents:
            return "저장된 문서가 없습니다."
        
        result = ["저장된 문서 목록:"]
        for doc_id in self.documents.keys():
            result.append(f"- {doc_id}")
        
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
