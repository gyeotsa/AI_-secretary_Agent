import anthropic
import requests
import json
import re
import time
from typing import Tuple, List, Dict, Any, Optional
from config import Config
from core.tools import get_tools_schema, AUTO_LOOP_EXCLUDED_TOOLS
from core.model_registry import get_model_registry
from core.plugin import ToolCancelledError
from core.turn_context import check_turn_cancelled
from core.model_transport import post_json


class ModelCallError(RuntimeError):
    """Typed transport/provider failure that must never be treated as model text."""

    def __init__(
        self,
        provider: str,
        model: str,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
    ):
        self.provider = str(provider)
        self.model = str(model)
        self.code = str(code)
        self.detail = str(detail)
        self.retryable = bool(retryable)
        super().__init__(f"{self.provider}/{self.model} {self.code}: {self.detail}")

    def user_message(self) -> str:
        if self.code == "connection":
            return "로컬 AI 모델 서버에 연결할 수 없습니다. Ollama 실행 상태를 확인해 주세요."
        if self.code == "timeout":
            return "AI 모델 응답 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요."
        if self.code == "authentication":
            return "AI 제공자 인증 설정을 확인해 주세요."
        return "AI 모델 호출에 실패했습니다. 진단 로그에서 상세 원인을 확인해 주세요."


PROSE_TRUNCATION_NOTICE = (
    "[응답이 완료되기 전에 중단되어 일부 내용만 표시합니다. "
    "완전한 답변이 아니며, 원하시면 이어서 설명해 달라고 요청해 주세요.]"
)


class ProseResponse(str):
    """Human-facing text, with explicit per-response completion metadata.

    The notice precedes the content so even an unfinished code fence cannot
    hide it. ``content`` retains the generated text without that UI notice.
    This type is never a substitute for strict JSON/code/tool output.
    """

    def __new__(cls, text: str, *, truncated: bool = False,
                finish_reason: str = "", provider: str = "", model: str = ""):
        content = str(text or "")
        if truncated and content.startswith(PROSE_TRUNCATION_NOTICE):
            content = content[len(PROSE_TRUNCATION_NOTICE):].removeprefix("\n\n")
        rendered = content
        if truncated:
            rendered = PROSE_TRUNCATION_NOTICE + ("\n\n" + content if content else "")
        result = super().__new__(cls, rendered)
        result.content = content
        result.truncated = bool(truncated)
        result.finish_reason = str(finish_reason or "")
        result.provider = str(provider or "")
        result.model = str(model or "")
        return result

    @property
    def metadata(self) -> Dict[str, Any]:
        return {key: getattr(self, key) for key in
                ("truncated", "finish_reason", "provider", "model")}


def has_configured_anthropic_key() -> bool:
    key = (Config.ANTHROPIC_API_KEY or "").strip()
    if not key:
        return False
    return not key.casefold().startswith(("your_", "replace_", "changeme", "example"))


class BaseLLMClient:
    def __init__(self):
        # UI responses are spoken automatically, and `listen` blocks waiting for
        # voice input, so neither should ever be offered to the model as a
        # callable tool (see core/tools.py AUTO_LOOP_EXCLUDED_TOOLS for the
        # single source of truth on this exclusion list).
        self.tools = [
            tool for tool in get_tools_schema()
            if tool.get("name") not in AUTO_LOOP_EXCLUDED_TOOLS
        ]
        self.system_prompt = Config.get_system_prompt()

    def set_system_prompt(self, prompt: str):
        self.system_prompt = prompt

    def chat_with_tools(self, messages: List[Dict], allowed_tool_names=None) -> Tuple[str, List[Dict]]:
        raise NotImplementedError

    def chat(self, messages: List[Dict]) -> str:
        raise NotImplementedError

    def chat_prose(self, messages: List[Dict]) -> ProseResponse:
        """Opt in to human-readable partial output; never continue implicitly."""
        check_turn_cancelled()
        result = self.chat(messages)
        check_turn_cancelled()
        return result if isinstance(result, ProseResponse) else ProseResponse(result)


