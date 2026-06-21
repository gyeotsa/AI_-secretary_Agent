import os
import re
import chromadb
from config import Config


try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False


class RAGManager:
    def __init__(self):
        self.data_dir = os.path.dirname(Config.DB_PATH)
        self.vector_db_path = os.path.join(self.data_dir, "chroma_db")
        os.makedirs(self.vector_db_path, exist_ok=True)
        
        self.client = chromadb.PersistentClient(path=self.vector_db_path)
        self.collection = self.client.get_or_create_collection("my_documents")
        
        if SENTENCE_TRANSFORMERS_AVAILABLE:
            self.embedder = SentenceTransformer("jhgan/ko-sroberta-multitask")
        else:
            self.embedder = None

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
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            return "오류: sentence-transformers가 설치되지 않았습니다. requirements.txt를 확인하세요."
        
        if not os.path.exists(file_path):
            return f"오류: 파일 '{file_path}'가 존재하지 않습니다."
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
            
            chunks = self.chunk_text(text)
            if not chunks:
                return "오류: 파일 내용이 비어있습니다."
            
            embeddings = self.embedder.encode(chunks).tolist()
            doc_id = os.path.basename(file_path)
            
            self.collection.add(
                documents=chunks,
                embeddings=embeddings,
                ids=[f"{doc_id}_chunk_{i}" for i in range(len(chunks))],
                metadatas=[{"source": file_path, "chunk": i} for i in range(len(chunks))]
            )
            
            return f"✅ 문서 '{doc_id}'가 성공적으로 추가되었습니다! ({len(chunks)}개 청크)"
        
        except Exception as e:
            return f"문서 추가 오류: {str(e)}"

    def search_docs(self, query: str, top_k: int = 3) -> str:
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            return "오류: sentence-transformers가 설치되지 않았습니다. requirements.txt를 확인하세요."
        
        try:
            query_emb = self.embedder.encode([query]).tolist()
            results = self.collection.query(query_embeddings=query_emb, n_results=top_k)
            
            if not results["documents"][0]:
                return "관련 문서를 찾을 수 없습니다."
            
            context_parts = []
            for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
                context_parts.append(f"[출처: {meta['source']}]\n{doc}")
            
            return "\n\n---\n\n".join(context_parts)
        
        except Exception as e:
            return f"문서 검색 오류: {str(e)}"

    def list_documents(self) -> str:
        try:
            metadatas = self.collection.get()["metadatas"]
            if not metadatas:
                return "저장된 문서가 없습니다."
            
            sources = set(meta["source"] for meta in metadatas)
            result = ["📚 저장된 문서 목록:"]
            for source in sources:
                result.append(f"- {os.path.basename(source)}")
            
            return "\n".join(result)
        
        except Exception as e:
            return f"문서 목록 오류: {str(e)}"


# Singleton instance
_rag_manager = None


def get_rag_manager() -> RAGManager:
    global _rag_manager
    if _rag_manager is None:
        _rag_manager = RAGManager()
    return _rag_manager
