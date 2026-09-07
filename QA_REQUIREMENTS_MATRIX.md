# ANIS 전 영역 요구사항·수락 추적 매트릭스

작성 기준: 2026-08-31 독립 요구사항 감사. 이 문서는 구현 목록이나 테스트 개수를 제품 완성도로 환산하지 않는다.

## 1. 범위와 판정 원칙

사용자가 반복해서 지정한 범위는 **강화학습을 제외한 전체 제품**이다. 자연스러운 대화, 명령 이해, 작업 수행, 모든 전문가 작업공간, 역할별 팀, 메모리/RAG, 시각·음성·제스처, 외부 서비스와 로컬 운영을 포함한다. 배포 파일 제작·모델 번들·호스팅은 2026-09-01 사용자 결정에 따라 제품이 더 완성된 뒤 별도 단계에서 진행한다. 모델 자체의 역량 차이를 제외하고 Codex 수준의 신뢰성을 목표로 하되, 동등함을 확인했다는 의미는 아니다.

- 아래 `R01`–`R27`은 `AGENT_RUNTIME_ROADMAP.md`의 27개 상위 방향과 같은 번호다. `U*`는 사용자 고유 요구사항, `O*`는 큰 운영 항목을 분리한 수락 단위, `GAP-*`는 감사에서 발견한 결함/공백이다. 이후에도 번호를 재사용하거나 재정렬하지 않는다.
- **자동 검증(A)**: 독립적인 오라클이 있는 계약·단위·회귀·통합 테스트. Mock 성공은 원격 서비스나 실제 장치 성공으로 승격하지 않는다.
- **실환경 검증(L)**: 해당 버전의 실제 앱·모델·장치·계정·산출물로 재현하고 관찰한 결과. 설치 여부, HTTP 200, 파일 존재, 버튼 활성화만으로 요청 완료를 판단하지 않는다.
- **사용자 수락(U)**: 결과의 내용·미감·편의·기대 동작을 사용자가 판단한다. 기능 구현과 별도로 기록한다.
- 아래 코드와 테스트는 이번 감사에서 **정적 대조한 증거 위치**다. 테스트명이 적혀 있다고 이번 감사에서 실행·통과했다는 뜻은 아니다. 기존 문서의 `907 passed, 9 skipped, 4 deselected` 등은 당시 실행 기록이며 현재 빌드의 전 영역 수락률이 아니다.
- 자동·실환경·사용자 수락 중 빠진 축을 숨기지 않는다. 전체 완료율, 임의의 진행률, “모델만 바꾸면 완성”이라는 판정을 사용하지 않는다.
- 다른 작업자가 동시에 코드를 수정 중이므로 아래 발견은 감사 시점 기준이다. 수정 후에는 해당 GAP에 수정 커밋/테스트/실환경 근거를 연결해야 닫을 수 있다.

## 2. 우선 확인된 구현 공백과 문서 모순

이 절은 **1차 감사 당시의 발견 원장**이다. 이후 수정된 항목도 원인을 추적하기 위해 보존한다. 현재 남은 범위는 10절과 **11절 재감사**를 함께 읽어야 하며, 이 표만으로 이미 수정한 코드를 계속 미구현이라고 판단하지 않는다.

| ID | 발견 및 정확한 근거 | 필요한 조치와 종료 조건 |
| --- | --- | --- |
| GAP-01 | **Photoshop 전문가의 편집 계약과 제공 도구가 다르다.** `core/specialist_workspaces.py`는 편집 결과 파일과 전후 시각 변경을 요구한다. `plugins/photoshop.py`는 `photoshop_status`, `photoshop_open_document`, `photoshop_active_document`만 제공하고, 열기 결과는 입력 원본을 `image_document`로 반환한다. 편집/저장/내보내기 도구는 없다. | 실제 편집·저장·내보내기 경로와 전후 검증을 구현하거나 지원 범위를 문서 열기/조회로 정확히 축소하고 부족한 작업을 사용자에게 알려야 한다. 열기만으로 편집 완료를 주장하지 못하는 부정 테스트와 실제 Photoshop 수락이 필요하다. |
| GAP-02 | **전문가의 개별 수락 기준이 독립 검증되지 않는다.** `core/specialist_team.py::_review_execution`은 성공 상태·일반 evidence·존재하는 산출물을 확인한 뒤 모든 `criteria_results[].verified`를 같은 `passed` 값으로 채운다. `test_specialist_workspaces.py::test_photoshop_contract_accepts_plugin_image_document_artifact`는 형식 허용만 검사하며 편집 결과를 확인하지 않는다. | 기준별 증거 종류와 관측값을 연결한다. 입력 원본/출력 결과 구분, 내용 요구사항, 전후 변경, 저장 반영을 따로 검증한다. 허용된 형식의 아무 파일이나 일반 evidence를 제출해도 완료가 되지 않는 테스트가 필요하다. |
| GAP-03 | **경량 설치기의 배포 내용이 비어 있다.** `packaging/bootstrap_manifest.json`의 `assets`가 빈 배열이다. `core/productization.py`와 `test_p12_productization.py::test_bootstrap_checksum_and_role_selection`은 다운로드 엔진의 근거이지 배포 가능한 모델 묶음의 근거가 아니다. `P12_PRODUCTIZATION.md`도 호스팅 미구성을 명시한다. | 배포할 역할별 파일·버전·라이선스·URL·SHA-256·크기·의존성을 실제로 채우고 깨끗한 PC에서 설치/복구/재실행한다. 호스팅 및 배포 범위 선택은 사용자 결정이 필요하다. 엔진 완성과 배포 완성을 분리한다. |
| GAP-04 | **실환경 수락 원장이 증거의 의미를 충분히 검증하지 않는다.** `core/acceptance_runtime.py`는 종류/비어 있지 않은 값/로컬 파일 존재·크기 등을 검사한다. 관찰 시 `packaged_runtime`의 근거 `build/installed-smoke.txt` 내용은 `OK`뿐이다. 파일 하나는 전체 설치·모델 실행·재부팅 수락을 증명하지 않는다. | 케이스 ID, 실행 빌드·환경, 관측값, 실제 결과, artifact hash를 가진 구조화 보고서를 사용한다. 무효·삭제·변조·만료된 근거와 다른 빌드의 근거가 수락을 유지하지 않도록 검증한다. 과거 smoke 결과 자체는 삭제하지 말고 좁은 의미로 보존한다. |
| GAP-05 | **네이티브 도구 hard timeout은 미완이다.** `core/plugin.py`의 `ThreadPoolExecutor`/Future 취소는 실행 중인 COM·네이티브 호출을 종료하지 못한다. `core/task_contracts.py`의 시간 예산 확인도 OS 강제 종료와 다르다. `README.md`, `PROJECT_REVIEW.md`가 이를 잔여 항목으로 명시한다. | 격리가 필요한 도구를 bounded worker process로 실행하고 종료/재시작/자원 회수 정책을 검증한다. 시간 초과한 외부 전송은 실제 성공 여부를 재조회하기 전 재전송하지 않는다. 무한 대기·프로세스 충돌·잠금 잔존 fault injection이 필요하다. |
| GAP-06 | **인물 분리 실패가 무변경 mask로 감춰질 수 있다.** `core/mockup_subject_runtime.py::segment`는 BiRefNet 실패 후 GrabCut, 다시 실패하면 전체 255 alpha를 반환한다. `status`의 모델 준비 확인도 `config.json` 존재 중심이다. | foreground/background 비율, alpha 실효성, backend와 fallback 사유를 검증·보고한다. 전체 불투명 mask를 배경 제거 성공으로 보고하지 않는다. 실제 모델 가중치 로드/다양한 인물 사진 평가가 필요하다. MediaPipe/InsightFace/SAM2가 설치됐다고 추정하지 않는다. |
| GAP-07 | **자유로운 이미지 편집 범위가 문구보다 좁다.** `core/mockup_generation.py::SDXLGenerationBackend.generate_background`는 text-to-image이고 전달받은 `reference_paths`를 사용하지 않는다. SD1.5 IP-Adapter 경로와 레이어 편집은 있으나 일반 inpainting/ControlNet/FLUX 경로는 확인되지 않았다. | 레이어 편집과 생성형 픽셀 편집의 지원 범위를 분리한다. 지원하지 않는 명령을 성공으로 처리하지 않는다. 실제 reference conditioning·영역 mask·피사체 보존이 필요한 편집 경로는 사용자 사용 사례와 VRAM 한도에 맞게 구현·실측해야 한다. |
| GAP-08 | **이미지 임베딩/학습을 과대 해석하면 안 된다.** `core/mockup_style_index.py`는 CLIP 실패 시 결정적 시각 descriptor로 fallback한다. `core/mockup_post_training.py`는 dataset manifest와 학습 명령 문자열을 만들지만 참조하는 `train_dreambooth_lora_sdxl.py`는 저장소에서 확인되지 않는다. | CLIP/descriptor 구분을 UI와 근거에 보존하고 검색 적합도를 평가한다. 실행되지 않은 학습을 완료로 표시하지 않는다. 강화학습은 사용자 결정에 따라 적용하지 않는다. 선택적 LoRA 역시 별도 요청/검증 없이는 제품 완성으로 계산하지 않는다. |
| GAP-09 | **카메라 기본값 문서가 상충한다.** `PLUGIN_ROADMAP.md`, `AGENT_RUNTIME_ROADMAP.md`의 2026-08-21 설명은 기본 비활성이다. `GESTURE_INTERFACE.md`, 현재 `core/assistant_settings.py`는 카메라 기본 활성, 명령형 제스처는 별도 opt-in이다. | 사용자가 요청한 카메라 기본 켜짐과 명령 실행 제스처의 별도 동의를 구분해 문서를 정리한다. 실제 장치 실패 시 설정값만으로 “켜짐”이라고 말하지 않는 계약을 유지한다. |
| GAP-10 | **일반 모델과 디자인 전용 역할이 문서에서 구별되지 않는다.** `MODEL_ROUTING.md`의 일반 Vision `gemma3:4b` 자체는 틀리지 않지만, `config.py`/`core/model_registry.py`의 `OLLAMA_DESIGN_VISION_MODEL=qwen2.5vl:7b`, `style_vision`, `visual_critic`, `design_planning`, `subject_analysis` 구분을 충분히 설명하지 않는다. | 각 워크스페이스의 실제 선택 모델·fallback·로드/해제·실행 흔적을 공개한다. 전용 역할 코드가 있는 것과 실제 모델 준비/사용/품질 수락을 구분한다. |
| GAP-11 | **수집되지 않는 배포 테스트가 있다.** 감사 당시 `test_packaging_entrypoint.py`의 4개 테스트가 `pytest.ini`의 명시적 `python_files` 목록에 없다. `QA_MASTER_AUDIT.md`에서 계획한 `manual_acceptance/`도 존재하지 않는다. | 테스트 수집 목록과 실제 파일을 자동 대조한다. 장치/계정 import 부작용이 있는 구형 수동 스크립트는 명시적 수동 진입점으로 옮기거나 격리한다. 이번 감사는 `pytest.ini`를 변경하지 않았다. |
| GAP-12 | **계획 문서의 외부 프로그램/서비스 일부는 실행 경로가 없다.** `PLUGIN_ROADMAP.md`의 IDE CLI, GitHub Issues/PR/CI, Docker, 연락처/할 일/지도·교통, 범용 브라우저 업로드·게시·결제 등은 해당 전용 구현/수락 근거가 확인되지 않았다. 로컬 Git이나 브라우저 열기가 대체 근거는 아니다. | 기능별로 구현할 실제 업무와 서비스/계정을 확정하고 입력→승인→실행→검증 경로를 만든다. 범용 빈 도구를 추가하거나 “연결 가능”을 완료로 계산하지 않는다. 결제/게시 같은 고위험 작업은 별도 명시 승인 계약이 필요하다. |
| GAP-13 | **“남은 것은 외부 조건뿐”이라는 설명은 현재 코드와 맞지 않는다.** `README.md` 및 로드맵의 일부 완료 표현과 달리 GAP-01/02/05/06/07은 코드 수준 잔여 작업이다. `DEPLOYMENT.md`의 7월 설치 파일과 `QA_MASTER_AUDIT.md`의 8월 EXE smoke는 산출물 범위도 다르다. | 역사적 기록은 유지하되 현재 지원표/배포표를 별도 관리한다. 배포 파일마다 소스 revision, 빌드 시각, 구성, smoke/설치/사용자 수락 범위를 표시한다. |

