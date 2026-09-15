# ANIS 전 영역 완료 원장

기준일: 2026-09-15. 이 문서는 **현재 PC에서 소스로 실행하는 ANIS 전체 제품**의 완료 조건을 추적한다. 공통 런타임 한 배치의 진척, 테스트 통과 개수, 모델 설치 상태를 전체 제품 완성도로 환산하지 않는다.

## 1. 범위와 현재 결론

- 범위는 [요구사항 매트릭스](QA_REQUIREMENTS_MATRIX.md)의 고정 ID **R01–R27, U01–U11, O01–O06: 총 44개**다. 번호를 재사용하거나 어려운 항목을 삭제하지 않는다.
- **O04 배포는 사용자 결정으로 보류**한다. 현재 활성 추적 항목은 **43개**다. 설치 파일·새 릴리스 폴더·모델 재배포 묶음·호스팅을 만들지 않는다. R27/O05의 배포본 간 설치·업데이트 실험도 O04 재개 때 수행하고, 현재 소스 실행의 데이터 보존·복구는 계속 검증한다.
- **강화학습은 적용하지 않는다.** U01은 학습 기능의 구현 요구가 아니라 이 정책을 지키는 게이트다. 승인 사례 기억/RAG는 모델 가중치 학습이 아니다. SFT/DPO/LoRA도 별도 사용자 요청 없는 필수 잔여 작업으로 추가하지 않는다.
- 대화·명령 해석·계획/실행·모든 전문가·기억/RAG·시각/음성/제스처·외부 서비스·장기 운영이 모두 범위에 있다. 계정이나 장치가 필요한 실검증과, 계정 없이도 고칠 수 있는 코드 공백을 구분한다.
- 현재 연결된 증거만으로 전 영역의 구현·자동 검증·실앱 검증·사용자 수락을 모두 닫을 수 없다. **전체 완료나 Codex 동등성을 선언할 근거는 없다.** 임의의 전체 진행률도 기록하지 않는다.
- 2026-09-14 사용자 답변: **네이버·구글·깃허브·카카오톡**이 주 사용 서비스다. 외부 연동 구현과 실수락은 이 네 서비스를 우선한다. 다른 서비스의 계획 항목을 삭제하지는 않으며, 계정 선택을 받지 않은 서비스에 임의로 연결하지 않는다.
- 추가 사용자 결정: 네이버는 **메일·캘린더부터** 진행한다. 검색·뉴스 먼저 구현하는 것으로 대체하지 않는다. [지원 범위/수락 계획](NAVER_INTEGRATION.md)을 따른다.

최초 원장 감사는 읽기 전용 대조였고 기준 HEAD는 `8cdb9b3b5099eb0d94896014a1ae2dee1c6ab325`였다. 이후 9월 14일의 답변 검수·클라우드·메일 변경과 실제 모델 QA 결과는 아래 및 QA_COMMON_RUNTIME.md에 별도로 연결한다. 사용자 수락 원장이나 실제 계정/장치 통과를 자동으로 갱신하지 않는다.

## 2. 네 가지 독립 게이트

| 게이트 | 닫을 때 필요한 근거 | 대체할 수 없는 것 |
| --- | --- | --- |
| I — 구현 | 실제 진입점→실행→결과 검증 경로, 명확한 지원 범위, 알려진 계약 결함의 수정과 소스 revision | 이름만 있는 도구/버튼, 설치됨, 계획 문서의 체크 표시 |
| A — 자동 검증 | 해당 revision에서 실행한 케이스별 oracle·명령·결과·환경·실패/skip 목록 | 테스트 파일 존재, 이전 revision의 통과, mock 원격 응답 |
| L — 현재 실앱/실환경 | 현재 앱·모델·장치·계정으로 요청→실행→관측→재열기/재조회한 기록과 실제 산출물 | Qt offscreen/합성 fixture, HTTP 200, 원격 ID 하나, 파일 존재, 서버 접수만 확인 |
| U — 사용자 수락 | 요청 내용·편의·미감·음질 등 해당 기준에 대한 실제 결과와 사용자 평가 기록 | 에이전트 자체 칭찬, verifier의 동일 판정 반복, 다른 기능에 대한 승인 |

아래 표의 **구현 근거**는 코드 경로가 있다는 뜻이며 I 전체 통과가 아니다. **과거 계약**은 연결 문서에 자동 통과 기록이 있지만 현재 revision의 해당 전체 범위는 재수락하지 않았다는 뜻이다. **부분 실증**은 적힌 표본만 관찰됐다는 뜻이다. **미연결**은 충분한 최신 증거가 이 원장에 연결되지 않았다는 뜻이지 과거 실행이 전혀 없었다는 단정이 아니다. **진행/미검증**은 병행 작업의 최종 결과를 기다리는 상태다. **보류**는 실패나 완료가 아니다.

행을 닫으려면 필요한 네 게이트를 모두 충족해야 한다. 게이트를 적용하지 않는 경우에는 그 이유와 사용자 범위 결정을 남긴다. 증거가 삭제·변조·만료됐거나 관련 코드/모델/계정 조건이 바뀌면 영향받는 게이트만 다시 연다. R27과 O01–O06, 시각 영역 R24와 U03–U08은 상위/하위 연결이므로 표본이나 성공 수를 중복 합산하지 않는다.

## 3. 최신 증거와 알려진 반례

9월 15일 네이버 메일 계정 UI/DPAPI 저장/실제 플러그인 연결 뒤 전체 지정 회귀는
**2848 passed, 17 skipped, 11 deselected (163.15초)**다. 계정 서비스 135개, Qt offscreen
24개와 실제 플러그인 통합 검사를 포함한다. 실제 네이버 로그인/도착이나 캘린더·장치 수락은 아니다.
Windows 화면 조작 도구가 초기화 ACL 오류로 실패해 실제 UI 조작은 미실행이다.

