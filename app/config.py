"""运行期配置，全部通过环境变量注入，便于容器部署。"""
from __future__ import annotations

import os


def _database_url() -> str:
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    # Compose 网络下的默认值；本地无 PostgreSQL 时回退到 SQLite 文件库。
    if os.getenv("DB_HOST"):
        return (
            f"postgresql+psycopg2://{os.getenv('DB_USER', 'trace')}:"
            f"{os.getenv('DB_PASSWORD', 'trace')}@{os.getenv('DB_HOST')}:"
            f"{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'trace')}"
        )
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return f"sqlite:///{os.path.join(here, 'trace.db')}"


DATABASE_URL = os.getenv("DATABASE_URL") or _database_url()

# 服务监听
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
