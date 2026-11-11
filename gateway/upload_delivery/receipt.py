# meiva:note
# One `staged_upload_received` event per staged-upload request: how big the body was, how long the
# gateway waited for it, which public path it came in on, and how the request ended.
#
# FastAPI parses the multipart body before the endpoint runs, so the endpoint cannot see when the
# last byte arrived. The route class below hands the unchanged handler a `Request` whose `receive`
# callable counts the body chunks and notes the instant the final one arrives; the upload is still
# read by the framework exactly as before. Time is measured from the moment the route handler
# starts (after the auth middleware), so `receive_ms` is the wait for the client's bytes and
# `after_ms` is everything the gateway did once it had them. A request that ends before the last
# byte (client disconnect) has `receive_ms` of `None` and status 499.

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.types import Message, Receive

from base import telemetry
from gateway.http.config import gateway_clock
from gateway.upload_delivery.ingress import classify_ingress

__all__ = ["StagedUploadRoute"]

CLIENT_CLOSED_STATUS = 499


# meiva:note
# Counts the body bytes a request's `receive` yields and records when the last chunk arrived.
class _BodyMeter:
    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self.received = 0
        self.completed_at: float | None = None

    def wrap(self, receive: Receive) -> Receive:
        async def metered() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                self.received += _chunk_size(message.get("body"))
                if not message.get("more_body", False):
                    self.completed_at = self._now()
            return message

        return metered


def _chunk_size(body: object) -> int:
    return len(body) if isinstance(body, bytes) else 0


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


class StagedUploadRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[object, object, Response]]:
        handle = super().get_route_handler()
        template = self.path

        async def timed(request: Request) -> Response:
            now = gateway_clock(request).monotonic
            started = now()
            meter = _BodyMeter(now)
            metered = Request(request.scope, meter.wrap(request.receive))
            status = 500
            try:
                response = await handle(metered)
                status = response.status_code
                return response
            except HTTPException as exc:
                status = exc.status_code
                raise
            except RequestValidationError:
                status = 422
                raise
            except asyncio.CancelledError:
                status = CLIENT_CLOSED_STATUS
                raise
            finally:
                _emit(request, template, status, meter, started, now())

        return timed


def _emit(
    request: Request, template: str, status: int, meter: _BodyMeter, started: float, ended: float
) -> None:
    ingress = classify_ingress(request.headers)
    done = meter.completed_at
    attributes: dict[str, object] = {
        "route": template,
        "status": status,
        "body_bytes": meter.received,
        "content_length": ingress.content_length,
        "receive_ms": None if done is None else _ms(done - started),
        "after_ms": None if done is None else _ms(ended - done),
        "total_ms": _ms(ended - started),
        "via_cloudflare": ingress.via_cloudflare,
        "cf_colo": ingress.cf_colo,
        "cf_connecting_ip": ingress.cf_connecting_ip,
        "host": ingress.host,
    }
    telemetry.emit("telemetry", "staged_upload_received", attributes=attributes)
