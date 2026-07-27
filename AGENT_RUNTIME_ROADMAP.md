# JARVIS Agent Runtime 개선 로드맵

기준일: 2026-07-27

## 목표

JARVIS의 목표는 기능이 많은 챗봇이 아니라 다음 순환을 신뢰성 있게 수행하는
개인 AI 에이전트입니다.

```text
이해 → 계획 → 권한 → 실행 → 관찰 → 검증 → 복구/재계획 → 기억 → 보고
```

모델이 자연스러운 문장을 만드는 것과 실제 작업이 완료되는 것은 별개입니다.
이 로드맵은 모델 성능보다 실행 구조, 문맥 유지, 검증 가능성, 안전성과 제품 품질을
우선합니다.

## 현재 판정

| 영역 | 현재 수준 | 판정 |
|---|---|---|
| 대화 UI·세션·음성 | 실제 사용 가능, 장치별 편차 존재 | 부분 완료 |
| Plugin Registry | 동적 Tool·Intent 로딩 | 기반 완료 |
| Intent/Slot | 선언형 계약+패턴, 표현 확장에 민감 | 개선 중 |
| 파일·문서 작업 | 주요 형식 생성·읽기와 일부 실검증 | 부분 완료 |
| 웹 조사 | DDGS 검색·출처·후속 문맥 | 1차 완료 |
| Coding | 단일 파일 생성·수정·문법 검증 | 초기 단계 |
| Planner/Executor | 구성 요소 존재, 책임 과대·문자열 계약 잔존 | 재설계 필요 |
| Verify/Recover | 일부 Tool별 검증·복구 | 부분 완료 |
| Workspace/Indexer | 수동 선택·기초 인덱서 | 연결 보강 필요 |
| RAG/Memory | 로컬 벡터 검색·복수 메모리 DB | 정책 보강 필요 |
| Automation/Observer | 알람·스케줄·감시 기반 | 장시간 검증 필요 |
| OAuth/Cloud | 로컬 초안·ICS 중심 | 미완료 |
| 배포 | 대형 오프라인 설치본 존재 | 경량화 예정 |

## P0 — 진실한 실행 시스템

목표: 실제 실행 없이 완료했다고 말할 수 없게 합니다.

진행 기록:

- 2026-07-27: `ToolRunResult`, `ToolRunStatus`, `Evidence`, `Artifact` 공통 타입을
  추가하고 Executor의 Plugin Intent 경로와 자율 Tool 실행 검증 경로에 1차 연결.
  레거시 Tool 반환값은 아직 문자열이므로 순차 이전이 필요합니다.

- [ ] 모든 Tool 결과를 `ToolRunResult` 타입으로 통일
- [ ] `status`, `output`, `error`, `artifacts`, `evidence`, `timing` 필수화
- [ ] 문자열의 `오류:` 포함 여부에 의존하는 성공 판정 제거
- [ ] 검증기가 없는 실행형 Tool은 `unverified` 처리
- [ ] 일반 대화 경로의 실행 완료 주장 차단
- [ ] Action Journal에 Tool 입력·결과·검증 증거·지연 시간 연결
- [ ] UI 표시, TTS, 기술 로그 Presenter 분리
- [ ] 취소·부분 완료·재시도 상태를 사용자에게 정확히 표시

수락 기준:

- 파일·앱·웹·알람·문서 작업이 증거 없이 `succeeded`가 될 수 없습니다.
- 실패한 작업은 최종 응답에서도 실패 또는 부분 완료로 유지됩니다.

## P1 — 대화와 Task State 통합

목표: “그 파일”, “같은 방식”, “이어서”가 작업 상태와 연결됩니다.

- [ ] Conversation, pending request, recent intent, Scratchpad 상태 통합
- [ ] Task별 목표·대상·Slot·계획·Artifact·검증 결과 저장
- [ ] 지시 수정, 대상 교체, 취소, 재개를 상태 전이로 처리
- [ ] 새 요청과 후속 답변 구분 신뢰도 도입
- [ ] 앱 재시작 후 대기 작업 복구 정책
- [ ] Workspace·세션별 Task 격리
- [ ] 오래된 pending task 만료와 UI 관리

