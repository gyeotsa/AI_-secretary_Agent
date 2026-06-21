import anthropic
from config import Config


class LLMClient:
    def __init__(self):
        if not Config.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
        self.client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)

    def chat(self, messages: list[dict]) -> str:
        try:
            response = self.client.messages.create(
                model=Config.MODEL_NAME,
                max_tokens=Config.MAX_TOKENS,
                system=Config.SYSTEM_PROMPT,
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
