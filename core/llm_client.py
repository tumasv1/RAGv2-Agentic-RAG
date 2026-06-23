"""
LLM-клиент (OpenAI-совместимый API).

По умолчанию ходит через LLM-шлюз LiteLLM Proxy, который сам делает fallback
с основного провайдера на запасной. См. core/config.py::GatewayConfig.

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
    Возвращает синглтон ChatOpenAI.

    Куда смотрит клиент — зависит от gateway.enabled:
    - True  → на LiteLLM Proxy (виртуальная модель + fallback внутри шлюза).
    - False → напрямую на primary-провайдера (аварийный откат, если шлюз недоступен).

    Температура, лимит токенов, таймаут — общие, из секции llm.
    Создаётся один раз, потом переиспользуется.
    """
    global _llm
    if _llm is None:
        cfg = get_config()
        if cfg.gateway.enabled:
            # обращаемся к шлюзу: модель — виртуальное имя, ключ — gateway_api_key
            model = cfg.gateway.model
            api_key = cfg.gateway_api_key
            base_url = cfg.gateway.base_url
        else:
            # откат: ходим к основному провайдеру напрямую
            model = cfg.llm_primary_model
            api_key = cfg.llm_primary_api_key
            base_url = cfg.llm_primary_base_url
        _llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=cfg.llm.temperature,
            max_tokens=cfg.llm.max_tokens,
            timeout=cfg.llm.request_timeout,
        )
    return _llm


# --- CLI: python -m core.llm_client ---

if __name__ == "__main__":
    cfg = get_config()
    target = f"шлюз {cfg.gateway.base_url}" if cfg.gateway.enabled else cfg.llm_primary_base_url
    print(f"Проверяю связь: {target} ...")

    try:
        llm = get_llm()
        response = llm.invoke("Ответь одним словом: 2+2=")
        print(f"OK. Ответ: {response.content}")
    except Exception as e:
        print(f"Ошибка подключения: {e}")