class AnthropicClient(BaseLLMClient):
    def __init__(self):
        super().__init__()
        if not has_configured_anthropic_key():
            raise ValueError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
        self.client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)

    def _prepare_messages(self, messages: List[Dict]) -> Tuple[str, List[Dict]]:
        """Anthropic API에서 허용하지 않는 system role을 최상위 system으로 이동."""
        has_explicit_system = any(message.get("role") == "system" for message in messages)
        system_parts = [self.system_prompt] if self.system_prompt and not has_explicit_system else []
        api_messages = []
        for message in messages:
            if message.get("role") == "system":
                system_parts.append(str(message.get("content", "")))
            else:
                api_messages.append(message)
        return "\n\n".join(part for part in system_parts if part), api_messages

    def chat_with_tools(self, messages: List[Dict], allowed_tool_names=None) -> Tuple[str, List[Dict]]:
        metric_started = time.perf_counter()
        check_turn_cancelled()
        try:
            system_prompt, api_messages = self._prepare_messages(messages)
            check_turn_cancelled()
            response = self.client.messages.create(
                model=Config.ANTHROPIC_MODEL,
                max_tokens=Config.MAX_TOKENS,
                system=system_prompt,
                messages=api_messages,
                temperature=Config.TEMPERATURE,
                tools=[tool for tool in self.tools if allowed_tool_names is None or tool["name"] in allowed_tool_names],
            )
            check_turn_cancelled()

            if getattr(response, "stop_reason", None) == "max_tokens":
                raise ModelCallError("anthropic", Config.ANTHROPIC_MODEL, "truncated_output",
                                     "모델 출력이 완료되기 전에 잘렸습니다.", retryable=False)
            tool_use_blocks = [block for block in response.content if block.type == "tool_use"]

            if tool_use_blocks:
                return "", tool_use_blocks

            text_blocks = [block for block in response.content if block.type == "text"]
            if text_blocks:
                return text_blocks[0].text, []

            return "", []
        except ToolCancelledError:
            raise
        except ModelCallError:
            check_turn_cancelled()
            raise
        except Exception as e:
            check_turn_cancelled()
            raise ModelCallError(
                "anthropic", Config.ANTHROPIC_MODEL, "provider", str(e), retryable=True
            ) from e

    def chat(self, messages: List[Dict]) -> str:
        return self._chat(messages)

    def chat_prose(self, messages: List[Dict]) -> ProseResponse:
        return self._chat(messages, prose=True)

    def _chat(self, messages: List[Dict], *, prose: bool = False) -> str:
        check_turn_cancelled()
        try:
            system_prompt, api_messages = self._prepare_messages(messages)
            check_turn_cancelled()
            response = self.client.messages.create(
                model=Config.ANTHROPIC_MODEL,
                max_tokens=Config.MAX_TOKENS,
                system=system_prompt,
                messages=api_messages,
                temperature=Config.TEMPERATURE,
            )
            check_turn_cancelled()
            finish_reason = getattr(response, "stop_reason", "") or ""
            truncated = finish_reason == "max_tokens"
            if truncated and not prose:
                raise ModelCallError("anthropic", Config.ANTHROPIC_MODEL, "truncated_output",
                                     "모델 출력이 완료되기 전에 잘렸습니다.", retryable=False)
            if prose:
                text = "\n".join(block.text for block in response.content
                                 if getattr(block, "type", None) == "text")
                return ProseResponse(text, truncated=truncated, finish_reason=finish_reason,
                                     provider="anthropic", model=Config.ANTHROPIC_MODEL)
            return response.content[0].text
        except ToolCancelledError:
            raise
        except ModelCallError:
            check_turn_cancelled()
            raise
        except Exception as e:
            check_turn_cancelled()
            raise ModelCallError(
                "anthropic", Config.ANTHROPIC_MODEL, "provider", str(e), retryable=True
            ) from e


