import os
import base64
import tempfile
import time
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
        from core.llm import get_llm_client
        self.vision_llm = get_llm_client("vision")

    @staticmethod
    def _camera_backends():
        """Windows에서 안정적인 순서로 카메라 백엔드를 반환합니다."""
        backends = []
        if os.name == "nt":
            backends.extend([
                ("MSMF", getattr(cv2, "CAP_MSMF", 1400)),
                ("DSHOW", getattr(cv2, "CAP_DSHOW", 700)),
            ])
        backends.append(("DEFAULT", getattr(cv2, "CAP_ANY", 0)))
        return backends

    @staticmethod
    def _configured_camera_indices(max_index: int = 5):
        requested = str(Config.CAMERA_INDEX).strip()
        if requested and requested.casefold() != "auto":
            try:
                return [int(requested)]
            except ValueError as exc:
                raise ValueError(f"CAMERA_INDEX는 auto 또는 0 이상의 정수여야 합니다: {requested}") from exc
        return list(range(max_index))

    def list_camera_devices(self, max_index: int = 5) -> list[dict]:
        if not OPENCV_AVAILABLE:
            return []
        devices = []
        found_indices = set()
        for index in self._configured_camera_indices(max_index):
            for backend_name, backend in self._camera_backends():
                cap = cv2.VideoCapture(index, backend)
                try:
                    if not cap.isOpened():
                        continue
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        height, width = frame.shape[:2]
                        devices.append({
                            "index": index,
                            "backend": backend_name,
                            "width": width,
                            "height": height,
                        })
                        found_indices.add(index)
                        break
                finally:
                    cap.release()
        return devices

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
            if save_path is not None:
                is_valid, error_msg = self.safety.validate_path(save_path)
                if not is_valid:
                    return f"오류: {error_msg}"
            else:
                save_path = os.path.join(
                    tempfile.gettempdir(), f"camera_capture_{os.urandom(4).hex()}.jpg"
                )

            for index in self._configured_camera_indices():
                for backend_name, backend in self._camera_backends():
                    cap = cv2.VideoCapture(index, backend)
                    try:
                        if not cap.isOpened():
                            continue
                        frame = None
                        for _ in range(5):
                            ok, candidate = cap.read()
                            if ok and candidate is not None:
                                frame = candidate
                            time.sleep(0.03)
                        if frame is None:
                            continue
                        if not cv2.imwrite(save_path, frame):
                            return f"오류: 카메라 이미지를 저장하지 못했습니다: {save_path}"
                        height, width = frame.shape[:2]
                        return (
                            f"성공: 카메라 {index}({backend_name}, {width}x{height})의 프레임을 "
                            f"{save_path}에 저장했습니다."
                        )
                    finally:
                        cap.release()
            return "오류: 프레임을 읽을 수 있는 카메라를 찾지 못했습니다."
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
