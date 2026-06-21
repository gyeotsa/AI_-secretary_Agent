import anthropic
from config import Config
from core.tools import get_tools_schema


class LLMClient:
    def __init__(self):
        if not Config.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
        self.client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        self.tools = get_tools_schema()
        self.system_prompt = Config.SYSTEM_PROMPT_TEMPLATE.format(user_profile_section="")

    def set_system_prompt(self, prompt: str):
        self.system_prompt = prompt

    def chat_with_tools(self, messages: list[dict]) -> tuple[str, list[dict]]:
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

    def chat(self, messages: list[dict]) -> str:
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


# Singleton instance
_llm_client = None


def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
