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


# --- Проброс trace_id в LLM-шлюз (Langfuse) ---


def get_langfuse_extra_body() -> dict | None:
    """
    Возвращает extra_body с trace_id текущего Langfuse-трейса — или None.

    Зачем: без этого один вопрос агенту порождает ДВА независимых трейса
    (дерево графа от CallbackHandler + плоская генерация от шлюза), и
    стоимость вызова считается в Langfuse дважды. Если передать шлюзу
    metadata.existing_trace_id, LiteLLM кладёт свою генерацию в тот же
    трейс, что и граф — один вопрос, один трейс, честная стоимость.

    None означает «не пробрасываем»: трейсинг выключен, ключей нет или
    вызов идёт вне активного трейса (eval, generate_title) — тогда шлюз
    просто создаст свой отдельный трейс, как раньше.
    """
    cfg = get_config()
    if not cfg.langfuse.enabled or not cfg.langfuse_public_key or not cfg.langfuse_secret_key:
        return None
    try:
        # сначала смотрим OTEL напрямую: если активного спана нет (eval,
        # generate_title) — выходим тихо, не дёргая langfuse (он пишет
        # шумный "Context error" в лог при вызове вне спана)
        from opentelemetry import trace as otel_trace

        if not otel_trace.get_current_span().get_span_context().is_valid:
            return None

        from langfuse import get_client

        trace_id = get_client().get_current_trace_id()
    except Exception:
        return None
    if not trace_id:
        return None
    return {"metadata": {"existing_trace_id": trace_id}}


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
