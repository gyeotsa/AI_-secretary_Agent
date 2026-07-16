import anthropic
import requests
import json
import re
from typing import Tuple, List, Dict, Any
from config import Config
from core.tools import get_tools_schema


class BaseLLMClient:
    def __init__(self):
        # UI responses are spoken automatically. Do not offer this tool to
        # Ollama, otherwise small instruct models frequently call it instead
        # of returning the answer text and can enter an endless tool loop.
        self.tools = [tool for tool in get_tools_schema() if tool.get("name") != "speak_text"]
        self.system_prompt = Config.SYSTEM_PROMPT_TEMPLATE.format(user_profile_section="")

    def set_system_prompt(self, prompt: str):
        self.system_prompt = prompt

    def chat_with_tools(self, messages: List[Dict]) -> Tuple[str, List[Dict]]:
        raise NotImplementedError

    def chat(self, messages: List[Dict]) -> str:
        raise NotImplementedError


class AnthropicClient(BaseLLMClient):
    def __init__(self):
        super().__init__()
        if not Config.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
        self.client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)

    def chat_with_tools(self, messages: List[Dict]) -> Tuple[str, List[Dict]]:
        try:
            response = self.client.messages.create(
                model=Config.MODEL_NAME,
                max_tokens=Config.MAX_TOKENS,
                system=self.system_prompt,
                messages=messages,
                temperature=Config.TEMPERATURE,
                tools=self.tools,
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
            response = self.client.messages.create(
                model=Config.MODEL_NAME,
                max_tokens=Config.MAX_TOKENS,
                system=self.system_prompt,
                messages=messages,
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
        # This small instruct model mistakes ordinary chat for tool requests
        # (notably `listen`), preventing a final response from being returned.
        self.tools = []

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

    def chat_with_tools(self, messages: List[Dict]) -> Tuple[str, List[Dict]]:
        try:
            # 시스템 프롬프트를 메시지에 추가
            ollama_messages = []
            if self.system_prompt:
                ollama_messages.append({"role": "system", "content": self.system_prompt})
            ollama_messages.extend(messages)
            
            # Ollama용 도구 스키마로 변환
            ollama_tools = self._convert_to_ollama_tools(self.tools)
            
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
            
            print(f"[DEBUG] Ollama chat_with_tools 호출 전")
            print(f"[DEBUG] Ollama 모델: {self.model}")
            print(f"[DEBUG] Ollama 요청 URL: {self.base_url}/api/chat")
            print(f"[DEBUG] Ollama 요청 페이로드: {json.dumps(payload, indent=2, ensure_ascii=False)}")
            
            response = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=120
            )
            print(f"[DEBUG] Ollama 응답 상태 코드: {response.status_code}")
            response.raise_for_status()
            result = response.json()
            print(f"[DEBUG] Ollama 응답 내용: {json.dumps(result, indent=2, ensure_ascii=False)}")
            
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
                if role == "user":
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


def get_llm_client() -> BaseLLMClient:
    global _llm_client
    if _llm_client is None:
        if Config.LLM_PROVIDER == "anthropic":
            _llm_client = AnthropicClient()
        elif Config.LLM_PROVIDER == "ollama":
            _llm_client = OllamaClient()
        else:
            raise ValueError(f"지원되지 않는 LLM 제공자: {Config.LLM_PROVIDER}")
    return _llm_client


# Singleton instance
_llm_client = None
