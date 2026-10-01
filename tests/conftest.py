"""pytest 夹具：每个测试用独立临时 SQLite 文件库，重建引擎与表。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app


@pytest.fixture
def engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    db.init_engine(url)
    db.init_db()
    yield db.get_engine()
    db.get_engine().dispose()


@pytest.fixture
def client(engine):
    app = create_app()
    # startup 事件会再次 init_db，对已存在的表无副作用
    with TestClient(app) as c:
        yield c
