# 공통 에이전트 런타임 개선 — 2026-09-08~10-06

## 2026-10-06 최신 상태 — 실제 표본 통과와 미검증 후속 구분

- 최신 실제 전체 허용 도구의 9-case 의미 분류는 처음 **8/9**
  (`Temp/anis-common-qa-s8zmd0o6`)였다. 인용 명령의 의미를 설명해 달라는 요청을 unknown으로
  처리했다. 공통 `utterance_scope`의 discussion 판정을 SemanticRequestInterpreter에서도
  재사용한 뒤 **9/9**(`Temp/anis-common-qa-m_bb5q1_`, 실제 qwen2.5:7b 및 model trace)다.
  인용 설명 분류는 추가 모델 호출 0회다. 미지원 응답에 근거 없는 기술 일반화 표현이 남아
  있으므로 **분류 표본 9/9이지 전체 답변 품질 통과가 아니다.** 외부 도구는 실행하지 않았다.
- 최신 실제 Executor/로컬 모델/합성 파일 1–1줄 읽기는 **8.12초 통과 / completed**,
  `Temp/anis-common-qa-7vg1vmem`다. soft shortlist 제거 후 전체 `allowed_tool_names`를
  명시하고 정확한 원문/CRLF·범위·원본 hash·receipt/evidence·표시·원본 보존을 확인했다.
  모델 cold load 3.919초를 포함한 1회 측정이며 평균 지연/속도 비교 근거가 아니다.
  앞선 **3.22초 통과**(`Temp/anis-common-qa-lfke1gyr`)는 soft shortlist 제거 전이다.
  GUI 입력이나 다른 서비스 E2E 수락을 대신하지 않는다. 이전 matrix 9/9
  (`Temp/anis-common-qa-sna_whmf`)와 읽기 3.3초(`Temp/anis-common-qa-asrk6le9`)는
  recovery 수정 전 기록으로 보존하고 최신 표본과 합산하지 않는다.
- typed constraint 검수와 제한 재고려를 구현했고, 근거 없는 대화 복구 경로를 보강했다.
  쉼표/세미콜론/줄바꿈은 완결된 긍정 명령의 경계만 분리하며 금지/조건/인용 범위를 보존한다.
  Executor가 lexical ToolLoadout을 hard allowed scope로 좁히던 9줄을 제거했다. 사용자가
  명시한 scope와 빈 목록 `[]`는 그대로 유지한다. 관련 집중은 **295 passed,
  7 deselected (35.02초)**다. 이전 집중 95 passed(7.61초)와 합산하지 않는다.
- 마지막 전체 통과 **3725 passed, 17 skipped, 34 deselected (239.11초)**는 최신 변경 전이다.
  후속 전체 A는 **2 failed, 3726 passed, 17 skipped, 34 deselected (246.56초)**였고,
  Qt fixture가 부모의 busy 알림을 캘린더 결과 알림과 섞은 오류를 수정한 집중 검사는
  **43 passed (3.34초)**다. 전체 D는 **23 failed, 3734 passed, 17 skipped,
  34 deselected (235.51초)**이며 설명 fastpath 뒤 response_mode 모델 호출 fixture의
  기대값 불일치를 수정했다. 설명/인용 설명 fastpath 3개 및 optional response_mode fixture를
  포함한 4파일 집중은 **345 passed, 23 deselected (32.04초)**다. 최종 전체 **E는
  3765 passed, 17 skipped, 34 deselected (237.18초)**,
  `tmp/qa-oct06-final-all-e.xml`로 통과했다. `pip check`는
  `No broken requirements found`, diff check도 통과했다. A/D 실패와 수정 내역은 보존하며
  skip/deselect·실계정·장치·사용자 수락을 통과에 합산하지 않는다.
  XML은 tests=3782/errors=0/failures=0/skipped=17이다. skip 17개는 Windows 심볼릭 링크
  생성 권한 8개와 Tool Intent 없는 대화 발화 변형 9개이며 integration 34개는 deselect다.
- 미지원 QA 판정은 confidence 수치만으로 통과시키지 않는다. `no_supported_tool`,
  `relation=new`, `operation=unknown`, `grounded=false`, 도구 없음, 재질문 없음, 비어 있지 않은
  제한 설명이 함께 필요하다. 낮은 confidence라도 이 계약을 충족하면 통과하며 내부 분류
  오류·unresolved·설명 누락을 정답으로 세지 않는다. 과거 .85 기준의 결과는 당시 기록이다.
- 네이버의 자체 **고정 `@playwright/mcp` 0.0.83 stdio** 어댑터와 P 연결/해제를 구현했다.
  수동 연결은 최대 300초, 일반 화면 작업은 90초다. 내부 고정 snippet용
  `browser_run_code_unsafe`를 모델 Registry에 노출하지 않는다. 선택한 캘린더 page nonce·
  origin/path·보이는 계정 표시명을 확인하지만 표시명은 고유 계정 ID가 아니다.
  `visible_view`만 읽으며 일반 조회의 시간대는 `null/not_observed`다.
- 실제 H/J Registry 조회는 실패했다. `.calendar_list_container` 클래스 대신 관찰한
  `#calendar_list_container` ID·visible 조건·5초 대기로 수정한 후 **K에서 실제 ANIS Registry
  조회 실행과 ToolVerifier 검수가 통과**했다. 로컬 보고서는
  `tmp/qa-oct05-naver-persistent-k.json`이다. 같은 세션에서 입력 화면의 **Asia/Seoul**과
  기본 **내 캘린더** 선택 표시를 관찰했다. 일반 조회의 시간대는 계속 `null/not_observed`이며
  표시명/hash는 고유 계정 ID가 아니다. 조회 payload/범위 검수는 별도 서버 API의 독립 검증이나
  기간 전체/반복 일정 수락이 아니다.
  `--interactive` QA는 같은 프로세스에서 읽기 세션을 유지한다.
  일정 초안·등록·apply 도구는 아직 없고, 승인받은 **2026-10-06 17:00–18:00 KST,
  ‘테스트 일정’, 기본 캘린더**도 저장을 시도하지 않았다. 현재 날짜에서 ‘내일’을 다시 계산해
  10월 7일로 바꾸지 않는다. 종료된 QA 프로세스의 연결이 계속 유지된다고 추정하지 않는다.
  최신 연결 probe는 origin 검수에서 차단됐다. 사용자는 캘린더 메인 탭이 없거나 연결이
  실패한다고 답했고 대기 중 QA 소유 세션은 종료했다. 반복 재연결 요청은 하지 않는다.
  `CalendarScreenError`의 `login / calendar_other / other / unknown` 고정 진단 코드와
  자동 회귀를 추가했지만, 새 분류 경로의 실제 화면 성공은 확인하지 않았다. K의 이전
  제한 조회 통과를 현재 연결 상태나 최신 live 수락으로 재사용하지 않는다. Save는 누르지 않았다.
  [네이버 연동 기록](NAVER_INTEGRATION.md)과 [MCP 경계](MCP_INTEGRATION.md)를 따른다.
- 코딩 정답률·K3·Graphify·강화학습·배포 파일·원격 push는 보류한다. 실제 계정·장치·전문가
  품질·장기 실행·사용자 수락 등은 [완료 원장](QA_COMPLETION_LEDGER.md)의 열린 요구로
  남긴다. 전체 작업 완료나 100%를 주장하지 않는다.

이하 날짜별 기록은 해당 revision과 실행 당시의 결과다. 같은 날의 과거 실패나
‘다음 작업/미확인’ 문장을 최신 구현 상태로 해석하지 않으며 위 최신 상태를 우선한다.

## 2026-10-05 전체 추론 기한·실모델 반례 후속 (이전 revision 기록)

- 잠금 대기가 호출별 request_timeout에 포함되지 않고, 늦게 획득한 잠금도 모델 본문에
  진입하던 결함을 공통 추론 경계에서 수정했다. 기한은 ContextVar로 현재 요청에만
  연결하며 Semantic 전체 120초와 중첩 호출/재시도/폴백이 같은 시계를 사용한다.
  정리 중 취소가 들어와도 취소가 우선한다. 만료는 parent turn 자체를 취소하지 않는다.
- 모델 HTTP는 기존 소유 async task/socket 정리를 재사용한다. helper thread로도
  turn/기한을 함께 전파한다. headers/body/trickle, 이벤트 루프 유무, turn 유무를 검사했다.
  DNS·동기 SDK·cleanup은 기한을 초과할 수 있으므로 강제 선점이나 GPU 즉시 해제를
  보장하지 않는다. 늦은 답변·도구 권한과 기한 초과 후 다른 모델 실행을 거부한다.
- 실제 A `Temp/anis-common-qa-rbfzud4u`는 **8/9**였다. unknown+[]/.0 index를 거부한 뒤
  자유 대화로 복구하는 경로가 근거 없는 외부 사실을 만들었다. 낮은 확신도의 빈 index
  abstention만 상세 계약 검토로 보내고, 내부 discovery 오류는 대화 복구하지 않는다.
  긍정/상세 도구 판단의 확신도 제한과 전체 16회 discovery ceiling은 유지한다.
