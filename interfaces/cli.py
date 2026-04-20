"""
CLI-запуск веб-интерфейса.

Примеры:
    python -m interfaces.cli                    # 127.0.0.1:8000
    python -m interfaces.cli --port 9000        # другой порт
    python -m interfaces.cli --reload           # авто-перезагрузка на правки
"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Запуск RAGv2 веб-интерфейса (FastAPI + uvicorn).")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес для прослушивания")
    parser.add_argument("--port", type=int, default=8000, help="Порт")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Автоперезагрузка при изменении кода (dev-режим)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    args = parser.parse_args()

    import uvicorn
    uvicorn.run(
        "interfaces.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
