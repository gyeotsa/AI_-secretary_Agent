# 시안 제작 생성형 백엔드

시안 제작 전문가는 두 입력을 엄격히 구분한다.

1. 학습용 참고 시안: 색상·비율·레이아웃 분석과 IP-Adapter 스타일 조건에만 사용한다.
2. 제작용 사진: 생성형 배경 위에 원본 픽셀을 보존해 합성한다.

## 실행 방식

- `auto`: CUDA와 모델 파일이 준비되면 생성형, 아니면 로컬 Pillow 합성
- `generative`: Stable Diffusion 1.5 fp16 + IP-Adapter Plus를 반드시 사용
- `local`: 네트워크와 생성 모델 없이 빠른 정량 스타일 합성

최초에는 시안 작업공간에서 **생성형 모델 준비**를 누른다. 모델은
`data/models/mockup_generation`에 저장되고 Git에는 포함되지 않는다. 이후 모델 파일이 있으면
오프라인 추론이 가능하다.

## 선택한 모델

- Stable Diffusion 1.5: https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5
- IP-Adapter / IP-Adapter Plus: https://huggingface.co/h94/IP-Adapter
- IP-Adapter 파일 목록: https://huggingface.co/h94/IP-Adapter/tree/main/models

IP-Adapter Plus는 참고 이미지 특징을 가벼운 어댑터로 주입한다. 프로젝트의 8GB RTX 4060
환경에서는 SDXL 계열보다 메모리와 대기시간이 작아 1차 생성형 백엔드로 적합하다.

## 정확한 의미와 한계

이 기능의 “학습”은 새 모델 가중치를 훈련하는 fine-tuning이 아니다. 참고 이미지의 정량 특성과
Vision 설명을 프로필로 저장하고, 생성할 때 IP-Adapter로 참고 이미지를 조건화한다. 따라서 색감과
분위기, 일부 구도는 전이할 수 있지만 로고·텍스트·정확한 좌표를 그대로 복제한다고 보장하지 않는다.
그 요구에는 유형별 템플릿, segmentation, ControlNet을 추가해야 한다.

생성형 배경은 글자와 로고를 만들지 않도록 제한한다. 최종 텍스트와 브랜드 자산은 별도의 결정적
합성 단계에서 배치하는 것이 안전하다. 현재 안전 필터는 로컬 업무용 배경 생성에서 비활성화되어
있으므로 공개 서비스나 재배포 전에 모델 라이선스와 콘텐츠 안전 정책을 반드시 다시 검토한다.