- 수정 후 실제 B `Temp/anis-common-qa-mvr4_5m7`도 **8/9**다. 미지원은 .9/no_supported_tool로
  통과했지만 검색만/재생 금지 요청을 재생 도구로 잘못 분류했다. 실제 도구는 실행하지
  않았다. 현재 Executor의 negation guard는 둘 다 차단하므로 안전한 부분 실행도 못 하는
  의미 해석 한계가 남는다. 특정 서비스 예외 대신 입력/모든 도구 계약에 묶인 typed
  constraint 검수 및 단일 재고려가 다음 작업이다. QA oracle나 확신도를 바꾸지 않았다.
- 전체 허용 도구 실제 Executor 파일 첫 줄 읽기는 **실패 / 22.77초**,
  `Temp/anis-common-qa-83z3zgn4`다. 읽기 전용 격리 진단 DB에서 `context_saturated`를
  확인했다. timeout이 아니며, 이전 revision의 성공을 최신 수락으로 대신하지 않는다.
  합성 파일/1–1줄 외 모든 leaf dispatch를 차단하는 QA guard는 유지한다.
- 집중 C는 **315 passed, 24 deselected (28.50초)**였다. D의 **9 failed, 365 passed**는
  새 직접 호출 fixture가 메시지 envelope를 누락한 것이며 수정했다. 최종 전체 지정
  회귀는 **3636 passed, 17 skipped, 34 deselected (243.98초)**,
  `tmp/qa-oct05-deadline-final-a.xml`이다. skip 17개의 기존 사유와 integration 제외는
  이전과 같으며 실모델/계정/장치/사용자 수락에 합산하지 않는다.
- 이전 격리 GUI는 자동 종료됐다. 이번 GUI 입력/장문 표시 수락은 없고, 공식 확장의
  설치·일반 Chrome 세션의 ANIS 캘린더 어댑터 연결도 확인되지 않았다. 전 영역 완료는 아니다.

## 2026-10-05 입력 예산·탐색 권한·실제 읽기 범위 (이전 revision 기록)

- 후속 배치별 후보는 전체 후보를 보존한 한 번의 계약 재선택으로 중복 대안을 제거한다.
  각 배치와 최종 선택의 16개 한도, 전체 16회 탐색 한도, 취소·입력 예산은 유지한다.
  복합 요청의 필수 조회/전송 단계를 잘라내거나 후보 앞 16개만 고르는 방식이 아니다.
  관련 fixture까지 포함한 11파일 집중 검사는 **326 passed, 25 deselected (42.20초)**다.
- 숫자 loopback 설정 후에도 게임 실행 중 실제 모델 검사는 실패했다. cold load는
  **120.03초 / 완료 호출 0개**, 다음 warm 검사도 **128.05초 / 완료 호출 1개**에서 timeout.
  별도 최소 readiness는 **36.92초**(모델 load 33.874초)에 완료했다. timeout 표본은 지우지 않는다.
  사용자가 게임을 종료한 뒤 최신 9-case matrix는 **8/9**, `Temp/anis-common-qa-mad9vvpj`다.
  인사는 결정적 경로로 **0.02초 / 모델 호출 0개**이며 나머지 사례와 구분한다.
  감정 13.14초, 설명 9.02초, 취소 후 인사 7.06초, 파일 분류 13.73초,
  인용 설명 8.03초, 메일 제한 조회 3.78초, 검색/재생 구분 3.56초였다.
  이는 해석 검사이며 메일·유튜브·카톡 도구를 실행한 증거가 아니다. 게임 종료·설정·캐시/
  load 상태가 함께 달라졌으므로 게임만이 원인이라고 단정하지 않는다.
- 미지원 우주선 요청은 전체 계약 검토 뒤 지원 불가라고 판단했지만 확신도 **.7**로
  엄격한 **.85** oracle를 못 넘었다. 점수를 올리려고 기준이나 모델 확신도를 바꾸지 않았다.
  별도 코드 결함은 미지원 판정을 설명 부족으로 표시하는 것이었다. `no_supported_tool`은
  새 작업의 지원 범위 안내이며 재질문/이전 작업 이어가기 권한이 없다. 복구 출력 스키마도
  이 상태에서는 `relation=new / needs_clarification=false`만 허용한다. 낮은 확신도와
  비실행 상태는 보존한다. 집중 **127 passed, 8 deselected (15.04초)**다.
- 전체 등록 목록을 명시한 실제 Executor/로컬 모델/파일 도구 읽기는 **21.74초 통과**,
  `Temp/anis-common-qa-2j3kiw3o`다. 정확한 합성 파일의 1–1줄, CRLF, 원본 hash,
  성공 receipt/file_content 증거, 표시 범위 및 원본 보존을 함께 검사했다.
  이는 이전 shortlist 1.81초와 다른 검사이며 실제 GUI 입력 수락을 대신하지 않는다.
- 전체 회귀 C는 **3590 passed, 17 skipped, 34 deselected (251.34초)**였다.
  그 뒤 미지원 재질문 경계를 수정한 D는 **2 failed, 3590 passed, 17 skipped,
  34 deselected (235.95초)**였다. 두 실패는 과거 테스트가 미지원에도 재질문을 요구한
  기대값이며, 새 계약과 비실행 검증으로 수정했다. 최종 E는 **3592 passed, 17 skipped,
  34 deselected (233.41초)**, `tmp/qa-oct05-final-e.xml`이다. skip/deselect는 통과가 아니다.
  소스·테스트 95개를 `da2a85fb45590fad95338d6b31d2b69c6155e7bb`로 로컬 커밋했다.
  마지막 테스트 파일의 EOF 빈 줄만 제거했으며 기능 코드는 검사 후 변경하지 않았다.
  skip 17개는 Windows 심볼릭 링크 생성 권한 부족 8개와 대화에 Tool Intent가 없는
  분류 변형 9개다. 실제 계정/장치 수락과 34개 integration 제외는 이 skip과 별도로 남는다.
- 실제 격리 GUI를 다시 실행해 음성 OFF/마이크 미로드/채팅 진입을 관찰했다. 전면 활성화
  전에는 화면 캡처가 다른 창과 불일치해 입력하지 않았고, 이후에도 입력칸 클릭의 초점이
  확인되지 않아 타이핑을 중단했다. 사용자에게 합성 파일 첫 줄 요청의 직접 입력을 부탁했다.
  입력 성공이나 응답 표시를 관찰 전부터 통과로 기록하지 않는다.
  일반 Chrome 캘린더 탭 연결은 공식 Playwright 확장 경로로 사용자 승인을 받았다.
  설치 페이지 조작이 차단돼 직접 설치를 요청했으며, 실제 설치/ANIS 어댑터 연결은 미확인이다.
  개인 DB/.env/brain/tmp/QA 산출물은 커밋하지 않았고 배포·push·실계정 쓰기는 하지 않았다.

- SemanticRequestInterpreter는 입력/최근 대화 역할을 보존한 실제 messages 전체를
  context8192 및 호출별 출력 공간과 함께 검사한다. UTF-8 bytes는 별도 tokenizer 없는
  보수적 상한이며 긴 요청/이력이 들어갈 수 없으면 명시적 context_saturated로 중단한다.
  202개 도구/36개 Plugin의 인덱스는 한 번의 예산 내 포함하며, 선택된 그룹의 상세 계약을
  동일 탐색 경로로 확인한다. Registry 소유권을 사용하고 발화 키워드로 목록을 누락하지 않는다.
- 발견한 별도 결함: 그룹 이름/짧은 설명만 본 `.95 conversation + []`가 상세 조회 없이
  grounded 대화로 확정됐다. 파일 후보는 실제 본문이 아니라는 공통 정보 출처 지침과
  `conversation/unsupported → 빈 목록`, `action → 최소 하나`인 oneOf 출력 계약만으로는
  충분하지 않았다. coarse index의 조기 음성 결론을 제거하고 상세 계약 경로를 재사용한다.
- 이전 실제 6-case matrix 두 번은 모두 **5/6**이다. 첫 번째는 감정 대화와 nonempty
  groups의 모순, 두 번째는 파일 첫 줄 요청의 high-confidence conversation 오분류였다.
  두 번째 작업 폴더는 `Temp/anis-common-qa-xa_709xh`. 개념 설명 **38.97초**,
  첫 번째 파일 분류 **40.09초**로 전체 상세 탐색의 지연 ceiling이 드러났다. 실패와 지연은
  JSON 구문 통과나 모델 자신감으로 완료 처리하지 않는다.
- 실제 생산 shortlist의 Executor/로컬 모델/파일 플러그인 읽기는 **1.81초 통과**,
  `Temp/anis-common-qa-nys9y35a`다. 당시 catalogue 라벨이 전체 목록이라고 잘못 적혔지만
  Executor가 shortlist로 좁혔으므로 전체 202개 검증 증거로 사용하지 않는다. 최신 script는
  전체 `allowed_tool_names`를 명시해 차이를 제거했다. GUI 입력 시험의 대체 증거는 아니다.
- 읽기 oracle는 경로·1–1 포함 범위·원본 전체 SHA-256·정확한 본문·성공 receipt·일치하는
  file_content 증거·원본 보존·최종 표시 범위를 함께 검사한다. 실제 leaf dispatch guard는
  재계획도 포함하며 다른 파일/범위/도구를 실행 전에 차단한다. 미지원 oracle도 schema 오류나
  unresolved를 정답으로 세지 않는다. 집중 **56 passed (6.12초)**,
  `tmp/qa-oct05-read-oracle-d`; 다른 실행과 합산하지 않는다.
