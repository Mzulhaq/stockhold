"""Bounded JSON API; database errors never expose SQL or connection secrets."""

import json
import math

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from stockhold.store import Database, DomainError


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _finite(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Nonfinite number")
    return parsed


async def read_body(request):
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4096:
                raise HTTPException(413, "Request exceeds 4 KiB")
            body.extend(chunk)
    except ClientDisconnect as error:
        raise HTTPException(400, "Incomplete request body") from error
    return bytes(body)


async def read_payload(request, required, optional=frozenset()):
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(415, "Content-Type must be application/json")
    body = await read_body(request)
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique,
            parse_constant=_finite,
            parse_float=_finite,
        )
    except (ValueError, RecursionError) as error:
        raise HTTPException(400, "Invalid JSON") from error
    if not isinstance(payload, dict) or not required <= set(payload) <= required | optional:
        raise HTTPException(422, "Invalid request fields")
    return payload


def create_app(db: Database):
    app = FastAPI(title="StockHold", version="0.1.0")

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse({"error": error.code, "detail": error.message}, error.status)

    @app.exception_handler(ValueError)
    async def invalid_value(request, error):
        return JSONResponse({"error": "invalid_request", "detail": str(error)}, 422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        return JSONResponse(
            {"error": "database_unavailable", "detail": "Database unavailable"}, 503
        )

    @app.get("/health")
    def health():
        if not db.ping():
            raise HTTPException(503, "Database schema unavailable")
        return {"status": "ok"}

    @app.post("/v1/products", status_code=201)
    async def create_product(request: Request):
        payload = await read_payload(request, {"sku", "quantity"})
        return await run_in_threadpool(db.create_product, payload["sku"], payload["quantity"])

    @app.get("/v1/products/{sku}")
    def get_product(sku: str):
        return db.get_product(sku)

    @app.post("/v1/reservations")
    async def reserve(request: Request):
        payload = await read_payload(request, {"sku", "quantity"}, {"ttl_seconds"})
        result = await run_in_threadpool(
            db.reserve,
            payload["sku"],
            payload["quantity"],
            payload.get("ttl_seconds", 300),
            request.headers.get("idempotency-key"),
        )
        return JSONResponse(result, 200 if result["replayed"] else 201)

    @app.get("/v1/reservations/{reservation_id}")
    def get_reservation(reservation_id: str):
        return db.get_reservation(reservation_id)

    async def change_state(request, reservation_id, action):
        if (await read_body(request)).strip() not in (b"", b"{}"):
            raise HTTPException(422, "Actions accept an empty body or empty JSON object")
        return await run_in_threadpool(db.transition, reservation_id, action)

    @app.post("/v1/reservations/{reservation_id}/confirm")
    async def confirm(reservation_id: str, request: Request):
        return await change_state(request, reservation_id, "confirm")

    @app.post("/v1/reservations/{reservation_id}/cancel")
    async def cancel(reservation_id: str, request: Request):
        return await change_state(request, reservation_id, "cancel")

    return app