class OllamaClient(BaseLLMClient):
    def __init__(self, role: str = "default"):
        super().__init__()
        self.base_url = Config.OLLAMA_BASE_URL
        self.role = role
        self.profile = get_model_registry().resolve(role)
        self.model = self.profile.model
        if self.profile.role == "code":
            self.system_prompt = (
                "당신은 로컬 파일 작업을 수행하는 시니어 소프트웨어 엔지니어입니다. 제공된 파일 "
                "내용과 수정 요청을 바탕으로 정확하고 실행 가능하며 유지보수 가능한 완성 파일 "
                "원문을 생성하세요. 작업 규모에 맞는 가장 단순한 올바른 구현을 선택하고 기존의 "
                "유효한 동작은 보존하세요. 파일을 직접 수정할 수 없다는 말, 사과, 설명, 작업 예정 "
                "문장, Markdown 코드 펜스를 출력하지 마세요. 항상 저장할 파일의 전체 내용만 "
                "반환하세요."
            )
        # 과거에는 여기서 self.tools = [] 로 Ollama의 tool calling을 통째로 꺼놨습니다.
        # (사유: 소형 instruct 모델이 일반 대화도 tool 호출로 착각해서 무한 루프에 빠지는 문제,
        #  특히 speak_text/listen 관련.)
        # 지금은 원인이었던 두 도구(speak_text, listen)를 BaseLLMClient에서 이미
        # AUTO_LOOP_EXCLUDED_TOOLS로 애초에 제외하고, Executor 쪽에서도 모델이 존재하지
        # 않는 tool 이름을 지어내면 무시하도록 검증하므로, tool calling 자체를 막을 필요가
        # 없어졌습니다. Executor를 native tool calling(chat_with_tools) 기반으로 전환하려면
        # 이 목록이 비어있으면 안 되므로 다시 켭니다.
        # 다만 소형 로컬 모델은 여전히 tool 선택 정확도가 떨어질 수 있으니, 실제로 오작동이
        # 잦으면 이 부분을 모델 교체(더 큰 모델) 또는 재검토 대상으로 삼으세요.

    @staticmethod
    def _extract_legacy_speak_text(content: str) -> str | None:
        """Extract final text from a tool call emitted in message content."""
        try:
            payload = json.loads(content.replace('""', '"'))
        except json.JSONDecodeError:
            match = re.search(r'"name"\s*:\s*"speak_text".*?"text"\s*:\s*"(.*)"\s*}\s*}', content, re.DOTALL)
            if not match:
                return None
            text = match.group(1).strip('"').strip()
            return text or None

        if payload.get("name") != "speak_text":
            return None

        parameters = payload.get("parameters", payload.get("arguments", {}))
        text = parameters.get("text") if isinstance(parameters, dict) else None
        return text.strip() if isinstance(text, str) and text.strip() else None

    def _extract_legacy_tool_call(self, content: str) -> Optional[Dict[str, Any]]:
        """본문 JSON으로 반환된 로컬 모델의 Tool 호출을 안전하게 구조화."""
        candidate = content.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            match = re.search(r'\{\s*"name"\s*:\s*"[^"\r\n]+".*\}\s*$', candidate, re.DOTALL)
            if not match:
                return None
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        if not isinstance(payload, dict):
            return None
        name = payload.get("name")
        allowed_names = {tool["name"] for tool in self.tools}
        if not isinstance(name, str) or name not in allowed_names:
            return None
        arguments = payload.get("arguments", payload.get("parameters", payload.get("input", {})))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return None
        if not isinstance(arguments, dict):
            return None
        return {
            "type": "tool_use",
            "id": f"legacy_{name}_{abs(hash(json.dumps(arguments, sort_keys=True, ensure_ascii=False)))}",
            "name": name,
            "input": arguments,
        }

    def _convert_to_ollama_tools(self, tools: List[Dict]) -> List[Dict]:
        """Anthropic 도구 스키마를 Ollama 형식으로 변환"""
        ollama_tools = []
        for tool in tools:
            ollama_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"]
                }
            })
        return ollama_tools

    def chat_with_tools(self, messages: List[Dict], allowed_tool_names=None) -> Tuple[str, List[Dict]]:
        metric_started = time.perf_counter()
        try:
            # 호출별 system 메시지가 있으면 전역 기본 프롬프트를 중복 삽입하지
            # 않는다. 이 규칙 덕분에 공유 클라이언트를 변경하지 않고 역할별
            # 프롬프트를 안전하게 전달할 수 있다.
            ollama_messages = []
            has_explicit_system = any(message.get("role") == "system" for message in messages)
            if self.system_prompt and not has_explicit_system:
                ollama_messages.append({"role": "system", "content": self.system_prompt})
            ollama_messages.extend(messages)

            # Ollama용 도구 스키마로 변환
            selected_tools = [
                tool for tool in self.tools
                if allowed_tool_names is None or tool["name"] in allowed_tool_names
            ]
            ollama_tools = self._convert_to_ollama_tools(selected_tools)

            payload = {
                "model": self.model,
                "messages": ollama_messages,
                "stream": False,
                "keep_alive": self.profile.keep_alive,
                "options": {
                    "temperature": self.profile.temperature,
                    "num_predict": self.profile.max_tokens
                },
                "tools": ollama_tools
            }

            print(f"[LLM] Ollama tool 호출: model={self.model}, tools={len(ollama_tools)}")

            check_turn_cancelled()
            response = post_json(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=120
            )
            check_turn_cancelled()
            response.raise_for_status()
            result = response.json()
            check_turn_cancelled()
            if result.get("done_reason") == "length" or result.get("done") is False:
                raise ModelCallError("ollama", self.model, "truncated_output",
                                     "모델 출력이 완료되기 전에 잘렸습니다.", retryable=False)
            from core.productization import METRICS
            METRICS.increment(f"model.{self.model}.success")
            METRICS.observe(f"model.{self.model}.latency", (time.perf_counter() - metric_started) * 1000)

            # Ollama 응답 처리
            if "message" in result:
                message = result["message"]

                # 도구 호출 확인
                if "tool_calls" in message and message["tool_calls"]:
                    tool_use_blocks = []
                    for tool_call in message["tool_calls"]:
                        function_info = tool_call.get("function", {})
                        tool_use_blocks.append({
                            "type": "tool_use",
                            "id": f"call_{function_info.get('name', 'unknown')}_{hash(json.dumps(function_info.get('arguments', {})))}",
                            "name": function_info.get("name", ""),
                            "input": function_info.get("arguments", {})
                        })
                    return "", tool_use_blocks

                # 텍스트 응답
                if "content" in message:
                    # 이모지 필터링
                    text = re.sub(r'[\U00010000-\U0010ffff]', '', message["content"])
                    legacy_tool_call = self._extract_legacy_tool_call(text)
                    if legacy_tool_call:
                        return "", [legacy_tool_call]
                    legacy_speak_text = self._extract_legacy_speak_text(text)
                    if legacy_speak_text:
                        return legacy_speak_text, []
                    return text, []

            return "", []
        except ToolCancelledError:
            raise
        except requests.exceptions.ConnectionError as exc:
            check_turn_cancelled()
            from core.productization import METRICS
            METRICS.increment(f"model.{self.model}.failure")
            raise ModelCallError(
                "ollama", self.model, "connection", str(exc), retryable=True
            ) from exc
        except requests.exceptions.Timeout as exc:
            check_turn_cancelled()
            from core.productization import METRICS
            METRICS.increment(f"model.{self.model}.failure")
            raise ModelCallError(
                "ollama", self.model, "timeout", str(exc), retryable=True
            ) from exc
        except requests.exceptions.HTTPError as e:
            # tool calling 미지원 모델일 경우 fallback으로 chat 메서드 사용
            try:
                error_detail = e.response.json()
                check_turn_cancelled()
                if "does not support tools" in str(error_detail):
                    # tool calling 미지원시 일반 chat으로 fallback
                    text = self.chat(messages)
                    return text, []
                raise ModelCallError(
                    "ollama", self.model, f"http_{e.response.status_code}",
                    str(error_detail), retryable=e.response.status_code >= 500,
                ) from e
            except ToolCancelledError:
                raise
            except ModelCallError:
                check_turn_cancelled()
                raise
            except Exception:
                check_turn_cancelled()
                # tool calling 미지원일 가능성 있으면 fallback
                if e.response.status_code == 400:
                    text = self.chat(messages)
                    return text, []
                raise ModelCallError(
                    "ollama", self.model, f"http_{e.response.status_code}",
                    str(e), retryable=e.response.status_code >= 500,
                ) from e
        except ModelCallError:
            check_turn_cancelled()
            raise
        except Exception as e:
            check_turn_cancelled()
            raise ModelCallError(
                "ollama", self.model, "protocol", str(e), retryable=False
            ) from e

    def chat(self, messages: List[Dict]) -> str:
        return self.chat_structured(messages)

    def chat_prose(self, messages: List[Dict]) -> ProseResponse:
        return self._chat(messages, prose=True)

    def release(self) -> bool:
        """Unload this role's model so another local specialist can use VRAM/RAM."""
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": "", "keep_alive": 0, "stream": False},
                timeout=20,
            )
            response.raise_for_status(); return True
        except Exception:
            return False

    def chat_structured(self, messages: List[Dict], json_schema: Optional[Dict] = None,
                        *, context_window: Optional[int] = None) -> str:
        return self._chat(messages, json_schema, context_window=context_window)

    def _chat(self, messages: List[Dict], json_schema: Optional[Dict] = None,
              *, context_window: Optional[int] = None, prose: bool = False) -> str:
        metric_started = time.perf_counter()
        check_turn_cancelled()
        try:
            # 역할을 하나의 문자열로 평탄화하면 작은 로컬 모델이 최근 사용자
            # 발화와 과거 assistant 응답을 혼동하기 쉽다. Ollama의 chat
            # endpoint에 역할 구조를 그대로 전달한다.
            ollama_messages = []
            has_explicit_system = any(message.get("role") == "system" for message in messages)
            if self.system_prompt and not has_explicit_system:
                ollama_messages.append({"role": "system", "content": self.system_prompt})
            for message in messages:
                if message.get("content") is None:
                    continue
                converted = {
                    "role": str(message.get("role", "user")),
                    "content": str(message.get("content", "")),
                }
                if message.get("images"):
                    converted["images"] = list(message["images"])
                ollama_messages.append(converted)

            payload = {
                "model": self.model,
                "messages": ollama_messages,
                "stream": False,
                "keep_alive": self.profile.keep_alive,
                "options": {
                    "temperature": self.profile.temperature,
                    "num_predict": self.profile.max_tokens
                }
            }
            if context_window is not None:
                if (isinstance(context_window, bool) or not isinstance(context_window, int)
                        or not 2048 <= context_window <= 32768):
                    raise ValueError("context_window must be an integer between 2048 and 32768")
                # Ollama's server default may be only 2048. A caller providing
                # a bounded catalogue must explicitly reserve its context.
                payload["options"]["num_ctx"] = context_window
            if json_schema:
                payload["format"] = json_schema

            check_turn_cancelled()
            response = post_json(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=120
            )
            # Transport aborts active turn requests on cancellation. Retain the
            # checkpoint for a cancellation concurrent with successful return.
            check_turn_cancelled()
            response.raise_for_status()
            result = response.json()
            check_turn_cancelled()
            finish_reason = str(result.get("done_reason") or "")
            truncated = finish_reason == "length" or result.get("done") is False
            if truncated and not prose:
                raise ModelCallError("ollama", self.model, "truncated_output",
                                     "구조화 출력이 완료되기 전에 잘렸습니다.", retryable=False)
            evaluated = result.get("prompt_eval_count")
            if (context_window and isinstance(evaluated, int)
                    and evaluated >= context_window - 64):
                # A saturated context cannot prove the beginning of the request
                # survived server truncation. Never execute from that response.
                raise ModelCallError("ollama", self.model, "context_saturated",
                                     "요청이 모델 문맥 한도에 도달했습니다. 입력을 나누어야 합니다.", retryable=False)
            from core.productization import METRICS
            METRICS.increment(f"model.{self.model}.{'partial' if truncated else 'success'}")
            METRICS.observe(f"model.{self.model}.latency", (time.perf_counter() - metric_started) * 1000)

            if isinstance(result.get("message"), dict):
                # Transport data is not UI prose. Removing Unicode here can
                # corrupt message bodies, filenames and structured tool inputs.
                # Console encoding is handled by the console, not by data loss.
                text = str(result["message"].get("content", ""))
                if prose:
                    return ProseResponse(text, truncated=truncated,
                                         finish_reason=finish_reason or ("incomplete" if truncated else ""),
                                         provider="ollama", model=self.model)
                return text

            if prose:
                return ProseResponse("", truncated=truncated,
                                     finish_reason=finish_reason or ("incomplete" if truncated else ""),
                                     provider="ollama", model=self.model)
            return ""
        except ToolCancelledError:
            raise
        except requests.exceptions.ConnectionError as exc:
            check_turn_cancelled()
            from core.productization import METRICS
            METRICS.increment(f"model.{self.model}.failure")
            raise ModelCallError(
                "ollama", self.model, "connection", str(exc), retryable=True
            ) from exc
        except requests.exceptions.Timeout as exc:
            check_turn_cancelled()
            from core.productization import METRICS
            METRICS.increment(f"model.{self.model}.failure")
            raise ModelCallError(
                "ollama", self.model, "timeout", str(exc), retryable=True
            ) from exc
        except requests.exceptions.HTTPError as e:
            check_turn_cancelled()
            # 오류 응답 자세히 보기
            try:
                error_detail = e.response.json()
            except Exception:
                error_detail = str(e)
            raise ModelCallError(
                "ollama", self.model, f"http_{e.response.status_code}",
                str(error_detail), retryable=e.response.status_code >= 500,
            ) from e
        except ModelCallError:
            check_turn_cancelled()
            raise
        except Exception as e:
            check_turn_cancelled()
            raise ModelCallError(
                "ollama", self.model, "protocol", str(e), retryable=False
            ) from e


