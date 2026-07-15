import os
import base64
import tempfile
from typing import Optional
from config import Config
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

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False


class MultimodalManager:
    def __init__(self):
        self.safety = SafetyLayer()

    def analyze_image(self, image_path: str, prompt: str = "이 이미지에 무엇이 있나요?") -> str:
        """
        이미지를 분석합니다.
        현재는 로컬 이미지 정보만 제공하고, 향후 Vision API와 연동할 수 있습니다.
        """
        if not PIL_AVAILABLE:
            return "오류: Pillow가 설치되지 않았습니다. requirements.txt를 확인하세요."

        is_valid, error_msg = self.safety.validate_path(image_path)
        if not is_valid:
            return error_msg

        try:
            with Image.open(image_path) as img:
                info = {
                    "파일명": os.path.basename(image_path),
                    "크기": f"{img.width}x{img.height}",
                    "포맷": img.format,
                    "모드": img.mode,
                    "프롬프트": prompt
                }

                result = [
                    f"🖼️ 이미지 분석 결과:",
                    f"",
                    f"📁 파일명: {info['파일명']}",
                    f"📐 크기: {info['크기']}",
                    f"🎨 포맷: {info['포맷']}",
                    f"🌈 모드: {info['모드']}",
                    f"",
                    f"💡 질문: {prompt}",
                    f"",
                    f"참고: 현재 이미지의 메타데이터만 분석합니다. 더 자세한 분석을 위해 Vision API를 추가로 연동할 수 있습니다."
                ]

                return "\n".join(result)

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
                    page = doc[page_num - 1]
                    all_text += page.get_text() + "\n"
                
                result.append(all_text[:3000])
                if len(all_text) > 3000:
                    result.append("\n... (텍스트가 길어서 일부만 표시)")

            doc.close()
            return "\n".join(result)

        except Exception as e:
            return f"PDF 분석 오류: {str(e)}"

    def capture_camera_frame(self, save_path: Optional[str] = None) -> str:
        """
        카메라에서 프레임을 캡처합니다.
        save_path가 지정되면 해당 경로에 저장하고, 아니면 임시 파일에 저장합니다.
        """
        if not OPENCV_AVAILABLE:
            return "오류: opencv-python이 설치되지 않았습니다. requirements.txt를 확인하세요."

        try:
            # 카메라 열기 (0은 기본 카메라)
            cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return "오류: 카메라를 열 수 없습니다."

            # 프레임 캡처
            ret, frame = cap.read()
            cap.release()

            if not ret:
                return "오류: 카메라에서 프레임을 캡처할 수 없습니다."

            # 저장 경로 결정
            if save_path is None:
                temp_dir = tempfile.gettempdir()
                save_path = os.path.join(temp_dir, f"camera_capture_{os.urandom(4).hex()}.jpg")

            # 프레임 저장
            cv2.imwrite(save_path, frame)

            return f"성공: 카메라 프레임을 {save_path}에 저장했습니다."

        except Exception as e:
            return f"카메라 캡처 오류: {str(e)}"

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
