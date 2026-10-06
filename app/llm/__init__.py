from app.config import Settings
from app.llm.base import LlmClient


def build_llm(settings: Settings) -> LlmClient:
    if settings.llm_provider == "mock":
        from app.llm.mock import MockLlm

        return MockLlm(settings.mock_stream_delay_ms)
    from app.llm.azure_openai import AzureOpenAIClient

    return AzureOpenAIClient(settings)