class HybridLLMClient(BaseLLMClient):
    """Claude 우선 호출 후 오류 시 Ollama로 자동 전환하는 역할 전용 클라이언트."""

    def __init__(
        self,
        primary: Optional[BaseLLMClient] = None,
        fallback: Optional[BaseLLMClient] = None,
        enable_configured_primary: bool = True,
    ):
        super().__init__()
        self.fallback = fallback or OllamaClient()
        self.primary = primary
        self.primary_unavailable_reason = ""
        if self.primary is None and enable_configured_primary:
            if has_configured_anthropic_key():
                try:
                    self.primary = AnthropicClient()
                except Exception as exc:
                    self.primary_unavailable_reason = str(exc)
            else:
                self.primary_unavailable_reason = "ANTHROPIC_API_KEY 미설정"
        self.last_provider = ""
        self.last_latency_ms = 0.0
        self.routing_stats = {
            "anthropic_success": 0,
            "anthropic_partial": 0,
            "anthropic_failure": 0,
            "ollama_fallback": 0,
        }
        self.set_system_prompt(self.system_prompt)

    def set_system_prompt(self, prompt: str):
        super().set_system_prompt(prompt)
        if getattr(self, "primary", None) is not None:
            self.primary.set_system_prompt(prompt)
        if getattr(self, "fallback", None) is not None:
            self.fallback.set_system_prompt(prompt)

    @staticmethod
    def _is_error_text(text: str) -> bool:
        normalized = (text or "").lstrip().casefold()
        return normalized.startswith(("오류:", "오류가 발생했습니다:", "error:"))

    def chat_with_tools(self, messages: List[Dict], allowed_tool_names=None) -> Tuple[str, List[Dict]]:
        started = time.perf_counter()
        check_turn_cancelled()
        if self.primary is not None:
            try:
                try:
                    text, tools = self.primary.chat_with_tools(messages, allowed_tool_names)
                except TypeError:
                    check_turn_cancelled()
                    text, tools = self.primary.chat_with_tools(messages)
                check_turn_cancelled()
                if tools or (text and not self._is_error_text(text)):
                    self.last_provider = "anthropic"
                    self.routing_stats["anthropic_success"] += 1
                    self.last_latency_ms = (time.perf_counter() - started) * 1000
                    return text, tools
                self.primary_unavailable_reason = text
            except ToolCancelledError:
                raise
            except Exception as exc:
                check_turn_cancelled()
                self.primary_unavailable_reason = str(exc)
            self.routing_stats["anthropic_failure"] += 1
        check_turn_cancelled()
        self.last_provider = "ollama"
        self.routing_stats["ollama_fallback"] += 1
        try:
            result = self.fallback.chat_with_tools(messages, allowed_tool_names)
        except TypeError:
            check_turn_cancelled()
            result = self.fallback.chat_with_tools(messages)
        check_turn_cancelled()
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        return result

    def chat(self, messages: List[Dict]) -> str:
        return self._chat(messages)

    def chat_prose(self, messages: List[Dict]) -> ProseResponse:
        result = self._chat(messages, prose=True)
        return result if isinstance(result, ProseResponse) else ProseResponse(result)

    def _chat(self, messages: List[Dict], *, prose: bool = False) -> str:
        started = time.perf_counter()
        check_turn_cancelled()
        if self.primary is not None:
            try:
                chat = (getattr(self.primary, "chat_prose", None) or self.primary.chat) if prose else self.primary.chat
                text = chat(messages)
                check_turn_cancelled()
                if text and not self._is_error_text(text):
                    self.last_provider = "anthropic"
                    stat = ("anthropic_partial" if isinstance(text, ProseResponse)
                            and text.truncated else "anthropic_success")
                    self.routing_stats[stat] += 1
                    self.last_latency_ms = (time.perf_counter() - started) * 1000
                    return text
                self.primary_unavailable_reason = text
            except ToolCancelledError:
                raise
            except Exception as exc:
                check_turn_cancelled()
                self.primary_unavailable_reason = str(exc)
            self.routing_stats["anthropic_failure"] += 1
        check_turn_cancelled()
        self.last_provider = "ollama"
        self.routing_stats["ollama_fallback"] += 1
        chat = (getattr(self.fallback, "chat_prose", None) or self.fallback.chat) if prose else self.fallback.chat
        result = chat(messages)
        check_turn_cancelled()
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        return result

    def get_routing_status(self) -> Dict[str, Any]:
        return {
            "last_provider": self.last_provider,
            "last_latency_ms": round(self.last_latency_ms, 2),
            "primary_available": self.primary is not None,
            "primary_unavailable_reason": self.primary_unavailable_reason,
            **self.routing_stats,
        }


def get_llm_client(role: str = "default") -> BaseLLMClient:
    normalized_role = role.strip().lower() or "default"
    cache_key = f"{Config.LLM_PROVIDER}:{normalized_role}"
    if cache_key not in _llm_clients:
        if Config.LLM_PROVIDER == "anthropic":
            client = AnthropicClient()
        elif Config.LLM_PROVIDER == "ollama":
            client = OllamaClient(normalized_role)
        elif Config.LLM_PROVIDER == "hybrid":
            if normalized_role in Config.HYBRID_CLAUDE_ROLES:
                client = HybridLLMClient()
            else:
                client = OllamaClient(normalized_role)
        else:
            raise ValueError(f"지원되지 않는 LLM 제공자: {Config.LLM_PROVIDER}")
        _llm_clients[cache_key] = client
    return _llm_clients[cache_key]


# Singleton instance
_llm_clients: Dict[str, BaseLLMClient] = {}
