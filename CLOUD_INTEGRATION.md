# ANIS 클라우드 계정·본문·메시지 연결

2026-10-04 기준 구현 계약이다. Google/Microsoft OAuth UI, Drive 본문 RAG 경로와
Slack/Teams 근거 추출형 요약은 구현되었지만 **실제 사용자 계정의 인증·본문 사용·요약 수락은
미검증**이다. 모의 제공자, 임시 저장소와 loopback 시험은 실제 계정 검증을 대신하지 않는다.
네이버 메일은 IMAP/SMTP 계정 연결이고, 네이버 캘린더는 별도의 로그인된 브라우저 방식이다.
둘 다 이 Google/Microsoft OAuth 연결과 구분한다.
범위는 [NAVER_INTEGRATION.md](NAVER_INTEGRATION.md)를 참고한다.

## Google · Microsoft 계정 연결

앱의 플러그인 화면 또는 플러그인 진단에서 **Google · Microsoft 연결**을 연다.
연결할 이메일과 직접 발급한 OAuth Client ID를 입력하고, 해당 앱에 필요한 경우에만
Client Secret을 입력한다. 로그인·동의는 기본 브라우저에서 사용자가 직접 진행한다.
비밀번호·인증 코드·토큰을 채팅에 입력하지 않는다. 기존 `oauth_begin`/`oauth_complete`는
저수준 도구 계약이며 직접 계정 연결에는 이 UI를 사용한다.

- Google: Google Cloud의 Desktop app 클라이언트를 사용한다. 기본 콜백 포트는 자동이며
  `http://127.0.0.1:<선택된 포트>/oauth/callback`에서 한 번만 응답을 받는다.
- Microsoft: Public client 앱 등록에 정확한
  `http://127.0.0.1:8765/oauth/callback` URI를 등록한다. 포트를 바꾸면 등록 URI도 맞춰야 한다.
  UI 또는 `MICROSOFT_OAUTH_LOOPBACK_PORT`로 지정하며 0은 허용하지 않는다.
- `.env`의 `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
  `MICROSOFT_OAUTH_CLIENT_ID`, `MICROSOFT_OAUTH_CLIENT_SECRET`도 사용할 수 있다.
  UI 연결의 리스너 주소는 실제 포트로 생성되므로 `OAUTH_REDIRECT_URI` 값을 읽어 사용하지 않는다.

현재 코드는 기능별 동의를 나누지 않고 다음 고정 범위를 함께 요청한다. 읽기만 사용할 때도
쓰기 범위가 포함되므로 동의 화면을 확인해야 한다. 요청 범위와 실제 허용 범위는 다르며,
제공자·앱 등록·조직 정책에 따른 별도 동의가 필요할 수 있다.

| 제공자 | 코드가 요청하는 범위 |
| --- | --- |
| Google | `openid`, `email`, `https://www.googleapis.com/auth/gmail.modify`, `https://www.googleapis.com/auth/calendar`, `https://www.googleapis.com/auth/drive.readonly` |
| Microsoft | `openid`, `profile`, `offline_access`, `User.Read`, `Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`, `Files.Read`, `ChannelMessage.Read.All`, `ChannelMessage.Send` |

PKCE S256와 일회성 state를 사용한다. 콜백은 IPv4 loopback, 정확한 Host·경로·state를 검사하고
중복 파라미터·Origin 헤더·만료 응답을 거부한다. UI 대기는 기본 180초이며, 토큰 교환과
계정 신원 조회는 각 15초 제한·리디렉션 금지다. 콜백을 받았다는 사실만으로 연결 완료를
표시하지 않는다. Google은 검증된 이메일, Microsoft는 `/me`의 신원과 이메일을 확인하고
입력한 계정과 다르면 저장하지 않는다. 기존 저장 계정의 원격 subject 변경도 거부한다.

