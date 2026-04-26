import re


def strip_sources_line(text: str) -> str:
    """Убирает строку 'Источники: ...' из конца ответа LLM."""
    return re.sub(r"\n+[Ии]сточники:\s*.+$", "", text, flags=re.DOTALL).rstrip()