- 후속 실제 matrix는 인사/감정/설명/취소 후 인사/파일 부분 조회/인용 명령/메일 제한 조회/
  검색과 재생 구분/미지원 작업 9개 중 **7/9**, `Temp/anis-common-qa-rsmgiggn`이다.
  메일 5개/본문 제외와 검색/재생 금지는 정확하게 해석했지만 실제 서비스는 실행하지 않았다.
  파일 상세에서 올바른 filesystem_read_file을 찾은 뒤 legacy 배치의 불필요한 선택까지
  합쳐져 16개 한도를 초과했다. 미지원 우주선은 지원 불가라고 답했지만 확신도 .8로
  보수적 .85 oracle를 못 넘었다. 점수를 올리려고 oracle를 낮추지 않는다.
- 전체 지정 회귀 첫 시도는 **5 failed, 633 passed, 34 deselected (79.16초)**에서 중단됐다.
  파일 생성 fixture가 새 discovery 호출을 고려하지 않았고, 긴 이력의 pre-send 예산 거부가
  기존 literal-origin 오류보다 먼저 실행됐다. fixture 호출을 맞추고 같은 긴 원문 그대로
  전송 전 거부 + 직접 literal 검증을 분리했다. 관련 2파일 **49 passed (20.17초)**다.
  승인·원문·leaf 보호는 약화하지 않았다. 최종 전체 재검사는 별도다.
- Windows loopback 합성 서버에서 localhost/숫자주소를 비교했다. ::1 connect 실패에
  **2.024초**, IPv4 numeric 요청 전체에 **0.00164초**였다. proxy나 SSL 초기화는 이 비용을
  재현하지 못했다. config 기본값과 .env.example, 현재 .env의 정확한 로컬 주소 한 줄만
  `http://127.0.0.1:11434`로 바꿨다. 다른 명시 URL/TLS/transport 취소 계약은 유지한다.
  설정·전송 경계 **24 passed**. 이미 실행 중인 앱은 재시작해야 설정이 반영된다.
- 저장된 MCP 재연결의 실제 성공/실패 수를 집계하고, 부분/전체 실패는 UI 경고로 표시한다.
  실패한 서버를 “연결 완료”로 알리는 오표시를 수정했고 기존 승인/취소/rollback은 유지한다.
  원격 오류 원문은 고정 코드로 대체한다. 집중 **34 passed, 1 deselected (3.44초)**,
  `tmp/qa-oct05-mcp-result-a`. 실제 외부 서버 수락을 대신하지 않는다.

재현은 저장소 루트 PowerShell과 실행 중인 loopback Ollama에서 다음과 같이 한다.
사용자 DB를 복사하지 않고 매번 새 임시 작업공간을 만들며 TTS/마이크/카메라는 OFF다.

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
$env:HF_HUB_OFFLINE = '1'
.\.venv\Scripts\python.exe -X utf8 -B scripts/qa_common_runtime.py --semantic-matrix --model-trace
.\.venv\Scripts\python.exe -X utf8 -B scripts/qa_common_runtime.py --runtime-read --model-trace
```

계정·일정·메시지 쓰기, K3 가중치, 코딩 정답률 개선, 강화학습, 배포와 push는 수행하지 않는다.

## 2026-10-04 통합 회귀와 실제 도구 카탈로그 실패

- 기존 미커밋 UI·보조 모델·continuity·MCP·OAuth·클라우드 본문/요약을 포함한 전체 작업 트리
  검사에서 **3509 passed, 17 skipped, 33 deselected (244.43초)**였다.
  `tmp/qa-oct04-final.xml`, 고유 basetemp `tmp/qa-oct04-final-d` 사용. pytest.ini에 지정된
  파일을 명시해 과거 ACL 제한 임시 폴더를 재귀 수집하지 않았다. 아래 입력 예산 후속 수정 전
  결과이므로 최종 revision의 전체 회귀로 취급하지 않는다.
- CU/K3/Plugin Hub/테마 집중 **103 passed, 3 deselected (27.30초)**와 headless Chromium
  합성 페이지 실제 loop/stale DOM 검사 **1 passed (4.05초)**도 실행했다. 중복 통과 수는 합산하지 않는다.
  Chromium의 계획은 대역이고 외부 사이트 계정·native Windows 어댑터의 업무 수락은 아니다.
- 첫 전체 회귀의 두 실패는 CU 전체 값 hash를 갖추지 않은 시험 fixture와 K3 시험 worker의
  PID 파일 읽기/쓰기 경합이었다. fixture 보강 및 원자적 PID 게시로 수정했다. 프로세스 Job
  할당/소유 handle/후손 정리/원래 오류 보존과 다운로드 보관 전용/닫기 경계는 별도로 검사했다.
- 실앱 Qwen에서 파일 첫 줄 요청을 코드 작성으로 오분류했다. 독립 답변 스타일 분류가
  도구 탐색을 선행 차단하던 경계를 제거하고, 검증된 대화에만 답변 스타일을 적용한다.
  집중 관련 **270 passed, 24 deselected (28.09초)**는 모델 정확도 표본이 아닌 대역 계약 검사다.
- 추가 실제 `--semantic-matrix --model-trace`는 **5/6**, 파일 조건 미보존 실패다.
  `read_file(path)`만 골라 `end_line=1`을 표현하지 못했다. 도구 실행을 하지 않은 분류 시험이며
  파일 조회 성공으로 표시하지 않는다. emotion/개념 설명/짧은 후속 인사/미지원 요청도 포함한다.
- 전체 202도구/36KB 설명은 클라이언트가 보내지만, Ollama 0.35.1 context8192가 앞선
  도구 메시지를 통째로 잘랐다. 출력 token 포화 검사만으로는 감지하지 못했다. 두 계약만
  보존한 대비 시험도 넓은 legacy 읽기를 선택해, 입력 보존과 조건 보존은 독립 문제임을 확인했다.
  공통 사전 예산 검사·Plugin 기반 계층 탐색·범위/필터/개수 계약 지침을 수정 중이다.
- GUI 새 검증 창의 입력은 Windows helper의 `foreground window did not report a process id`로
  차단됐다. 사용자의 입력칸 클릭을 요청했고, 다른 native 자동화로 우회하거나 GUI 성공을
  만들지 않았다. 실제 파일/Executor 읽기는 별도의 격리 합성 작업 폴더에서 검사한다.
- 네이버는 로그인된 캘린더를 Codex 브라우저로 읽기 관찰했으나 ANIS 자체 어댑터 수락은 아니다.
  실제 계정 전송/등록·장치/미감 수락·K3 가중치·코딩 정답률 개선·강화학습·배포·push는 하지 않았다.
  최신 클라우드 지원 범위는 [CLOUD_INTEGRATION.md](CLOUD_INTEGRATION.md), 전체 열린 게이트는
  [완료 원장](QA_COMPLETION_LEDGER.md)에 구분한다.

## 2026-09-29 문맥 기반 대화와 안전한 정보 수집

- 증상은 카톡 요청에 고정 분류 검증 실패만 반복되는 것이었다. 관측된 원인은 모델의
  수신자 환각(ungrounded_literal:recipient)이며, 사용자 설명 부족으로 단정하지 않는다.
- SemanticRequestInterpreter는 실행 스키마와 nullable 접수 스키마를 분리하고 실제 대화
  역할/확정 슬롯을 유지한다. 제한 재시도 후 복구는 실행 명세가 아닌 질문/설명만 생성한다.
  누락 필드는 모델이 판단하며 특정 명령에 고정 질문을 매핑하지 않는다.
- Executor.render_outcome → ResponseRealizer가 상태·실제 결과·승인 입력을 받아 표현한다.
  실패/제어/승인과 Qt 알림·워크플로 응답도 이 경계를 사용한다. 생성된 질문은 재작성하지
  않으며 화면 질문과 영속 pending 질문을 일치시킨다. 원문/코드/정확한 승인 본문은 보존한다.
- 새 생성 경로는 loopback Ollama만 허용한다. 모델 불가나 출력 검증 실패에는 명시적인
  시스템 상태를 표시한다. 실행 검증·승인·취소를 우회하거나 작업 성공을 꾸며내지 않는다.
- 최종 작업 트리의 관련 27파일: **752 passed, 24 deselected (70.39초)**.
  test_adaptive_dialogue.py와 test_semantic_clarification.py를 추가했고 semantic/Executor,
  GUI/continuity, 응답 무결성/취소/승인, proactive와 Jev 회귀를 함께 검사했다.
  기존 미커밋 기능을 포함한 결과이므로 HEAD 또는 이번 독립 커밋만의 전체 회귀가 아니다.
- 실제 qwen2.5:7b-instruct, context8192, temperature0.2, output2048 표본:
  **1 passed, 13 deselected (48.80초)**. 실제 Executor/생산 shortlist와 이전 오류 이력에서
  “카톡 보내줘” → “민수” → “내일 6시에 보자”를 수집하고 awaiting_approval에 도달했다.
  승인 답변에 정확한 수신자/본문이 유지됐다. 강제 검증 실패의 로컬 복구도 질문만 반환했다.
  모든 실제 도구 실행을 실패 대역으로 막았으며, 실제 카톡 전송/실앱 사용자 수락은 아니다.
- 실모델 재현(PowerShell, 저장소 루트, 실행 중인 Ollama 필요):

  ```powershell
  $env:PYTHON_DOTENV_DISABLED = '1'
  $env:JARVIS_RUN_LIVE_CLARIFICATION = '1'
  .\.venv\Scripts\python.exe -X utf8 -B -m pytest test_adaptive_dialogue.py -q -s -m integration --tb=short --basetemp="$env:TEMP/jarvis-adaptive-live-check"
  Remove-Item Env:JARVIS_RUN_LIVE_CLARIFICATION
  ```

  basetemp는 실행마다 사용하지 않은 전용 경로를 선택한다(pytest가 기존 내용을 정리함).
  캘린더 대역에 경로의 날짜가 혼입되지 않도록 날짜 없는 경로를 사용한다.
- 제한: 모델 질문 품질의 전역 보장은 아니다. 개발 중 언어 혼입과 승인 사실 누락을
  재현해 출력 검사/제한 재시도로 보강했다. 실행 승인과 필수 정보 수집은 별개다.
  UI 상태·장치 진단·모델 오프라인 시스템 안내는 정적 사실로 남으며, 앱 재실행이 필요하다.
  빌드/배포/계정 전송을 수행하지 않았다. 검증 횟수를 과거 수치와 합산하지 않는다.

### 커밋 범위만의 독립 회귀

Git 인덱스의 21개 변경 파일과 HEAD 기준 나머지 파일을 별도 검증 폴더로 추출했다.
미커밋 Jev/Kimi/continuity·UI 개편, 개인 DB와 임시 산출물은 제외했다.
관련 25파일 **722 passed, 24 deselected (67.71초)**, exit_code=0.
이는 위의 작업 트리 752개 결과와 다른 범위이며 합산하지 않는다. 실제 모델은 재호출하지 않았다.
원래 .venv의 Python으로 검증 폴더를 cwd로 삼아 다음과 같이 실행했다:

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
$env:QT_QPA_PLATFORM = 'offscreen'
$qaFiles = @(
  'test_adaptive_dialogue.py', 'test_semantic_clarification.py', 'test_semantic_request.py',
  'test_semantic_response_mode.py', 'test_semantic_executor_flow.py', 'test_runtime_quality_matrix.py',
  'test_executor_semantic_conversation.py', 'test_dialogue_runtime.py', 'test_conversation_context.py',
  'test_qa_dialogue_boundary.py', 'test_qa_dialogue_adversarial.py', 'test_coding_conversation_context.py',
  'test_request_channel_boundary.py', 'test_gui_integration.py', 'test_response_realizer.py',
  'test_executor_approval_safety.py', 'test_finance_messaging.py', 'test_conversation_repair_gating.py',
  'test_conversation_prose_truncation.py', 'test_response_integrity.py', 'test_turn_execution_context.py',
  'test_turn_envelope.py', 'test_proactive_policy.py', 'test_p11_automation_proactive.py',
  'test_conversation_output_guard.py'
)
# 새 checkout에서는 해당 가상환경 Python 경로로 변경한다.
& C:/Users/ice31/lepo/AI_-secretary_Agent/.venv/Scripts/python.exe -X utf8 -B -m pytest @qaFiles -q --tb=short --basetemp=C:/Users/ice31/Documents/Codex/dialogue-index-pytest-a
```

