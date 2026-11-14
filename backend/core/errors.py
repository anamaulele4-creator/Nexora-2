"""Erros da API com um formato estável: {"error": {"code", "message"}}."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def _body(code: str, message: str, request: Request, details: object | None = None) -> dict:
    err: dict = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    rid = getattr(request.state, "request_id", None)
    if rid:
        err["request_id"] = rid
    return {"error": err}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, request), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(p) for p in e.get("loc", [])[1:]), "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        return JSONResponse(_body("validation_error", "Dados inválidos", request, details), status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover - rede de segurança
        import logging

        logging.getLogger("connector_ai").exception("erro não tratado", exc_info=exc)
        return JSONResponse(_body("internal_error", "Erro interno", request), status_code=500)