## 3. 로드맵 27개 요구사항

현재 증거 열은 “구현·자동 테스트의 위치”이며 각 행의 최종 수락을 뜻하지 않는다. 특히 L/U 열의 결과는 원장에 연결된 실행 기록이 있어야 닫는다.

### 대화·의도·계획

| ID / 단계 | 수락 기준 | 현재 구현 및 테스트 근거 | A: 남은 자동 검증 | L: 남은 실환경 검증 | U: 사용자 수락 |
| --- | --- | --- | --- | --- | --- |
| R01 / P1,P5 | 목표·대상·제약·계획·산출물·승인 상태가 한 작업으로 이어지고 재시작/취소/재개가 상태와 일치한다. | `core/dialogue_state.py`, `core/plan_runtime.py`; `test_dialogue_runtime.py::test_full_task_lifecycle_status_priority_pause_resume_and_cancel`, `test_unified_task_state_persists_intent_plan_artifacts_and_evidence` | 작업 동시 진행, pending 만료, 승인 직후 취소, process crash의 교차 조합과 상태 불변식 검사. | 실제 앱 재시작 중 작업/승인 경계를 복원하고 중복 실행이 없는지 확인. | 작업 상태와 중단 이유를 사용자가 이해하며 다른 작업으로 자연스럽게 전환할 수 있음. |
| R02 / P1 | 긴 대화에서 필요한 정보만 유지하고 다른 주제·작업공간 정보가 섞이지 않는다. | `core/conversation_context.py`, `core/context_lifecycle.py`; `test_dialogue_runtime.py::test_pending_and_recent_intent_are_isolated_by_workspace`, `test_memory_consolidation.py::test_limited_session_history_returns_latest_messages_in_chronological_order` | 장기 다중 주제 held-out 대화, 요약 압축 후 지시 보존, 정보 부족 표현 검증. | 실제 모델의 context 한도와 장시간 대화 메모리/지연 실측. | 말투·길이·맥락 적절성을 사람 rubric으로 평가. 단순 문자열 매칭은 대체 불가. |
| R03 / P1,P2 | 새 요청은 이전 작업과 독립이며 “그것/아까/조금 더”는 올바른 현재 대상을 참조한다. | `core/conversation_context.py`, `core/utterance_scope.py`; `test_conversation_context.py::test_independent_message_request_never_receives_previous_stock_context`, `test_explicit_topic_switch_does_not_inherit_previous_design_context`, `test_elliptical_visual_edit_uses_recent_context_without_domain_keywords` | 부정·정정·주제 전환·동명이인·복수 대상·인용문이 혼합된 보류 발화 확대. | 실제 음성 인식 오류까지 포함해 금융 분석→카카오톡→시안 수정 연속 수행. | 사용자가 전환/정정 설명을 반복하지 않아도 기대 작업이 선택됨. |
| R04 / P2 | 일반·비정형 한국어 지시의 동작/대상/제약을 보존하며 낮은 확신을 숨기지 않는다. | `core/intent_router.py`, `core/structured_output.py`; `test_routing_dataset.py::test_realistic_user_utterance_routing_dataset`, `test_routing_is_stable_under_harmless_spacing_and_punctuation` | 학습/수정에 사용하지 않은 희귀 표현·오타·반말·완곡 요청·금지 요청 corpus, 잘못 실행한 비율 별도 측정. | 실제 planner/model 응답으로 schema·도구 선택·제약 준수 비교. | 재설명 빈도 및 이해 정확도에 대한 사용자 평가. |
| R05 / P2,P6 | 실제 가능한 도구만 선택하며 복합 요청의 필요한 기능을 빠뜨리지 않는다. | `core/tool_loadout.py`, `core/capability_audit.py`; `test_capability_inventory.py::test_complete_registry_capability_inventory_has_no_contract_or_permission_errors`, `test_capability_router.py::test_compound_request_uses_multi_tool_loadout_instead_of_fast_path` | 등록 여부 외에 실제 호출 가능한 dependency/인증/출력 검증, GUI 진입 경로 포함 inventory 확장. | YouTube 검색·재생, 파일/문서·브라우저·전문가 복합 작업을 현재 설치 환경에서 끝까지 확인. | 사용자가 시도한 업무가 기능 목록과 일치하고 미지원은 명료하게 안내됨. |
| R06 / P1,P2 | 필수 정보만 한 번에 묻고 답변을 원 요청에 연결한다. 안전 관련 모호함은 임의 추정하지 않는다. | `core/dialogue_state.py`; `test_dialogue_runtime.py::test_clarification_answer_resumes_original_request`, `test_capability_router.py::test_close_intent_collision_asks_one_explainable_question_without_execution` | 질문 반복/루프, 답변 도중 새 요청, 일부 답변, 취소, 모호한 수신자 테스트. | 실제 모델에서 확인 질문의 유용성과 빠진 slot 재사용 확인. | 질문이 과도하지 않고 사용자가 의도한 작업을 재작성할 필요가 없음. |
| R07 / P5 | 계획은 실행 가능한 DAG이며 각 제약·산출물·검증·승인 경계를 포함한다. | `core/planner.py`, `core/plan_runtime.py`; `test_planner_contract.py::test_compound_planner_retries_until_every_required_tool_is_present`, `test_p5_plan_runtime.py::test_independent_steps_run_in_parallel_and_dependency_waits` | 순환 의존/미지원 도구/누락된 인용문/부분 실행 이후 수정/동일 자원 충돌 테스트. | 실제 3단계 이상 복합 업무에서 계획과 실행 로그·산출물 비교. | 계획 설명이 작업 이해를 돕고 실행 전후 차이를 알 수 있음. |
| R08 / P5 | 실패/관찰 결과로 필요한 단계만 재계획하고 이미 성공한 외부 부작용을 반복하지 않는다. | `core/plan_runtime.py`, `core/recovery.py`; `test_p5_plan_runtime.py::test_observation_can_replace_plan_with_new_revision`, `test_recovery_reuses_verifier_and_blocks_duplicate_failure` | 수정 계획의 승인 범위 재검증, 오래된 관측/중복 응답/부분 실패 조합. | 실제 서버 오류·창 이동·파일 잠김에 대한 관찰→재계획→재검증. | 복구 중 무엇이 유지/변경됐는지 확인 가능. |

### 실행 진실성·도구·보안

| ID / 단계 | 수락 기준 | 현재 구현 및 테스트 근거 | A: 남은 자동 검증 | L: 남은 실환경 검증 | U: 사용자 수락 |
| --- | --- | --- | --- | --- | --- |
| R09 / P0,P6 | 성공/실패/미검증/취소/부분 완료가 타입화되어 UI·대화·TTS가 같은 사실을 말한다. | `core/tool_result.py`, `core/response_presenter.py`; `test_tool_result_runtime.py::test_failed_verification_cannot_claim_success`, `test_terminal_state_is_visible_in_user_response` | 모든 registry tool의 실제 반환을 schema 검증, 예외/None/손상 artifact 출력 조합. | 실제 원격/COM 오류가 어떤 사용자 문장·음성으로 나오는지 확인. | 기술 오류를 숨기지 않되 이해 가능한 후속 조치가 안내됨. |
| R10 / P0 | 요청한 결과의 내용·원격 ID·전후 변경을 증거로 확인하며 원본 파일/빈 문서/일반 로그는 결과로 오인하지 않는다. | `core/artifact_validation.py`, `core/verifier.py`, `core/specialist_team.py`; `test_tool_result_runtime.py::test_executor_downgrades_direct_success_without_evidence`, `test_specialist_workspaces.py::test_specialist_reviewer_rejects_zero_byte_artifact_shell` | **GAP-01/02** 기준별 검증, 형식은 정상이나 내용이 틀린 파일, 원본 재제출 부정 사례. | 실제 문서/시안/메시지/Office 저장 결과 재열기 또는 재조회. | 산출물 내용이 요청에 부합함을 확인. |
| R11 / P5 | 재시도 예산·전략·오류 서명이 보존되며 불확실한 외부 동작을 맹목 재시도하지 않는다. | `core/recovery.py`, `core/plugin.py`; `test_plugin_execution_safety.py::test_side_effect_timeout_never_retries`, `test_same_idempotency_key_after_uncertain_timeout_does_not_execute_again` | **GAP-05** 강제 종료·프로세스 재시작 이후 idempotency와 자원 회수 검증. | 네트워크 단절·COM hang·절전 중 복구, 실제 도구 중복 실행 점검. | 실패 시 이미 수행한 부분과 재시도 가능 범위가 분명함. |
| R12 / P0 | 실행하지 않았거나 결과를 확인하지 못한 작업을 완료했다고 말하지 않는다. | `core/executor.py`, `core/agent_prompt_policy.py`; `test_conversation_routing.py::test_conversation_path_blocks_unexecuted_completion_claim`, `test_evaluation_runtime.py::test_execution_contract_rejects_false_completion_without_evidence` | 전문가 검수 우회(GAP-02), 부정형/인용형 완료 문장, 부분 도구 결과의 평가 확대. | 실제 실패/미검증 사건을 수집해 false completion 분모와 원인별 기록. | “보냈다/저장했다/수정했다”가 실제 체감 결과와 일치. |
| R13 / P6 | 레지스트리가 유일한 실행 경계이며 입력/출력/권한/의존성/취소·timeout을 선언한다. 설치·인증·연결·검증은 별도 상태다. | `core/plugin.py`, `plugins/legacy_runtime.py`; `test_p6_plugin_runtime.py::test_input_and_output_json_schema_are_both_enforced`, `test_plugin_status_axes_are_independent`, `test_tool_executor_exposes_legacy_tools_only_through_registry` | 레거시 우회/잘못된 등록/설치 후 재로드/버전 충돌/해제 중 실행 테스트. | 실제 플러그인 dependency 누락·OAuth 만료가 readiness에 정확히 반영됨. | “사용 가능” 표시와 실제 실행 가능성이 일치. |
| R14 / P6,P12 | 사용자/작업/경로/계정/내용에 묶인 승인, 비밀 보호, 최소 권한, 안전한 취소를 보장한다. | `core/permission.py`, `core/productization.py`; `test_p12_productization.py::test_scoped_permissions_support_once_session_always_and_boundaries`, `test_executor_approval_safety.py::test_contextual_approval_never_merges_distinct_send_tasks` | 경로 traversal/symlink, content 수정 뒤 승인 무효화, 재시작 후 claim 경합, OS worker 격리(GAP-05). | 실제 Windows 계정·보호 폴더·자격 증명 저장/삭제/로그 마스킹. | 승인 문구에 실제 대상과 내용이 보이고 변경 시 재확인됨. |

### 작업공간·코딩·기억·조사

