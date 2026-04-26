"""
LLM-клиент для nanogpt (OpenAI-совместимый API).

Один синглтон ChatOpenAI покрывает все сценарии:
- LangGraph: llm.bind_tools(tools) для ReAct-агента
- RAGAS: LangchainLLMWrapper(llm) для оценки качества
- Structured output: llm.with_structured_output(MyModel)

Почему один клиент, а не два?
В RAG v1 был отдельный сырой OpenAI-клиент для JSON mode.
Но ChatOpenAI в 2026 умеет всё то же самое. Один клиент — проще.

Использование:
    from core.llm_client import get_llm
    llm = get_llm()
    response = llm.invoke("Привет!")
"""

from langchain_openai import ChatOpenAI

from core.config import get_config

# --- Синглтон ---

_llm: ChatOpenAI | None = None


def get_llm() -> ChatOpenAI:
    """
    Возвращает синглтон ChatOpenAI, настроенный для nanogpt.

    Модель, base_url, api_key, температура, таймаут — всё из конфига.
    Создаётся один раз, потом переиспользуется.
    """
    global _llm
    if _llm is None:
        cfg = get_config()
        _llm = ChatOpenAI(
            model=cfg.nano_gpt_model,
            api_key=cfg.nano_gpt_api_key,
            base_url=cfg.nano_gpt_base_url,
            temperature=cfg.llm.temperature,
            max_tokens=cfg.llm.max_tokens,
            timeout=cfg.llm.request_timeout,
        )
    return _llm


# --- CLI: python -m core.llm_client ---

if __name__ == "__main__":
    print("Проверяю связь с nanogpt...")

    try:
        llm = get_llm()
        # простой тестовый запрос
        response = llm.invoke("Ответь одним словом: 2+2=")
        print(f"Связь с nanogpt OK. Ответ: {response.content}")
    except Exception as e:
        print(f"Ошибка подключения к nanogpt: {e}")
