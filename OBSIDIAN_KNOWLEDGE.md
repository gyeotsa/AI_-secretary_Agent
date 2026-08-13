# Obsidian 기반 Anis Knowledge Vault

Obsidian은 지식을 저장하는 Markdown 편집기이고, 실제 분류·검색·링크 탐색·RAG 동기화는
아니스 Runtime이 수행한다. Obsidian을 종료해도 Vault는 일반 파일이므로 읽고 쓸 수 있다.

## 기본 위치와 연결

- 기본 Vault: `data/AnisKnowledgeVault`
- 환경변수: `OBSIDIAN_VAULT_PATH`
- 자연어/Tool: `obsidian_configure_vault`
- 앱 열기: `옵시디언 볼트 열어줘`

Obsidian에서 **Open folder as vault**를 선택해 위 경로를 한 번 열면 된다. 다른 기존 Vault를
사용하려면 `obsidian_configure_vault`에 그 폴더를 지정한다. 이 설정은 로컬
`data/obsidian_settings.json`에 저장되며 Git에는 포함되지 않는다.

## 기억 계층

```text
raw/conversations/       전체 대화 감사 원문, 기본 RAG 제외
wiki/facts/              장기 사실
wiki/preferences/        말투·응답·작업 선호
wiki/settings/           이름·호칭·장치 등 설정
wiki/projects/           프로젝트 결정
wiki/tasks/              반복 작업과 실행 기억
wiki/cases/              성공·실패 사례
wiki/patterns/           3회 이상 반복된 질문·명령 패턴
wiki/chitchat/           반복성이 확인된 잡담 패턴만 승격
wiki/topics/             주제별 Map of Content
wiki/actions/            setting/action/question 등 행동 지도
derived/                 일시적 파생 결과
```

전체 대화는 복구와 재컴파일을 위해 `raw/`에 보관하지만 활성 RAG에는 넣지 않는다. 명시적으로
“기억해”, 설정 변경, 장기 선호, 프로젝트 결정처럼 지속 가치가 있는 내용만 원자적 `wiki/`
페이지가 된다. 일반 발화는 서로 다른 대화에서 같은 패턴이 3회 이상 나타난 경우에만 낮은
중요도의 패턴 페이지로 승격된다.

## 링크와 검색

`[[wikilinks]]`는 그 자체로 모델이 자동 이동하는 기능이 아니다. `obsidian_explore`는 먼저
키워드로 시작 페이지를 찾고, 링크를 파싱한 뒤 BFS로 최대 3단계·50개 노트까지만 탐색한다.
따라서 링크 유무를 확인한 뒤 관련 파일을 여는 일반적인 파일 에이전트 방식보다 명시적이고
재현 가능한 그래프 탐색이다.

`obsidian_sync_to_rag`는 `wiki/`만 Chroma/BGE-M3 RAG에 넣는다. 검색 시에는 벡터+어휘
하이브리드 검색이 넓은 후보를 찾고, Wikilink 탐색이 관계 문맥을 확장한다.

## Knowledge Graph 작업공간

그래프 캔버스는 Obsidian Graph View와 비슷한 감각의 **실시간 force-directed layout**을 사용한다.
각 노드는 서로 밀어내고, 연결된 노드는 링크의 장력으로 모이며, 전체 그래프에는 약한 중심 중력과
감쇠가 적용된다. 초기 배치와 필터 변경 후 자연스럽게 안정화되며 불필요하게 CPU를 계속 사용하지 않는다.

- 노드 드래그: 노드를 직접 옮기면 연결선과 주변 노드가 즉시 반응한다.
- 관계 강조: 노드 위에 마우스를 올리거나 클릭하면 해당 노드와 1단계 이웃 및 링크가 강조된다.
- 탐색: 빈 캔버스 드래그로 이동하고 휠로 확대·축소한다.
- 움직임 제어: `움직임 일시정지/재개`로 물리 애니메이션을 제어한다.
- 다시 배치: 현재 필터 결과를 안정적인 초기 위치로 되돌린 뒤 물리 배치를 다시 계산한다.
- 로컬 우선: WebEngine이나 외부 CDN 없이 Qt Graphics View로 렌더링해 오프라인에서도 동작한다.

메인 UI의 `S` 전문가 목록에서 **Knowledge Graph**를 선택하거나 “지식 그래프 열어줘”,
“그래프 뷰 실행해줘”라고 말하면 독립 작업공간이 열린다. Obsidian 내장 Graph View를 화면
캡처하거나 임베드하지 않고, Vault의 frontmatter와 `[[wikilinks]]`를 아니스가 직접 읽어
Qt 네이티브 캔버스에 그린다. 따라서 인터넷이나 Obsidian 실행 여부와 무관하게 동작한다.

- 노드 색상: 기억 유형(선호·프로젝트·작업·사실·사례·반복 패턴)
- 노드 크기: 중요도와 링크 차수
- 검색/필터: 본문·제목·주제, 기억 유형, 행동, 주제, 최소 중요도
- 전역/국소 보기: 전체 Vault 또는 선택 노드에서 2단계 이내 관계
- 노드 클릭: 원문과 frontmatter 미리보기
- 노드 더블클릭: 해당 Markdown 문서를 Obsidian에서 열기
- 유지보수: RAG 동기화와 깨진 링크·고아·중복 구조 검사
- 자동 갱신: Vault의 `wiki/` 폴더 변경을 감지해 그래프를 다시 구성

원문 대화인 `raw/`는 기본 그래프에서도 제외된다. 한 화면에는 기본 최대 300개 노드만
표시하여 대규모 Vault에서도 UI와 검색 컨텍스트가 무제한으로 커지지 않게 한다.

## 유지보수

- `obsidian_lint`: 깨진 링크, 고아 페이지, 얇은 페이지, 완전 중복 검사
- 원본은 `raw/`, AI가 수정하는 지식은 `wiki/`로 분리
- 민감정보는 자동 저장하지 않음
- 모든 자동 페이지는 `managed_by: anis` frontmatter 보유
- 사용자가 Obsidian에서 직접 수정한 문서는 다음 RAG 동기화 때 반영

## 토큰과 저장 용량

로컬 Ollama 모델에는 상용 API의 토큰당 과금이 없다. 하지만 토큰은 여전히 컨텍스트 길이,
RAM/VRAM, 응답 지연, 전력 소비를 결정한다. 따라서 문서를 무제한 프롬프트에 넣지 않고
검색·신뢰도 필터·제한 깊이 링크 탐색을 사용한다.