| ID / 단계 | 수락 기준 | 현재 구현 및 테스트 근거 | A: 남은 자동 검증 | L: 남은 실환경 검증 | U: 사용자 수락 |
| --- | --- | --- | --- | --- | --- |
| R15 / P4 | 마지막/복수 작업공간·별칭·설정·분기·dirty 상태를 정확히 복원하고 사라진 경로는 안전하게 처리한다. | `core/workspace.py`, `core/project_bootstrap.py`; `test_p4_workspace_intelligence.py::test_workspace_catalog_alias_settings_and_restore`, `test_inaccessible_last_workspace_does_not_block_startup` | 네트워크/이동식 경로·동명이름·다른 브랜치·설정 migration 경계. | 앱 재시작 후 실제 작업 폴더 선택·프로젝트 이동/삭제 복구. | 작업공간 이동이 뜻하지 않은 폴더 선택창/명령 실행을 유발하지 않음. |
| R16 / P4 | gitignore·비밀·대용량 폴더를 제외하고 변경/삭제 파일만 namespace별 반영한다. | `core/project_indexer.py`; `test_p4_workspace_intelligence.py::test_project_index_honors_gitignore_and_syncs_changes`, `test_project_profile_detects_framework_and_commands` | 대규모 rename/delete burst, 인덱싱 중 수정, unreadable/깨진 인코딩, generation 교체의 원자성. | 실제 큰 저장소에서 초기/증분 시간·메모리·앱 반응성 측정. | 변경 후 검색이 최신이며 다른 프로젝트 결과와 혼동되지 않음. |
| R17 / P3 | 관련 코드·심볼·테스트를 찾아 최소 patch로 구현하고 사용자 변경을 보존하며 검증 실패 시 안전하게 복구한다. | `core/coding_agent.py`, `core/self_development.py`, `plugins/coding.py`; `test_coding_agent.py::test_hash_conflict_and_ambiguous_patch_do_not_modify_user_file`, `test_natural_language_feature_request_requires_test_and_creates_it_atomically`, `test_failed_validation_can_repair_and_reverify_with_bounded_retry` | 여러 언어/빌드 시스템·binary/큰 파일·미커밋 변경·의존성 실패, 실제 기능 의미 검증 corpus. | 별도 시험 저장소에서 기능 추가/버그 수정/ANIS UI 자기 수정까지 실행·테스트·diff 확인. | 변경 설명/실행 결과가 요청과 맞고 “코드 생성”과 “동작 확인”을 구분. |
| R18 / P1,P7 | 대화/작업/사실/선호/승인 결과/반복 습관을 구별해 저비용 통합하고 정정·만료·삭제를 처리한다. | `core/memory_pipeline.py`, `core/memory_consolidator.py`, `core/knowledge_memory.py`; `test_memory_pipeline.py::test_exchange_capture_is_cheap_and_habits_require_repetition`, `test_idle_consolidation_retries_failed_events`, `test_p7_memory_rag.py::test_user_correction_supersedes_contradictory_active_records` | 장시간 반복/반복하지만 부정적인 작업, 삭제 전파, 동시 namespace, 민감정보 오기억 부정 사례. | 실제 며칠간 기록·유휴/종료 통합·충돌 수정과 자원 사용 실측. | 자동 기억/잊기/선호 적용이 기대와 일치하며 사용자가 정정 가능. |
| R19 / P7 | 출처·시각·신뢰도·namespace가 있는 검색을 수행하며 검색 후보/프롬프트 포함/답변 실제 사용을 구분한다. | `core/rag.py`, `core/obsidian_vault.py`, `core/knowledge_memory.py`; `test_p7_memory_rag.py::test_structure_preserving_chunking_and_claim_evidence_link`, `test_modified_and_deleted_documents_are_synchronized`, `test_obsidian_integration.py::test_vault_reverse_sync_only_reindexes_changed_notes` | 주장을 뒷받침하지 않는 유사 chunk, 오래된 웹 근거, 사용 추적 오탐, Obsidian 동시 편집/삭제·충돌. | 실제 vault의 사용자 수정→검색→답변 역동기화 및 held-out 검색 relevance 평가. | 출처를 열어 확인할 수 있고 근거 없는 내용을 기억처럼 단정하지 않음. |
| R20 / P8 | 검색만 열지 않고 실제 자료를 읽어 최신성·출처·상충 정보를 비교하고 분석 답변을 작성한다. | `core/research.py`, `plugins/browser.py`, `plugins/finance.py`; `test_p8_research_agent.py::test_cross_validation_has_claim_citations_and_visible_conflicts`, `test_prompt_injection_is_removed_but_source_warning_is_retained`, `test_p8_browser_plugin.py::test_youtube_learning_routes_and_requires_real_transcript` | 검색 결과와 본문 불일치·paywall·로그인·동적 표·PDF·인용 누락·자막 없음·prompt injection corpus. | 실제 한국어 기업/뉴스/제품 조사 및 YouTube 조회·자막 처리, 서버 변경/차단 확인. | 브라우저 표시와 별도로 요청한 분석이 제공되고 주장·추론·불확실성을 구분. |

### 문서·외부 실행·멀티모달·운영

| ID / 단계 | 수락 기준 | 현재 구현 및 테스트 근거 | A: 남은 자동 검증 | L: 남은 실환경 검증 | U: 사용자 수락 |
| --- | --- | --- | --- | --- | --- |
| R21 / P9 | Word/Excel/PowerPoint/HWP·PDF 생성/편집은 실제 내용과 서식을 보존하고 재열기/렌더링으로 검증한다. | `core/office_runtime.py`, `plugins/office_editing.py`, 문서별 plugins; `test_p9_office_runtime.py::test_docx_template_edit_preserves_run_format_and_package_styles`, `test_xlsx_template_edit_preserves_cell_style`, `test_pptx_template_edit_preserves_layout_parts`, `test_rendered_pdf_visual_qa_rejects_blank_and_accepts_content` | 복잡한 표·수식·병합·차트·각주·개체·다국어·폰트 부재/기존 template round-trip. | 실제 Office/HWP COM 버전별 열기→편집→저장→재열기, PDF/Office export 시각 검사. | 문서 내용과 가독성·수식·레이아웃이 목적에 충분. |
| R22 / P9 | 메일·캘린더·Drive/OneDrive·Notion·Slack/Teams는 실계정 연결 및 초안/승인/원격 반영/재조회가 구별된다. | `core/remote_runtime.py`, `plugins/cloud_communication.py`, `plugins/mail.py`; `test_p9_remote_runtime.py::test_oauth_pkce_complete_and_secret_free_status`, `test_expired_oauth_token_is_refreshed`, `test_google_calendar_and_slack_requery_remote_ids`, `test_plugin_keeps_draft_and_apply_separate` | provider별 pagination/rate limit/token expiry/중복/부분 반영·수정/취소 검증. Mock remote ID는 실제 수신 근거가 아님. | 사용자가 선택한 계정에서 OAuth→조회→승인 작업→원격 재조회/수신. 서비스별로 별도 수락. | 실제 수신/이벤트·문서 상태와 말한 결과가 일치. |
| R23 / P10 | API/CLI/COM/UIA 우선으로 창·수신자·입력창·본문·실제 반영을 확인하고 좌표/검색창 입력을 전송으로 오인하지 않는다. | `core/windows_automation.py`, `core/desktop_messaging.py`, `plugins/windows_control.py`; `test_p10_runtime.py::test_windows_policy_never_silently_uses_coordinates`, `test_desktop_messaging_uia_hardening.py` | 친구 추가창/동명 채팅/포커스 변경/중복 탐색/보내기 단축키/전송 후 검증 실패·timeout 부정 사례. | 실제 KakaoTalk 버전/DPI/UI 언어에서 지정 수신자 **형택**에게 승인된 테스트 전송·수신 확인. 해당 라이브 테스트는 주 담당자가 조율하며 이 감사는 보내지 않음. | 사용자가 승인한 사람/내용만 전달되고 미확인은 성공으로 보고되지 않음. |
| R24 / P10 | 이미지 순서·참조 역할·영역·프레임 시각을 보존해 시각 근거로 답하고 OCR/표/차트/영상의 한계를 표시한다. | `core/vision_runtime.py`, `plugins/multimodal_runtime.py`; `test_p10_runtime.py::test_multi_image_vision_preserves_order_and_hashes`, `test_screen_region_capture_records_dimensions_and_hash`, `test_video_frames_have_monotonic_timeline` | 참조 이미지와 제작용 이미지 혼동, 회전/저해상도/표·숫자 OCR, 잘못된 파일/빈 프레임. | 현재 Vision 모델로 실제 이미지·문서·화면·영상 평가. 이미지 이해 품질을 schema 유효성과 분리. | 중요한 텍스트/객체·관계를 놓치거나 잘못 단정하지 않음. |
| R25 / P10 | 정확한 호출·음성 입력·후보 재평가·장치 복구를 제공하고 미호출/에코/무음의 행동 실행을 막는다. | `core/hardware.py`, `core/voice_runtime.py`; `test_hardware_devices.py::test_only_exact_first_wake_word_opens_voice_command`, `test_faster_whisper_rejects_low_confidence_hallucination`, `test_microphone_auto_selects_default_native_rate` | 사투리/작은 목소리/유사 호출어/TV 소리/긴 무음/빠른 정정/장치 변경 corpus. | 실제 마이크·오디오 드라이버·모델로 오인식/미인식/지연·절전 복귀 측정. | 호출 및 명령 수정이 자연스럽고 뜻하지 않은 실행이 없음. |
| R26 / P10 | 선택한 목소리로 실제 재생하고 문장 품질·prebuffer·끼어들기·취소·종료가 일치한다. | `core/custom_tts.py`, `core/audio_processor.py`, `core/tts_normalizer.py`; `test_custom_tts.py::test_selected_custom_voice_does_not_fall_back_to_windows_voice`, `test_custom_tts_playback_failure_is_not_reported_as_user_cancellation`, `test_tts_quality_and_settings.py::test_streaming_tts_prebuffers_and_preserves_all_pcm` | 숫자/URL/코드/긴 문장 발음·stream 끊김/소켓 종료/다중 발화·상태 경합. | 실제 스피커·STT 동시 가동 및 GPT-SoVITS 출력 청취·barge-in 기록. | 자연스러움/발음/시작 지연/중단 반응을 사용자 청취 평가. TTS만 통과해 전체 수락으로 확장하지 않음. |
| R27 / P11,P12 | 예약·제안·관측·진단·평가·설치·업데이트를 실제 장기 운영 중 신뢰할 수 있다. | `core/scheduler.py`, `core/proactive_runtime.py`, `core/productization.py`, `core/acceptance_runtime.py`; `test_p11_automation_proactive.py::test_scheduler_heartbeat_and_accelerated_soak`, `test_p12_productization.py::test_e2e_probe_never_claims_unrun_external_checks` | O01–O06으로 분리. 가속 시계 테스트를 wall-clock soak로 간주하지 않음. | 실제 장기 실행/절전/재부팅/배포 설치와 모델·권한 복원 확인. | 알림·오류·성능·복구가 일상 사용에 충분. |

## 4. 사용자 고유 요구사항과 작업공간 품질

