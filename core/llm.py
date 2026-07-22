import anthropic
import requests
import json
import re
import time
from typing import Tuple, List, Dict, Any, Optional
from config import Config
from core.tools import get_tools_schema, AUTO_LOOP_EXCLUDED_TOOLS


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
        self.system_prompt = Config.SYSTEM_PROMPT_TEMPLATE.format(user_profile_section="")

    def set_system_prompt(self, prompt: str):
        self.system_prompt = prompt

    def chat_with_tools(self, messages: List[Dict], allowed_tool_names=None) -> Tuple[str, List[Dict]]:
        raise NotImplementedError

    def chat(self, messages: List[Dict]) -> str:
        raise NotImplementedError


class AnthropicClient(BaseLLMClient):
    def __init__(self):
        super().__init__()
        if not has_configured_anthropic_key():
            raise ValueError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
        self.client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)

    def _prepare_messages(self, messages: List[Dict]) -> Tuple[str, List[Dict]]:
        """Anthropic API에서 허용하지 않는 system role을 최상위 system으로 이동."""
        system_parts = [self.system_prompt] if self.system_prompt else []
        api_messages = []
        for message in messages:
            if message.get("role") == "system":
                system_parts.append(str(message.get("content", "")))
            else:
                api_messages.append(message)
        return "\n\n".join(part for part in system_parts if part), api_messages

    def chat_with_tools(self, messages: List[Dict], allowed_tool_names=None) -> Tuple[str, List[Dict]]:
        try:
            system_prompt, api_messages = self._prepare_messages(messages)
            response = self.client.messages.create(
                model=Config.ANTHROPIC_MODEL,
                max_tokens=Config.MAX_TOKENS,
                system=system_prompt,
                messages=api_messages,
                temperature=Config.TEMPERATURE,
                tools=[tool for tool in self.tools if allowed_tool_names is None or tool["name"] in allowed_tool_names],
            )

            tool_use_blocks = [block for block in response.content if block.type == "tool_use"]

            if tool_use_blocks:
                return "", tool_use_blocks

            text_blocks = [block for block in response.content if block.type == "text"]
            if text_blocks:
                return text_blocks[0].text, []

            return "", []
        except Exception as e:
            return f"오류가 발생했습니다: {str(e)}", []

    def chat(self, messages: List[Dict]) -> str:
        try:
            system_prompt, api_messages = self._prepare_messages(messages)
            response = self.client.messages.create(
                model=Config.ANTHROPIC_MODEL,
                max_tokens=Config.MAX_TOKENS,
                system=system_prompt,
                messages=api_messages,
                temperature=Config.TEMPERATURE,
            )
            return response.content[0].text
        except Exception as e:
            return f"오류가 발생했습니다: {str(e)}"