9월 14일 통합 작업 트리의 지정 전체 회귀는 **2674 passed, 17 skipped, 11 deselected (218.75초)**다.
기본 디렉터리 재귀 수집 대신 pytest.ini의 전체 파일을 명시해 과거 접근 불가 임시 폴더를 건드리지
않았다. 이전 실패 2건과 fixture 수정은 QA_COMMON_RUNTIME.md에 보존했다. 실계정/장치/사용자 수락
및 모델 검수 timeout은 이 통과 수로 닫지 않는다.

| 증거 | 확인한 범위 | 아직 증명하지 못한 범위 |
| --- | --- | --- |
| [공통 런타임 QA](QA_COMMON_RUNTIME.md), [최신 인수인계](Agent%20인수인계.txt) | 2026-09-13 기록의 전체 자동 회귀 **2,368 passed, 17 skipped, 11 deselected (167.68s)**. 앞선 겹치는 집중 검사는 합산하지 않는다. | 이번 병행 변경 이후 전체 회귀, skip된 실제 계정/장치, 모든 기능의 내용 정확도 |
| [실제 GUI와 모델 품질](QA_COMMON_RUNTIME.md#실제-gui와-모델-품질) | 격리 JarvisApp의 첫 줄 원문/CRLF 읽기, TTS OFF, 빈 작업공간 전환, 장문 내부 스크롤·접기/복원. 재귀 예제 10개 응답 표시, 800×700 창/입력창 유지, 정상 종료 | 동일 답변의 **print_stairs 출력 순서 설명 오류, count_char 종료값 오류, count_pattern의 긴 패턴 종료 조건 누락**. 생성 코드는 실행하지 않았으며, 개수 준수는 코드 정확도가 아니다. 부분 응답 안내의 실앱 재현도 별도 필요 |
| [모델 취소·지연 기록](QA_COMMON_RUNTIME.md) | 실제 Ollama 요청 본문 전송 뒤 취소 0.016초, worker 종료, 이후 인사 5.12초. 각 1회 관측 | Anthropic 전송 중 취소, DNS 지연, 서버 GPU 즉시 반환, 롤백을 보장하지 않음. 동일 장문 요청의 직전 120초 read timeout은 해결되지 않은 표본 |
| [최신 요구사항 재감사](QA_REQUIREMENTS_MATRIX.md#117-2026-09-01-구현-후-재검증), [9월 7일 후속 QA](QA_MASTER_AUDIT.md#2026-09-07-후속-qa-최종-정리) | Photoshop 타입 편집·복사 저장, 일부 Office process 격리, 시안 revision/문구/렌더 결과 검증, 원격 read-back·uncertain 전이, 증거 무결성·만료 방어의 구현/자동 계약 | 실제 설치 전문 앱, 실계정 도착, 실제 사진·장치·미감 수락. 과거 GAP-01/02/05/06/14/16 전체를 계속 미구현으로 나열하지 않되 남은 하위 범위는 유지 |
| [답변 검수 코드](core/answer_verification.py), [회귀](test_answer_verification.py), [연결 검사](test_answer_review_integration.py) | 실제 모델에서 중첩 목록 코드 펜스 오인식과 제목 뒤 본문 잘림을 발견해 수정. 허위 진행 주장과 개수/원문/정적 Python 구문 검사·최대 1회 답변 교정 연결 | 배열 기준 ID 중복을 keyed schema로 수정한 뒤 실제 검수 요청이 120초 timeout. 전체 139.17초, 3개 초안 보존/unverified. 일반 정확도·지연 해결이 아니며 실모델 검수의 안정성 잔여 |
| [클라우드 런타임](core/remote_runtime.py), [동기화 검사](test_cloud_catalog_sync.py) | 제한된 다중 페이지/취소/부분 결과 보존/계정별 revision 충돌 방어. 계정 저장소 v2 식별과 구형 인증정보의 추정 이관 금지, 가짜 DPAPI 검사 33개 통과 | 실계정 미검증. 부분/재개 목록은 merge-only이고 완전 증거가 있는 전체 조회만 교체. 파일 본문/RAG 및 일반 사용자 OAuth UX는 별개 |
| [네이버 메일](NAVER_INTEGRATION.md), [메일 검사](test_mail_read_runtime.py) | 받은편지함/선택 본문, 읽기 전용 IMAP, STARTTLS 발송. 가짜 IMAP+MIME 79개 통과 | 실제 계정 연결 UI, 네이버 접속·수신 수락과 캘린더 조회/작성 미완. SMTP 접수만으로 도착 완료를 기록하지 않음 |

과거 문서의 `packaged_runtime=passed`/`OK` 파일이나 12개 실환경 key의 `not_run` 스냅샷은 [당시 원장 해석](QA_REQUIREMENTS_MATRIX.md#6-현재-실환경-원장의-의미와-부족한-근거)과 함께만 사용한다. 이 문서 작성 중 현재 `AcceptanceRuntime`을 실행하지 않았으므로 과거 스냅샷을 현재 상태로 복제하지 않는다.

## 4. 로드맵 R01–R27

표의 다음 작업은 최소 닫힘 조건이다. 더 세부적인 원 수락 기준과 반례는 [요구사항 매트릭스](QA_REQUIREMENTS_MATRIX.md#3-로드맵-27개-요구사항)에 보존되어 있다.

### 대화·명령·계획

| ID / 범위 | I 구현 | A 자동 | L 실앱 | U 사용자 | 근거와 다음 닫힘 조건 |
| --- | --- | --- | --- | --- | --- |
| R01 작업 상태·취소·재개 | 구현 근거 | 과거 계약 | 부분 실증: 최신 입력·Ollama 취소 | 미연결 | [계획](core/plan_runtime.py), [턴 검사](test_turn_execution_context.py), [QA](QA_COMMON_RUNTIME.md). 실제 승인 직후 취소/앱 재시작/복수 작업 전환에서 상태·산출물·중복 실행을 대조하고 사용자가 중단 이유를 이해하는지 확인. |
| R02 장기 대화·자연스러움 | 구현 근거; 품질 결함 잔존 | 과거 계약; 신규 검수 미검증 | 부분 실증; 말투 혼합·근거 없는 “확인 중” 발견 | 미연결 | [문맥](core/conversation_context.py), [QA](QA_COMMON_RUNTIME.md). 긴 다중 주제 held-out 대화에서 지시·요약 보존, 어조/길이/무근거 상태 주장을 평가. 장문 timeout과 잘림 안내를 다른 원인으로 재현. |
| R03 새 주제·지시어·정정 | 구현 근거 | 과거 계약 | 부분 실증: 작업공간/새 입력 | 미연결 | [검사](test_conversation_context.py), [QA](QA_COMMON_RUNTIME.md). 금융→카카오톡→시안 등 실제 연속 업무에서 인용·부정·대상 정정·동명이인을 교차 평가. 대화가 끊겼다는 이유로 이전 승인·대상을 상속하지 않아야 함. |
| R04 비정형 한국어 이해 | 구현 근거 | 과거 계약 | 부분 실증: 제한된 140도구 카탈로그 표본 | 미연결 | [해석 검사](test_runtime_interpreter.py), [QA](QA_COMMON_RUNTIME.md). 미사용 표현·오타·완곡/금지 지시 corpus를 독립 정답으로 평가. 지원 불가 요청을 불명확으로 막은 사례를 정답으로 부풀리지 않고 오실행률을 별도 기록. |
| R05 도구 탐색·복합 기능 누락 방지 | 구현 근거; 서비스별 공백 | 과거 계약 | 부분 실증: 파일 읽기 1건 | 미연결 | [카탈로그 검사](test_capability_inventory.py), [계획 검사](test_planner_contract.py). 등록뿐 아니라 dependency/인증/GUI 진입 가능성을 대조하고 복합 업무의 필수 도구→실행→결과를 전부 확인. 미지원은 정확히 안내. |
| R06 필수 정보 질문·후속 답 연결 | 구현 근거 | 과거 계약 | 미연결 | 미연결 | [대화 검사](test_dialogue_runtime.py). 일부 답변·정정·취소·새 주제 전환에서 질문 루프/slot 유실을 검사하고 실모델에서 필요한 질문만 하는지 사용자 평가. |
| R07 실행 가능한 계획·DAG | 구현 근거 | 과거 계약 | 미연결 | 미연결 | [계획 검사](test_p5_plan_runtime.py), [계약](test_planner_contract.py). 3단계 이상 실제 복합 업무에서 요구·승인·의존성·산출물·검수 단계를 대조. 순환/동일 자원 충돌/부분 실행 후 계획 수정도 검증. |
| R08 관찰 기반 재계획 | 구현 근거 | 과거 계약 | 미연결 | 미연결 | [복구](core/recovery.py), [계획 검사](test_p5_plan_runtime.py). 시험 환경의 서버 오류/파일 잠김/창 이동에서 필요한 단계만 재계획하며 기존 원격 부작용과 승인 범위를 보존하는지 실증. |

### 실행 진실성·도구·보안

| ID / 범위 | I 구현 | A 자동 | L 실앱 | U 사용자 | 근거와 다음 닫힘 조건 |
| --- | --- | --- | --- | --- | --- |
| R09 타입 상태·UI/답변/TTS 일치 | 구현 근거 | 과거 계약 | 부분 실증: 파일 읽기·취소·TTS OFF | 미연결 | [결과 검사](test_tool_result_runtime.py), [QA](QA_COMMON_RUNTIME.md). 실제 실패/미검증/부분 결과를 UI·작업 DB·답변·음성에서 대조. 장문 잘림의 partial 안내는 실모델 표본 추가. |
| R10 요청 내용·산출물 검증 | 기준별 검수 구현; 범위 잔존 | 과거 계약; 신규 답변 검수 미검증 | 부분 실증: 파일/합성 문서 | 미연결 | [전문가 검수](core/specialist_team.py), [수락 검사](test_specialist_acceptance.py), [QA](QA_COMMON_RUNTIME.md). 모든 전문가별 실제 결과 재열기/재조회와 기준별 oracle 연결. 빈 문서·원본·내용이 틀린 정상 파일·검수 예외를 통과시키지 않고 시각 품질은 별도 수락. |
| R11 timeout·재시도·자원 회수 | 일부 process 격리; 상태 보유 COM 잔존 | 과거 계약 | 부분 실증: Ollama HTTP 취소 | 미연결 | [worker 검사](test_plugin_process_isolation.py), [재감사](QA_REQUIREMENTS_MATRIX.md). 격리 opt-in 도구 범위를 명시하고 실제 COM hang/절전/네트워크 단절에서 안전한 회수·복구 실증. 사용자 앱 일괄 종료 금지, uncertain 전송의 자동 재실행 금지. |
| R12 허위 완료·진행 주장 방지 | 실행 증거 방어; 표현/의미 한계 | 과거 계약; 신규 검수 미검증 | 부분 실증; 무근거 진행 주장 발견 | 미연결 | [대화 방어](test_conversation_output_guard.py), [신규 검수](core/answer_verification.py), [QA](QA_COMMON_RUNTIME.md). 실행하지 않은 전송/저장뿐 아니라 확인 중이라는 상태 주장·잘못된 코드 설명을 실패 표본으로 평가. 유한 패턴 검사를 보편 진실성 판정으로 설명하지 않음. |
| R13 Registry·의존성·준비 상태 | 구현 근거 | 과거 계약 | 미연결 | 미연결 | [Registry 검사](test_p6_plugin_runtime.py), [상태 검사](test_plugin_status_truth.py). 실제 dependency 누락·인증 만료·버전 충돌·실행 중 해제에서 상태/진입점/실행 계약이 일치하는지 확인. |
| R14 권한·비밀·경계 | 구현 근거; 네이티브 격리 범위 제한 | 과거 계약 | 미연결 | 미연결 | [승인 검사](test_executor_approval_safety.py), [권한](core/permission.py). 대상/본문 변경 뒤 승인 무효화·경로 우회·재시작 claim 경합을 검증하고 실제 Windows 보호 경로/자격 증명/로그 마스킹 및 승인 UI 확인. |

### 작업공간·코딩·기억·조사

| ID / 범위 | I 구현 | A 자동 | L 실앱 | U 사용자 | 근거와 다음 닫힘 조건 |
| --- | --- | --- | --- | --- | --- |
| R15 작업공간 복원·전환 | 구현 근거 | 과거 계약 | 부분 실증: 빈 작업공간 전환 | 미연결 | [작업공간 검사](test_p4_workspace_intelligence.py), [QA](QA_COMMON_RUNTIME.md). 앱 재시작 후 복수 폴더/별칭/dirty branch·사라진 경로를 복원하고 원치 않는 폴더창/명령이 발생하지 않는지 확인. |
| R16 프로젝트 증분 인덱싱 | 구현 근거 | 과거 계약 | 부분 실증: 빈 폴더 0건 | 미연결 | [인덱서](core/project_indexer.py), [검사](test_p4_workspace_intelligence.py). 대형 시험 저장소에서 rename/delete burst·수정 경합·비밀 제외·namespace 분리와 시간/메모리/반응성 측정. |
| R17 Coding Agent·자체 수정 | 구현 근거; 생성 의미 정확도 잔존 | 과거 계약; 신규 검수 미검증 | 부분 실증: 답변 예제 3개 결함 확인 | 미연결 | [코딩 검사](test_coding_agent.py), [QA](QA_COMMON_RUNTIME.md). 별도 시험 저장소에서 기능 추가/버그 수정/ANIS UI 수정→테스트→diff→복구 확인. 재귀 예제 반례를 포함하되 함수명 특례가 아닌 일반 정확도 oracle·안전한 검증으로 평가. |
| R18 기억 통합·정정·삭제 | 구현 근거 | 과거 계약 | 미연결 | 미연결 | [기억 검사](test_memory_pipeline.py), [통합 검사](test_memory_consolidation.py). 며칠간 유휴/종료 통합·동시 namespace·민감정보·정정/삭제 전파를 실증하고 기억/잊기 결과를 사용자 확인. |
| R19 근거형 RAG·Obsidian | 로컬 구현; 클라우드 본문 경로 공백 | 과거 계약 | 미연결 | 미연결 | [RAG 검사](test_p7_memory_rag.py), [Obsidian 검사](test_obsidian_integration.py). 실제 허용된 vault의 수정/삭제→재색인→검색→인용 사용과 held-out 관련성 평가. 검색 후보·프롬프트 포함·실제 답변 사용을 구분하며 cloud metadata를 본문 RAG로 계산하지 않음. |
| R20 조사·브라우저·실시간 정보 | 구현 근거; 사이트별 범위 제한 | 과거 계약 | 미연결 | 미연결 | [조사 검사](test_p8_research_agent.py), [브라우저 검사](test_p8_browser_plugin.py). 실제 한국어 자료/기업·제품/YouTube 자막에서 본문·최신성·상충·출처를 대조한 분석을 제공. 검색창/재생 페이지를 여는 것만으로 조사나 실제 재생 완료로 처리하지 않음. |

### 문서·외부 연동·멀티모달·운영

| ID / 범위 | I 구현 | A 자동 | L 실앱 | U 사용자 | 근거와 다음 닫힘 조건 |
| --- | --- | --- | --- | --- | --- |
| R21 Office/HWP·PDF | 생성/편집/렌더 구현; 형식별 수락 잔존 | 과거 계약 | 과거 파일 재열기 기록; 현재 설치 앱 미연결 | 미연결 | [Office 검사](test_p9_office_runtime.py), [9월 QA](QA_MASTER_AUDIT.md). 합성 시험 문서로 실제 Word/Excel/PowerPoint/HWP 편집→저장→앱 재열기·PDF/PNG 전 페이지 검수. 표/수식/차트/폰트/레이아웃 보존과 사용자 가독성 확인. |
| R22 클라우드·메일·일정·메신저 | read-back 구현; OAuth UX/본문 RAG/요약 공백; pagination 진행 | 과거 계약; 동기화 변경 미검증 | 실계정 증거 미연결 | 미연결 | [원격 런타임](core/remote_runtime.py), [원격 검사](test_remote_readback_contract.py), [클라우드 검사](test_cloud_catalog_sync.py). 6절의 서비스별 구현·실계정 게이트를 각각 닫음. ID/HTTP 성공을 전달·내용 일치로 확장하지 않음. |
| R23 Windows·카카오톡 실제 작업 | UIA/포커스/원문 방어 구현 | 과거 모의 계약 | 실제 도착·중복 여부 미연결 | 미연결 | [카카오톡 검사](test_desktop_messaging_uia_hardening.py), [재감사](QA_REQUIREMENTS_MATRIX.md). 이미 승인된 수신자 **형택**의 기존 시험 전송 상태부터 확인. 새 시험이 필요할 때 승인된 내용·정확 창·실제 도착·중복 없음 증거를 연결. 미확인을 무전송으로 추정해 재전송하지 않음. |
| R24 Vision·OCR·영상·참조 | 구현 근거; 모델/입력별 품질 잔존 | 과거 계약 | 실제 현재 모델 품질 미연결 | 미연결 | [Vision](core/vision_runtime.py), [멀티모달 검사](test_p10_runtime.py). 다중 이미지/문서/화면/영상의 참조 역할·시간·중요 텍스트/숫자·관계를 실모델 정답셋으로 평가. 시안 편집은 U03–U06 별도 게이트. |
| R25 STT·호출·마이크 복구 | 구현 근거; 지연 로딩 반영 | 과거 계약 | 부분 실증: large-v3 CUDA 로딩, 발화 아님 | 미연결 | [장치 검사](test_hardware_devices.py), [지연 로딩 검사](test_stt_lazy_initialization.py), [QA](QA_COMMON_RUNTIME.md). 실제 마이크의 호출/미호출·TV/에코·무음·정정·ON/OFF·절전/장치 재연결에서 오탐/지연 기록. |
| R26 TTS·음성 OFF·끼어들기 | 구현 근거 | 과거 계약 | 과거 합성/준비·현재 TTS OFF; 청취 미연결 | 미연결 | [TTS 검사](test_tts_quality_and_settings.py), [턴 검사](test_turn_envelope.py), [QA](QA_COMMON_RUNTIME.md). 실제 선택 음성·스피커/STT 동시 사용에서 발음/소리·중단·재생 오류·서버 자원 회수 확인 및 사용자 청취 평가. |
| R27 운영 총괄 | 하위 운영 구현 근거 | 과거 계약 | 장기 실시간 운영 미연결 | 미연결 | [운영 검사](test_p11_automation_proactive.py), [수락](core/acceptance_runtime.py). 현재는 O01/O02/O03/O05/O06을 닫아야 함. O04 배포는 보류를 명시하고 활성 범위 성공으로 배포까지 완료했다고 하지 않음. |

## 5. 사용자 고유 요구 U01–U11 및 운영 O01–O06

### 모든 전문가·시안·뇌 화면·장치

| ID / 범위 | I 구현 | A 자동 | L 실앱 | U 사용자 | 근거와 다음 닫힘 조건 |
| --- | --- | --- | --- | --- | --- |
| U01 강화학습 제외·기억 통제 | 운영 정책 명시 | 정책 경계 최신 확인 필요 | 학습 미실행 정책 준수 유지 | 제외 결정 기록됨 | [현재 학습 정책](POST_TRAINING.md), [요구사항](QA_REQUIREMENTS_MATRIX.md). 정상 업무에서 모델 가중치 학습이 묵시적으로 시작되지 않아야 함. 과거 데이터 포맷·dataset ready를 학습 실행/완료로 표시하지 않음. |
| U02 모든 전문가의 역할별 팀 | 역할/기준별 검수 구현; workspace별 수락 잔존 | 과거 계약 | 일부 문서 표본; 모든 팀 미연결 | 미연결 | [팀](core/specialist_team.py), [검사](test_specialist_workspaces.py), [QA](QA_COMMON_RUNTIME.md). 제공하는 각 workspace별 준비→계획→실행→요구별 검수 trace와 실제 결과 연결. RTX 4060 Laptop의 순차 로드/해제·peak VRAM/RAM·지연을 측정. |
| U03 현재 시안 revision·연속 수정 | 구현 근거 | 과거 계약 | 실사진·모델 연속 수정 미연결 | 미연결 | [이력 검사](test_mockup_preview_history.py), [검수/저장 검사](test_mockup_review_commit_contract.py). 사용자 26–29 계열과 새 사례에서 현재 화면→수정→undo/redo→분기→저장 diff를 대조하고 요청하지 않은 문구/배치/장식 보존 확인. |
| U04 시안 픽셀·문구·인물·스타일 | 레이어/분할/상태 구현; 생성형 편집 일부 미지원 | 과거 계약 | 모델 추론·설치 폰트·사진 품질 미연결 | 미연결 | [피사체 검사](test_mockup_subject_integrity.py), [문구 검사](test_mockup_text_style_runs.py), [준비 상태 검사](test_mockup_generation_readiness.py). 실제 원형 alpha·크롭/얼굴·안쪽 점선·한글 span/곡선·사진 분할·스타일 검색 평가. SDXL 준비/로드/추론 분리, 미지원 inpainting/ControlNet/FLUX/참조 픽셀 편집을 성공 처리하지 않음. |
| U05 실제 렌더 결과 검수·제한 교정 | 구현 근거 | 과거 계약 | 실제 VLM 오탐/미탐 미연결 | 미연결 | [검수 계약](test_mockup_review_commit_contract.py), [QA](QA_MASTER_AUDIT.md). 실제 PNG/SVG/JSON revision과 요청별 기준을 재검수하고 교정 전후·실패/미실행/예산 소진을 보관. 사용자가 승인한 결과만 품질 승인 기억으로 축적. |
| U06 시안 편집 GUI·스케치·첨부 | 구현 근거 | 과거 offscreen 계약 | Windows 실제 조작 미연결 | 미연결 | [전문가 UI 검사](test_specialist_workspaces.py), [UI](ui/specialist_workspaces.py). Explorer drag-drop/사진 버튼/스케치→Vision/폰트/fit·zoom·pan을 실제 DPI·창 크기에서 확인하고 임시 파일/현재 revision 보존 검증. |
| U07 2.5D 뇌·360도·내부 지식 탐색 | 구현 근거 | 과거 계약 | 실제 vault·큰 그래프 성능 미연결 | 미연결 | [뇌 화면 검사](test_brain_graph_transition.py), [지식 그래프](core/knowledge_graph.py). 실제 허용 vault에서 회전 경계→연속 확대→내부 노드 선택→노트 원본 열기를 확인하고 삭제 노트/대형 그래프/DPI별 frame budget 측정. |
| U08 한 손 이동·두 손 zoom·설정 | 구현 근거 | 과거 합성 landmark 계약 | 실제 손 동작 미연결 | 미연결 | [두 손 검사](test_gesture_runtime_multihand.py), [설정 검사](test_gesture_configuration.py), [제스처 명세](GESTURE_INTERFACE.md). 거리/조명/속도/가림/손 순서 교체·재획득에서 오탐/지연과 민감도·매핑을 실제 장치로 확인. 원치 않는 창/외부 행동을 발생시키지 않아야 함. |
| U09 채팅·카메라·앱 수명주기 UI | 구현 근거 | 과거 계약 | 부분 실증: 장문·채팅 복원·종료 | 미연결 | [UI/장치 검사](test_ui_device_acceptance.py), [QA](QA_COMMON_RUNTIME.md). 실제 카메라 ON/OFF·hotplug·권한 거부·앱 재시작·최소화/복원 상태 확인. **카메라 기본 ON, 명령 제스처 별도 opt-in**은 이미 정한 정책. |
| U10 실제 전문 앱 편집·저장 | Photoshop 타입 편집·복사 저장 등 구현 | 과거 fake COM/fixture 계약 | 현재 설치 앱 재열기 미연결 | 미연결 | [Photoshop 검사](test_photoshop_runtime.py), [전문가 수락](test_specialist_acceptance.py). 각 실제 설치 앱/형식/폰트/색 프로필에서 합성 시험 문서 편집→저장→재열기·원본 보존·변경 내용 확인. 열기만 가능한 기능을 편집 완료로 표시하지 않음. |
| U11 계획 서비스·실제 지원표 | 일부 실행 경로 미구현/범위 미확정 | 카탈로그 과거 계약; 개별 업무 검증 필요 | 서비스별 미연결 | 구체 업무/계정 선택 일부 필요 | [플러그인 로드맵](PLUGIN_ROADMAP.md), [지원표/결정 경계](QA_REQUIREMENTS_MATRIX.md). IDE CLI·GitHub Issue/PR/CI·Docker·연락처/할 일/지도·교통·범용 게시/업로드/결제의 실제 업무를 분리. 안전한 코드·지원 상태는 먼저 정비하고 계정/게시 대상/비용만 필요 시 사용자 확정. 빈 범용 도구로 닫지 않음. |

### 로컬 운영·진단·평가·보존

| ID / 범위 | I 구현 | A 자동 | L 실앱 | U 사용자 | 근거와 다음 닫힘 조건 |
| --- | --- | --- | --- | --- | --- |
| O01 예약·선제 알림·장기 실행 | 구현 근거 | 과거 가속 시계/복원 계약 | wall-clock soak 미연결 | 미연결 | [예약 검사](test_p11_automation_proactive.py), [정책 검사](test_proactive_policy.py). 실제 시간의 장기 실행·절전/재부팅·시계 변경·잡 충돌/부분 완료를 관찰. 중복 행동 없음, 알림 빈도·집중 모드·복구를 사용자 확인. |
| O02 진단·trace·비밀 마스킹 | 구현 근거; 모델 trace 추가 | 과거 계약 | 부분 실증: 로컬 대화 trace 2턴 | 미연결 | [진단 검사](test_agent_command_center.py), [trace 검사](test_qa_model_trace.py), [QA](QA_COMMON_RUNTIME.md). 실제 장애와 UI/답변/디스크 로그의 correlation·not_run·복구 안내를 대조. 새 trace의 본문 미기록이 기존 앱 전체 로그의 비밀 제거를 뜻하지 않음. |
| O03 품질 평가·held-out·성능 | 평가 기반 구현; 일반 답변 검수 진행 | 과거 계약; 신규 변경 미검증 | 제한된 실제 표본·반례 존재 | 사람 corpus/rubric 미연결 | [평가](core/evaluation_runtime.py), [품질 지표](core/quality_metrics.py), [QA](QA_COMMON_RUNTIME.md). 정확도/자연스러움/도구 선택/수락/허위 완료/지연을 분리하고 케이스·표본수·oracle·모델/revision·사람 점수를 연결. 최소 표본수 충족만으로 전 영역 다양성이나 Codex 동등성을 선언하지 않음. |
| O04 설치·배포·모델 재배포 | **보류** | **보류** | **보류** | 나중에 시작하기로 결정됨 | [범위 결정](QA_REQUIREMENTS_MATRIX.md#116-2026-09-01-현재-마일스톤-범위). 현재 활성 게이트에서 제외. 재개 시 실제 자산 manifest/라이선스/hash/설치→모델 실행→재시작→복구/제거를 깨끗한 Windows에서 검증. 과거 smoke를 배포 수락으로 재사용하지 않음. |
| O05 데이터·설정·migration·복구 | 로컬 보존/복구 구현 근거 | 과거 계약 | 현재 소스 실행의 복구 미연결 | 미연결 | [제품화 검사](test_p12_productization.py), [권한 영속성 검사](test_permission_persistence.py). 복제한 시험 DB로 이전 schema/동시 실행/중간 실패→백업 hash→rollback→기억/작업/예약/설정 보존을 검증. 배포본 간 업그레이드는 O04 재개까지 유예하며 사용자 원본 DB로 장애 주입하지 않음. |
| O06 증거 원장·유효성·범위 | hash/재검증/원격 구조·만료 방어 구현 | 과거 계약 | 현재 전 영역 증거 연결 미완 | 필요한 평가 기록 미연결 | [수락 검사](test_acceptance_runtime.py), [재감사](QA_REQUIREMENTS_MATRIX.md). 케이스별 관측·source revision·산출물과 주장의 의미 일치 확인. 원장 밖 실제 보고서를 연결하고 오래된/변조/빈/다른 빌드 증거로 통과시키지 않음. 이 Markdown 작성만으로 runtime 수락을 변경하지 않음. |

## 6. 클라우드·원격 기능의 분리된 닫힘 조건

R22/R19/U11은 하나의 “클라우드 연결 완료”로 닫지 않는다. 아래는 같은 상위 ID의 하위 체크이며 고정 ID 총수를 늘리거나 별도 성공 개수로 중복 집계하지 않는다.

### 주 사용 서비스 실체 감사 (2026-09-14, 계정 미접속)

| 서비스 | 현재 실행 경로 | 다음 구현/검증 경계 |
| --- | --- | --- |
| 네이버 | `core/mail_runtime.py`, `plugins/mail.py`: 읽기 전용 받은편지함/본문 + 승인 SMTP/STARTTLS. `core/mail_accounts.py`, `ui/mail_account_dialog.py`: DPAPI 단일 계정 UI/인증 전용 검사/로컬 해제와 실제 실행 연결 | 메일·캘린더 우선. UI/서비스 오프라인 계약 검증, 실제 로그인·도착은 미검증. 캘린더 공식 API/CalDAV 제약과 다음 작업은 NAVER_INTEGRATION.md 참조 |
| 구글 | `plugins/cloud_communication.py`, `core/remote_runtime.py`: OAuth 백엔드, 승인 Gmail 전송, Calendar 생성/읽기, Drive 메타데이터 | 일반 연결 UI/callback, Gmail 받은편지함/검색, Calendar 다중 페이지, Drive 본문/RAG와 실제 계정 검증 필요 |
| 깃허브 | `plugins/git.py`: 로컬 저장소 Git status/diff/log/commit/push/pull | GitHub 계정 상태·Issues/PR/CI 전용 API/CLI 경로 없음. 기존 Git 인증을 GitHub 연결 검증으로 간주하지 않음 |
| 카카오톡 | `core/desktop_messaging.py`: Windows 대상/입력창 확인, 승인 전송, 새 발신 말풍선 비교 | 형택 실제 도착 수락, 비전송 준비 상태 진단. 대화 기록 검색·첨부·그룹·서버 수신 영수증은 현재 계약과 별개 |

- 계정 저장소 충돌 수정: v2는 provider/account 정확한 튜플의 SHA-256 파일명과 암호화 payload의 계정 결합을 검증한다. 구형 자격증명은 자동 복호화/이관/삭제하지 않으며 재연결이 필요하다. 원격 신원·scope·토큰 만료 검증과 OAuth status 개선은 여전히 남아 있다.
- 클라우드 플러그인이 인증 불필요/로컬 연결이라고 표시하던 기본 probe는 이번 작업에서 `unchecked`로 바로잡았다. 이 표시 수정은 실제 계정 인증이나 위 보안 작업 완료를 뜻하지 않는다.
- Codex에 설치하는 플러그인과 ANIS 자체 `core/plugin.py`가 로드하는 Python 플러그인은 별개다. Codex 플러그인 설치만으로 ANIS 연동이 완성됐다고 기록하지 않는다.

### 공통 클라우드 하위 게이트

| 하위 업무 | 현재 경계 | 구현/자동 검증의 다음 작업 | 실계정/사용자 수락 |
| --- | --- | --- | --- |
| Drive/OneDrive/Notion 메타데이터 | pagination·기존 목록 보존 변경 진행/최종 결과 미연결 | 페이지 경계·cursor 반복/만료·bounded partial·delta/삭제·중단 재개·계정 격리·부분 실패 뒤 기존 항목 보존을 테스트하고 지원 범위를 표시 | 선택 계정에서 여러 페이지·수정/삭제·권한 회수 후 앱 목록과 원격 목록 대조 |
| 클라우드 파일 본문/RAG | metadata 목록과 다른 미완 경로 | 본문 fetch/export·문서 파싱·hash·source URL·namespace·변경/삭제 재색인·접근 철회 반영·답변 인용까지 실제 연결 | 허용된 파일의 원격 변경→검색→답변이 최신 본문을 실제 사용함을 확인. 제목/snippet만으로 본문을 읽었다고 하지 않음 |
| 일반 사용자 OAuth 연결 | PKCE/token/DPAPI 기반 코드와 연결 UX는 별개 | 연결 UI·안전한 callback transaction·state 만료/중복·취소/동의 거부·재시작 복귀·계정/scope 결합 | 사용자가 선택한 provider/account로 로그인·동의·갱신·로그아웃/철회를 완료. 실제 자격증명은 사용자가 직접 입력 |
| Gmail/Outlook/Calendar/Slack/Teams 변경 | 대상/본문/시간/채널 read-back와 uncertain 방어 구현 | 첨부 hash·provider idempotency·crash 뒤 applying/uncertain reconciliation을 실제 지원 범위별 검증. 불명확 상태 재전송 차단 유지 | 승인된 초안과 정확한 계정/수신자/본문/시간을 서버 재조회 및 도착/반영 증거와 대조 |
| Slack/Teams 종합 요약 | snippet 배열/미리보기는 실제 요약이 아님 | 출처 ID를 유지하는 종합 요약·결정/할 일 추출 단계와 누락/오인용 oracle 구현, 또는 UI 지원명을 미리보기로 정확히 제한 | 실제 허용 채널의 대화에 대한 요약 정확도·주요 결정·담당자·근거를 사용자 확인 |
| 추가 계획 서비스 | 전용 실행/검증 경로 없는 범위 존재 | 서비스·업무별 지원/미지원/계정 필요를 명시하고 진입점→권한→실행→검증을 연결 | 게시/결제/새 계정·조직 변경은 사용자 범위 확정 후에만 실검증. 이미 있는 로컬 기능을 대체 증거로 사용하지 않음 |

## 7. 증거 연결 양식과 수락 운영

각 실행 보고서는 아래 필드를 갖는다. 이 문서는 양식만 정의하며 가짜 실행 기록을 만들지 않는다.

| 필드 | 기록 내용 |
| --- | --- |
| 식별/범위 | 요구 ID, 하위 케이스 ID, 검증 게이트 I/A/L/U, 정확한 사용자 요청/수락 기준 |
| 소스/환경 | commit 또는 tree 상태·변경 목록, 실행 시각/시간대, Windows/앱/라이브러리 버전, 모델 이름/양자화/context·장치·계정 별칭(비밀 제외) |
| 입력/행동 | 합성/실제 입력 구분, 승인 범위, 실행 명령/단계, 외부 부작용 유무, 재시도/취소 여부 |
| 관측/oracle | 기대 결과, 실제 결과, 독립 검증 방법, 실패/부분/미실행/불확실 사유, 표본수와 전체 분모 |
| 산출물/영수증 | 로컬 보고서·전후 이미지·저장 파일의 경로/hash/크기, 원격 작업/대상/검증 시각·payload hash·read-back(필요한 개인정보만 최소 기록) |
| 판정 | I/A/L/U별 결과, 사용자 평가자/시각/rubric/피드백, 남은 작업, 관련 변경 후 재검증 조건 |

- 실환경 key `conversation_human_eval`, `specialist_human_eval`, `mockup_visual_eval`, `camera_gesture`, `microphone_stt`, `speaker_tts`, `kakao_delivery`, `mail_delivery`, `oauth_roundtrip`, `office_com`, `wall_clock_soak`는 위 활성 ID와 연결한다. `packaged_runtime`은 O04 보류와 함께 유지한다. key 12개와 요구 ID 44개를 일대일로 오해하지 않는다.
- 사람 수락은 코드 오류를 사용자가 대신 설계하라는 질문이 아니다. 먼저 가능한 구현/오프라인 회귀를 수행하고, 실제 결과의 내용·미감·청취/조작 평가가 필요한 시점에 참여를 요청한다.
- 카카오톡 수신자 형택, 카메라 기본 ON/명령 제스처 opt-in, 강화학습 제외, 배포 보류는 이미 결정됐다. 같은 선택을 다시 묻는 대신 필요한 실행 증거를 확인한다.
- 계정/provider/시험 문서·새 외부 게시 대상·비용은 임의로 고르지 않는다. 장치/상용 앱이 없으면 정확한 unavailable과 안전한 모의 검증은 먼저 수행하고 실환경 게이트를 남긴다.
- [수동 진단 분류](QA_TEST_CLASSIFICATION.md)에 따라 실제 카메라·음성·네트워크 진단을 명시적 실행과 별도 보고서로 취급한다. 자동 회귀의 skip/deselected를 통과로 계산하지 않는다.
- `data/`, `brain/`, 사용자 문서·볼트·모델·QA 산출물·임시 캐시는 보존한다. 증거 연결을 위해 사용자 데이터를 정리/삭제하거나 원본에 장애를 주입하지 않는다.

## 8. 다음 작업 순서

1. **현재 병행 변경의 결과를 먼저 연결**한다. 답변 검수와 cloud pagination의 최종 소스·집중/전체 회귀를 받은 뒤 해당 I/A만 갱신한다. 결과를 받기 전 완료로 표시하지 않는다.
2. **이미 실앱에서 발견한 공통 품질 결함을 재검증**한다. 코드 예제/출력·설명·종료 조건, 요구 개수, 무근거 진행 주장, 말투, 장문 timeout/부분 응답 안내를 분리한 실패 corpus로 평가한다. 모델·전체 회귀를 동시에 과부하시켜 원인이 섞이지 않게 실행 조건을 기록한다.
3. **계정 없이 가능한 구현 공백을 닫는다.** OAuth 연결 UX, cloud 본문/RAG, 진짜 요약, 상태 보유 네이티브 도구의 안전한 timeout/복구, 미지원 생성 편집·계획 서비스의 정직한 지원 계약을 처리한다. 범위 확대나 비용이 필요할 때만 사용자 선택을 구한다.
4. **현재 PC의 실제 기능을 묶음별 수락**한다. 모든 전문가 산출물 재열기, 시안 연속 편집/실모델, 실제 음성/손 동작/장치, 허용된 계정의 전달·변경·동기화, 승인된 카카오톡 시험, 장기 운영을 서로 다른 보고서로 남긴다.
5. **사용자 평가를 연결하고 닫힘을 재감사**한다. 43개 활성 ID에 필요한 네 게이트와 유효 증거가 모두 연결됐는지 확인한다. 미지원/미검증을 숨기지 않으며 O04는 그대로 보류한다. 공통 비교 과제·조건·독립 평가 없이 Codex 수준 동등성이나 전 영역 백분율을 만들지 않는다.

이 원장은 문서 수준의 추적표다. 실제 코드·테스트·계정·장치·사용자 결과가 바뀌지 않았는데 표의 상태만 바꿔 완료를 만들 수 없다.