| ID / 연결 | 수락 기준 | 현재 구현 및 테스트 근거 | A: 남은 자동 검증 | L: 남은 실환경 검증 | U: 사용자 수락 |
| --- | --- | --- | --- | --- | --- |
| U01 / 전체 | **강화학습을 적용하지 않는다.** 승인 결과 기억/사례 검색과 모델 가중치 학습을 혼동하지 않는다. | `POST_TRAINING.md`, `core/learning_runtime.py`, `core/mockup_post_training.py`에 서로 다른 학습/기억 경로가 존재. | 자동 요청 처리 중 모델 학습/명령 실행이 묵시적으로 시작되지 않는 정책 테스트. | 실행 시 실제 모델 다운로드/학습/기억 통합의 상태를 구분. | 사용자가 기억 관리와 선택적 모델 학습 여부를 통제. |
| U02 / R05,R07,R10,R18 | 메인과 **모든 전문가**가 준비·계획·실행·개별 검수 역할을 실제로 수행하고 공유 기억/작업 상태를 사용한다. 모델은 자원 한도 내 순차 로드한다. | `core/specialist_team.py`, `core/specialist_workspaces.py`, `core/model_registry.py`, `core/gpu_scheduler.py`; `test_specialist_workspaces.py::test_specialist_team_exposes_sequential_role_pipeline`, `test_workspace_readiness_records_each_check_and_blocks_missing_executor`, `test_p10_runtime.py::test_gpu_queue_enforces_budget_and_priority` | 역할이 빈 설명만 반환하는 경우, reviewer 실패/누락, 계획 저하, 모델 fallback/자원 회수. GAP-02 기준별 검증이 필수. | workspace별 실제 입력→role trace→도구 실행→산출물→검수. RTX 4060 Laptop에서 peak VRAM/RAM·로드 시간·처리 지연 측정. | “팀” 이름보다 실제 결과의 정확성·품질·대화 연결성을 평가. |
| U03 / R24 | 시안 생성/수정은 현재 표시된 revision을 기준으로 요청 대상만 바꾸고 undo/redo 분기와 동시 완료를 안전하게 처리한다. | `core/mockup_document.py`, `core/mockup_design.py`, `core/mockup_scene.py`; `test_mockup_architecture.py::test_design_document_detects_stale_or_mutated_preview`, `test_mockup_design.py::test_completed_ai_edit_is_discarded_if_user_changed_active_preview`, `test_edit_write_barrier_preserves_unrequested_groups_and_copy` | 다중 연속 편집/undo→새 수정/redo 무효화, 스케치+텍스트·다중 대상, 참조 자료에 포함된 지시 격리. | 사용자가 제시한 26–29 결과 계열을 실제 UI·모델로 재현하고 매 revision의 diff/이미지 비교. | 명령하지 않은 문구/색상/테두리/사진이 변하지 않음. |
| U04 / R24 | 원형·크롭·얼굴 보존·안쪽 점선·안전 영역·참조 스타일이 실제 픽셀에 적용된다. 폰트/색상/span/자간·행간·stroke·shadow·곡선 문구가 정확하다. | `core/mockup_layer_graph.py`, `core/mockup_subject_runtime.py`, `core/mockup_style_index.py`; `test_mockup_architecture.py::test_render_contract_checks_actual_circle_alpha`, `test_phrase_specific_font_and_true_circular_sticker_contract`, `test_svg_arc_text_uses_real_text_path`, `test_mockup_design.py::test_white_dashed_inner_border_is_added_and_rendered` | GAP-06/07/08, 누락 font fallback 표시, 투명도/경계 alpha/긴 한글 overflow/전체 문구 보존, 스타일 relevance corpus. | 실제 설치된 한글 폰트·Qt SVG·CLIP/BiRefNet/생성 backend에서 참고 이미지·저해상도·복수 인물 평가. | 8–29/111 등 사용자 사례와 새 시안에서 미감·읽기 쉬움·얼굴/문구 관계 수락. |
| U05 / R10,R24 | 렌더링한 **실제 결과**를 재검수하고 불합격은 원인/제한된 교정/재시도 예산과 함께 처리한다. 검수 실패·미실행을 승인으로 기록하지 않는다. | `core/mockup_design.py`, `core/mockup_pipeline_policy.py`; `test_mockup_architecture.py::test_visual_review_contract_uses_scene_facts_not_subjective_gaze`, `QA_MASTER_AUDIT.md`의 2026-08-30 검수 장애 격리 기록. | 모델 주관 평가와 결정적 픽셀/geometry 검사 구분, JSON만 바뀐 실패·근거 없는 검수·요청 밖 자동 교정 방지. | 실제 Vision 재검수의 오탐/미탐과 자동 교정 전후 결과를 보관·채점. | 사용자가 승인한 결과만 스타일 메모리/품질 승인으로 축적. |
| U06 / R24 | 미리보기 기본 중앙 fit, 확대/축소·드래그 pan, 스케치·사진 버튼/drag-drop·폰트 GUI가 실제 편집 요청으로 연결된다. | `ui/specialist_workspaces.py`; `test_specialist_workspaces.py::test_mockup_preview_defaults_to_centered_fit_and_supports_drag_pan`, `test_edit_reference_drop_box_accepts_local_image_urls`, `test_mockup_workspace_exposes_zoom_sketch_font_and_drop_guidance_controls` | resize/DPI·다중 사진·잘못된 파일·임시 스케치 경로 권한·저장 시 현재 revision 보존. | 실제 Windows Explorer→첨부 영역 drag-drop, 스케치→Vision 전달, 창 크기/DPI별 조작. | 탭/도구가 찾기 쉽고 스케치 의미가 결과에 반영됨. |
| U07 / R19,R24 | 2.5D 노드/연결선으로 뇌 형태를 이루고 360도 회전 및 내부로 이동하는 확대가 연속적이다. 내부 노드가 실제 Obsidian 노트/링크 정보에 연결된다. | `ui/brain_orbit.py`, `ui/knowledge_graph_workspace.py`, `core/knowledge_graph.py`, `core/obsidian_vault.py`; `test_brain_graph_transition.py::test_brain_network_is_dense_bilateral_and_lobed`, `test_brain_yaw_crosses_full_turn_without_projection_snap`, `test_deep_zoom_uses_explicit_states_and_embedded_existing_graph`, `test_spatial_nodes_select_locally_and_double_click_opens_workspace` | empty/대형 graph·삭제 노트·선택 충돌·회전 경계·숨은 노드 hit-test·프레임 budget. | 실제 vault 및 다양한 창 크기/DPI/GPU에서 노드→상세→원본 열기·성능 실측. | 화면 캡처를 띄우는 느낌이 아니라 공간 안으로 진입하며 원하는 정보를 선택할 수 있음. |
| U08 / R24 | 한 손은 회전/상하좌우 navigation, 두 손만 zoom. 속도 반영·사용자 민감도·매핑 설정·release/hysteresis가 있고 오작동이 외부 실행을 유발하지 않는다. | `core/gesture_runtime.py`, `core/interface_control.py`; `test_gesture_runtime_multihand.py::test_swipe_uses_horizontal_hysteresis_release_and_cooldown`, `test_two_hand_tracking_keeps_identity_when_detector_order_changes`, `test_gesture_configuration.py::test_gesture_dialog_previews_and_saves_complete_configuration`, `test_brain_graph_transition.py::test_one_hand_rotates_but_only_two_hands_can_zoom` | 좌/우 손 순서 교체·조명/occlusion·dropout·gesture 경합·고/저 민감도·intentional vs neutral motion. | 실제 카메라에서 여러 거리/속도/두 손/손실/재획득 시 오탐/인식률·지연 기록. | 직접 조절한 민감도와 동작이 직관적이고 폴더 선택 등 원치 않은 창이 열리지 않음. |
| U09 / R15,R25 | 채팅창 숨김/복원, 뇌 화면 중심 배치, 카메라 켜기/끄기, 종료/장치 수명주기와 실제 상태 표시가 일치한다. | `main_qt.py`, `core/assistant_settings.py`; `test_ui_device_acceptance.py::test_chat_panel_toggle_changes_real_visibility_and_restores_it`, `test_camera_persistence_follows_verified_runtime_state`, `test_shutdown_continues_after_one_cleanup_failure_and_detaches_ui_bridge` | 설정 persistence·카메라 실패·device hotplug·숨겨진 animation timer·종료 중 callback. | 실제 장치와 창 최소화/복원·앱 종료/재시작·권한 거부. | 제어가 눈에 보이고 상태/권한 안내와 현실이 일치. |
| U10 / R21,R23 | Photoshop 등 “전문가” 이름으로 제공되는 모든 도구가 약속한 편집·저장 업무를 실제 수행한다. | `core/specialist_workspaces.py`, `plugins/photoshop.py`; 현재 GAP-01/02 발견. | 실제 도구 inventory→workspace 계약의 실행 가능성 대조, 조회-only를 수정 완료로 승인하는 부정 테스트. | 정식 앱/문서 형식에서 편집→저장→재열기·전후 비교. | 빈 작업공간이나 열기만 가능한 기능을 완성된 전문가로 보여주지 않음. |
| U11 / R05,R13,R22,R23 | 계획된 기능도 사용자에게 지원/미지원/계정 필요 상태를 정확히 제시하고 필요한 플러그인/MCP를 실제 업무 경로에 연결한다. | `PLUGIN_ROADMAP.md`, `core/capability_audit.py`, `core/plugin.py`; GAP-12. | 선언만 있고 진입 경로/실행/검증 없는 capability 탐지, connector별 schema/version/권한 검사. | 선택된 IDE/GitHub/Docker/연락처·할 일 등 실제 서비스별 end-to-end. | 어떤 업무가 추가됐는지 확인. 플러그인 수나 MCP 연결 수를 기능 품질로 대체하지 않음. |

## 5. 운영·평가·배포를 분리한 수락 단위

| ID / 연결 | 수락 기준 | 현재 구현 및 테스트 근거 | A: 남은 자동 검증 | L: 남은 실환경 검증 | U: 사용자 수락 |
| --- | --- | --- | --- | --- | --- |
| O01 / R27 | 예약 작업이 재시작/절전/시계 변경을 견디며 중복 실행하지 않는다. 알림과 외부 행동 권한은 분리한다. | `core/scheduler.py`, `core/proactive_runtime.py`; `test_p11_automation_proactive.py::test_scheduler_restores_enabled_jobs_after_restart`, `test_scheduler_detects_sleep_gap_and_reloads_jobs`, `test_proposal_approval_never_executes_target_tool` | DST/시계 역행·잡 충돌·실행 중 종료·반복 알림 de-dup·부분 완료 fault injection. | wall-clock 장기 운영과 절전/재부팅 후 예약 복구. | 알림 시점·빈도·근거·집중모드가 적절. |
| O02 / R27 | 진단·trace가 실제 사건에 연결되고 비밀을 가린다. 검사를 하지 않은 기능은 not_run으로 표시한다. | `core/diagnostics_runtime.py`, `core/command_center.py`, `core/runtime/action_journal.py`; `test_agent_command_center.py::test_diagnostics_never_marks_an_unrun_probe_passed`, `test_p12_productization.py::test_json_trace_has_correlation_ids_and_redaction` | provider token·경로·다중 작업 correlation·회전/보존/손상 trace 검사. | 실제 장애→Command Center 상태→사용자 답변→디스크 로그 대조. | 기술 도움 없이 실패 위치와 다음 행동을 파악 가능. |
| O03 / R27 | 시험 corpus·평가 rubric·표본수·빌드 버전이 있는 품질 게이트로 답변/도구/전문가 품질을 평가한다. | `core/evaluation_runtime.py`, `core/quality_metrics.py`; `test_acceptance_runtime.py::test_quality_gate_rejects_lucky_single_sample`, `test_evaluation_runtime.py::test_specialist_quality_metrics_need_real_sample_volume` | held-out 다양성·케이스별 oracle·회귀 누락·평가자 일치도·다른 빌드의 근거 재사용 방지. | 실제 모델·도구 평가와 사용자 corpus; 동일 업무의 비교 benchmark가 없으므로 Codex와의 수치 차이는 산정 불가. | 자연스러움/정확성/과도한 질문/실제 성과를 사람 기준으로 평가. |
| O04 / R27 | 배포 파일이 현재 코드와 일치하고 깨끗한 Windows에서 설치→모델 준비→실제 기능 실행→재시작→제거/복구가 된다. | `packaging/bootstrap_manifest.json`, `core/productization.py`, `test_packaging_entrypoint.py`, `test_packaging_runtime.py::test_frozen_runtime_configures_bundled_model_and_browser_paths` | **GAP-03/11/13**, packaged resources/모델/Playwright 경로, offline bootstrap·중단/재개·checksum·disk-full. | 깨끗한 테스트 계정/PC에서 실제 설치 묶음과 핵심 서비스 실행. `OK` smoke만으로 완료하지 않음. | 설치·초기 모델 준비·오류 복구가 문서대로 가능. |
| O05 / R27,R14 | migration/update 실패가 사용자 데이터·설정·승인 상태를 망가뜨리지 않고 복구 가능하다. | `core/productization.py`; `test_p12_productization.py::test_migration_update_and_rollback`, `test_update_rejects_traversal` | 실제 이전 DB schema/대용량 파일/중간 종료/동시 실행/백업 hash·rollback 검증. | 이전 배포본→현재 배포본 실제 업그레이드·의도적 실패 후 복원. | 기억·작업공간·예약·설정이 보존됨. |
| O06 / R27,R14 | 실환경 수락 원장이 케이스·버전·유효한 증거와 연결되어 과거/빈/변조된 기록이 전체 완료를 만들지 못한다. | `core/acceptance_runtime.py`; `test_acceptance_runtime.py::test_live_acceptance_cannot_pass_without_operator_and_complete_evidence`, `test_expired_live_evidence_does_not_count_as_complete`, `test_local_evidence_must_exist_and_be_nonempty` | **GAP-04**, case-specific report schema, artifact hash·source revision·읽기 시 재검증, 수락 범위와 증거 범위 불일치. | 12개 원장 항목을 해당 실제 환경에서 실행해 보고서/산출물 연결. | 사용자 수락이 필요한 케이스에 명시적 평가 기록이 있음. |