위 basetemp는 이미 사용한 검증 경로다. 재실행 시 새 전용 경로로 바꾼다.
continuity 전용 notify_user 연결과 Jev 테스트 대역 수정은 해당 미커밋 기능과 함께 남겼다.
독립 커밋에는 기존 기본 분류 경로와 일반 Qt 알림 경계만 포함한다.

## 2026-09-26 현재 작업 트리 회귀·취소·탭 재정렬

- 모델 풀이 정답률 향상은 사용자 결정으로 보류했다. 런타임 종료, 데이터 격리, 도구 권한,
  안전한 실패 표시와 UI 검사는 계속한다. 강화학습/배포/실제 외부 전송은 수행하지 않았다.
- 전체 지정 파일 회귀: **3292 passed, 17 skipped, 28 deselected (237.65초)**, exit_code=0.
  HEAD 0fab2c4에 기존 미커밋 UI·보조 모델·continuity 및 이번 수정이 포함된 작업 트리 기준이다.
  실행: PYTHON_DOTENV_DISABLED=1, QT_QPA_PLATFORM=offscreen, pytest.ini의 python_files를
  명시하고 고유 tmp/qa-remainder-final-* basetemp 사용. XML은
  `tmp/qa-remainder-sep26-final.xml`이다. 17 skip은 Windows symlink 권한 부족 8건과
  Tool Intent 대상이 아닌 대화 9건이며, integration 28건은 선택 제외다.
- 이전 전체 실행은 19 failed / 3252 passed / 17 skipped / 28 deselected였다.
  16건은 대화 대역 signature, 1건은 ungrounded 파일명 오류의 기대 상태, 1건은 탭 UI 기대값,
  1건은 실제 Windows 하위 프로세스의 로그 핸들 누수였다. 실패 기록은 지우지 않았다.
- Kimi 종료는 자신의 자손 프로세스를 먼저 회수한다. 작은 실제 Python 자식/손자 실행으로
  시간 초과, 설정 OFF, 턴/도구 취소, 임시 로그 정리를 확인했다. 대형 모델 추론은 하지 않았다.
- 독립 대화 회귀는 8f2e616에 커밋했다. 사용자 정보가 충분하지만 모델이 이전 파일명을
  선택하면 failed로 표시하고, 불필요한 질문이나 실제 파일 접근은 발생하지 않는지 검사한다.
- 전체 회귀 이후 진단 이벤트 격리를 더한 대화/실행 경계 95 passed(13.79초), 숫자 대신
  widget identity로 바꾼 탭 재정렬·닫기·재열기 포함 Qt/UI 115 passed(35.43초).
  집중 검사는 전체 수치와 합산하지 않는다. 최종 UI 변경 후 전체 회귀는 아직 재실행하지 않았다.
- 알림 회귀는 전경 전체화면 탐지를 격리하고, 전체화면 동안 보류→해제 후 단 한 번 전달을
  별도로 확인했다. 운영 전체화면 억제 기능은 끄지 않았다.
- 캘린더 실화면 접근은 도구 kernel assets 초기화 오류로 미실행이다.
  [네이버 연동 기록](NAVER_INTEGRATION.md)에 사용자 선택과 재개 조건을 남겼다.
  자동 통과는 실제 계정/장치/디자인 품질/사용자 수락이나 프로젝트 완성을 뜻하지 않는다.

## 2026-09-16 P 진단·메일 창 대비와 로컬 연결 지연 점검

- 전체 지정 회귀: **2944 passed, 17 skipped, 12 deselected (261.81초)**, exit_code=0.
  XML은 `tmp/qa-sep16-all-results.xml`(커밋 제외). skip은 Windows symlink 생성 권한8개와
  Tool Intent 변형 검사에 해당하지 않는 대화9개다. 별도 integration12개는 선택에서 제외됐다.
  compileall/pip check/git diff --check 통과. 실제 계정·장치 수락 완료와는 별개다.
- 실제 Executor 답변 검사 `_03h9ymq`는 **실패**했다. qwen2.5-coder:7b-instruct의 초안
  생성(prose/context8192/output4096) 첫 호출이120.281초 후 Timeout, 프로세스 exit_code=1.
  새 검수 단계에 도달하지 않았고 답변도 생성되지 않았다. 검수 예산이 일반 생성timeout을
  줄이지 않았다는 경계가 실측됐다. 모델 로딩/서버 실행 원인은 미확정이며 재시도 성공으로
  덮지 않는다. 120초 원인을 IPv6의250ms 비용으로 설명하지 않는다.
- 고정 합성 초안(재귀 예제3개)을 검수 서비스에 직접 전달한 별도 실제 표본:
  qwen2.5:7b-instruct / keyed schema / context8192 / output1536, 활성turn에서 호출했다.
  해당 프로세스만 IPv4 endpoint를 선택했다(사용자 설정 불변). 검수 호출38.250초에 Timeout,
  전체50.031초, status=unverified/critique_calls=1/repair_calls=0,
  review_budget_exhausted/original_preserved=true/code_executed=false를 확인했다.
  이는 예산 만료 뒤 추가 호출 방지·원문 보존의 실증이며 검수 정확도/응답 성공이 아니다.
  클라이언트 초기화·QA준비를 포함한 전체 경과는45초를 넘을 수 있고 절대 wall-clock 제한이 아니다.
  임시 Python 출력의 한글은 콘솔 인코딩이 깨져 문구 시각 검증으로 사용하지 않았다.
  추후 고정 초안 검사를 UTF-8 출력의 정식 QA harness로 옮기는 것이 안전하다.
- 검수 전용 `AnswerReviewPolicy`를 추가했다. 기본 45초의 공통 수락 예산과 검수 출력1536/
  교정 출력4096토큰 한도를 사용한다. 더 작은 역할별 출력 한도는 키우지 않는다.
  Ollama의 호출별 request_timeout/max_output_tokens override만 사용하고 기존 일반 생성의
  timeout120/역할별 profile·keep_alive/사용자 설정을 바꾸지 않는다.