토큰·갱신 토큰·앱 자격증명은 DB 디렉터리 아래 `oauth_tokens/`의 Windows DPAPI v2
봉투에 저장한다. 제공자와 계정 문자열의 정확한 조합을 SHA-256 파일명과 봉투 내용에 묶는다.
예전 계정 구분이 불명확한 파일은 자동 복호화·이관·삭제하지 않고 재연결을 요구한다.
상태 UI는 저장 여부, 마지막 신원 확인, 로컬 만료 정보와 응답에 명시된 scope만 표시하며
현재 API 접속 성공을 보장하지 않는다. scope가 응답에 없으면 허용되었다고 추측하지 않는다.

연결 중 취소·닫기는 작업 종료를 기다리며 비밀 입력을 지운다. 취소 직전에 저장이 완료될
수 있으므로 로컬 상태를 확인한다. **로컬 연결 해제**는 확인 후 해당 계정의 v2 토큰 파일만
삭제한다. 제공자 측 앱 접근 철회나 이미 저장한 RAG 본문 삭제는 별도다.

## 카탈로그와 Drive 본문은 별개

`cloud_sync_catalog`는 Drive/OneDrive/Notion의 **메타데이터만** 조회한다. 본문 RAG가 아니다.
페이지 크기는 기본 50(최대 100), 한 호출은 기본 5페이지(최대 10)다. 부분·필터·재개 결과는
기존 목록에 병합하며, 재개 조회가 끝에 도달해도 전체 스냅샷 완료로 표시하지 않는다.
첫 페이지부터 필터 없이 끝까지 확인한 결과만 해당 계정의 카탈로그를 교체한다.
Notion은 `NOTION_API_TOKEN`, Google/Microsoft는 위 OAuth 연결이 필요하다.

Drive 본문 도구는 `plugins/cloud_knowledge.py`에서 등록한다.

```json
{"provider":"google_drive","account":"owner@example.com","file_ids":["EXPLICIT_DRIVE_FILE_ID"]}
```

예시의 `EXPLICIT_DRIVE_FILE_ID`는 사용자가 선택한 실제 ID로 바꿔 `cloud_sync_documents`를
호출한다. URL·제목에서 ID를 추측하거나 전체 Drive를
백그라운드 수집하지 않는다. 한 번에 서로 다른 ID 1~10개만 받는다.
`cloud_search_evidence`는 같은 `provider`·`account`와 `query`, 선택적 `top_k`(기본 3, 최대 10)를
받아 **이미 명시적으로 등록한 본문**의 제한된 후보를 검색한다.

- 지원: UTF-8(선택적 BOM) `text/plain`, `text/markdown`, `text/x-markdown`, `text/csv`,
  Google Docs의 `text/plain` 내보내기. 본문은 최대 2MiB, 메타데이터는 64KiB다.
- 미지원: PDF, Office, Google Sheets/Slides, Drive 바로가기, 바이너리·비 UTF-8 본문.
  메타데이터나 제목을 문서 본문으로 대신 색인하지 않는다.
- 고정 Drive API URL만 사용하고 인증 리디렉션을 따르지 않는다. 요청별 15초, 동기화는
  협조적 총 60초 예산이며 도구 외부 상한은 90초다. 네이티브/색인 작업을 즉시 되돌리는
  하드 실시간 제한은 아니다.
- 다운로드 전후 원격 버전을 비교하고 일반 텍스트 파일의 제공된 MD5도 검사한다.
  본문 SHA-256, 파일 ID·버전·출처 URL과 Chunk ID를 기존 RAG에 저장한다.
  정확한 계정 문자열별 namespace로 격리하며, 이 경로는 global 문서를 반환하지 않는다.
- 검색 후보는 원격 접근·버전을 다시 확인한다. 접근 거절·삭제·휴지통·미지원 형식은 해당
  계정의 오래된 본문 제외를 시도한다. 일시 오류는 캐시를 보존하지만 새 근거로 반환하지 않는다.
  제외 실패와 부분 성공은 명시한다. 취소 전에 반영한 파일을 전체 롤백했다고 주장하지 않는다.

