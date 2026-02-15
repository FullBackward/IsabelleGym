import threading
import queue
import concurrent.futures
from dataclasses import dataclass
from typing import Any, Callable
from repl.src.python.repl_backend_gateway import ReplBackend

@dataclass
class _Job:
    fn: Callable[[], Any]
    fut: concurrent.futures.Future

class ThreadedBackend:
    def __init__(self, backend: ReplBackend, name: str):
        self._backend = backend
        self._q: queue.Queue[_Job] = queue.Queue()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name=name, daemon=True)
        self._t.start()
    
    @property
    def raw(self) -> ReplBackend:
        return self._backend

    def submit(self, fn: Callable[[], Any]) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put(_Job(fn=fn, fut=fut))
        return fut

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                job.fut.set_result(job.fn())
            except Exception as e:
                job.fut.set_exception(e)

    def close(self) -> None:
        self._backend.exit()
        self._stop.set()
        self._t.join(timeout=2.0)