수락 기준:

- 대상과 지시를 여러 발화에 나눠 말해도 다시 묻지 않습니다.
- 다른 주제로 전환하면 이전 대기 작업이 새 요청을 가로채지 않습니다.

## P2 — 의미 기반 Capability Router

목표: 키워드가 아니라 구조화된 의도와 Capability 계약으로 Tool을 선택합니다.

- [ ] `domain/action/target/constraints/reference/confidence` 표준 의도 타입
- [ ] Plugin별 입력·출력·부작용·권한·검증 계약
- [ ] 낮은 신뢰도에서만 최소 확인 질문
- [ ] 시간 민감 질문의 웹 검색 자동 라우팅
- [ ] 대화, 조회, 실행, 변경, 외부 전송을 분리
- [ ] Intent 충돌 점수와 설명 가능한 라우팅 로그
- [ ] 실제 사용자 발화 기반 라우팅 회귀 데이터셋

수락 기준:

- 새 표현을 추가할 때 Executor 도메인 분기를 수정하지 않습니다.
- 잘못된 도구 선택이 실행 전에 계약 검사에서 차단됩니다.

## P3 — 범용 Coding Agent

목표: 단일 파일 재작성에서 저장소 단위 개발 에이전트로 발전합니다.

- [ ] 저장소 구조·README·의존성·Git 상태 사전 분석
- [ ] 관련 심볼과 파일 검색
- [ ] 변경 계획과 영향 범위 작성
- [ ] 전체 파일 재생성 대신 최소 patch 적용
- [ ] 인코딩·줄바꿈·사용자 변경 보존
- [ ] 언어별 문법·formatter·lint·type check
- [ ] 관련 테스트 자동 선택·실행
- [ ] 실패 로그 분석 후 수정·재검증
- [ ] 최종 diff 자체 검토
- [ ] 원자적 적용과 실패 롤백
- [ ] 새 기능 테스트 생성
- [ ] 빌드·실행 결과와 Artifact 보고

수락 기준:

- 여러 파일 변경 요청을 계획부터 테스트까지 끝냅니다.
- 테스트 실패 상태에서 “완료”라고 말하지 않습니다.

## P4 — Workspace·Project Intelligence

- [ ] 마지막 Workspace 자동 복원
- [ ] Workspace 목록·별칭·프로젝트별 설정
- [ ] 선택 직후 자동 인덱싱
- [ ] `.gitignore` 준수와 대형 폴더 제외
- [ ] 파일 변경 감지 기반 증분 인덱싱
- [ ] 언어·프레임워크·테스트 명령 감지
- [ ] 프로젝트별 Memory/RAG Namespace
- [ ] 현재 Git 브랜치·dirty 상태 UI
- [ ] 새 프로젝트 템플릿·가상환경·Git 초기화

## P5 — Planner·Executor·Recovery 재설계

- [ ] `executor.py`의 대화·계획·실행·응답 책임 분리
- [ ] 실행 가능한 Plan DAG
- [ ] 단계별 사전 조건·예상 Artifact·검증 방법
- [ ] 독립 단계 병렬 실행
- [ ] 관찰 결과 기반 재계획
- [ ] 오류 서명·시도 전략·재시도 예산 저장
- [ ] 동일 실패 반복 차단
- [ ] 복구 후 같은 검증기 재실행
- [ ] 사람 승인이 필요한 중단점 명시

## P6 — Tool Runtime과 Plugin 완전 단일화

- [ ] `core/tools.py` 레거시 Tool을 Plugin으로 이전
- [ ] 중복 이름·Schema 충돌을 시작 시 차단
- [ ] JSON Schema 입력과 출력 모두 검증
- [ ] Plugin 버전·의존성·지원 OS·취소 가능성 선언
- [ ] 동기/비동기·Timeout·재시도 정책
- [ ] Plugin 상태·인증·진단 UI
- [ ] 연결됨/설치됨/인증됨/검증됨 상태 분리

## P7 — Memory·RAG·Knowledge