- 검수→교정→재검수의 각 호출 직전/직후와 최종 판정에서 monotonic 기한을 확인한다.
  예산 소진 후 새 호출/늦은 passed를 허용하지 않고 가용 초안과 고정 오류코드를 보존한다.
  교정 후 재검수 실패 시 이전 실패 이유는 이전 초안 이력으로 구분하며 새 후보의 실패 판정으로
  재사용하지 않는다. turn context 없이 client가 직접 내는 취소도 만료보다 우선한다.
- requests/httpx timeout은 대기/비활동 제한이지 절대 wall-clock 강제 종료가 아니다.
  주입 클라이언트가 timeout 인자를 지원하지 않으면 호출 전후 수락 검사만 받는다.
  스레드 강제 종료/분리 작업/자동 재시도는 추가하지 않았다. 정확도나 모든 지연 해결이 아니다.
- 답변·LLM·취소·전송·대화 보정·trace 집중8파일 **356 passed(28.39초)**.
  가짜 시계로 initial/repair/recheck 이전 만료, 늦은 성공/오류, 취소 경합, 기준/초안 보존,
  per-call 한도/일반 호출 불변을 대조했다. 전체 회귀·실제 모델 검증은 별도 기록한다.
- P 진단 창은 메인 창의 밝은 글자색만 상속하고 Windows의 흰색 기본 배경을 남겼다.
  `ui/dialog_theme.py`의 명시적 배경/글자/입력/선택/비활성/스크롤바 팔레트와 QSS를
  PluginDiagnosticsDialog, MailAccountDialog에 한정 적용했다. 앱 전체/Windows 테마는 바꾸지 않는다.
- 밝은 팔레트의 Windows/Fusion 스타일에서 부모 유무, 메인 QSS 상속, 비활성 선택,
  안내문/비밀번호 입력, 비활성 버튼, 실제 static QMessageBox를 Qt offscreen 렌더링했다.
  텍스트 대비 4.5 이상을 검사하고 실제 한글 PNG 3종을 눈으로 확인했다.
  offscreen Qt의 시스템 폰트 목록이 비어 있어 테스트 프로세스에서만 맑은 고딕/Segoe UI를
  로드했다. 한글 글리프 검사와 모달 테스트 watchdog도 포함한다. 실제 사용자 창 조작 수락은 아니다.
- 테마 단독 25 passed, 테마+메일 UI+연결+장치 UI 집중 67 passed/1 deselected(8.09초).
  이전 샘플 좌표가 수평 viewport 밖에 있던 테스트와 QMessageBox.done 호출의 잘못된
  테스트 전제는 visible rect/No 버튼으로 수정했다. 별도 리뷰의 49 passed와 합산하지 않는다.
- 격리 실제 JarvisApp `5ppncuo2`: offscreen 시작 39.75초, STT not_loaded/model_loaded=false,
  마이크·카메라·TTS OFF, exit_code=0/shutdown_errors=[]. 시작 시간이 이전 11.08초보다
  길었으며 원인을 이번 표본으로 단정하지 않는다. 사용자 DB/계정은 사용하지 않았다.
- 로컬 모델 연결 조사(전일 실행): localhost resolver는 0.37~0.45ms로 빨랐으나 ::1:11434는
  2026~2029ms 뒤 WSAECONNREFUSED 10061, 127.0.0.1 연결은 0.3~14.6ms였다.
  GET /api/version requests: localhost 2031~2060ms / IPv4 1.5~19.7ms;
  새 httpx.AsyncClient 경로: localhost 253~266ms / IPv4 1.5~2.9ms(클라이언트 생성 약9ms 별도).
  IPv6 미수신 뒤 IPv4로 넘어가는 비용을 재현했으며 DNS 자체 지연은 아니다.
  활성 턴에 2초 비용을 그대로 적용하거나 120초 검수 timeout 전체 원인이라고 하지 않는다.
- 단일 IPv4 Ollama 구성이라면 명시적으로 `OLLAMA_BASE_URL=http://127.0.0.1:11434`를
  선택하는 것이 후보 해결책이다. transport 내부에서 localhost를 자동 치환하지 않았다.
  구성된 endpoint/Host/proxy/TLS 의미를 보존하며 실제 .env/사용자 설정은 변경하지 않았다.
  독립 transport 회귀 18 passed(1.08초); 모델 생성/외부 네트워크는 이 연결 측정에 없었다.


## 2026-09-15 네이버 계정 설정 후속

- P → Plugin 상태 및 진단 → 네이버 메일 연결에서 DPAPI 단일 계정 저장/인증 전용 검사/
  취소/로컬 해제를 제공한다. 실제 읽기·발송·초안 발신 계정에 연결하며 환경변수 계정 혼합을 차단한다.
  UI 저장은 인증 성공이 아니며 IMAP/SMTP 로그인 검사도 실제 메일 도착을 증명하지 않는다.
- 해제/손상 정보에서 환경변수로 복귀하지 않으며 변경 전 요청과 늦은 검사 완료를 revision으로
  차단한다. Qt worker 취소/부모 소멸/종료 수명과 비밀번호 미표시를 검증했다.
- `test_mail_accounts.py` 135 passed, `test_mail_account_dialog.py` 24 passed. 실제 플러그인/
  진단 창을 가짜 계정·서버로 연결한 통합 검사가 별도로 포함된다. 실제 계정 접속·메일 전송은 없다.
- pytest.ini에 지정된 전체 파일을 명시하고 새 basetemp에서 회귀 실행:
  **2848 passed, 17 skipped, 11 deselected (163.15초)**.
  결과 XML: `tmp/qa-mail-account-sep15-results.xml` (로컬 QA 산출물, 커밋 제외).
- 실제 화면 조작은 computer-use 초기화와 한 번의 재시도 모두 Windows sandbox deny-read ACL
  오류로 실행하지 못했다. Qt offscreen 테스트를 실제 사용자의 마우스/키보드 수락으로 계산하지 않는다.
- 답변 검수의 120초 지연/형식 안정성 문제는 여전히 미해결이다. 아래 실측 실패 기록을 유지한다.
- 추가 실제 Windows DPAPI 검사: 새 임시 vault에 합성 계정/가짜 비밀번호만 저장→재로딩→
  로컬 해제를 수행해 1 passed (0.60초). 실제 토큰/계정/네트워크는 사용하지 않았다.
  이 검사는 선택형 integration이며 기존 전체 회귀의 통과 개수에 합산하지 않는다.
- 실제 JarvisApp offscreen `uw6g0gru`: 시작 11.08초, STT not_loaded/model_loaded=false,
  종료 exit_code=0, shutdown_errors=[]. 마이크·카메라·TTS는 OFF였다.
- 실제 로컬 대화 재검사 `0_la9sw8`: 인사 7.50초(초안 5.062초 + 보정 2.438초),
  지연 원인을 추정으로 구분해 달라는 후속 2.89초. 도구 없이 답했다. 첫 답변은
  “안녕! 어떻게 도와줄 수 있을까?”였고 두 번째는 “알겠어”와 “같아요/맞아요”를 혼용했다.
  말투와 불필요 호출 여부의 일반 개선은 미완이다. 인사 원초안은 trace에 저장하지 않았다.
- 두 호출의 클라이언트 시간과 서버 total_duration 차이가 약 2초여서 추론 없는 GET /api/version을
  각 3회 측정했다. localhost: 2.042/2.026/2.048초; 127.0.0.1: 0.016/0.017/0.001초.
  로컬 주소 연결 경로의 지연은 재현됐지만 IPv6 재시도/DNS/프록시 중 세부 원인은 미확정이다.
  이 대화 실행은 turn_bound=false/requests 경로이므로 활성 턴의 httpx 경로와 같은 비용이라고
  단정하지 않는다. 120초 검수 timeout 전체 원인으로 확대 해석하지 않는다. 사용자 .env는 변경하지 않았다.

## 2026-09-14 답변 검수·클라우드 보존·네이버 메일 후속

아래 과거 10단계의 90%는 공통 런타임 배치 표시이며 전체 제품 진행률이 아니다.
현재 전체 범위는 [완료 원장](QA_COMPLETION_LEDGER.md)의 43개 활성 요구로 분리했다.
코드/모델/실앱/사용자 수락 증거를 혼합해서 100%로 환산하지 않는다.

### 이번 구현

- `AnswerVerificationService`는 도구 없는 설명 답변에 요청 개수, 항목별 코드, Python 정적 구문,
  사용자 원문 보존, 모델 기반 기준별 근거 검수를 적용한다. 코드를 실행하지 않는다.
  검수는 최대 2회, 답변 교정은 최대 1회이며 거절된 교정이 원문 초안을 삭제하지 않는다.
- 검수된 답변은 Executor/대기 기록/품질 지표에 passed/partial/unverified를 구분해 전달한다.
  도구 실행 근거 없는 “확인 중/조사 중” 활동 주장은 별도 실패 지표로 전달한다. 유한한 표현
  규칙이며 모든 자연어의 진실성을 증명하는 장치는 아니다.
- 일반 짧은 인사에서 같은 인사를 답했다고 불필요한 모델 교정을 호출하던 판정을 줄였고,
  반말 종결 `까요`를 인용/코드 밖에서만 처리했다. 서술형 긴 초안은 외국 글자 하나를 이유로
  뒷부분 전체를 삭제하는 기존 sanitizer 경로를 우회해 본문을 보존하고 검수 결과를 표시한다.
