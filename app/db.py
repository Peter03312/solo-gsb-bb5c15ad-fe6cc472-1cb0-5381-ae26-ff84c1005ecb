"""SQLAlchemy 引擎与会话管理。

并发正确性依赖单条事务 + 行锁：
- PostgreSQL：读批次时使用 ``SELECT ... FOR UPDATE``，串行化对同一批次余量的扣减；
- SQLite（本地开发/测试）：所有写事务以 ``BEGIN IMMEDIATED`` 开始，直接升级为
  全库写锁，配合 WAL 与 busy timeout，同样能拒绝并发超用。
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def is_sqlite(engine: Engine | None = None) -> bool:
    eng = engine or _engine
    return bool(eng is not None and eng.dialect.name == "sqlite")


def init_engine(url: str | None = None) -> Engine:
    """（重新）初始化全局引擎；测试时用内存/临时库覆盖。"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    target = url or config.DATABASE_URL
    sqlite = target.startswith("sqlite")
    if sqlite:
        connect_args = {"timeout": 20, "check_same_thread": False}
        # 静态池让内存库在多线程/多连接间共享同一份数据
        if ":memory:" in target:
            from sqlalchemy.pool import StaticPool

            connect_args["poolclass"] = StaticPool
        _engine = create_engine(target, connect_args=connect_args, future=True)

        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            # 关闭 pysqlite 自带的事务管理，改由 begin 事件显式发起 BEGIN IMMEDIATE
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=20000")
            cur.close()

        # 让 SQLAlchemy 的事务以 BEGIN IMMEDIATE 开始，写事务全局串行
        @event.listens_for(_engine, "begin")
        def _sqlite_begin_immediate(conn):  # noqa: ANN001
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        _engine = create_engine(
            target,
            pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
            max_overflow=10,
            pool_pre_ping=True,
            future=True,
        )
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def init_db() -> None:
    """建表（幂等），应用启动与容器入口都会调用。"""
    # 触发模型注册
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())
    _migrate_additive_columns(get_engine())
    _backfill_locations(get_engine())


def _migrate_additive_columns(engine: Engine) -> None:
    """对既有数据库做纯增量补列（新字段均可空/有默认值，不触碰历史数据）。

    历史版本的 productions 表没有撤销字段，这里以 ALTER TABLE ... ADD COLUMN
    补齐；全新库由 create_all 直接带列创建，检查到列已存在则跳过。

    注意：必须在开启写事务之前完成 inspection——SQLite 写事务以
    BEGIN IMMEDIATE 持有全库写锁，若在同一写事务内再开第二个连接做
    inspector 查询会自死锁（busy_timeout 后报 database is locked）。
    """
    # (列名, PostgreSQL 列定义, SQLite 列定义)：仅新增可空或带默认值的列
    additions: dict[str, list[tuple[str, str, str]]] = {
        "productions": [
            (
                "is_cancelled",
                "BOOLEAN NOT NULL DEFAULT FALSE",
                "BOOLEAN NOT NULL DEFAULT 0",
            ),
            ("cancel_reason", "TEXT", "TEXT"),
            ("cancelled_at", "TIMESTAMPTZ", "DATETIME"),
        ],
        "production_inputs": [
            ("location_code", "VARCHAR(32)", "VARCHAR(32)"),
        ],
    }

    inspector = inspect(engine)
    missing: list[tuple[str, str, str]] = []
    is_pg = engine.dialect.name == "postgresql"
    for table, columns in additions.items():
        if not inspector.has_table(table):
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, pg_def, sqlite_def in columns:
            if name in existing:
                continue
            missing.append((table, name, pg_def if is_pg else sqlite_def))

    if not missing:
        return

    # 再用单一写事务一次性补列
    with engine.begin() as conn:
        for table, name, column_def in missing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {column_def}"))


def _backfill_locations(engine) -> None:
    """为引入库位层之前的既有数据回填默认库位。

    - 每个尚缺任何库位行的批次：以其当前全局余量在默认库位建行
      （新批次在业务事务内建行，这里通常只处理升级前的老批次）；
    - 每笔尚无逐库位消耗明细的投料行：整笔记入默认库位。
      这样撤销历史生产单时统一按“原消耗库位”返还到默认库位。

    幂等：以 NOT EXISTS 谓词跳过已回填/已新建的数据，重复启动无副作用。
    整个回填写事务与业务事务一样在 SQLite 上以 BEGIN IMMEDIATE 串行。
    """
    from .models import DEFAULT_LOCATION_CODE

    inspector = inspect(engine)
    if not inspector.has_table("batches"):
        return
    # 注意：inspection 必须在写事务开始前完成（同 _migrate_additive_columns
    # 的约束）——SQLite 写事务持 BEGIN IMMEDIATE 全库写锁，事务内再借连接
    # 做 inspector 查询会自死锁。
    has_inputs = inspector.has_table("production_inputs") and inspector.has_table(
        "production_input_locations"
    )

    default = DEFAULT_LOCATION_CODE
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO batch_locations (batch_no, location_code, qty, created_at)
                SELECT b.batch_no, :loc, b.remaining_qty, CURRENT_TIMESTAMP
                FROM batches b
                WHERE NOT EXISTS (
                    SELECT 1 FROM batch_locations bl WHERE bl.batch_no = b.batch_no
                )
                """
            ),
            {"loc": default},
        )
        if has_inputs:
            conn.execute(
                text(
                    """
                    INSERT INTO production_input_locations
                        (production_input_id, location_code, qty)
                    SELECT pi.id, :loc, pi.qty
                    FROM production_inputs pi
                    WHERE NOT EXISTS (
                        SELECT 1 FROM production_input_locations pil
                        WHERE pil.production_input_id = pi.id
                    )
                    """
                ),
                {"loc": default},
            )


@contextmanager
def session_scope() -> Iterator[Session]:
    """业务事务边界：业务异常回滚，正常结束提交。"""
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI 依赖。"""
    with session_scope() as session:
        yield session


def ping() -> None:
    """供容器入口等待数据库就绪。"""
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
        conn.commit()
