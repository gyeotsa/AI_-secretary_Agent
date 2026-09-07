"""Separate the current speech act from quoted payloads before tool routing.

This is an authority boundary, not a second tool catalogue.  Domain/slot
interpretation still belongs to plugin contracts.  Only explicit discourse
markers are handled here; polite questions remain executable requests.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


_LITERAL = re.compile(
    r"```[\s\S]*?```|~~~[\s\S]*?~~~|"
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
    r"“[^”]*”|‘[^’]*’|`[^`\n]+`"
)
_EXPLANATION_END = re.compile(
    r"(?:설명|번역|해석|풀이)(?:만)?\s*(?:해\s*줘|해\s*주세요|해줄래|해봐|부탁해|해)"
    r"[.!?\s]*$|"
    r"(?:무슨\s*(?:뜻|의미|일)|뜻이|의미가|차이가|원리가|어떻게\s*(?:처리|작동)|"
    r"왜\s*.{0,30}(?:안\s*됐|실패|안\s*되)).{0,35}[?？][.!?\s]*$|"
    r"(?:방법|원리|의미|차이)(?:만|을|를|가|이)?\s*(?:알려\s*줘|알려\s*주세요|뭐야|뭐지)"
    r"[.!?\s]*$",
    re.IGNORECASE,
)
_MENTION = re.compile(
    r"(?:라고|라고는)\s*(?:말하|말했|말해|요청하|요청했|시키|시켰)|"
    r"(?:라는|란)\s*(?:명령|문장|표현|요청)", re.IGNORECASE,
)
_NEGATION = re.compile(
    r"지\s*(?:는\s*)?(?:말아\s*(?:줘|주세요)?|마세요|마(?![가-힣])|말고|"
    r"않아도\s*(?:돼|됩니다|괜찮))|"
    r"(?:면|서는)\s*안\s*(?:돼|됩니다|되)|"
    r"필요(?:는|가)?\s*없",
    re.IGNORECASE,
)
_CONDITION = re.compile(
    r"\S*(?:하면|한다면|되면|된다면|있으면|없으면|오면|내리면|성공하면|실패하면|"
    r"됐으면|끝났으면|충족될\s*때만)(?=\s|[,，])", re.IGNORECASE,
)
_COURTESY_CONDITIONS = frozenset({"가능하면", "그러면", "그렇다면", "괜찮으면"})
_CURRENT_ACTION_END = re.compile(
    r"(?:해\s*줘|해\s*주세요|해줄래|해주세요|줘|주세요|줄래|해봐|부탁해|부탁합니다)"
    r"[.!?\s]*$", re.IGNORECASE,
)
_EXPLICIT_RESEARCH = re.compile(
    r"(?:웹에서|인터넷에서|검색(?:해|해서|한\s*뒤)|찾아\s*(?:보고|봐|줘)|"
    r"조사(?:해|한)|출처(?:를|와))", re.IGNORECASE,
)
_DISCUSSION_SUBJECT = re.compile(
    r"(?:방법|원리|의미|뜻|문법|라는\s*(?:명령|문장|표현)|"
    r"(?:하면|다면).{0,25}(?:무슨|어떻게|왜))", re.IGNORECASE,
)
_EXTERNAL_SOURCE = re.compile(
    r"(?:파일|문서|보고서|pdf|사진|이미지|화면|웹\s*페이지|링크|영상|노트|"
    r"현재\s*상태|지금\s*상태)", re.IGNORECASE,
)
_BARE_CONTINUATION = re.compile(
    r"(?:(?:그거|그걸|그것(?:을)?|이거|이걸|그대로|이어서|계속|다시|또|"
    r"한\s*번\s*더|좀\s*더|더)\s*)+"
    r"(?:(?:자세히|쉽게)\s*)?"
    r"(?:해\s*(?:줘|주세요|줄래|봐)|부탁해|해)?[.!?\s]*$", re.IGNORECASE,
)


def mask_quoted_payloads(text: str) -> str:
    """Keep offsets/newlines while removing payload words from routing evidence.

    A quoted URL is still a literal target, not an instruction, so retain its
    scheme for URL capability detection.  Slot extraction always receives the
    unmodified outer request and exact payload.
    """
    def replace(match: re.Match) -> str:
        value = match.group(0)
        inner = value[1:-1]
        if re.fullmatch(r"https?://[^\s<>]+", inner, re.IGNORECASE):
            scheme = inner[:inner.index("://") + 3] + "x"
            return " " + scheme.ljust(len(inner)) + " "
        if value.startswith(("```", "~~~")):
            return "".join("\n" if char == "\n" else " " for char in value)
        # Keep quote grammar ("..."라고 읽어줘), but no command tokens.
        return value[0] + "".join("\n" if char == "\n" else " " for char in inner) + value[-1]

    return _LITERAL.sub(replace, str(text or ""))


@dataclass(frozen=True)
class UtteranceScope:
    action_text: str
    routing_text: str
    discussion: bool = False
    negated: bool = False
    conditional: bool = False
    condition: str = ""
    reason: str = "current_request"


def analyze_utterance_scope(text: str, history: list[dict] | None = None) -> UtteranceScope:
    """Identify what the user asks *now*, rather than every verb they mention."""
    original = str(text or "").strip()
    visible = mask_quoted_payloads(original)
    if history and _BARE_CONTINUATION.fullmatch(visible):
        # An elliptical repetition inherits the user's speech act, not whichever
        # executable intent happened to be stored last. Explicit new actions do
        # not match this grammar and are still routed independently.
        for message in reversed(history):
            if message.get("role") != "user":
                continue
            prior = str(message.get("content", "")).strip()
            if _BARE_CONTINUATION.fullmatch(mask_quoted_payloads(prior)):
                continue
            if analyze_utterance_scope(prior).discussion:
                return UtteranceScope(
                    original, visible, discussion=True,
                    reason="discussion_continuation_not_execution",
                )
            break
    explained = bool(_EXPLANATION_END.search(visible))
    # Looking up documentation is still an explicit read-only research task.
    # The discussion guard is for an answer about supplied text or a method;
    # it must not silently discard a requested source lookup.
    researched = bool(_EXPLICIT_RESEARCH.search(visible))
    mention = bool(_MENTION.search(visible))
    if explained and not researched and (
        mention or _LITERAL.search(original) or _DISCUSSION_SUBJECT.search(visible)
        or not _EXTERNAL_SOURCE.search(visible)
    ):
        return UtteranceScope(original, visible, discussion=True, reason="explanation_not_execution")
    if mention and not _CURRENT_ACTION_END.search(visible):
        return UtteranceScope(original, visible, discussion=True, reason="reported_command_not_execution")

    # "X 하지 말고 Y 해줘" authorizes only Y.  It is safe to project that
    # explicit corrective clause, but never a `말고` inside quoted message data.
    for negative in _NEGATION.finditer(visible):
        if negative.group(0).rstrip().endswith("말고"):
            tail = original[negative.end():].lstrip(" ,，")
            if tail:
                tail_scope = analyze_utterance_scope(tail)
                return UtteranceScope(
                    tail_scope.action_text, tail_scope.routing_text,
                    tail_scope.discussion, tail_scope.negated,
                    tail_scope.conditional, tail_scope.condition,
                    "explicit_corrective_clause",
                )

    negated = bool(_NEGATION.search(visible))
    if negated:
        return UtteranceScope(original, visible, negated=True, reason="explicit_non_authorization")

    conditional = next((
        match for match in _CONDITION.finditer(visible)
        if match.group(0).strip() not in _COURTESY_CONDITIONS
    ), None)
    if conditional:
        return UtteranceScope(
            original, visible, conditional=True,
            condition=original[:conditional.end()].strip(),
            reason="condition_must_be_verified_before_execution",
        )
    return UtteranceScope(original, visible)


def allows_execution_follow_up(history: list[dict]) -> bool:
    """A mentioned/cancelled command must not refocus an older runnable task."""
    for message in reversed(history):
        if message.get("role") == "user":
            scope = analyze_utterance_scope(str(message.get("content", "")))
            return not (scope.discussion or scope.negated or scope.conditional)
    return True
