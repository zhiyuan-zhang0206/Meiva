import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor

from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base.agents.upload_delivery.models import ObjectReadRequest, UploadDeliveryConflictError
from base.api_contracts.agent_contract import AgentNotFound
from ops.upload_delivery import ReceiverConfig, read_delivered_object
from services.agent_runner.agent_ops import in_flight


def upload_object_response(
    body: bytes, *, pool: ConnectionPool, receiver: ReceiverConfig
) -> tuple[int, bytes, str]:
    try:
        request = ObjectReadRequest.model_validate_json(body)
        content, content_type = read_delivered_object(pool, request, receiver)
    except ValidationError:
        return 422, b"Invalid upload object request", "text/plain"
    except (AgentNotFound, UploadDeliveryConflictError, OSError):
        return 404, b"Delivered upload file is no longer available", "text/plain"
    return 200, content, content_type


async def upload_object_route(
    body: bytes,
    *,
    pool: ConnectionPool,
    receiver: ReceiverConfig,
    executor: ThreadPoolExecutor,
    dispatch_sem: asyncio.Semaphore,
    requests: in_flight.RequestTokens,
    workers: in_flight.WorkerFutures,
) -> tuple[int, bytes, str]:
    with in_flight.admitted(requests=requests):
        async with dispatch_sem:
            future = asyncio.get_running_loop().run_in_executor(
                executor,
                functools.partial(upload_object_response, body, pool=pool, receiver=receiver),
            )
            in_flight.track_worker(future, workers=workers)
            return await asyncio.shield(future)
