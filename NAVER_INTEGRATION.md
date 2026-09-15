# 네이버 메일·캘린더 연동 상태와 수락 기준

기준일: 2026-09-14. 사용자는 네이버 공개 검색보다 **메일·캘린더를 우선**하기로 결정했다.
이 문서는 ANIS 자체 연동에 관한 것이며 Codex 플러그인 설치 상태와 무관하다.

## 현재 실행 가능한 메일 경로

- `mail_list_inbox`: 받은편지함의 최신 메일 헤더 목록. 최대 50개, 읽지 않은 메일 필터,
  UID 기반 이전 페이지. 다음 페이지에는 최초 결과의 `uid_validity`가 필요하다.
- `mail_read_message`: 위 목록의 `uid`와 `uid_validity`로 지정한 한 메일의 일반 텍스트 본문.
  IMAP의 읽기 전용 INBOX와 `BODY.PEEK`만 사용한다. 읽음 표시·삭제·이동·첨부 실행은 하지 않는다.
- MIME의 일반 텍스트 부분만 반환한다. HTML 전용 메일은 `body_available=false`이며,
  첨부파일·연결된 이미지·외부 URL을 자동으로 가져오지 않는다. 본문은 최대 256 KiB 수신 범위와
  표시 16,000자 한도를 적용하고 `truncated`를 명시한다. 전체 메일 백업/본문 RAG 동기화가 아니다.
- UIDVALIDITY가 바뀌거나 다른 UID의 응답이 오면 메일을 잘못 읽지 않고 목록 재조회를 요구한다.
  목록 조회 뒤 새 메일/삭제가 발생할 수 있으므로 원격 전체 스냅샷이라고 표시하지 않는다.
- `mail_create_draft`: 로컬 RFC 822 초안. `mail_send_smtp`: 기존 `mail_send` 승인 경계의 발송.
  TLS 전용이며 STARTTLS는 암호 전송 전에 TLS 전환을 완료해야 한다. 평문 fallback은 없다.
- SMTP 접수는 수신함 도착·읽음과 다르다. 전송 도중 오류/종료 실패/일부 수신자 거부는
  `unverified`이며 자동 재전송하지 않는다. 생성된 코드를 실행하거나 실제 메일을 보내는 QA는 하지 않았다.

## 연결 설정 — 아직 사용자용 계정 연결 UI는 없음

현재 구현은 `.env.example`에 있는 명시적 설정을 사용한다. 실제 `.env`·계정·비밀번호는
이번 작업에서 열람하거나 변경하지 않았다. 비밀번호를 채팅/프롬프트/테스트 로그에 붙여 넣지 않는다.

| 설정 | 네이버 값 / 의미 |
| --- | --- |
| `MAIL_PROVIDER` | `naver` |
| `MAIL_IMAP_HOST` / `MAIL_IMAP_PORT` | 생략 시 `imap.naver.com` / `993` (SSL) |
| `MAIL_IMAP_USERNAME` / `MAIL_IMAP_PASSWORD` | 사용자 직접 설정하는 메일 계정과 인증정보 |
| `MAIL_SMTP_HOST` / `MAIL_SMTP_PORT` | 생략 시 `smtp.naver.com` / `587` |
| `MAIL_SMTP_SECURITY` | 생략 시 `starttls`; 명시적으로 `ssl` 또는 `starttls`만 허용 |
| `MAIL_SMTP_USERNAME` / `MAIL_SMTP_PASSWORD` | 발송 계정과 인증정보. IMAP 비밀번호를 임의 상속하지 않음 |
| `MAIL_FROM` | 필요 시 실제 계정에서 허용되는 발신 주소 |

네이버 메일 설정에서 IMAP/SMTP 사용이 필요하다. 서버/포트는
[네이버 공식 IMAP/SMTP 안내](https://help.naver.com/service/30029/contents/21351?osType=COMMONOS)를 따른다.
2단계 인증을 끄는 방식은 사용하지 않는다. 앱 비밀번호가 필요한 계정은 사용자가 네이버 보안 화면에서
직접 발급하고 연결 화면/로컬 설정에 직접 입력해야 한다. 다음 구현에서는 이 평문 환경변수 경로를
사용자용 기본 UX로 삼지 않고 DPAPI 기반 계정 입력·검증·철회 UI로 연결한다.

## 캘린더의 공식 지원 범위와 남은 작업

- 공개 [캘린더 API 명세](https://developers.naver.com/docs/login/calendar-api/calendar-api.md)는
  OAuth와 개발자 애플리케이션 등록/권한을 요구하고, 일정 추가 엔드포인트를 제공한다.
  이를 일정 목록 조회·삭제·완전 동기화 API로 확대 해석하지 않는다.
- [공식 튜토리얼](https://developers.naver.com/docs/login/calendar-tutorial/calendar-tutorial.md)은
  같은 UID의 재전송이 수정으로 처리될 수 있음을 설명한다. 따라서 UID 생성/보존과 승인 범위,
  중복 재전송 차단이 필요하다. API 응답 ID만으로 원격 재조회 검증이 된 것은 아니다.
- [공식 CalDAV 안내](https://help.naver.com/service/5620/bookmark/2426?osType=COMMONOS)는
  iOS 지원, Mac 읽기 전용, Android 미지원 및 앱 비밀번호 조건을 안내한다. Windows ANIS의
  상호운용성은 이 자료만으로 확인되지 않는다. 다른 OS로 가장하거나 2단계 인증을 해제하지 않는다.
- 따라서 이번 변경은 캘린더 연동 완료가 아니다. 기존 `calendar_create_event`는 로컬 ICS 생성이며
  네이버 서버의 일정 생성/조회와 다르다. 계정 연결 버튼이나 빈 성공 핸들러를 추가하지 않았다.

다음 구현 순서:

1. 공통 계정 UI: 사용자 직접 입력, DPAPI 저장, 계정별 연결 시험, 마지막 검증 시각,
   설정됨/인증됨/실행 확인됨 구분, 취소/철회. 메일 읽기와 발송의 인증 결과는 따로 표시한다.
2. 캘린더 조회: 명시적으로 선택한 계정에서 표준 CalDAV 읽기 호환성을 확인한다. 미지원이면
   사용자 로그인 세션의 관찰 기반 브라우저 경로 또는 명시적 ICS 내보내기 경로를 선택한다.
   지원 불가를 조회 성공으로 표시하지 않는다. 반복 일정·시간대·기간·동기화 변경을 검사한다.
3. 일정 작성: 네이버 개발자 등록/OAuth 선택 후 승인된 일정만 API에 반영하거나,
   지원되는 브라우저 경로에서 화면을 재관찰한다. 서버 접수와 실제 일정 내용 검증을 구분한다.
4. 실제 수락: 시험 메일 목록→본문→읽음 상태 보존, 승인 발송→수신 확인;
   일정 조회→초안→승인→등록→네이버 화면의 제목/시간/시간대 재확인. 계정별 접근 철회도 검사한다.

## 테스트 증거의 경계

- `test_mail_read_runtime.py`: 가짜 IMAP 서버와 실제 MIME 파서를 사용한 79개 오프라인 검사.
- `test_mail_security_contract.py`: TLS 순서, 취소, 불확실 전송, 비밀값 비노출, 도구 연결 검사.
- 실제 네이버 계정 접속·실제 메일 발송/도착·캘린더 수락은 아직 수행하지 않았다.
- 자동 계약 검사는 실제 서비스 호환성이나 사용자 체감 품질을 대신하지 않는다.