## 6. 현재 실환경 원장의 의미와 부족한 근거

아래 표는 1차 감사의 **저장된 원문 상태**다. 무결성 검증을 추가한 뒤 조회한 현재 유효 상태는 11.4절에 기록했다. 저장된 `passed`와 재검증을 통과한 `passed`는 다르다.

관찰한 `data/acceptance/results.json`에는 `packaged_runtime`의 `passed` 한 항목이 저장되어 있었다. 나머지는 기본값상 `not_run`이다. 이는 **원장에 남은 상태**이며 모든 과거 실행이 없었다는 단정도, 전체 제품이 특정 비율로 끝났다는 뜻도 아니다. 원장 밖에서 수행한 검사가 있다면 원본 보고서를 연결해야 한다.

| 원장 key | 현재 기록 | 요구되는 추가 근거/범위 |
| --- | --- | --- |
| conversation_human_eval | not_run | 일반·드문 발화·후속 정정·다중 주제 corpus의 실제 응답과 사람 평가. |
| specialist_human_eval | not_run | 모든 제공 작업공간별 실제 과제, 산출물, 기준별 검수와 사람 평가. |
| mockup_visual_eval | not_run | 현재 미리보기 기반 연속 편집, 실제 생성 이미지, 요청 충족/유지 영역/미감 평가. |
| camera_gesture | not_run | 실제 두 손 zoom/한 손 navigation·민감도·손실 복구의 관측 보고서. |
| microphone_stt | not_run | 실제 마이크·소음·호출어/미호출어·에코·정정 발화의 녹음/관측 보고서. |
| speaker_tts | not_run | 실제 재생 오디오와 장치 보고서, 청취·barge-in 평가. |
| kakao_delivery | not_run | 정확한 수신자/내용/송신 후 화면 및 수신 확인. 원격 receipt를 모의 값으로 채우지 않음. |
| mail_delivery | not_run | 실제 Message ID·서버 재조회·수신 증거. |
| oauth_roundtrip | not_run | 선택 provider/account에서 실제 인증·token refresh·remote ID 동작. |
| office_com | not_run | 실제 Office/HWP 앱에서 저장한 산출물과 재열기/application report. |
| wall_clock_soak | not_run | 실시간 장기 실행 보고서와 절전/재부팅/장애·회복 기록. 가속 시계 결과와 분리. |
| packaged_runtime | passed 기록 있음: 2026-08-30 | `build/installed-smoke.txt`의 `OK`는 최소 런타임 smoke 범위로만 해석. 구조화 빌드/설치/모델 실행·재시작 근거 추가 필요. |

`core/quality_metrics.py`의 최소 표본은 작업 성공 30, 허위 완료 30, 확인 질문 15, 도구 선택 30, 지연 20, RAG 사용 15, 장기 복구 3, STT false wake 30, STT echo 30, 전문가 산출물 10, 시안 승인 10이다. 이 값은 작은 행운성 표본을 거부하는 하한이지 다양한 모든 기능의 충분한 표본을 보장하지 않는다. 기능·표현·위험군별 분포와 회귀 hold-out을 별도로 관리해야 한다.

## 7. 누락 방지를 위한 범위 연결

### QA_MASTER_AUDIT의 14개 축

| QA 축 | 이 문서의 수락 단위 |
| --- | --- |
| 자연스러운 대화 | R02,R04,R06,U01,O03 |
| 지시 이해 | R03,R04,R05,R06,U03,U04 |
| 문맥 | R01,R02,R03,R18,R19 |
| 라우팅 | R04,R05,R13,U11 |
| 계획/팀 | R07,R08,R11,U02 |
| 안전 | R11,R14,R22,R23,U08,O05 |
| 증거 | R09,R10,R12,U05,U10,O06 |
| 복구 | R01,R08,R11,R15,O01,O05 |
| 기억/RAG | R16,R18,R19,U07 |
| 전문가 작업공간 | R17,R20,R21,U02,U03,U04,U05,U06,U10 |
| 멀티모달 | R24,R25,R26,U04,U05,U06 |
| 제스처/UI | R15,U06,U07,U08,U09 |
| 외부 연동 | R20,R21,R22,R23,U10,U11 |
| 운영 | R27,O01,O02,O03,O04,O05,O06 |

### P0–P12

| 단계 | 연결 |
| --- | --- |
| P0 | R09,R10,R12,O06 |
| P1 | R01,R02,R03,R06,R18 |
| P2 | R03,R04,R05,R06 |
| P3 | R17 |
| P4 | R15,R16,U09 |
| P5 | R01,R07,R08,R11,U02 |
| P6 | R05,R09,R13,R14,U11 |
| P7 | R18,R19,U07 |
| P8 | R20 |
| P9 | R21,R22,U10 |
| P10 | R23,R24,R25,R26,U03,U04,U05,U06,U08,U09 |
| P11 | R27,O01 |
| P12 | R14,R27,O02,O03,O04,O05,O06 |

## 8. 확인한 문서와 갱신 시 주의점

다음 루트 문서는 이번 독립 감사에서 전문을 읽고 서로 대조했다.

- `README.md`: 목표·일반 실행·전체 기능·최근 결과. 과거 통과 수와 현재 제품 수락을 구분해야 한다.
- `AGENT_RUNTIME_ROADMAP.md`: 27개 상위 방향과 P0–P12, 시안 재설계·운영 계층. `[x]`가 코드 존재/계약/실환경 중 무엇을 뜻하는지 명시해야 한다.
- `QA_MASTER_AUDIT.md`: 완료 정의, 14개 축, 2026-08-28–30 감사 기록. 이 문서는 기존 기록을 변경하지 않고 추가 매트릭스로 연결한다.
- `PROJECT_REVIEW.md`: 시점별 재평가·구현 수락·잔여 과제. 날짜가 다른 “완료”를 합쳐 최종 완성으로 해석하지 않는다.
- `MODEL_ROUTING.md`: 일반 모델/VRAM 정책. 디자인 전용 역할 구분은 GAP-10.
- `MOCKUP_GENERATION.md`: 생성 backend·검증·한계. 레거시 auto/Pillow 설명과 현재 Qt layer graph/검수 경계를 버전별 구분해야 한다.
- `GESTURE_INTERFACE.md`: 한/두 손·민감도·카메라·안전 정책. 과거 기본 비활성 문서와 GAP-09 대조.
- `OBSIDIAN_KNOWLEDGE.md`: 원문/승인 기억/증분·역동기화·그래프. 저장과 검색, 실제 답변 사용을 별도 검증한다.
- `PLUGIN_ROADMAP.md`: 기존/계획 서비스 범위. 미구현 서비스·조회-only 범위를 GAP-01/12로 남긴다.
- `P12_PRODUCTIZATION.md`, `DEPLOYMENT.md`: bootstrap 엔진과 배포 자산/설치본은 다른 단계. GAP-03/04/13.
- `POST_TRAINING.md`: 선택적 학습과 승인 사례 기록을 분리. 사용자 강화학습 제외 결정을 우선한다.

보조적으로 `Agent 인수인계.txt`의 앞부분/규칙 관련 검색, `Javis 로드맵.txt`와 `Level2 리팩토링 10단계.txt`의 제목·상태 색인을 확인했다. 보조 문서 전체를 읽었다고 주장하지 않는다. 인수인계서·`pytest.ini`·기존 `QA_MASTER_AUDIT.md`는 이 감사에서 수정하지 않는다.

## 9. 다음 작업의 닫힘 조건

1. **허위 수락 가능성을 먼저 제거한다:** GAP-01/02/04/06. 정상 응답이나 정상 형식의 산출물이 있어도 요청 결과를 증명하지 못하면 미검증으로 남겨야 한다.
2. **범용 회귀 검증을 강화한다:** 대화/복합 요청/승인·복구/전문가 현재 revision/수락 우회의 다양한 부정 사례, 테스트 수집 누락(GAP-11), 코드-only shell 탐지. 이번 사용자 사례만 hardcode하지 않는다.
3. **실제 실행 경로의 공백을 구현한다:** Photoshop 편집, 필요한 생성형 영역 편집, OS worker 격리, 실제 배포 manifest. 각각 구현 범위를 확정하고 수락 기준을 먼저 정한다.
4. **계정/장치/설치 환경에서 검증한다:** 실제 앱을 여는 것과 업무 완료를 구분한다. 사용자에게 이미 승인받은 시험도 수신자·내용·범위·중복 방지 조건은 계속 지킨다.
5. **사람 평가가 필요한 품질을 평가한다:** 대화·전문가 결과·시안·음성·제스처는 자동 테스트만으로 “완벽” 판정하지 않는다. 사용자가 기대하는 사용 사례와 rubric을 연결한다.

사용자 선택이 실제로 필요한 항목은 배포 호스팅/배포할 자산, 신규 원격 서비스·사용 계정, Adobe/Office 등 사용 가능한 정식 앱, 미구현 계획 서비스의 실제 업무 범위다. 이미 구현된 문제의 원인 분석과 안전한 로컬 회귀 작업까지 이 선택을 기다릴 필요는 없다. “모든 것 승인”은 아무 계정이나 쓰거나 결제/메시지를 임의로 수행할 권한으로 확대하지 않는다.

최종 완성 판정은 **관련 구현 공백이 닫히고, 자동 검사와 실제 환경 증거가 해당 빌드에 연결되고, 사람 평가가 필요한 항목을 사용자가 수락한 뒤**에만 한다.

## 10. 감사 이후 수정 추적: Photoshop 편집 계약

2026-08-31 후속 구현. 위 GAP 표는 수정 전 발견을 보존한다. 아래 조치는 GAP-01과 GAP-02의 **Photoshop 범위**에 대한 코드·자동 검증이며, 전체 제품이나 실제 Photoshop 수락을 완료했다는 뜻은 아니다.

