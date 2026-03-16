from __future__ import annotations

import concurrent.futures
import contextvars
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable

from repl.src.python.repl_backend_gateway import ReplBackend
from server.app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _Job:
    fn: Callable[[], Any]
    fut: concurrent.futures.Future
    ctx: contextvars.Context


class ThreadedBackend:
    def __init__(self, backend: ReplBackend, name: str):
        self._backend = backend
        self._name = name
        self._q: queue.Queue[_Job] = queue.Queue()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name=name, daemon=True)
        self._t.start()
        logger.info("threaded backend started worker=%s", name)

    @property
    def raw(self) -> ReplBackend:
        return self._backend

    def submit(self, fn: Callable[[], Any]) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put(_Job(fn=fn, fut=fut, ctx=contextvars.copy_context()))
        logger.debug("job submitted to threaded backend worker=%s queue_size=%s", self._name, self._q.qsize())
        return fut

    def _run(self) -> None:
        logger.info("threaded backend worker loop running worker=%s", self._name)
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                result = job.ctx.run(job.fn)
                job.fut.set_result(result)
            except Exception as e:
                logger.exception("threaded backend job failed worker=%s", self._name)
                job.fut.set_exception(e)
        logger.info("threaded backend worker loop stopped worker=%s", self._name)

    def close(self) -> None:
        logger.info("closing threaded backend worker=%s", self._name)
        try:
            self._backend.exit()
        finally:
            self._stop.set()
            self._t.join(timeout=2.0)
            logger.info("threaded backend closed worker=%s", self._name)
