# P12 Security, Observability, Productization

P12는 폴더·앱·도메인·계정 범위 권한과 `once/session/always` 정책, Windows Credential
Manager, 민감정보 마스킹 회전 JSON Trace, Task/Tool/verification 상관관계 ID, 메트릭,
진단 보고서와 안전 모드를 구현합니다.

경량 배포는 `packaging/bootstrap.py`와 `bootstrap_manifest.json`을 사용합니다. Release
자동화가 asset URL과 SHA-256을 채우면 사용자가 선택한 역할만 첫 실행에 내려받아 원자
설치합니다. 업데이트는 checksum, ZIP 경로 검증, backup/rollback을 사용하고 DB migration은
version 원장으로 중복 적용을 막습니다.

자동 E2E 프로브는 실행하지 않은 네트워크 검사와 자격증명 없는 OAuth를 성공으로 표시하지
않습니다. 실제 계정, 외부 네트워크, 마이크·카메라·GPU와 장시간 운영은 배포 후보를 일반
Windows 사용자 세션에서 검증해야 합니다.
