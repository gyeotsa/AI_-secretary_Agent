# P12 Security, Observability, Productization

P12는 폴더·앱·도메인·계정 범위 권한과 `once/session/always` 정책, Windows Credential
Manager, 민감정보 마스킹 회전 JSON Trace, Task/Tool/verification 상관관계 ID, 메트릭,
진단 보고서와 안전 모드를 구현합니다.

경량 배포 엔진은 `packaging/bootstrap.py`와 `bootstrap_manifest.json`을 사용합니다. 다만 실제
호스팅 URL을 선택하지 않은 저장소 기본 manifest는 명시적인 미게시 템플릿이며, 빈 자산을 성공으로
표시하지 않습니다. 배포 운영자가 URL과 SHA-256을 게시한 뒤에만 사용자가 선택한 역할을 원자적으로
설치합니다. 현재 완전 오프라인 배포는 `packaging/build_release.ps1`이 담당합니다. 업데이트는
checksum, ZIP 경로 검증, backup/rollback을 사용하고 DB migration은 version 원장으로 중복 적용을
막습니다.

자동 E2E 프로브는 실행하지 않은 네트워크 검사와 자격증명 없는 OAuth를 성공으로 표시하지
않습니다. 실제 계정, 외부 네트워크, 마이크·카메라·GPU와 장시간 운영은 배포 후보를 일반
Windows 사용자 세션에서 검증해야 합니다.

실제 배포는 표시용 런처 `JARVIS.exe`와 대형 ML/Qt 의존성을 담은
`JARVIS-runtime.exe`를 분리합니다. 런처 smoke는 독립적으로 배포 구조를 확인하고,
런타임 smoke는 Qt 플랫폼·아이콘·응용 모듈 import를 실제 실행합니다. 빌드 환경의
Poppler ICU는 Windows system ICU를 사용하는 Qt 6와 호환되지 않아 배포에서 제외하며,
`jamo`/`g2pk` 데이터는 음성 정규화 실행을 위해 명시적으로 포함합니다.