- **재현한 결함:** 변경하지 않은 기존 PNG를 `image_document`로, “원본만 열었다”는 `photoshop_document` evidence와 성공 상태를 반환하면 기존 `_review_execution`은 두 편집 수락 기준을 모두 참으로 판정했다. 픽셀 편집·새 파일 저장은 수행하지 않은 재현이었다.
- **실행 경로:** `core/photoshop_runtime.py`와 `plugins/photoshop.py`의 `photoshop_edit_document`는 엄격한 타입 작업(`resize`, `crop`, 90/180/270도 `rotate`, `text`)만 받는다. 선택한 저장 원본→분리 복제→순서별 편집→임시 PNG 픽셀 검증→새 PNG/PSD 저장→저장 파일 재열기·픽셀 비교→원본 해시/원본 문서 상태 확인→세션 복원→덮어쓰기 없는 게시 순서다. 원본과 기존 출력에는 저장하지 않는다.
- **기준별 증거:** `core/specialist_workspaces.py`는 `photoshop_saved_copy`, `photoshop_visual_change`, `photoshop_source_preserved` 검증기를 선언한다. `core/specialist_team.py`는 입력 역할 artifact를 결과에서 제외하고 각 기준을 독립 검증한다. 파일 열기/일반 evidence만으로 통과할 수 없다. 편집 도구가 등록되지 않은 열기-only 레지스트리도 편집 준비 완료가 되지 않는다.
- **명령과 지원 범위:** 열기 intent에서 일반 “포토샵에서” 힌트를 제거해 편집 명령을 문서 열기로 삼키지 않게 했다. 자연어 편집은 실제 편집 도구 schema가 제공되는 planner로 전달한다. 글꼴은 `photoshop_list_fonts`가 반환한 실제 PostScriptName/유일한 이름을 사용하며 없거나 모호하면 묻는다. 임의 JavaScript·액션·원본 덮어쓰기·무근거 글꼴 대체는 제공하지 않는다.
- **실행한 자동 검증:** `.venv\\Scripts\\python.exe -m pytest -q -o python_files=test_*.py test_photoshop_runtime.py test_specialist_workspaces.py test_capability_inventory.py` → **138 passed (21.98s)**. 관련 5개 Python 파일의 `py_compile`도 통과했다. 테스트 backend는 명시적인 fake COM이며 픽셀 fixture/oracle만 Pillow를 사용한다. production 편집의 자동 Pillow 대체 경로는 없다.
- **구체적 회귀:** PNG/PSD 출력과 JPEG/BMP/TIFF/PSD 입력, 6개 회전 방향, 자르기 영역 픽셀, 텍스트 글꼴·색·크기·위치, 순차 복합 작업, 열린 원본/활성 문서/설정 보존, 저장 실패·손상·재열기 불일치·PSD 레이어 손실·폰트 대체·무변경·경계 초과·비정상 schema·원본 변조·출력 경합·가짜 evidence·수락 기준 불일치·네이티브 COM unavailable을 검증했다. `test_specialist_workspaces.py::test_photoshop_opening_original_cannot_satisfy_edit_contract`는 초기 허위 완료 재현을 차단한다.
- **남은 A/L/U:** Photoshop 외 전문가의 일반 evidence 수락(GAP-02 나머지), 네이티브 COM hard timeout(GAP-05)은 아직 별도 작업이다. 실제 설치된 Photoshop/pywin32/글꼴 조합에서 복제·픽셀 단위·프로필·PNG/PSD 저장 및 reopen을 확인하는 L과 결과 품질 U는 **미실행**이다. 테스트 PSD는 fake COM 저장/재열기 fixture이므로 진짜 PSD 호환성을 증명하지 않는다. 실제 사용자 문서나 Photoshop 앱은 이번 검증에서 조작하지 않았다.

## 11. 후속 재감사: 실제 남은 기능과 필요한 결정

2026-08-31, 위 초기 발견 이후 공유 작업 트리의 코드·문서를 다시 대조했다. **이 절은 읽기 전용 감사이며 새 모델 설치, 외부 API 호출, 로그인, 메시지 전송, 실제 Photoshop/Office 조작은 수행하지 않았다.** 코드 존재와 테스트의 증명 범위를 구별하며, 메인 작업자가 병행 수정하는 부분은 최종 회귀 결과로 다시 연결해야 한다.

### 11.1 이미 바뀐 부분을 미구현으로 남기지 않기

| 연결 | 재감사에서 확인한 변경 | 아직 완료로 확대하면 안 되는 범위 |
| --- | --- | --- |
| GAP-01/02, U10 | Photoshop 타입 편집·비파괴 복사 저장·3개 기준별 증거 검증은 10절대로 구현됐다. `test_photoshop_runtime.py`, `test_specialist_workspaces.py::test_photoshop_opening_original_cannot_satisfy_edit_contract`가 입력 파일 열기만으로 편집 완료가 되는 결함을 다룬다. | 실제 Adobe 앱·저장 형식·색상 프로필·폰트 호환 수락은 별도다. Photoshop 외 작업공간은 `acceptance_verifiers`가 없고 `core/specialist_team.py::_review_execution`의 일반 분기가 모든 기준에 같은 `not failures`를 넣는다. 문서 내용·분석 정확도·디자인 품질을 각각 검증한 팀으로 과대 표시할 수 없다. |
| GAP-04, O06 | `core/acceptance_runtime.py::_validate_evidence`, `record`, `snapshot`에 로컬 증거 SHA-256/크기 봉인, 매 조회 재검증, 손상 원장·미래 시각·무원자 저장 방어가 있다. `test_acceptance_runtime.py::test_evidence_deletion_or_change_invalidates_in_memory_and_reloaded_pass`, `test_legacy_unsealed_local_evidence_requires_fresh_attestation`가 관련 증거다. | 파일 무결성과 **보고서 내용의 충분함**은 다르다. 케이스별 실행 관측값·빌드 revision·산출물과 주장 일치 여부의 의미 검증은 아직 없다. 새로 `OK` 파일의 hash를 기록하는 것만으로 설치 E2E를 증명할 수 없다. |
| GAP-05, R11 | `core/plugin_worker.py`와 Registry의 `execution_isolation="process"` 경로가 생겼다. 현재 실제 opt-in은 `plugins/office_editing.py`의 `office_render_visual_qa`다. `test_plugin_process_isolation.py::test_real_deadline_kills_worker_before_late_file_write`, `test_office_only_stateless_renderer_is_process_isolated`가 범위를 드러낸다. | 모든 네이티브 도구를 process로 옮긴 것은 아니다. Photoshop 편집과 나머지 Office/HWP 상태 보유 도구는 기본 thread 경로다. 실행 중인 COM 호출·공유 앱의 안전한 종료/복구 문제는 남는다. 사용자 문서가 열린 앱 프로세스를 일괄 강제 종료하는 것은 해결책이 아니다. |
| GAP-06, U03/U05 | `core/mockup_subject_runtime.py`는 실패·변경 없음·유효 분할을 구별하고, `core/mockup_subject_assets.py`와 `core/mockup_design.py`의 `prepared_subject_sources` 호출이 실제 렌더러 입력을 연결한다. `test_mockup_subject_integrity.py::test_backend_failures_never_become_an_opaque_success`, `test_mockup_subject_pipeline.py::test_render_pipeline_uses_cutouts_but_persists_only_original_sources`가 있다. | 실제 BiRefNet의 머리카락·유사색 배경·다중 인물 품질은 합성 alpha fixture로 확정할 수 없다. config/가중치가 존재한다는 것과 모델을 로드해 처리했다는 것은 다르다. 요청 충족·fallback 증거와 실제 사진 사람 수락이 필요하다. |
| U03/U05, R10 | `test_mockup_review_commit_contract.py`는 자동 교정 이후 재검증, 최신 편집 지시, 필수 문구, 저장본 변조/다른 revision, 실패 후 PNG/SVG 잔류를 재현한다. 메인 작업에서 검수·저장 경계 수정이 진행 중이다. | 테스트 추가만으로 해결로 표시하지 않는다. 최종 소스의 테스트 실행 결과 및 실제 연속 편집 수락을 연결해야 한다. 이 절에서는 병행 중인 최종 통과 수를 임의로 고정하지 않는다. |
| GAP-11, O04 | 현재 `pytest.ini`에 `test_packaging_entrypoint.py`, Photoshop·process worker·시안 교정 계약 테스트가 등록되어 있다. 초기의 배포 테스트 수집 누락은 해당 등록 범위에서 해소됐다. | `manual_acceptance/` 디렉터리는 여전히 없다. 다른 수동 하네스로 대체한다면 진입점/시간 제한/보고서와 연결해야 하며, 디렉터리 하나를 만드는 것으로 닫지 않는다. |

### 11.2 코드 구현이 더 필요한 핵심 공백

다음 항목은 계정이나 사용자의 선택만 기다리면 저절로 완성되는 기능이 아니다. 안전한 로컬 구현·모의 회귀는 지금 진행할 수 있다.

