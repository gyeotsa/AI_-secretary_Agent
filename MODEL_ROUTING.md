# Jarvis 역할별 로컬 모델 구성

모델 교체만으로 Codex 수준의 성능을 만들 수는 없습니다. 현재 우선순위는 모델
확대보다 Task State, Capability Router, 타입 기반 실행 결과, 검증·복구·재계획,
Coding Agent 폐쇄 루프입니다. 더 강한 모델은 복잡한 계획과 검토 역할에 선택적으로
사용합니다.

## 공급자 선택과 오류 계약

- 기본값은 `LLM_PROVIDER=ollama`이며 API 키 없이 로컬 역할 모델만 사용합니다.
- Anthropic은 향후 선택적 하이브리드 경로입니다. 실제 키와 역할 목록을 사용자가 명시한
  경우에만 `anthropic` 또는 `hybrid`로 활성화합니다. 예제 placeholder 키는 설정된 키로
  간주하지 않습니다.
- timeout, 연결 실패, 빈 응답과 공급자 오류는 typed `ModelCallError`로 전달합니다.
  이 오류는 모델의 자연어 답변, 학습 데이터, 작업 성공 결과로 저장되지 않습니다.
- System Prompt는 고정된 이름·호칭·25자 제한·모드 태그가 아니라 현재 사용자 설정,
  응답 목적, Tool 사용 가능성과 최신성 요구를 조합합니다.

## 현재 활성 구성

| 역할 | 모델 | 목적 | Ollama 유지 시간 |
|---|---|---|---|
| conversation | `qwen2.5:7b-instruct` | 한국어 일반 대화와 캐릭터 말투 | 10분 |
| planning / reasoning / tool_selection | `qwen2.5:7b-instruct` | 계획, 검증, 도구 선택 | 5분 |
| code / document | `qwen2.5-coder:7b-instruct` | 코드·Git·Office 문서 작업 | 5분 |
| vision | `gemma3:4b` | 이미지·화면·카메라 프레임 분석 | 2분 |
| STT | `faster-whisper large-v3` | 한국어 음성 인식 | 기존 구성 |
| TTS | `GPT-SoVITS Anis` | 선택 음성 합성 | 기존 구성 |
| RAG | `bge-m3` | 기억·문서 검색 | 기존 구성 |

`core/model_registry.py`가 역할과 실행 정책의 단일 진실 공급원이다.
`ModelRoleRouter`는 사용자 문장의 특정 키워드를 하드코딩하지 않고 Planner가
선택한 Plugin Registry 도구와 입력 modality를 기준으로 전문 역할을 선택한다.

P10부터 STT와 Vision 추론은 `core/gpu_scheduler.py`의 process-wide admission queue를
공유한다. STT가 Vision보다 높은 우선순위를 가지며 합산 예약량이 예산을 넘으면 대기한다.
기본 예산은 CUDA VRAM의 82%이고 `.env`의 `GPU_VRAM_BUDGET_MB`로 낮출 수 있다.
Ollama는 요청된 모델만 로드하고 역할별 `keep_alive` 이후 자동으로 메모리에서
내린다. RTX 4060 Laptop 8GB에서 여러 생성 모델을 동시에 상주시켜 발생하는
VRAM 부족을 피하기 위한 정책이다.

## NVIDIA 모델 검토 결과

- 사용자가 제공한 NVIDIA AI 링크는 특정 모델이 아니라 NVIDIA AI 솔루션
  포털이다.
- Nemotron 3 Nano Omni는 텍스트·이미지·오디오·비디오를 통합하는 30B-A3B
  모델이지만 전체 모델을 로컬에 보관·구동해야 하므로 현재 8GB GPU의 주 모델로
  채택하지 않았다.
- `nemotron-mini:4b`는 2.7GB로 가볍고 function calling을 지원하지만 공식
  설명상 영어 중심이고 문맥이 4K이므로 한국어 개인 비서의 대화 모델로는
  채택하지 않았다.
- `qwen3:4b`도 다운로드해 실측했지만 현재 설치된 Ollama에서 thinking 비활성
  옵션이 적용되지 않았다. 짧은 요청도 내부 사고가 출력 한도를 소진해 활성
  역할에서 제외했다. 파일은 향후 Ollama 업데이트 후 재평가할 수 있도록
  설치 상태로 유지한다.
- `qwen2.5:7b-instruct`는 이 PC에서 한국어 다중 턴 응답과 역할 보존을 실제
  검증해 대화·추론 모델로 선정했다.
- `gemma3:4b`는 3.3GB급 이미지 입력 모델이며 실제 Jarvis 아이콘 분석 호출을
  검증해 Vision 역할로 선정했다.

## 설정

`.env` 또는 배포 환경에서 다음 값을 교체하면 코드 수정 없이 역할별 모델을
바꿀 수 있다.

```dotenv
LLM_PROVIDER=ollama
OLLAMA_CONVERSATION_MODEL=qwen2.5:7b-instruct
OLLAMA_REASONING_MODEL=qwen2.5:7b-instruct
OLLAMA_CODE_MODEL=qwen2.5-coder:7b-instruct
OLLAMA_VISION_MODEL=gemma3:4b
```

## 검토 자료

- NVIDIA Nemotron: https://developer.nvidia.com/topics/ai/nemotron
- NVIDIA Nemotron 3 Nano Omni: https://developer.nvidia.com/blog/nvidia-nemotron-3-nano-omni-powers-multimodal-agent-reasoning-in-a-single-efficient-open-model/
- Qwen3: https://qwenlm.github.io/blog/qwen3/
- Gemma 3 Ollama: https://ollama.com/library/gemma3/tags
- NVIDIA Nemotron Mini Ollama: https://ollama.com/library/nemotron-mini/tags