- 목록 안에 중첩된 코드 펜스의 들여쓰기를 Markdown 컨테이너 기준으로 해석한다. CRLF와
  코드 내부 상대 들여쓰기/리터럴은 유지한다. 모든 Markdown 문법을 지원하는 파서는 아니다.
- Drive/OneDrive/Notion 카탈로그는 페이지/시간/취소 한도를 두고 부분/필터/재개 결과는
  기존 목록에 합친다. 오류 없이 완료된 새 전체 조회만 목록을 교체한다. revision 경합 시
  이전 목록을 보존한다. 파일 본문·RAG·지속 delta 토큰 동기화까지 완성한 것은 아니다.
- 자격증명 v2는 정확한 provider/account 튜플의 해시 파일명과 DPAPI payload의 계정 결합을
  확인한다. 구형 이름 충돌 가능 토큰은 자동 해독/추정 이관하지 않고 재연결을 요구한다.
  원본 파일은 보존한다. OAuth 원격 신원/유효 scope 검증과 계정 연결 UI는 별도 잔여다.
- 사용자 우선순위는 네이버·구글·깃허브·카카오톡, 네이버는 메일·캘린더부터다.
  IMAP 받은편지함/선택 본문과 STARTTLS 발송 경로를 구현했다. UIDVALIDITY/UID 확인,
  읽음 상태 보존, MIME/크기 한도, HTML·첨부 제외, 전송 중 불확실 결과의 자동 재전송 차단을
  추가했다. [네이버 연동 범위](NAVER_INTEGRATION.md)에 설정과 캘린더 제약을 기록했다.

### 실제 로컬 모델 관측 — 코드 실행·외부 계정 접속 없음

`scripts/qa_common_runtime.py --answer-review --model-trace`가 격리 Executor에서
도구 허용 목록을 비우고 재귀 예제 3개(코드·설명·종료/빈 입력)를 요청했다. 생성 역할은
`qwen2.5-coder:7b-instruct`, 검수 역할은 `qwen2.5:7b-instruct`, context 8192다.
사용자 작업 파일/계정/메시지를 사용하지 않았으며 생성된 코드는 실행하지 않았다.

| 표본 | 관측 결과 | 후속 |
| --- | --- | --- |
| pc6a1wwq, 30.66초 | 5칸 들여쓴 코드 펜스를 누락으로 오인; 외국 글자가 있는 제목 뒤 본문 잘림 | 목록 펜스 파서와 본문 보존 수정 |
| ac2fuf4a, 49.31초 | 3개 코드 인식, 모델 검수 형식 오류로 unverified | 안전한 프로토콜 오류 코드/정확한 인용 선택지 추가 |
| uly02swe, 39.51초 | 배열 검수 기준 ID 누락/중복, unverified | 기준 ID를 필수 객체 키로 표현하는 schema로 변경 |
| 2dysm35i, 139.17초 | 초안 18.797초 + 검수 120.281초 Timeout. 3개 답변 보존, unverified, 도구/작업공간 변경 없음 | **미해결**: keyed schema의 실모델 안정성/지연은 통과하지 못함 |

각 값은 단발 관측이며 평균이나 개선율이 아니다. 마지막 표본은 팩토리얼/피보나치/이진 탐색
예시를 제공했지만 도메인/경계 조건의 일반 정답률은 검증하지 못했다. static 검사와 실제 답변
수락을 구분한다. 오류 경고를 붙였다는 이유로 답변 품질 문제를 해결했다고 보고하지 않는다.
일반 대화 검사 2턴은 9.41/2.78초였으며 인사 교정 호출과 말투 혼합을 관찰한 뒤 교정했다.
교정 이후 실제 말투 재검증은 별도 기록이 필요하다.

### 자동 검사와 실행 환경

- 답변·클라우드·대화/진단 집중 280 passed (11.06초).
- 계정 저장소 가짜 DPAPI 33 passed; 원격/cloud 포함 134 passed. 실제 토큰을 열람하지 않았다.
- 메일 읽기 가짜 IMAP+실제 MIME 파서 79 passed (1.64초). TLS/발송 상태/답변/계정 보안
  집중 152 passed (13.01초). 겹치는 집중 검사 개수는 합산하지 않는다.
- 첫 기본 전체 수집은 과거 사용자 소유 `tmpdozr9d5t` 접근 거부로 중단됐다. ACL/파일 삭제로
  우회하지 않았다. 이후 `pytest.ini`의 **모든 지정 테스트 파일**을 명시하고 새 basetemp를 사용했다.
- 첫 전체 결과: 2671 passed, 2 failed, 17 skipped, 11 deselected (226.32초).
  실패 2건은 시스템 temp 루트를 전제한 fixture와 실시간 Edge 음성 목록 의존성이었다.
  전자를 명시적 OS-temp fixture로, 후자를 가짜 온라인 목록으로 격리했다. 실제 프로젝트에
  pytest라는 이름이 있다는 이유로 삭제/제외하지 않는 반대 사례도 추가했다.
- `compileall -q core plugins scripts`, `pip check`, `git diff --check` 통과.
- 실제 Windows UI 조작 도구가 sandbox helper ACL 오류로 시작되지 않았다. 문서화된 도구를
  한 번 재시도한 뒤 중단했으며 다른 자동화 방식으로 우회하지 않았다. 이번 실행의 마우스/
  키보드 GUI 수락, 실제 메일 도착·캘린더 계정 수락은 **미검증**이다.

최종 전체 회귀: **2674 passed, 17 skipped, 11 deselected (218.75초)**.
실제 격리 JarvisApp `--seconds 5 --model-trace`(2ka4arfm)는 offscreen 시작 13.75초,
STT not_loaded/마이크·카메라·TTS OFF, 종료 exit_code=0/shutdown_errors=[]였다.
이는 화면 조작 수락이 아니다. 이후 짧은 대화 재검사는 도구 승인 사용량 제한으로 실행되지 않았다.
9월 15일 사용자 재개 시 이 미검증 상태를 유지한다. 강화학습·배포·push는 하지 않는다.

## 이전 공통 런타임 배치 단계

이번 작업의 진행률은 아래 10개 검증 단계 기준이다. 프로젝트 전체 완성률이나
Codex와의 동등성을 의미하지 않는다. 실제 외부 전송과 장치 수락은 별도 증거가 필요하다.

| 단계 | 검증 범위 | 상태 |
|---|---|---|
| 10% | 기존 실패 재현, 실앱 격리 실행 기반 | 실행·재현 완료 |
| 20% | 입력/세션 불변성 및 대기 실행 취소 | 자동 계약 검증 완료 |
| 30% | 코드·인용·메시지 원문 보존 | 자동 계약 검증 완료 |
| 40% | 파일 읽기/쓰기 구분 및 파일명 탐색 | 자동 검사·실제 파일 읽기 완료 |
| 50% | 후속 답변/정정/취소 의미 해석 | 자동 검사·실모델 제한 표본 완료 |
| 60% | 의미 해석→도구 탐색→실행 경로 통합 | 자동 검사·실제 읽기 실행 완료 |
| 70% | 전문가 요구별 실제 검수 및 완료 상태 반영 | 자동 검사·실모델 문서 수락/거절 완료 |
| 80% | 실모델 대화·멀티턴 시나리오 실행 | 실행 완료, 자연스러움·지연 개선 필요 |
| 90% | 전체 회귀와 실제 GUI 재검사 | 자동 회귀 및 실제 장문/스크롤/입력창/접기·복원 확인 |
| 100% | 잔여 결함 재검증·증거·인수인계 | 진행 중 |

단계 완료는 명시된 검사를 실행했다는 뜻이며 모든 표현이나 작업이 성공한다는 뜻이 아니다.
이 과거 배치에서는 검증 단계 진행률을 90%로 기록했다. 현재 제품 전체 완성률은 이 표로
산정하지 않는다. 최신 상태는 문서 상단과 완료 원장을 따른다. 당시 실앱에서 발견한
답변 정확도·지연 결함은 아래 남은 작업의 이력으로 유지한다.

## 검증 실행

- `python scripts/qa_common_runtime.py --gui --seconds 120`: 실제 JarvisApp,
  별도 임시 DB/볼트, 카메라·마이크 자동시작 및 TTS 비활성. 사용자 데이터 복사 없음.
- `python scripts/qa_common_runtime.py --conversation`: 현재 설정된 실제 대화 모델 호출.
- `python scripts/qa_common_runtime.py --runtime-read`: 격리된 합성 파일을 실제 Executor,
  로컬 Ollama, 파일 읽기 Plugin으로 읽고 원문·줄 범위·증거·원본 불변성을 확인한다.
- `python scripts/qa_common_runtime.py --stt-prepare`: 다운로드를 막은 상태에서 설치된 STT
  모델만 필요 시 로딩한다. 마이크 스트림은 열지 않는다.
- `python scripts/qa_common_runtime.py --semantic-matrix`: 실제 전체 도구 목록으로 합성 요청을
  해석하되 외부 도구를 실행하지 않는다.
- `python scripts/qa_common_runtime.py --model-cancel`: 로컬 Ollama에 요청 본문이 전송된 것을
  확인한 뒤 취소하고 새 인사를 요청한다. 외부 계정·메시지 전송은 없다.