| ID / 연결 | 현재 구현의 정확한 한계 | 필요한 실제 구현·수락 조건 |
| --- | --- | --- |
| GAP-03 / O04 | `packaging/bootstrap_manifest.json`은 여전히 `assets: []`인 미게시 템플릿이다. `BootstrapInstaller.install`은 빈 역할 집합과 잘못된 SHA-256을 거부하지만, 요청한 여러 역할 중 **일부만** manifest에 있어도 설치 성공을 반환할 수 있다. 파일별 `os.replace`이므로 뒤 자산 실패 시 앞 자산 설치를 되돌리지 않는다. `P12_PRODUCTIZATION.md`의 역할별 원자 설치 설명은 전체 bundle transaction으로 해석하면 과장이다. | 실제 배포 자산/정확한 버전/호스팅/검증 hash를 채우고, 모든 요청 역할·의존성·경로를 다운로드 전에 검증한다. 전체 staging→검증→commit 또는 명확한 부분 설치/복구 원장을 구현한다. `test_p12_productization.py::test_bootstrap_checksum_and_role_selection`, `test_bootstrap_rejects_empty_or_unpublished_role`는 다운로드 엔진의 좁은 근거이며 실제 배포 묶음 수락이 아니다. |
| GAP-07 / U03/U04 | `core/mockup_generation.py::SDXLGenerationBackend`는 실제 `AutoPipelineForText2Image` 기반 SDXL Turbo 배경 생성을 호출한다. 그러나 `reference_paths` 인자는 사용되지 않으며 mask/현재 픽셀을 받는 inpainting, ControlNet, FLUX 실행 경로는 확인되지 않는다. SD1.5의 IP-Adapter 참조 conditioning과 동일한 기능이 아니다. | 텍스트→새 배경 생성과 참조/영역 기반 사진 편집을 다른 capability로 선언한다. 후자를 지원하려면 원본·mask·보존 영역·참고 이미지 조건부 입력, 실행, 결과 차이/피사체 보존 검증을 실제로 연결한다. 지원하지 않는 포즈/국소 편집은 레이어 배치나 빈 JSON 변경으로 성공 처리하지 않는다. |
| GAP-14 / GAP-07,U11 | **SDXL 설치·선택·자연어 라우팅의 끝단이 연결되지 않는다.** `ui/specialist_workspaces.py`에는 `generative_sdxl` 선택 항목이 있지만 `_prepare_models_worker`는 항상 SD1.5의 `prepare_generation_models`를 부르고 `_on_model_status`도 SD1.5/IP-Adapter 상태만 표시한다. `MockupDesignRuntime.prepare_sdxl_model`은 정의 외 호출부가 없고 `plugins/mockup_design.py`의 `mockup_render.backend` enum은 `auto/generative/local`뿐이다. `route_mockup_request`가 자연어 인페인팅을 `diffusion`으로 분류해도 `render`는 `backend` 명시값만 보고 생성 모델을 실행한다. | 선택한 backend와 준비 버튼·진단·Tool schema·실행을 하나의 capability 상태로 연결한다. 라우팅 결과가 실행 계약이 되거나, 아직 지원하지 않는 작업은 실행 전에 정확히 거부해야 한다. `test_mockup_architecture.py::test_capability_router_activates_only_required_heavy_stage`는 분류값만 검사하며 생성 모델 호출을 증명하지 않는다. 미설치/오프라인/로드 실패/자동 모드/GUI와 대화 도구의 같은 요청을 검증한다. |
| GAP-08 / U03,R17 | `StyleTrainingDataset.collect`는 승인 이미지 manifest를 만들고 20개 이상이면 `ready`를 표시한다. `training_command`는 `train_dreambooth_lora_sdxl.py --dataset_manifest ...`라는 argv만 반환한다. 해당 trainer 파일 및 그 인자를 받는 실행 adapter, 학습 job/체크포인트 검증, adapter 로드 경로는 저장소에서 확인되지 않는다. `prepare_style_training_dataset`도 GUI/Plugin 호출부가 없다. | 현재 명칭은 **학습 데이터 준비**로 한정한다. 실제 LoRA를 제공하려면 dataset format→호환 trainer→자원/취소·재개→checkpoint→추론 로드→held-out 품질·퇴행 검증→명시적 배포를 모두 잇는다. 20개 파일이나 manifest의 `ready`는 학습 성공/미감 학습의 증거가 아니다. 강화학습은 제외하며, LoRA 학습 역시 데이터·자원 조건 없이 자동 시작하지 않는다. |
| GAP-15 / R21,U11 | **클라우드 동기화는 파일 본문/RAG 동기화가 아니다.** `ProviderApi.sync`는 Drive 목록 1페이지, OneDrive root delta 1페이지, Notion search 1페이지만 읽는다. `RemoteCatalogStore.replace`는 이 응답으로 provider/account의 기존 카탈로그 전체를 교체한다. 페이지가 더 있어도 완료 범위가 표시되지 않고, 본문 다운로드/임베딩 연결도 없다. | bounded 조회라면 부분 결과·cursor·범위를 명시하고 기존 전체 목록을 파괴하지 않는다. 전체/증분 동기화를 약속하려면 pagination/delta token·삭제 항목·중단 재개·권한 회수와 본문 수집/RAG 경로를 구현·검증한다. Drive/OneDrive 파일 업로드나 Notion 페이지 편집을 지원한다고 표시하지 않는다. |
| GAP-16 / R10,R21,U11 | `ProviderApi.apply`는 Gmail/Outlook/Calendar/Teams에서 생성/전송 후 GET을 호출하지만 반환 body의 원격 ID·대상·내용·전송 상태를 대조하지 않는다. Slack은 ts 일치까지 검사한다. `communication_read_summary`는 메시지마다 앞 160글자를 잘라 배열로 반환하며 실제 종합 요약·결정/할 일 추출 단계는 없다. | HTTP 성공/ID 문자열을 수신·내용 검증으로 확대하지 않는다. 작업별 read-back 검증과 불확실 상태를 추가하고 ID만 있는 잘못된 body/초안 잔존/다른 대상·본문을 거부하는 테스트가 필요하다. 요약은 출처 ID를 보존한 실제 요약기로 연결하거나 UI에서 메시지 미리보기로 정확히 이름 붙인다. |
| GAP-17 / R21,U11 | `OAuthCoordinator.begin/complete`는 PKCE URL·토큰 교환·DPAPI 저장·갱신을 제공한다. 그러나 URL 발급 후 사용자가 `redirect_uri`, `state`, `code`를 별도로 전달해야 하며, 코드 검색에서 연결 UI/loopback callback 수신/취소·동의 실패 복귀 경로는 확인되지 않았다. | 계정 연결을 일반 사용자가 완료할 UI와 안전한 callback transaction을 구현한다. provider/계정/승인 범위를 결합하고 state 만료·중복 callback·취소·오류·앱 재시작을 검증한다. 계정 선택/실로그인은 사용자에게 필요하지만 연결 UX 구현 자체를 사용자 설정 탓으로 남기지 않는다. |
| GAP-12 / U11 | `PLUGIN_ROADMAP.md`의 IDE CLI 진단, GitHub Issue/PR/CI, Docker 격리, 연락처/할 일/지도·교통, 범용 브라우저 업로드·게시·결제는 해당 전용 업무 경로와 수락 근거가 확인되지 않는다. 앱 실행/로컬 Git/웹 검색/예약 알림은 이 업무들의 완료 근거가 아니다. | 각 실제 업무를 지원표에 분리하고 입력→도구→승인→실행→관찰→검증을 구현한다. 이미 사용자에게 제공된 기능은 빈 버튼으로 남기지 않는다. 새 외부 계정·게시 대상·비용이 필요한 부분만 11.5의 선택으로 분리한다. |
| GAP-18 / 범위 정책 | `POST_TRAINING.md`는 아직 Verifier-RL을 권장 실험으로 서술하며 `core/learning_runtime.py::export_training_data`는 `verifier_rl.jsonl` 후보도 내보낸다. manifest의 `automatic_training=False`이고 실제 강화학습 실행 경로는 확인되지 않았다. | 사용자의 **강화학습 적용 안 함** 결정을 현재 운영 정책으로 명시한다. 과거 후보 데이터 포맷을 보존할지와 무관하게 강화학습을 미완성 기능으로 계산하거나 다음 필수 작업으로 제안하지 않는다. 문서의 권장 실험과 실제 활성 기능을 구분한다. |

추가로 `VisualStyleIndex.embed`는 로컬 CLIP 실행이 실패하면 `visual-descriptor-v1`으로 fallback한다. 두 backend를 DB에 구분해 저장하는 것은 구현되어 있다. 다만 설정 파일 존재만으로 현재 실행에서 CLIP을 사용했다고 말할 수 없고, backend 전환 시 벡터 차원 차이로 과거 항목이 검색에서 제외될 수 있다. 실제 스타일 검색의 재색인·적합도와 사용자 미감 수락은 별도 평가다.

### 11.3 실제 서비스 연결과 구현 존재를 분리한 지원표

이 감사는 자격증명 값이나 실제 계정 목록을 열지 않았고 원격 호출을 하지 않았다. 따라서 계정이 연결되지 않았다고 단정하지도, 과거 문서의 “연결됨”을 현재 계정 수락으로 승격하지도 않는다.

| 영역 | 실제 실행 코드 | 자동 근거가 증명하는 범위 | 제품 수락에 남은 것 |
| --- | --- | --- | --- |
| Google/Microsoft OAuth | `OAuthCoordinator.begin/complete/access_token`의 인증 URL·HTTP 토큰 교환·갱신, `SecureTokenVault` DPAPI 저장 | `test_p9_remote_runtime.py::test_oauth_pkce_complete_and_secret_free_status`, `test_expired_oauth_token_is_refreshed`는 fake Session/Vault를 사용한다. DPAPI round-trip도 시험 token의 로컬 암호화 증거다. | 연결 UX(GAP-17), 실제 선택 계정의 동의·갱신·scope/계정 일치, 로그아웃·권한 회수. |
| Gmail/Outlook·원격 일정 | `ProviderApi.apply`와 `read_calendar`, 초안/반영 Plugin Tool | `test_google_calendar_and_slack_requery_remote_ids`는 정해진 ID를 돌려주는 fake HTTP 응답의 호출 순서를 검사한다. `test_plugin_keeps_draft_and_apply_separate`도 apply를 대체한다. | GAP-16 read-back 보강 후 실제 계정/대상·시간·내용 확인. SMTP의 서버 접수와 수신자 도착도 구분한다. |
| Slack/Teams | 메시지 조회·전송과 원격 ID 재조회 코드 존재 | `test_communication_summary_preserves_message_ids`는 snippet 배열과 ID 보존만 확인한다. | 실제 채널/권한 검증, 전달과 본문 확인, 종합 요약 품질. |
| Drive/OneDrive/Notion | HTTP 메타데이터 목록→SQLite 카탈로그 | `test_remote_draft_and_catalog_are_persisted`는 로컬 fake 항목의 영속성이다. | GAP-15의 부분/전체 동기화와 본문/RAG 범위 구분, 실계정 전체 페이지·삭제/변경 동작. |
| Obsidian | `core/obsidian_vault.py`의 로컬 Markdown vault, `plugins/obsidian.py`, `core/knowledge_graph.py` | 로컬 파일/링크/증분 동기화 코드가 근거다. | Obsidian REST API나 외부 플러그인이 자동 정리하는 시스템이라고 설명하지 않는다. 사용자가 실제 vault에서 편집한 내용의 역동기화·그래프·답변 사용을 실환경에서 확인한다. |
| Photoshop / Office / HWP | 타입 편집/복사 저장 또는 네이티브 COM adapter 존재. Office의 일부 시각 QA는 별도 process worker 사용 | fake COM·합성 파일·독립 worker 테스트는 계약과 실패 경계를 증명한다. | 정식 설치 앱과 실제 형식/폰트/색 프로필·reopen 수락. 미설치일 때 unavailable이 정확해야 한다. |
| 카카오톡 | `plugins/desktop_messaging.py`와 로컬 UI 자동화 실행·수신자/창 검증 경로 | 모의 창·화면·응답 검증은 실제 메시지 전달이 아니다. | 지정 수신자 **형택**에게 허용된 테스트의 중복 여부·정확 내용·실제 도착 확인. 수신자 선택은 이미 사용자에게 받았으므로 다시 물을 필요가 없다. |

### 11.4 읽기 전용으로 확인한 현재 근거 상태

- `packaging/bootstrap_manifest.json`: `assets`는 빈 배열. 미게시 bootstrap을 성공으로 표시하지 않는 것은 맞지만, 배포 내용은 아직 없다.
- 로컬 경로 `data/models/mockup_generation/sdxl-turbo/model_index.json`은 없었다. CLIP와 BiRefNet의 `config.json`은 있었다. 이 확인은 **설정 파일 유무만**이며 모델 실행·가중치 무결성·GPU 호환·추론 품질을 검사하지 않았다.
- `AcceptanceRuntime().snapshot()`을 읽기 전용 실행한 결과 유효 상태는 `packaged_runtime=failed`, 나머지 11개 `not_run`이었다. 원문에는 `packaged_runtime=passed`가 남아 있지만, 현재 검증기는 **“로컬 증거 무결성 기록이 없어 다시 수락 확인해야 합니다.”**로 거부했다. 원장·증거 파일은 변경하지 않았다.
- 이 상태는 원장에 유효하게 연결된 수락 근거의 상태이지, 전체 기능 구현 진행률 또는 과거에 어떠한 실검사도 없었다는 뜻이 아니다. 원장 밖 실검사 보고서는 케이스·빌드·산출물과 연결해야 한다.
- 이번 후속 문서 감사에서는 새 테스트를 실행하지 않았다. 위 테스트 이름은 읽어 대조한 증거 위치다. 직전 Photoshop·시안 합성 QA 실행 기록은 각각 10절과 해당 작업 보고에 있으며 실제 Adobe/VLM/장치 수락으로 대체하지 않는다.

### 11.5 정말 사용자 결정이 필요한 것과 필요하지 않은 것

