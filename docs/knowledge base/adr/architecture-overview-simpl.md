# Архитектурный обзор RAGv2 (упрощенный)

Упрощённая схема для быстрого ознакомления. Полная версия — [`architecture-overview.md`](architecture-overview.md).

```mermaid
%%{init: {"layout": "elk"}}%%
flowchart TB
    subgraph User["👤 Пользователь"]
        Browser["Браузер (web UI)"]
        Obsidian["Obsidian (моб. и десктоп)"]
    end

    subgraph Dev["💻 Разработка"]
        Repo["GitHub репозиторий"]
        Make["make deploy"]
    end

    subgraph Server["🏠 Сервер (docker‑compose)"]
        subgraph App["📦 app (Python)"]
            Web["FastAPI + Jinja2"]
            Agent["LangGraph Agent"]
            Core["Core<br/>Config · LLM Client"]
            Eval["Eval (RAGAS CLI)"]
        end

        Qdrant["📚 Qdrant"]
        WebDAV["💾 WebDAV"]
        SQLite[("SQLite DB")]
    end

    subgraph Gateway["🔀 LLM-шлюз (LXC 192.168.3.203)"]
        LiteLLM["LiteLLM :4000<br/>LB · fallback · spend"]
    end

    subgraph Ext["☁️ Внешние API"]
        Providers["OpenRouter / nano-gpt"]
        HF["HuggingFace Hub"]
    end

    Observability["📊 Наблюдаемость<br/>Prometheus/Grafana + Langfuse<br/>(отдельные LXC)"]

    Browser -->|"HTTPS"| Web
    Obsidian -->|"WebDAV HTTPS"| WebDAV
    Web --> Agent
    Agent --> Core
    Core -->|"OpenAI API"| LiteLLM
    LiteLLM -->|"LB / fallback"| Providers
    Agent --> Eval
    Eval -->|"Judge LLM"| LiteLLM
    Agent -->|"REST"| Qdrant
    Core -->|"sqlite"| SQLite
    Qdrant --> WebDAV
    Make -->|"deploy → git pull"| Server
    Repo -->|"main branch"| Server
    HF -.->|"модели (1 раз)"| App
    Agent -.->|"метрики + трейсы"| Observability
    LiteLLM -.->|"метрики + трейсы"| Observability

    %% Цвета
    classDef user fill:#fef3c7,stroke:#a16207,color:#000
    classDef dev fill:#e0e7ff,stroke:#4338ca,color:#000
    classDef runtime fill:#dcfce7,stroke:#15803d,color:#000
    classDef infra fill:#e2e8f0,stroke:#475569,color:#000
    classDef external fill:#fee2e2,stroke:#b91c1c,color:#000
    classDef gateway fill:#fff7ed,stroke:#c2410c,color:#000
    classDef monitoring fill:#dbeafe,stroke:#1d4ed8,color:#000

    class Browser,Obsidian user
    class Repo,Make dev
    class Web,Agent,Core,Eval runtime
    class Qdrant,WebDAV,SQLite,Server infra
    class Providers,HF external
    class LiteLLM,Gateway gateway
    class Observability monitoring
```
