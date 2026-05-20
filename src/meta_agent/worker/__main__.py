"""Worker process entrypoint: ``python -m meta_agent.worker``.

Reads settings from the environment, wires the loop via
:func:`meta_agent.worker.bootstrap.build_worker`, and drives
:meth:`WorkerLoop.run_forever` until ``SIGINT`` / ``SIGTERM`` triggers
a graceful shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from meta_agent.infra.observability import configure_logging
from meta_agent.worker.bootstrap import build_worker, build_worker_settings_from_env

logger = logging.getLogger(__name__)

_SHUTDOWN_TIMEOUT_SECONDS = 10.0


async def _amain() -> None:
    configure_logging()
    settings = await build_worker_settings_from_env()
    logger.info(
        "worker.startup",
        extra={
            "db_url": settings.db_url,
            "redis_url": settings.redis_url,
            "topic": settings.task_topic,
            "consumer_group": settings.consumer_group,
            "consumer_name": settings.consumer_name,
        },
    )
    runtime = await build_worker(settings)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_shutdown(signame: str) -> None:
        logger.info("worker.signal_received", extra={"signal": signame})
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_shutdown, sig.name)

    run_task = asyncio.create_task(runtime.worker.run_forever(), name="worker-loop")
    stop_task = asyncio.create_task(stop_event.wait(), name="worker-stop")
    try:
        await asyncio.wait(
            {run_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_event.is_set():
            await runtime.worker.stop()
            try:
                await asyncio.wait_for(run_task, timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("worker.shutdown_timeout, cancelling")
                run_task.cancel()
                with contextlib.suppress(BaseException):
                    await run_task
        elif (exc := run_task.exception()) is not None:
            logger.error("worker.loop_crashed", exc_info=exc)
            raise exc
    finally:
        stop_task.cancel()
        with contextlib.suppress(BaseException):
            await stop_task
        logger.info("worker.shutdown")
        await runtime.aclose()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
