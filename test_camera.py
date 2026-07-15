from core.multimodal import get_multimodal_manager

def test_camera():
    print("카메라 테스트를 시작합니다...")
    manager = get_multimodal_manager()
    
    result = manager.capture_camera_frame()
    print(result)

if __name__ == "__main__":
    test_camera()