class OllamaClient(BaseLLMClient):
    def __init__(self):
        super().__init__()
        self.base_url = Config.OLLAMA_BASE_URL
        self.model = Config.OLLAMA_MODEL
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
        try:
            # 시스템 프롬프트를 메시지에 추가
            ollama_messages = []
            if self.system_prompt:
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
                "options": {
                    "temperature": Config.TEMPERATURE,
                    "num_predict": Config.MAX_TOKENS
                },
                "tools": ollama_tools
            }

            print(f"[LLM] Ollama tool 호출: model={self.model}, tools={len(ollama_tools)}")

            response = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=120
            )
            response.raise_for_status()
            result = response.json()

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
        except requests.exceptions.ConnectionError:
            return "오류: Ollama가 실행 중이지 않습니다. 'ollama serve'로 시작해주세요.", []
        except requests.exceptions.HTTPError as e:
            # tool calling 미지원 모델일 경우 fallback으로 chat 메서드 사용
            try:
                error_detail = e.response.json()
                if "does not support tools" in str(error_detail):
                    # tool calling 미지원시 일반 chat으로 fallback
                    text = self.chat(messages)
                    return text, []
                return f"오류: {e.response.status_code} - {error_detail}", []
            except:
                # tool calling 미지원일 가능성 있으면 fallback
                if e.response.status_code == 400:
                    text = self.chat(messages)
                    return text, []
                return f"오류: {e.response.status_code} - {str(e)}", []
        except Exception as e:
            return f"오류가 발생했습니다: {str(e)}", []

    def chat(self, messages: List[Dict]) -> str:
        try:
            # 시스템 프롬프트와 사용자 메시지 결합
            full_prompt = self.system_prompt + "\n\n"
            for msg in messages:
                role = msg["role"]
                content = msg["content"]
                if role == "system":
                    full_prompt += f"System: {content}\n"
                elif role == "user":
                    full_prompt += f"User: {content}\n"
                elif role == "assistant":
                    full_prompt += f"Assistant: {content}\n"
            full_prompt += "Assistant: "

            payload = {
                "model": self.model,
                "prompt": full_prompt,
                "stream": False,
                "options": {
                    "temperature": Config.TEMPERATURE,
                    "num_predict": Config.MAX_TOKENS
                }
            }

            response = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=120
            )
            response.raise_for_status()
            result = response.json()

            if "response" in result:
                # 이모지 필터링 (Windows cp949 문제 해결)
                text = result["response"]
                # 간단한 이모지 제거: 이모지 범위의 문자 제거
                import re
                text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
                return text

            return ""
        except requests.exceptions.ConnectionError:
            return "오류: Ollama가 실행 중이지 않습니다. 'ollama serve'로 시작해주세요."
        except requests.exceptions.HTTPError as e:
            # 오류 응답 자세히 보기
            try:
                error_detail = e.response.json()
                return f"오류: {e.response.status_code} - {error_detail}"
            except:
                return f"오류: {e.response.status_code} - {str(e)}"
        except Exception as e:
            return f"오류가 발생했습니다: {str(e)}"


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
        if self.primary is not None:
            try:
                try:
                    text, tools = self.primary.chat_with_tools(messages, allowed_tool_names)
                except TypeError:
                    text, tools = self.primary.chat_with_tools(messages)
                if tools or (text and not self._is_error_text(text)):
                    self.last_provider = "anthropic"
                    self.routing_stats["anthropic_success"] += 1
                    self.last_latency_ms = (time.perf_counter() - started) * 1000
                    return text, tools
                self.primary_unavailable_reason = text
            except Exception as exc:
                self.primary_unavailable_reason = str(exc)
            self.routing_stats["anthropic_failure"] += 1
        self.last_provider = "ollama"
        self.routing_stats["ollama_fallback"] += 1
        try:
            result = self.fallback.chat_with_tools(messages, allowed_tool_names)
        except TypeError:
            result = self.fallback.chat_with_tools(messages)
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        return result

    def chat(self, messages: List[Dict]) -> str:
        started = time.perf_counter()
        if self.primary is not None:
            try:
                text = self.primary.chat(messages)
                if text and not self._is_error_text(text):
                    self.last_provider = "anthropic"
                    self.routing_stats["anthropic_success"] += 1
                    self.last_latency_ms = (time.perf_counter() - started) * 1000
                    return text
                self.primary_unavailable_reason = text
            except Exception as exc:
                self.primary_unavailable_reason = str(exc)
            self.routing_stats["anthropic_failure"] += 1
        self.last_provider = "ollama"
        self.routing_stats["ollama_fallback"] += 1
        result = self.fallback.chat(messages)
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
            client = OllamaClient()
        elif Config.LLM_PROVIDER == "hybrid":
            if normalized_role in Config.HYBRID_CLAUDE_ROLES:
                client = HybridLLMClient()
            else:
                client = OllamaClient()
        else:
            raise ValueError(f"지원되지 않는 LLM 제공자: {Config.LLM_PROVIDER}")
        _llm_clients[cache_key] = client
    return _llm_clients[cache_key]


# Singleton instance
_llm_clients: Dict[str, BaseLLMClient] = {}