| 결정 ID | 사용자에게 필요한 정보/결정 | 결정 없이 먼저 진행할 수 있는 일 |
| --- | --- | --- |
| D01 / 배포 | **현재 단계 결정 완료:** 현재 PC의 소스·로컬 런타임 완성도를 먼저 높인다. 설치 파일·모델 번들·호스팅·공개 배포는 제품이 더 완성된 뒤 사용자가 별도로 시작한다. 재배포 권한 없는 유료 프로그램·모델을 임의로 묶지 않는다. | 현재 작업에서는 배포 산출물을 새로 만들지 않는다. 기존 manifest validator·역할 누락·transaction·복구 회귀는 안전망으로 유지하되, 빈 manifest를 배포 완료로 표시하지 않는다. GAP-03/O04는 **현재 로컬 완료 게이트가 아닌 향후 배포 마일스톤**으로 남긴다. |
| D02 / 외부 서비스 | 실제로 사용할 Google/Microsoft/Slack/Teams/Notion 등의 provider·계정·조직/채널·캘린더. 사용자 로그인/동의, 발신 계정과 시험 대상은 agent가 임의 선택할 수 없다. 토큰 원문은 대화에 붙여 달라고 요청하지 않는다. | OAuth 연결 UX, scoped permission, schema/read-back, pagination/불확실 재시도 방지, fake 서버 회귀. 서비스별 모두 설치할지 반복 질문하기보다 실제 업무/계정이 필요한 시점에 묻는다. |
| D03 / 상용 앱 | 필요한 기능을 실제 Photoshop/Office/HWP에서 수락할 때 현재 사용 가능한 라이선스·시험 앱/문서 환경. 구매나 라이선스 우회는 승인을 추론할 수 없다. | read-only 설치 진단, typed unavailable, fake COM/비파괴 fixture 검증, process/자원 회수 안전성. 설치되어 있다면 승인된 범위의 합성 시험 문서로 검증하며 기존 사용자 문서를 건드리지 않는다. |
| D04 / 생성·학습의 자원 범위 | 로컬 VRAM/처리시간으로 충족할 수 없는 자유 생성이나 실제 LoRA 실험에 외부 GPU/API·추가 비용·이미지 업로드가 필요해질 경우 그 사용 여부와 예산/자료 범위. LoRA용 승인 데이터의 사용 권리·학습/평가 분리도 확인해야 한다. | SDXL GUI/Tool 준비 경로, 정확한 미지원 분류, 로컬 mask/adapter contract, trainer 호환성·checkpoint validator·비실행 회귀. 단순한 설치 버튼 연결 오류를 이 결정 때문에 미루지 않는다. 강화학습은 이미 제외됐으므로 다시 제안하지 않는다. |
| D05 / 아직 특정되지 않은 계획 서비스 | 연락처/할 일/지도·교통/IDE·GitHub CI 등에서 **어느 실제 업무와 기존 서비스가 기준인지**. 로컬 할 일과 원격 Todoist/Google Tasks, Git 저장과 원격 PR 발행을 임의로 같은 요구사항으로 볼 수 없다. | 미구현 지원표/진입 경로 감사, 안전한 로컬 업무, 재사용 가능한 connector 계약·권한·검증기. 새 외부 게시/구매·조직 변경만 필요한 범위를 확정한 뒤 진행한다. |

다음은 다시 선택을 받을 문제가 아니다.

- **강화학습 제외**는 이미 결정됐다. 필수 잔여 작업으로 되살리지 않는다.
- **카메라 기본 켜짐**, 한 손 navigation/두 손 zoom, 민감도·매핑 UI는 이미 정해졌다. `core/assistant_settings.py`의 `gesture_camera_enabled=true`, `gesture_command_enabled=false`, `main_qt.py::_schedule_gesture_autostart`와 `_set_gesture_camera_sync`가 현재 동작 근거다. `GESTURE_INTERFACE.md`는 이에 맞고 `PLUGIN_ROADMAP.md`의 2026-08-21 절 및 `AGENT_RUNTIME_ROADMAP.md` P10의 “기본 비활성”은 오래된 설명이다. 카메라 영상 처리 활성과 승인/취소 같은 **명령 제스처 opt-in**을 분리해 문서를 바로잡으면 된다. 사용자 선택이 없어 생긴 기능 오류가 아니다.
- **카카오톡 시험 수신자 형택**은 이미 정해졌다. 같은 테스트를 다시 전송할지는 이전 결과의 실제 상태와 현재 시험 목적에 따라 판단해야 하며, 무응답을 무전송으로 추정해 중복 전송하지 않는다.
- 코드 오류·빈 진입 경로·검증 우회·현재 미리보기 상태 보존·외부 상태를 성공으로 과장하는 문제는 사용자에게 설계 책임을 돌리지 않고 수정할 대상이다.

### 11.6 2026-09-01 현재 마일스톤 범위

- 이번 마일스톤은 **현재 사용자 PC에서의 소스 실행과 로컬 기능 완성**이다. 대화·명령·전문가 작업공간·시안 편집·RAG·제스처·로컬 앱 연동과, 연결된 외부 서비스의 안전한 실행 계약을 계속 검증한다.
- 설치 프로그램, 새로운 릴리스 폴더, 모델 재배포 묶음, 호스팅 URL과 공개 배포 문서는 이번 작업에서 생성하지 않는다. 과거 배포 기록은 역사적 증거로만 보존한다.
- 따라서 GAP-03/O04의 미완료는 숨기지 않지만 현재 로컬 기능의 결함과 섞어 진행률을 계산하지 않는다. 사용자가 배포 단계를 다시 시작하면 그 시점의 revision으로 깨끗한 Windows 설치·모델 준비·재시작·복구·제거를 새로 수락한다.

### 11.7 2026-09-01 구현 후 재검증

| 연결 | 이번에 닫은 로컬 계약 | 여전히 별도 수락이 필요한 범위 |
| --- | --- | --- |
| U03/U05, R10 | 현재 화면의 scene/effect revision을 편집 기준으로 묶고, D4 회전·반전 뒤 화면 방향을 source 좌표로 변환한다. 반대 부호 이동, 채도 0과 색상 지시 충돌, 조정 결과를 새 기준 픽셀로 잘못 누적하는 경로를 거부한다. PNG·SVG·JSON은 모두 사전 검증 뒤 bundle transaction으로 게시하고 SVG SHA-256 변조를 차단한다. | 합성 raster/offscreen 계약은 실제 VLM의 미감, 실사진, 설치 글꼴, 장시간 GUI 드래그를 대신하지 않는다. bundle은 파일별 원자 교체와 역순 rollback이며 전원 차단 순간의 OS 전역 transaction은 아니다. |
| GAP-14 / GAP-07 | SDXL 준비 상태가 정식 SDXL Turbo scheduler와 FP16 단일/샤드 snapshot을 인식하고, LFS 포인터·잘린 safetensors·잘못된 설정/offset을 거부한다. `files_ready`, `packages_ready`, `cuda`, `model_loaded`, `package_imports_verified`, `weight_values_verified`, `checksums_verified`, `source_revision_verified`, `inference_verified`를 분리해 Tool 응답까지 보존한다. | 현재 PC에 SDXL snapshot이 없어 실제 tensor 값·upstream checksum·CUDA 추론·시각 품질은 미검증이다. 인페인팅·ControlNet·FLUX·참조 픽셀 조건은 계속 미지원으로 선언한다. |
| GAP-16 / R10,R21,U11 | 원격 적용 원장이 `draft→applying→applied/uncertain`을 원자 claim으로 관리한다. Gmail·Google/Outlook Calendar·Outlook Mail·Slack·Teams는 원격 ID뿐 아니라 수신자/제목/본문/시간/채널을 재조회한 receipt만 성공한다. 불확실 결과와 ID-only adapter는 같은 작업 ID로 자동 재전송하지 않는다. | fake provider 회귀이며 실제 계정 수락은 미실행이다. Gmail 첨부 hash, provider idempotency key, crash 뒤 `applying/uncertain` 자동 reconciliation은 남았다. 확인 전에는 새 draft로 재전송하지 않는다. |
| GAP-18 / U01 | `POST_TRAINING.md`의 현재 운영 정책은 강화학습 제외를 첫머리에 고정한다. `verifier_rl.jsonl`은 과거 포맷 호환 산출물일 뿐 활성 학습이나 필수 잔여 기능이 아니며, 결정론적 verifier는 실행 결과 QA에만 사용한다. | 선택적 SFT/DPO/LoRA도 자동 실행하지 않는다. 사용자가 향후 별도 요청하고 데이터 권리·자원·held-out 평가를 합의한 경우에만 독립 실험으로 취급한다. |

집중 자동 회귀는 시안·SDXL·원격 계약을 묶어 **245 passed, 4 skipped**였다. skip 4건은 현재 Windows 계정의 native symlink 생성 권한 부재다. 실제 모델 다운로드·GPU 추론·외부 계정 전송·배포 산출물 제작은 이 실행에서 수행하지 않았다.

이 변경을 `pytest.ini`의 정규 수집 목록에 포함한 중간 전체 회귀는 **1869 passed, 17 skipped, 4 deselected**였다. 후속 교정까지 포함한 최종 결과는 11.8에 기록한다. skipped/deselected 항목은 외부 계정·장치·Windows 권한·integration 표식처럼 별도 환경이 필요한 계약이며, 이를 통과로 계산하지 않는다.

실제 손 동작, 마이크 발화, 스피커 청취, 시안 미감/대화 자연스러움의 평가는 **사용자 선택**과 별개의 **L/U 수락 참여**다. 카메라를 여는 데 성공하거나 합성 landmark가 통과했다고 완료로 바꾸지 않는다. 현재 원장 기준으로 이 수락들이 남아 있고, 모델 차이를 제외한 Codex 수준 동등성을 입증할 공통 과제·held-out 대화·기준별 사람 평가도 아직 연결되지 않았다.

### 11.8 2026-09-07 후속 교정과 최종 자동 검증

| 범위 | 수정 및 검증 근거 | 남은 수락 범위 |
| --- | --- | --- |
| 현재 미리보기·문구별 수정 | 현재 scene의 문구로 대상 span을 해석하고, 따옴표 없는 대상·복합 지시·문구 교체를 검증한다. 다른 문구/배치/장식의 변경을 차단하며 Pillow fallback에서도 각 span의 글꼴·색상·크기·굵기를 그린다. `test_mockup_text_style_runs.py`, `test_mockup_preview_history.py`에 회귀가 있다. | 실제 사진/설치 글꼴/VLM 해석과 사용자 미감. |
| Office 결과 묶음 | PDF와 모든 페이지 PNG를 `commit_artifact_bundle`로 함께 사전 검증하고 중간 교체 실패는 rollback한다. 게시 경계의 취소·손상 파일·범위 밖 결과도 검사한다. `test_plugin_process_isolation.py`의 실제 별도 worker와 finalizer 회귀가 근거다. | 설치 Office/HWP COM의 실제 문서. 전원 차단/프로세스 강제 종료 시 전체 묶음의 원자성은 보장하지 않는다. |
| 카카오톡 입력·포커스 | 검색 요소 identity·UIA 입력 read-back을 유지하고 선택/Enter에 foreground·동일 요소·키보드 포커스 검사를 적용한다. 전역 Ctrl+A와 무검증 첫 결과 Enter를 제거한다. `test_desktop_messaging_uia_hardening.py`가 모의 창 전환과 요소 교체를 검증한다. | 형택에게 실제 전달된 내용과 중복 여부. 이번 자동 회귀는 메시지를 보내지 않았다. |
| 수락 증거·비동기 UI | 원격 증거는 문자열 ID가 아닌 검증 상태/방법/공급자/작업/대상/시각/hash 구조를 요구한다. 등록 시각과 실제 확인 시각 중 이른 시각부터 만료를 계산해 재등록으로 유효기간이 갱신되지 않는다. 잘못된 render/adjustment 응답에도 busy를 복구하고 비활성 요청 응답은 무시한다. `test_acceptance_runtime.py`, `test_mockup_preview_history.py`가 근거다. | 구조화 증거만으로 실제 계정·장치·사람 수락이 생기지 않는다. 해당 실행의 증거를 별도 기록해야 한다. |
| GAP-11 테스트 수집 | 레거시 12개 진단을 import-safe로 바꾸고 부작용 없는 자동 테스트 2개만 기본 수집에 추가했다. 나머지는 명시적 CLI 실행으로 분리했다. 세부 내용은 `QA_TEST_CLASSIFICATION.md`에 기록했다. | 카메라·TTS·Ollama 등 실제 진단의 명시적 실행. |

원격 증거 만료 교정까지 포함해 다시 실행한 최종 전체 자동 회귀는 **1907 passed, 17 skipped, 4 deselected (203.32s)**다. `compileall -q core plugins ui main_qt.py scripts`, `pip check`, `git diff --check`를 통과했다. 레거시 12개 파일의 별도 collect-only는 부작용 없는 **2 tests collected**였고 실제 오디오·카메라·네트워크 진단을 시작하지 않았다.

이 결과는 위 자동 계약의 통과다. GAP-15의 전체/증분 클라우드 본문 동기화, GAP-17의 일반 사용자 OAuth 연결 흐름, 아직 구현되지 않은 생성형 편집과 계획 서비스, 실모델·실계정·사람 품질 수락을 완료로 바꾸지 않는다. 배포는 사용자 결정에 따라 이후 마일스톤이며 강화학습은 제외한다.