본문·출처·계정 메타데이터는 기존 `simple_rag.json`/벡터 저장소에 남는다. OAuth 토큰의
DPAPI 암호화가 RAG 본문까지 암호화하는 것은 아니다. 자동 보존 기간이나 연결 해제 시
본문 자동 삭제는 없다. 검색 결과는 `untrusted_source_data`이며, 실제 최종 답변에 올바르게
사용되었다는 수락 증거는 `answer_usage_verified: false`로 남긴다.

## Slack · Teams 근거 추출형 요약

`communication_read_summary`는 Slack의 `channel` 또는 Teams의 `team_id`·`channel_id`와
계정을 받는다. Slack은 `SLACK_BOT_TOKEN`을 사용하며 Slack OAuth UI나 계정별 토큰 저장은
구현하지 않았다. Teams는 저장한 Microsoft 계정으로 조회한다. 조회 가능한 채널 권한은
실제 토큰과 제공자 정책으로 확인해야 한다.

한 요청의 메시지 묶음만 읽으며 기본 50개·최대 100개다. 전체 채널 페이지, 스레드 답글,
첨부 자료를 수집해 누락 없는 대화를 재구성하는 기능은 아니다. 메시지당 최대 3,000자,
전체 본문 16,000자를 선별용 입력으로 사용하고 생략이 있으면 부분 결과로 표시한다.

일반 대화의 클라우드 모델을 사용하지 않고 `OllamaClient("reasoning")`을 사용한다.
이 요약 경로는 자격증명 없는 `localhost`/`127.0.0.1`/`::1` HTTP(S) endpoint만 허용하며
원격 Ollama 설정은 거부한다. 본문은 JSON 인용 자료로 전달하고 모델 도구는 제공하지 않는다.
생성은 협조적 45초 예산, 요청별 최대 30초·출력 2,048토큰이며 자동 재시도하지 않는다.

핵심 내용은 최대 8개, 결정·할 일은 각각 최대 5개다. 각 항목은 실제 제공한 메시지 ID와
원문 인용에 연결하며 항목 문구 자체가 인용 하나와 정확히 같아야 한다. 담당자·기한도
인용에 있는 문자열만 허용하고 없으면 null이다. 이는 출처 일치 검증이지 결정/할 일의
의미 분류 정확도 검증이 아니다. 오류·시간 초과는 조회 ID·미리보기만 보존한 부분/실패이며
요약 완료로 표시하지 않는다. 실제 계정의 요약 품질 수락은 아직 없다.

## 외부 변경과 검증

메일 전송·일정 생성·Slack/Teams 전송은 `remote_create_draft`로 로컬 초안을 만들고
`remote_apply_draft`에서 기존 외부 행동 승인을 거친다. 원격 ID만 받으면 완료가 아니며
요청한 본문/수신자/일정 등을 재조회한 증거가 필요하다. 결과가 불확실한 작업은 원장에
`uncertain`으로 남겨 같은 작업 ID의 재전송을 차단한다. 제공자 재조회는 최종 수신자의
실제 수신·열람 증거가 아니다. 실제 계정 업무별 수락은 별도다.

## 재현 가능한 확인 범위

```powershell
.\.venv\Scripts\python.exe -m pytest test_oauth_connection.py test_oauth_account_dialog.py test_cloud_content.py test_cloud_knowledge.py test_communication_summary.py -q
```

테스트는 모의 OAuth 응답과 임시 DPAPI 저장소, 실제 loopback 콜백, Qt worker 정리,
기존 RAG의 본문·hash·계정 격리·버전/철회/취소 계약, 원문 요약 검증·크기/취소/시간 제한을
다룬다. 실제 Google/Microsoft 클라이언트 등록과 사용자 계정 동의, Drive 계정 본문으로
최종 답변 수락, Slack/Teams 실제 계정 요약 수락은 실행하지 않았다.
