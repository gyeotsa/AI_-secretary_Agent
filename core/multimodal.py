import os
import base64
from typing import Optional
from core.harness import SafetyLayer


try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False


class MultimodalManager:
    def __init__(self):
        self.safety = SafetyLayer()
        from core.llm import get_llm_client
        self.vision_llm = get_llm_client("vision")

    def analyze_image(self, image_path: str, prompt: str = "이 이미지에 무엇이 있나요?") -> str:
        """이미지를 역할 전용 로컬 Vision 모델로 분석합니다."""
        if not PIL_AVAILABLE:
            return "오류: Pillow가 설치되지 않았습니다. requirements.txt를 확인하세요."

        is_valid, error_msg = self.safety.validate_path(image_path)
        if not is_valid:
            return error_msg

        try:
            with open(image_path, "rb") as image_file:
                encoded = base64.b64encode(image_file.read()).decode("ascii")
            response = self.vision_llm.chat([
                {
                    "role": "system",
                    "content": (
                        "당신은 Jarvis의 이미지 분석 담당 모델입니다. 이미지에서 실제로 "
                        "관찰되는 내용만 한국어로 답하고 불확실한 내용은 추측이라고 명시하세요."
                    ),
                },
                {"role": "user", "content": prompt, "images": [encoded]},
            ]).strip()
            return response or "이미지 분석 모델이 빈 응답을 반환했습니다."

        except Exception as e:
            return f"이미지 분석 오류: {str(e)}"

    def extract_text_from_pdf(self, pdf_path: str, page_num: Optional[int] = None) -> str:
        """
        PDF에서 텍스트를 추출합니다.
        page_num을 지정하면 특정 페이지만 추출하고, 지정하지 않으면 전체 페이지를 추출합니다.
        """
        if not PYMUPDF_AVAILABLE:
            return "오류: PyMuPDF가 설치되지 않았습니다. requirements.txt를 확인하세요."

        is_valid, error_msg = self.safety.validate_path(pdf_path)
        if not is_valid:
            return error_msg

        try:
            doc = fitz.open(pdf_path)
            total_pages = doc.page_count
            result = []

            result.append(f"📄 PDF 분석 결과:")
            result.append(f"📁 파일명: {os.path.basename(pdf_path)}")
            result.append(f"📖 총 페이지: {total_pages}")
            result.append("")

            if page_num is not None:
                if page_num < 1 or page_num > total_pages:
                    return f"오류: 페이지 번호는 1~{total_pages} 사이여야 합니다."
                
                page = doc[page_num - 1]
                text = page.get_text()
                result.append(f"📄 페이지 {page_num}:")
                result.append("-" * 40)
                result.append(text[:2000])
                if len(text) > 2000:
                    result.append("\n... (텍스트가 길어서 일부만 표시)")
            else:
                result.append("📄 전체 텍스트:")
                result.append("-" * 40)
                all_text = ""
                for i in range(total_pages):
                    page = doc[i]
                    all_text += page.get_text() + "\n"
                
                result.append(all_text[:3000])
                if len(all_text) > 3000:
                    result.append("\n... (텍스트가 길어서 일부만 표시)")

            doc.close()
            return "\n".join(result)

        except Exception as e:
            return f"PDF 분석 오류: {str(e)}"

    def image_to_base64(self, image_path: str) -> str:
        """
        이미지를 base64로 인코딩합니다 (API 전송용)
        """
        is_valid, error_msg = self.safety.validate_path(image_path)
        if not is_valid:
            return error_msg

        try:
            with open(image_path, "rb") as img_file:
                return base64.b64encode(img_file.read()).decode("utf-8")
        except Exception as e:
            return f"이미지 인코딩 오류: {str(e)}"


# Singleton instance
_multimodal_manager = None


def get_multimodal_manager() -> MultimodalManager:
    global _multimodal_manager
    if _multimodal_manager is None:
        _multimodal_manager = MultimodalManager()
    return _multimodal_manager