- [ ] 단기 대화/Task/프로젝트/선호/사실/사례 메모리 분리
- [ ] 사실·추측·출처·기록 시각 저장
- [ ] 사용자 정정과 모순 해결
- [ ] 민감 정보 자동 기억 금지
- [ ] 문서 유형별 구조 보존 Chunking
- [ ] 삭제·수정 문서 인덱스 동기화
- [ ] Metadata filter와 reranking
- [ ] 답변 주장과 근거 Chunk 연결
- [ ] 웹 정보의 만료·재검증 정책

## P8 — Research·Browser Agent

- [ ] 검색 결과 페이지 실제 방문과 본문 추출
- [ ] 게시·수정 날짜 확인
- [ ] 공식 출처 탐지와 우선순위
- [ ] 다중 출처 교차 검증과 상충 정보 표시
- [ ] 주장별 인용
- [ ] 로그인 세션·쿠키·다운로드
- [ ] PDF·표·동적 페이지 처리
- [ ] Prompt Injection과 웹 지시 격리
- [ ] 검색 Cache와 만료

## P9 — Office·Cloud·Communication

- [ ] 기존 서식·템플릿 보존형 Office 편집
- [ ] 렌더링 기반 시각 QA
- [ ] HWP·Office COM 라이브 어댑터
- [ ] Gmail/Google Calendar OAuth
- [ ] Outlook/Graph OAuth
- [ ] Drive/OneDrive/Notion 동기화
- [ ] 초안·승인·외부 반영 Tool 분리
- [ ] 원격 Message/Event ID 검증
- [ ] Slack/Teams 조회·요약·승인 후 전송

## P10 — OS·Multimodal·Voice

- [ ] API/CLI/COM/UI Automation 우선, 좌표 클릭은 최후 수단
- [ ] 창 Handle·접근성 트리·포커스 검증
- [ ] 화면 선택 영역·OCR·표·차트 이해
- [ ] 다중 이미지·영상 프레임 문맥
- [ ] Wake word 전용 감지와 STT 후보 재평가
- [ ] TTS 에코 제거·사용자 끼어들기·재생 취소
- [ ] GPU 중앙 큐와 VRAM 예산
- [ ] 장치 변경·절전 복귀 자동 복구

## P11 — Automation·Proactive Policy

- [ ] 집중 모드·회의·전체화면·방해 금지
- [ ] 중요도·중복·보류·묶음 알림
- [ ] 알림 근거와 “왜 알려줬는지” 제공
- [ ] 장시간 Scheduler soak test
- [ ] 재부팅·절전 후 작업 복원
- [ ] 선제 제안과 실제 실행 권한 분리

## P12 — Security·Observability·Productization

- [ ] 폴더·앱·도메인·계정별 권한
- [ ] 한 번/세션/항상 허용 정책
- [ ] Credential Manager 기반 비밀 저장
- [ ] 로그 개인정보·토큰 마스킹
- [ ] 구조화 JSON Trace와 회전 로그
- [ ] Task/Tool/검증 ID 연결
- [ ] 모델·GPU·지연·재시도 지표
- [ ] 진단 보고서와 안전 모드
- [ ] 실제 장치·네트워크·OAuth E2E
- [ ] 경량 Bootstrap·모델 선택 다운로드
- [ ] 업데이트·DB migration·rollback

## 모델 정책

로컬 7B 모델은 장문 계획과 미묘한 Tool 선택에 한계가 있습니다. 그러나 모든 요청을
큰 모델로 바꾸는 대신 대화, 라우팅, 코딩, 문서, Vision, 검증 역할을 분리하고
복잡한 계획·검토에만 더 강한 모델이나 선택적 API를 사용하는 것을 원칙으로 합니다.

## 개발 순서

```text
P0 진실한 실행
→ P1 Task State
→ P2 Capability Router
→ P3 Coding Agent
→ P4 Workspace Intelligence
→ P5 Orchestrator
→ P6 Plugin 단일화
→ P7~P12 영역 확장과 제품화
```

각 Phase는 파일이나 클래스의 존재가 아니라 자동 회귀 테스트와 실제 E2E 수락 기준을
통과했을 때만 완료로 표시합니다.
