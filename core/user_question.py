"""Extract only choices explicitly offered by an assistant question."""
import re


_OPTION = re.compile(r"^\s*(?:(\d{1,2})[.)]\s+|([①-⑳])\s*)(\S.*)$")
_SELECTION = re.compile(
    r"(?:선택|골라|고르)|(?:어느|어떤)[^\n?？]{0,60}(?:원하|할까요|인가요|드릴까요)|"
    r"(?:다음|아래)\s*중[^\n]{0,60}(?:[?？]|알려|말씀)"
)
_YES_NO = re.compile(r"예\s*(?:/|또는)\s*아니[오요]")
_CONFIRMATION = re.compile(r"(?:진행|계속|실행|저장)할까요\s*[?？]\s*$")
_OPEN_QUESTION = re.compile(r"언제|어디|어느|어떤|무엇|뭐|누구|어떻게|얼마|몇")


def question_choices(text: str) -> tuple[tuple[str, str], ...]:
    """Return short button labels and verbatim answers, never guessed preferences."""
    visible = re.sub(r"```.*?(?:```|$)", "", str(text or ""), flags=re.S).strip()
    lines = visible.splitlines()
    matches = [match for line in lines if (match := _OPTION.fullmatch(line))]
    context = "\n".join(line for line in lines if not _OPTION.fullmatch(line))
    if 2 <= len(matches) <= 6 and _SELECTION.search(context):
        numbers = [int(match[1]) if match[1] else ord(match[2]) - ord("①") + 1
                   for match in matches]
        if numbers == list(range(1, len(matches) + 1)):
            choices = []
            for match in matches:
                answer = match[3].strip()
                label = re.sub(r"\s+", " ", answer)
                choices.append((label[:59] + "…" if len(label) > 60 else label, answer))
            return tuple(choices)
    if matches:
        return ()
    if (_YES_NO.search(visible) and re.search(r"[?？]|답|선택|말씀|알려", visible)):
        return (("예", "예"), ("아니오", "아니오"))
    final_question = lines[-1] if lines else ""
    if _CONFIRMATION.search(final_question) and not _OPEN_QUESTION.search(final_question):
        return (("예", "예"), ("아니오", "아니오"))
    return ()
