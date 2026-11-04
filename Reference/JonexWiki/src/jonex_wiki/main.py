import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from jonex_wiki.api.deps import make_request_id
from jonex_wiki.application import WikiApplication
from jonex_wiki.kernel.errors import DomainError


def _error_body(code: str, message: str, rid: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": rid}, "request_id": rid}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    if settings is None:
        yield  # 测试 base app(无依赖接线)不跑生命周期
        return
    # 接线全部下沉到 WikiApplication(B1:进程内门面,引擎独立服务与平台能力共用)
    await app.state.app.start()
    # 镜像状态到 app.state,兼容现有 router 对 app.state.{session_factory,executor,layout,gw} 的访问
    app.state.settings = app.state.app.settings
    app.state.session_factory = app.state.app.session_factory
    app.state.executor = app.state.app.executor
    app.state.layout = app.state.app.layout
    app.state.gw = app.state.app.gw
    yield
    await app.state.app.stop()


def create_app(settings=None, session_factory=None, executor=None):
    if settings is None:
        from jonex_wiki.infra.settings import Settings
        settings = Settings()   # 生产:factory 模式无参构造;缺必填 env 在此 fail-fast
    from importlib.metadata import version
    app = FastAPI(title="Jonex Wiki", version=version("jonex-wiki"), lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.executor = executor
    # 进程内门面:router 委托与平台能力共用同一实例;lifespan 只负责 start/stop 接线
    app.state.app = WikiApplication(settings, session_factory, executor)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = make_request_id()
        request.state.request_id = rid
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 —— 异常路径也交给 FastAPI 处理器,但必须设头(spec §7.4)
            raise
        response.headers["X-Request-Id"] = rid
        return response

    def _rid(request: Request) -> str:
        return getattr(request.state, "request_id", "")

    # 显式注册三个处理器(FastAPI 对 HTTPException/RequestValidationError 有更具体的内置处理器,
    # 只注册 Exception 不会接管它们——统一 envelope 必须三处都注册)
    @app.exception_handler(DomainError)
    async def domain_handler(request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.http_status, content=_error_body(exc.code, str(exc), _rid(request)),
                            headers={"X-Request-Id": _rid(request)})

    @app.exception_handler(HTTPException)
    async def http_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        code = detail.get("code", "validation_failed") if isinstance(detail, dict) else "validation_failed"
        msg = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
        return JSONResponse(status_code=exc.status_code, content=_error_body(code, msg, _rid(request)),
                            headers={"X-Request-Id": _rid(request)})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        msg = str(exc.errors()[:3])
        return JSONResponse(status_code=422, content=_error_body("validation_failed", msg, _rid(request)),
                            headers={"X-Request-Id": _rid(request)})

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        logging.getLogger("jonex_wiki").exception("unhandled error")
        return JSONResponse(status_code=500,
                            content=_error_body("internal_error", "服务内部错误", _rid(request)),
                            headers={"X-Request-Id": _rid(request)})

    @app.get("/healthz")
    async def healthz():
        return JSONResponse({"ok": True})

    @app.get("/readyz")
    async def readyz():
        sf = app.state.session_factory
        if sf is None:
            return JSONResponse({"ok": True})  # 未接库阶段默认就绪(测试)
        async with sf() as s:
            await s.execute(__import__("sqlalchemy").text("SELECT 1"))
        return JSONResponse({"ok": True})

    # 路由挂载(各 router 随任务逐步加入)
    from jonex_wiki.api.routers import compile as compile_router
    from jonex_wiki.api.routers import documents as documents_router
    from jonex_wiki.api.routers import kbs as kbs_router
    from jonex_wiki.api.routers import outline as outline_router
    from jonex_wiki.api.routers import pages as pages_router
    from jonex_wiki.api.routers import prompts as prompts_router
    from jonex_wiki.api.routers import query as query_router
    from jonex_wiki.api.routers import tasks as tasks_router

    app.include_router(tasks_router.router)
    app.include_router(documents_router.router)
    app.include_router(kbs_router.router)
    app.include_router(pages_router.router)
    app.include_router(outline_router.router)
    app.include_router(prompts_router.router)
    app.include_router(compile_router.router)
    app.include_router(query_router.router)
    from jonex_wiki.api.routers.scenarios import router as scenarios_router
    app.include_router(scenarios_router)

    return app


def run() -> None:
    import uvicorn

    host = os.environ.get("JONEX_WIKI_API_HOST", "0.0.0.0")   # 容器可达:默认绑 0.0.0.0
    port = int(os.environ.get("JONEX_WIKI_API_PORT", "8007"))
    uvicorn.run("jonex_wiki.main:create_app", factory=True, host=host, port=port)
