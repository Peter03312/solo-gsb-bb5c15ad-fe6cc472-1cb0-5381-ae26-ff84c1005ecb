"""容器入口：等待关系数据库就绪，随后建表并启动 HTTP 服务。"""
from __future__ import annotations

import os
import sys
import time

from . import config, db


def wait_for_db(max_attempts: int = 60, delay: float = 1.0) -> None:
    if db.is_sqlite():
        return  # SQLite 为本地文件，无需等待
    for attempt in range(1, max_attempts + 1):
        try:
            db.ping()
            print(f"[entrypoint] 数据库已就绪（第 {attempt} 次尝试）", flush=True)
            return
        except Exception as exc:  # 数据库容器可能尚未接受连接
            print(f"[entrypoint] 等待数据库... ({attempt}/{max_attempts}) {exc}", flush=True)
            time.sleep(delay)
    print("[entrypoint] 数据库连接超时，退出", flush=True)
    sys.exit(1)


def main() -> None:
    print(f"[entrypoint] DATABASE_URL={config.DATABASE_URL}", flush=True)
    db.init_engine()
    wait_for_db()
    db.init_db()
    print("[entrypoint] 建表完成，启动 uvicorn", flush=True)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
