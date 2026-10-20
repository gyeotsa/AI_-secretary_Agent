import pytest

from core.user_question import question_choices


@pytest.mark.parametrize("text,expected", [
    ("어떤 형식으로 저장할까요?\n1. PDF\n2. DOCX", (("PDF", "PDF"), ("DOCX", "DOCX"))),
    ("다음 중 하나를 선택해 주세요.\n1) 한국어\n2) English", (("한국어", "한국어"), ("English", "English"))),
    ("어느 방식을 원하시나요?\n① 기본 설정\n② 직접 지정", (("기본 설정", "기본 설정"), ("직접 지정", "직접 지정"))),
    ("계속할까요?", (("예", "예"), ("아니오", "아니오"))),
    ("이 파일을 저장할까요?", (("예", "예"), ("아니오", "아니오"))),
    ("진행할까요? 예/아니오로 답해 주세요.", (("예", "예"), ("아니오", "아니오"))),
    ("예 또는 아니오로 알려주세요.", (("예", "예"), ("아니오", "아니오"))),
])
def test_explicit_choices_and_confirmations(text, expected):
    assert question_choices(text) == expected


@pytest.mark.parametrize("text", [
    "어느 지역의 날씨를 알아볼까요?",
    "받을 분과 내용을 알려주세요.",
    "어디에 저장할까요?", "언제 실행할까요?", "어떻게 진행할까요?",
    "진행할까요?라는 문장은 질문입니다.",
    "상태: 예/아니오",
    "아래 코드 출력은 무엇인가요?\n1. print('a')\n2. print('b')",
    "출력 형식을 선택하세요.\n```python\n1. print('a')\n2. print('b')\n```",
    "선택해 주세요.\n1. 하나뿐인 목록",
    "선택해 주세요.\n1. 첫 항목\n3. 셋째 항목",
    "선택해 주세요.\n1. 첫 항목\n1. 또 첫 항목",
    "선택해 주세요.\n" + "\n".join(f"{i}. 항목 {i}" for i in range(1, 8)),
    "'읽기'와 '수정' 중 어떤 작업을 원하시는지 말씀해 주세요.",
    "작업 ID만 입력하거나 ‘작업 ID 승인’ 형식으로 지정해 주세요.",
    "선택해 주세요.\n```python\n1. print('a')\n2. print('b')",
    "", None,
])
def test_does_not_invent_answers_or_parse_code_and_task_ids(text):
    assert question_choices(text) == ()


def test_answers_preserve_literal_spaces_paths_and_long_text_while_labels_are_bounded():
    first = r"C:\자료\새  폴더\원본.txt"
    second = "아주 긴  선택지  " * 20
    choices = question_choices(f"어느 항목을 선택할까요?\n1. {first}\n2. {second}")
    assert choices[0] == (r"C:\자료\새 폴더\원본.txt", first)
    assert len(choices[1][0]) == 60 and choices[1][0].endswith("…")
    assert choices[1][1] == second.strip()


def test_six_choices_supported_without_dropping_the_last_one():
    choices = question_choices("선택해 주세요.\n" + "\n".join(f"{i}) 항목 {i}" for i in range(1, 7)))
    assert len(choices) == 6 and choices[-1] == ("항목 6", "항목 6")
