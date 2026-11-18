import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.errors import AppError
from app.db.session import SessionLocal
from app.features import (
    abk,
    abk_admin,
    abk_egg_sales,
    auth,
    farm,
    internal_references,
    inventory_integration,
    lele_biomass,
    people,
)
from app.features import settings as settings_feature

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings.validate_runtime_secrets()
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    summary="Daily farm-operations API",
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

if settings.cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-ABK-Username", "X-ABK-PIN"],
    )


@app.exception_handler(AppError)
def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    body: dict[str, Any] = {"code": exc.code, "detail": exc.detail}
    if exc.errors:
        body["errors"] = exc.errors
    body.update(exc.extra)
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
    )


@app.exception_handler(RequestValidationError)
def request_validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors: dict[str, list[str]] = {}
    for item in exc.errors():
        location = ".".join(str(part) for part in item["loc"] if part not in ("body", "query", "path")) or "request"
        errors.setdefault(location, []).append(item["msg"])
    return JSONResponse(
        status_code=422, content={"code": "VALIDATION_ERROR", "detail": "The request is invalid.", "errors": errors}
    )


@app.exception_handler(IntegrityError)
def integrity_error_handler(_: Request, exc: IntegrityError) -> JSONResponse:
    logging.getLogger(__name__).info("Database constraint rejected request", exc_info=exc)
    return JSONResponse(
        status_code=409, content={"code": "DATABASE_CONFLICT", "detail": "The operation conflicts with existing data."}
    )


@app.get("/health/live", tags=["System"])
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["System"])
def health_ready() -> dict[str, str]:
    with SessionLocal() as db:
        db.execute(text("select 1"))
    return {"status": "ready"}


for feature_router in (
    auth.router,
    people.router,
    farm.router,
    lele_biomass.router,
    settings_feature.router,
    abk.router,
    abk_admin.router,
    abk_egg_sales.router,
    inventory_integration.router,
    internal_references.router,
):
    app.include_router(feature_router, prefix="/api/v1")


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, summary=app.summary, routes=app.routes)
    security_schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    security_schemes["BearerAuth"] = {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    security_schemes["InternalApiKeyAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-FM-Internal-Key",
    }
    # ABK Web App: no login, both headers on every request.
    security_schemes["AbkUsername"] = {"type": "apiKey", "in": "header", "name": "X-ABK-Username"}
    security_schemes["AbkPin"] = {"type": "apiKey", "in": "header", "name": "X-ABK-PIN"}
    for path, operations in schema.get("paths", {}).items():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            if path == "/api/v1/auth/login":
                operation.pop("security", None)
            elif path.startswith("/api/v1/internal/"):
                operation["security"] = [{"InternalApiKeyAuth": []}]
            elif path.startswith("/api/v1/abk/"):
                operation["security"] = [{"AbkUsername": [], "AbkPin": []}]
            elif path.startswith("/api/v1/"):
                operation["security"] = [{"BearerAuth": []}]
            else:
                operation.pop("security", None)
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]