- `--model-trace`를 위 실행에 추가하면 호출별 역할/출력 종류/문맥·출력 한도와 로딩·생성 시간을
  진단한다. 이 추가 trace는 요청/응답 본문·URL·헤더·예외 상세를 기록하지 않고 종료 시 복원한다.
  기존 QA/앱 디버그 로그의 본문 출력까지 제거하는 옵션은 아니다.
- 자동 테스트, 실모델 실행, GUI 관찰 결과는 서로 구분해 기록한다.
- 학습 강화·배포 파일 생성·원격 push는 이번 작업 범위가 아니다.

## 변경과 증거

### 입력·실행·취소

- 요청 시점의 턴/세션/작업공간을 고정하고 DAG 및 재사용 Plugin worker까지 전달한다.
- 새 입력이 이전 턴을 대체하면 대기·후속 실행을 취소하고 오래된 UI/음성 응답을 버린다.
- 이미 관측한 실행 결과는 늦은 취소나 검수/표현 오류로 삭제하지 않는다. 검수 예외는
  증거·산출물을 유지한 `unverified`로 남기고 재시도/재계획으로 중복 실행하지 않는다.
- 화면 원문과 TTS용 표현을 분리하고 코드·인용·경로·이모지·공백을 보존한다.

### 의미 해석과 도구 실행

- 전체 등록 도구를 대상으로 의미 기반 탐색을 수행한다. 큰 카탈로그에서는 전체 도구의
  간략 목록 탐색과 선택된 도구의 상세 계약 해석을 두 호출로 분리한다.
- 실제 140개 도구 테스트에서 이전 프롬프트는 약 36,985자였으나 모델이 처리한 입력이
  2,050토큰에 그쳤다. 단순 context 확대 후에도 첫 줄 요청이 두 줄로 해석됐다.
- 두 단계 해석에서는 8,192 context 안에서 각각 4,611/803 입력 토큰을 처리하고
  파일명과 첫 줄 범위가 맞았다. 총 38.672초이며 지연 문제는 남아 있다.
- 의미 해석 스키마/타입/출처/읽기와 변경 권한을 검증하며 알 수 없는 실행은 질문으로 남긴다.
- 인용 본문 일부만 반환하거나 본문을 생략한 schema-valid 응답도 공통 원문 보존 검사에서
  차단한다. 복합 요청에서 선택한 필수 도구를 Planner의 단계 누락 검사에 전달한다.
- 해석 실패를 모두 연결 오류라고 하지 않고 연결/시간 초과/길이 한도/명세 오류로 구분한다.

### 실제 로컬 모델 검사 (외부 메시지 전송 없음)

| 검사 | 결과 | 증거의 한계 |
|---|---|---|
| 소규모 카탈로그 의미 해석 5건 | 5/5 통과, 4.686~13.880초 | 실제 도구 실행 아님 |
| 140개 실제 등록 도구에서 파일·첫 줄 선택 | 통과, 38.672초 | 해석 검사, 도구 실행 차단 |
| 문서의 요구 시각 일치/불일치 검수 | 일치 수락·불일치 거절, 2회 모델 호출 | 합성 문서 각 1건, 전체 전문가 품질 아님 |
| 실제 Executor + 모델 + 파일 읽기 | completed, 10.77초 | 읽기 1건, 다른 기능 E2E 아님 |
| 실제 ConversationService 2턴 | 응답 7.78/6.34초 | 반복적·어색한 표현이 남음 |
| 실제 JarvisApp GUI | 시작 51.03초, 정상 종료, shutdown_errors=[] | 창 활성화 실패로 채팅 입력/응답 수락 미검증 |
| STT 지연 로딩 후 실제 JarvisApp offscreen | 시작 34.58초, STT not_loaded, 정상 종료 | 초기화·종료 검사이며 화면 조작 수락 아님 |
| 설치된 large-v3 실제 CUDA 지연 로딩 | not_loaded→ready, 16.38초, 마이크 OFF 유지 | 모델 준비 검사이며 실제 발화 전사 아님 |

실제 읽기 응답은 `x = [1, 2] 🙂`와 CRLF를 그대로 포함했고 읽기 전후 파일 바이트가 같았다.
대화의 두 번째 답변은 추측이라고 명시했지만 원인을 확인한 것은 아니며, 반복적인 말투도 남았다.
위 표의 실행 시간은 각 1회 관측값으로 성능 벤치마크나 대표 평균이 아니다.
51.03초와 34.58초는 플랫폼/캐시 조건이 동일하지 않아 직접적인 개선율로 비교하지 않는다.

### 음성 초기화 지연

- HardwareManager 생성 시 GPU 탐색과 대형 STT 모델 로딩을 하지 않는다. 실제 음성 사용 때
  한 번 로드하며 동시 호출은 로딩 잠금으로 직렬화한다. 실패는 저장하고 명시적 재시도만 허용한다.
- 마이크 준비는 GUI 시작을 막지 않는 worker에서 수행하며 상태 표시를 Qt signal로 전달한다.
- 준비 중 마이크 OFF/앱 종료 시 늦게 입력 스트림을 여는 경로를 막고 종료 상태에서는 재시작하지 않는다.
- 집중 자동 회귀 **75 passed (16.64s)**: 기존 하드웨어/GUI/TTS/턴 계약과 새로운
  초기화·동시 호출·오류·취소·종료 테스트. 실제 마이크 입력 지연·인식률 평가는 포함하지 않는다.

### 전문가 수락·측정

- 문서·코딩·리서치는 실제 산출물을 다시 읽고 요구별 근거 및 내용 해시를 검수한다.
- 실행만 성공하고 요구 충족 검수는 실패한 결과를 완료로 표시하지 않고 partial로 반영한다.
- 같은 수락 판정을 중복 집계하지 않고 세션/작업공간을 넘는 상태 갱신을 막는다.
- 실행 완료율과 실제 요구 수락 성공률을 분리했다. 등록 도구 선택 여부 등의 기존 지표는
  의미적 정답률을 보장하지 않으므로 전반적인 품질 점수로 해석하지 않는다.

## 2026-09-11~13 추가 수정과 실앱 재현

### 공통 해석·출처·완료 판정

- 작업 폴더에서 앱을 실행하면 기본 `config/workflows.json`을 찾지 못해 모든 입력이 실패했다.
  기본 설정은 앱 루트(번들은 `_MEIPASS`) 기준으로 찾고 명시적 경로 재정의는 유지했다.
- 인용 본문 검사를 Router/Planner/의미 해석에서 공통화했다. 멀티라인, 코드 펜스, 공백,
  CRLF, 경로 역슬래시를 보존한다. 명시적 대상 정정과 문자열 검색/교체의 필수 원문을 구분한다.
- 큰 카탈로그의 간략 목록에도 인자 이름을 포함해 legacy 전체 읽기와 줄 범위 읽기 도구를 구분한다.
  근거 있는 고신뢰 일반 대화만 두 번째 해석 호출을 생략하며, 도구가 없다는 이유만으로 대화로
  단정하지 않는다. 지원 불가/불명확한 행동은 실행 성공으로 바꾸지 않는다.
- 표현 보정이 코드·인용·외국어 번역 원문을 손상하거나 제공자 오류로 정상 초안을 버리지 않도록
  했다. 취소 예외는 보정 실패로 숨기지 않는다.
- 의미 해석기가 행동 요청을 일반 대화로 잘못 분류하면 “실행했다”는 허위 답변이 완료 지표에
  들어가는 경로를 재현했다. 도구 증거 없는 외부 완료 주장을 대화 서비스와 Executor 경계에서
  차단하고, 응답별 메타데이터로 실패 상태와 허위 완료 지표를 전달한다.
  이는 유한한 언어 패턴을 사용하는 방어선이며 모든 표현의 진실성을 판정하는 검수기는 아니다.

### 실행 중 모델 HTTP 취소

- 활성 턴의 Ollama 호출은 소유권이 있는 httpx 비동기 요청으로 실행한다. 취소 시 헤더 대기와
  본문 읽기를 끊고 요청·클라이언트·소켓 정리를 기다린다. 버려진 HTTP worker를 남기지 않는다.
  기존 requests 응답/오류 계약과 HTTP 400 도구 미지원 fallback은 보존했다.
- 다른 event loop 안에서 호출하는 경우에도 단일 소유 helper를 join한다. Hybrid 경로에서 취소를
  모델 실패로 감싸거나 다른 제공자로 재시도하지 않는다. httpx 의존성을 명시했다.
- 루프백 실제 HTTP 서버 기반 취소/타임아웃/오류/기존 event loop 검사와 LLM 계약 검사 통과.
- 실제 로컬 모델: 본문 전송 확인, 취소 전 응답 미완료, 취소 **0.016초**, worker 종료,
  `ToolCancelledError` 확인. 이후 새 인사 **5.12초**. 각 1회 관측값이며 벤치마크가 아니다.
- 한계: OS DNS 조회가 막히면 resolver 종료 대기 때문에 취소가 지연될 수 있다. 숫자 IP는 그
  DNS 단계를 피한다. Anthropic 동기 SDK는 호출 전후 취소 확인만 하며 전송 중 즉시 중단은 아직
  보장하지 않는다. 연결 종료는 서버의 즉시 GPU 반환이나 이미 실행된 작업의 롤백을 뜻하지 않는다.

### 실제 GUI와 모델 품질

