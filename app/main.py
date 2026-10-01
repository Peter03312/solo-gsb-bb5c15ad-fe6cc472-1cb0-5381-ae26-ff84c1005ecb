"""FastAPI 应用入口：异常处理、启动建表、路由注册。"""
from __future__ import annotations

import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db
from .errors import AppError
from .routes import router

logger = logging.getLogger("batch_trace")


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.init_db()
        logger.info("数据库表已就绪")
        yield

    app = FastAPI(
        title="批次追溯服务",
        description=(
            "小型食品加工厂本地批次追溯：登记、分装、投料、撤销、余量、"
            "库位库存与调拨、谱系、污染隔离"
        ),
        version="1.4.0",
        lifespan=lifespan,
    )

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        def _safe(obj: object) -> object:
            if isinstance(obj, dict):
                return {k: _safe(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_safe(v) for v in obj]
            if isinstance(obj, (str, int, float, bool)) or obj is None:
                return obj
            return str(obj)

        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求参数无效（数量必须为正数等），整笔请求已拒绝",
                    "details": _safe(exc.errors()),
                }
            },
        )

    app.include_router(router)

    return app


app = create_app()
