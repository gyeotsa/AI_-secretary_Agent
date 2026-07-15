import os
import re
import json
from config import Config


class SimpleRAGManager:
    """PyTorch/Transformers가 필요 없는 간단한 키워드 기반 RAG"""
    def __init__(self):
        self.data_dir = os.path.dirname(Config.DB_PATH)
        self.rag_file = os.path.join(self.data_dir, "simple_rag.json")
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 문서 저장소: {doc_id: {chunks: [text, ...], source: file_path}}
        self.documents = {}
        self._load()
    
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
            
            return f"문서 '{doc_id}'가 성공적으로 추가되었습니다! ({len(chunks)}개 청크)"
        
        except Exception as e:
            return f"문서 추가 오류: {str(e)}"
    
    def search_docs(self, query: str, top_k: int = 3) -> str:
        if not self.documents:
            return "저장된 문서가 없습니다."
        
        # 간단한 키워드 기반 검색: 쿼리의 단어가 청크에 얼마나 많이 포함되어 있는지로 점수 계산
        query_words = set(re.split(r'\W+', query.lower()))
        results = []
        
        for doc_id, doc_data in self.documents.items():
            for i, chunk in enumerate(doc_data["chunks"]):
                chunk_words = set(re.split(r'\W+', chunk.lower()))
                overlap = len(query_words & chunk_words)
                if overlap > 0:
                    results.append((overlap, chunk, doc_data["source"]))
        
        if not results:
            return "관련 문서를 찾을 수 없습니다."
        
        # 점수 높은 순으로 정렬
        results.sort(reverse=True, key=lambda x: x[0])
        top_results = results[:top_k]
        
        context_parts = []
        for score, chunk, source in top_results:
            context_parts.append(f"[출처: {os.path.basename(source)}]\n{chunk}")
        
        return "\n\n---\n\n".join(context_parts)
    
    def list_documents(self) -> str:
        if not self.documents:
            return "저장된 문서가 없습니다."
        
        result = ["저장된 문서 목록:"]
        for doc_id in self.documents.keys():
            result.append(f"- {doc_id}")
        
        return "\n".join(result)


# Singleton instance
_rag_manager = None


def get_rag_manager() -> SimpleRAGManager:
    global _rag_manager
    if _rag_manager is None:
        _rag_manager = SimpleRAGManager()
    return _rag_manager