- 격리된 실제 JarvisApp에서 첫 줄 파일 읽기 원문/CRLF, 새 입력 후 이전 답변 미표시, TTS OFF,
  작업 폴더를 두 번째 빈 폴더로 전환하고 0개 파일을 인덱싱한 상태를 확인했다.
  초기 창 활성화 실패는 사용자 협조 후 재검사했다. 다른 사용자 앱·DB·볼트는 조작하지 않았다.
- 새 입력 검사 중 하나는 이전 답변이 이미 끝난 뒤 제출된 것으로 로그에서 확인됐다. 이 표본은
  실행 중 취소 증거로 합산하지 않고 위 별도 HTTP 취소 검사를 구분한다.
- 추가 140개 카탈로그 표본 6건: 5건은 의도/도구 선택 일치, 물리적으로 불가능한 요청 1건은
  고신뢰 지원 불가 판정을 만들지 못했지만 불명확 상태로 실행하지 않았다. 6/6 정답으로 표기하지 않는다.
- 실앱에서 긴 답변이 창 최소 높이를 늘려 입력창을 화면 밖으로 밀어내는 문제를 재현했다.
  대화 내용만 스크롤 영역으로 만들고 입력창을 밖에 유지했다. 긴 질문 뒤에는 새 답변의 시작을
  보여주며 사용자의 수동 스크롤/새 질문은 자동 이동을 중단한다. Qt 자식 배경 자동 채우기로 생긴
  흰 사각형도 제거했다. 장문·코드·공백 없는 긴 문자열·접기/복원·스크롤 계약을 자동 검사한다.
- 실제 장문 모델 응답에는 요청 개수 미준수(100개 요구에 4개)와 문자열 역순 예제의 잘못된
  출력 주석, 말투 혼합이 있었다. UI나 취소 개선을 근거로 설명/코딩 품질도 해결됐다고 하지 않는다.
- 9월 11일 실제 재귀 예제 10개 설명 요청에서는 `chat()`이 엄격한 구조화 출력 경로를 공유하여
  `truncated_output` 발생 시 작성된 본문 전체가 사라지고 일반 호출 실패만 표시되는 별도 결함을
  재현했다. 명시적인 `chat_prose()`와 응답별 잘림 메타데이터를 도입했다. 일반 대화만 생성된
  본문을 보존하고 미완료 안내를 앞에 붙이며, JSON/코드/도구용 기존 API의 잘림 거절은 유지한다.
  자동 이어쓰기·무제한 재시도는 하지 않는다. 보정/문체 처리 후에도 안내와 메타데이터를 보존하고
  Executor/대기 작업 DB/완료 지표에는 `partial`로 반영한다. 허위 실행 주장 차단의 `failed`가
  우선한다. 이 수정은 요구한 모든 내용을 완성하는 기능이나 코드 정확도 검증을 대신하지 않는다.
- 9월 13일 동일한 재귀 예제 10개 요청을 격리된 실제 창에서 다시 제출했다. 실제 응답 10개가
  표시됐고 800×700 창/입력창을 유지한 채 답변 내부 스크롤, 채팅 접기·복원 및 수동 스크롤 위치
  보존을 확인했다. 창 시작 32.44초, 정상 종료 exit_code=0, shutdown_errors=[]였다.
  이 표본은 완전한 응답이어서 부분 응답 안내의 실앱 검증으로 계산하지 않는다(부분 계약은 자동 검증).
- 해당 답변의 정적 검토에서 print_stairs의 출력 순서 설명 오류, count_char의 종료값 오류
  (일치 수에 문자열 길이가 더해짐), 입력보다 긴 패턴에서 count_pattern 종료 조건 누락을 발견했다.
  예제 개수 준수와 내용 정확도는 다르며 생성 코드는 실행하지 않았다. 말투 혼합도 남아 있다.
- 직전 같은 요청은 ConversationService→chat_prose의 120초 read timeout으로 실패했다.
  당시 전체 회귀가 병행됐지만 그것이 원인이라고 확정하지 않는다. 재검사 중 Ollama는
  qwen2.5:7b-instruct Q4_K_M, context_length=4096, size=size_vram=4,748,056,984를 보고했고
  GPU 사용률 99%, 메모리 5,377/8,188MiB였다. 그 순간 CPU offload 증거는 없었다.
  한 번 성공한 것으로 간헐적 시간 초과를 해결했다고 하지 않는다. stream=False이므로 응답 수신
  전 timeout에는 복구할 부분 본문이 없으며 길이 한도에 의한 잘림과 구분한다.
- 추가한 선택형 모델 trace를 실제 로컬 ConversationService 2턴에서 확인했다. 인사에는 prose
  2.812초+보정 2.579초(총 5.39초), 후속 답변에는 prose 3.203초(총 3.20초)가 관측됐다.
  로딩은 각 0.009초 이하, 생성 토큰 수는 14/14/42였다. 단발 headless 대화 검사이며 GUI 지연
  벤치마크가 아니다. 후속 답변이 근거 없이 "이유를 확인 중"이라고 한 결함도 남긴다.

## 자동 회귀

- 이전 커밋 전체 회귀: **2,133 passed, 17 skipped, 11 deselected (181.70s)**.
- 추가 취소/해석/완료 경계 및 기본 스크롤 수정 뒤 전체 회귀:
  **2,319 passed, 17 skipped, 11 deselected (166.95s)**.
  이후 답변 위치 추적과 장문 잘림 처리 수정 뒤 **2,355 passed, 17 skipped,
  11 deselected (235.44s)**.
- QA trace 포함 최종 전체 회귀: **2,368 passed, 17 skipped, 11 deselected (167.68s)**.
  trace 전용 13개 검사, compileall, pip check, git diff --cached --check도 통과했다.
- 장문 부분 응답 API/제공자/취소/본문 무결성 관련 집중 검사 **408 passed, 6 deselected (25.25s)**.
  Executor 부분 상태·대기 작업 저장과 UI를 합친 추가 집중 검사 **78 passed (16.06s)**.
- 중간 2,117개 통과 뒤 교차 검토에서 원문 누락/복합 필수 단계 누락 3건을 추가 재현하고 수정했다.
- 이후 실행 경계 집중 **103 passed, 6 deselected (36.29s)**, 음성/GUI/TTS/턴 집중
  **75 passed (16.64s)**, compileall/py_compile, pip check, git diff --cached --check 통과.
- skip/deselected에는 선택 실행 모델·장치·외부 환경 검사가 포함된다. 자동 통과 수에
  미실행 기능의 수락을 합산하지 않는다.

## 커밋

- 9월 15일 네이버 메일 계정 UI·실행 연결·검사·기록 15개 파일:
  `0bf646448ecfd7bfc185ebf6ca9115961b1859da` — `[Feat] 네이버 메일 암호화 계정 UI와 안전한 실행 연결`.
- 답변 수락·클라우드 보존·메일 보안 26개 파일:
  `6df5cdb81640a6e2802ffcbd144c4f78cf377374` — `[Fix] 답변 수락 검증과 클라우드 보존 및 메일 연동 안전성 강화`.
- 9월 13일 후속 안정화 코드·테스트·QA 기록 27개 파일:
  `00928103daaa4c137048b6b132a881c91ceb427f` — `[Fix] 공통 대화 무결성과 모델 취소 및 장문 표시 안정화`.
- 코드·테스트·격리 QA 실행기 41개 파일:
  `de7c10107f732b3b572f7dd762eb002185e0476f` — `[Fix] 공통 요청 해석과 실행 수락 계약 강화`.
- Author/Committer: `gyeotsa <gyeotsa@users.noreply.github.com>`.
- 원격 push와 배포 파일 생성은 하지 않았다.

## 남은 작업 / 완료로 표시하지 않을 항목

1. 실제 QA 창의 기본 대화/파일 읽기/새 입력/작업공간 전환과 장문 스크롤/접기·복원을 확인했다.
   부분 응답 안내는 자동 검증이며 실제 제공자 길이 한도 재현 검사는 별도 표본이 필요하다.
   간헐적 장문 시간 초과는 새 trace로 로딩/생성/전송 시간을 분리해 더 조사한다.
2. STT 불필요 로딩은 제거했다. 실제 마이크 ON/OFF·발화·끼어들기의 장치 수락과 전체 앱
   시작 비용(RAG 등)의 추가 프로파일링은 남아 있다.
3. 긴 문맥·복합 작업·정정·코딩/문서/시안에 대한 더 넓은 실모델 표본과 사람 품질 평가가 필요하다.
4. 의미 해석의 반복 호출·모델 로딩 지연을 줄이되, 미관측 성공·명령 누락을 다시 허용하지 않는다.
5. 카카오톡 실제 도착, 장치, OAuth/외부 계정, 전문 앱 재열기는 각각 별도 수락 증거가 필요하다.
6. 일반 답변의 요구 개수 준수·예제 정확도·완료 상태를 검증하는 평가를 확장한다. 특히 실앱에서
   확인한 코드 설명/종료 조건 오류 및 근거 없는 "확인 중" 상태 주장을 다음 회귀의 실패 표본으로
   삼는다. 유한한 완료 주장 패턴 검사를 모든 언어/표현에 대한 정확한 판정으로 해석하지 않는다.

사용자 DB/볼트·임시 산출물·검증 캐시는 보존하고 코드 커밋에 포함하지 않는다.
