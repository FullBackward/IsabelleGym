# IsabelleGym Server — Issue Investigation & Fix Plan

**Date:** 2026-06-02 (revised)
**Scope:** Server layer (`server/`), REPL layer (`repl/`)
**Status:** Sledgehammer is implemented and verified — its section has been removed from this file (see `claude-work/impl-sledgehammer/` for the test/demonstration artifacts and notes). What remains below is the memory-management / session-closing review, re-checked against the current code.

---

## Table of Contents

1. [Memory Management / Session Closing](#memory-management--session-closing)
   - [Bug 1: Race Condition in `ThreadedBackend.close()` — RESOLVED](#bug-1-race-condition-in-threadedbackendclose--resolved)
   - [Bug 2: Join Timeout Too Short — RESOLVED](#bug-2-join-timeout-too-short--resolved)
   - [Bug 3: Leased Sessions Never Idle-Evicted — OPEN](#bug-3-leased-sessions-never-idle-evicted--open)
   - [Bug 4: TOCTOU on `in_use` Check — OPEN](#bug-4-toctou-on-in_use-check--open)
   - [Bug 5: Isabelle Processes Persist After Close — OPEN](#bug-5-isabelle-processes-persist-after-close--open)

---

## Memory Management / Session Closing

The full close call chain (current code) is:

```
HTTP DELETE /sessions/{id}
  → asyncio.to_thread(session_manager.close_session(...))   [session_manager.py:497]
  → session.close()                                         [session.py:562]
  → threaded_backend.close()                                [threaded_backend.py:62]
  → submit(self._backend.exit) on the worker thread         [threaded_backend.py:64]
  → ReplBackend.exit()   ← Py4J call to Scala               [repl_backend.scala:157]
  → Repl_ML_Communication.clear_channel(channel_id)
  → session_manager_instance.shutdown()                     [session_manager.scala:400]
  → remove_session_async(...) per running session  +  Server_Utils.stop_server(...)
```

**Status summary:** Bugs 1 and 2 (the original high-severity Python-side close bugs) are now fixed in the current code. Bugs 3 and 4 remain open; the suggested fixes were re-checked against the current code and still apply. Bug 5 is new: it captures the still-observed symptom that Isabelle OS processes linger after a session is closed, which the Python-side fixes do **not** explain — the remaining cause is in the Scala/Isabelle shutdown path.

---

### Bug 1: Race Condition in `ThreadedBackend.close()` — RESOLVED

**File:** `server/app/services/threaded_backend.py`
**Severity:** High
**Status:** ✅ Resolved — current code matches the recommended fix.

#### Original root cause

The old `close()` called `self._backend.exit()` directly from the calling thread, bypassing the single-worker serialisation. Because the Py4J gateway uses one socket connection and the Scala `ReplBackend` is not thread-safe, that could collide with an in-flight job on the worker thread (`Py4JNetworkError` / corrupted Scala state).

#### Verification (current code)

`exit()` is now submitted through the job queue and awaited, so it is serialised after any pending job — exactly the recommended fix:

```python
def close(self) -> None:
    logger.info("closing threaded backend worker=%s", self._name)
    exit_fut = self.submit(self._backend.exit)   # queued, not called directly
    try:
        exit_fut.result(timeout=self.EXIT_TIMEOUT)
    except Exception:
        logger.exception("backend exit raised during close worker=%s", self._name)
    finally:
        self._stop.set()
        self._t.join(timeout=self.JOIN_TIMEOUT)
        logger.info("threaded backend closed worker=%s", self._name)
```

No further action needed.

---

### Bug 2: Join Timeout Too Short — RESOLVED

**File:** `server/app/services/threaded_backend.py`
**Severity:** High
**Status:** ✅ Resolved — timeouts are now configurable and generous.

#### Original root cause

The old code used `self._t.join(timeout=2.0)`. The Scala `shutdown()` (remove sessions, join async removals, stop the Isabelle server) routinely takes 5–30 s for any non-trivial theory, so the join timed out and `close()` returned while the worker (and Isabelle subprocess) was still alive.

#### Verification (current code)

The class reads its timeouts from config (`server/app/core/config.py`), defaulting to values long enough for the Scala shutdown:

```python
class ThreadedBackend:
    EXIT_TIMEOUT: float = Repl.BACKEND_EXIT_TIMEOUT   # ISABELLE_BACKEND_EXIT_TIMEOUT, default 60.0
    JOIN_TIMEOUT: float = Repl.BACKEND_JOIN_TIMEOUT   # ISABELLE_BACKEND_JOIN_TIMEOUT, default 5.0
    QUEUE_POLL_TIMEOUT: float = Repl.BACKEND_QUEUE_POLL  # default 0.1
```

`exit_fut.result(timeout=EXIT_TIMEOUT)` waits up to 60 s for the Scala shutdown, and the subsequent join is for a loop that has already stopped. No further action needed.

> Caveat: this guarantees the *Python* call waits for `exit()` to *return*. It does not by itself guarantee the Isabelle OS process actually died — see Bug 5.

---

### Bug 3: Leased Sessions Never Idle-Evicted — OPEN

**File:** `server/app/services/session_manager.py`, `cleanup_idle_sessions()` (lines ~546–561)
**Severity:** Medium
**Status:** ❌ Open — fix below re-verified against current code and ready to apply.

#### Root cause (still present)

```python
async def cleanup_idle_sessions(self) -> None:
    while True:
        await asyncio.sleep(self.cleanup_interval)
        ...
        for sid, session in list(self._lru.items()):
            if session.leased:
                continue  # never evict a leased session   <-- unconditional
```

If a client acquires a lease and then disconnects (crash, network failure, killed process), the session is never reclaimed — a steady leak for ML training loops that lease many sessions. This is a likely contributor to pool exhaustion (`PoolExhausted` / HTTP 503 on `acquire`).

#### Fix

Force-close leases that have been idle longer than `idle_timeout * 2`. The helpers used below (`session.is_idle`, `session.last_activity`, `session.lease_id`, `session.status`, `close_session(require_lease=False)`) all exist in the current code.

```python
async def cleanup_idle_sessions(self) -> None:
    max_lease_age = self.idle_timeout * 2  # abandoned leases reclaimed after this

    while True:
        await asyncio.sleep(self.cleanup_interval)
        now = time.time()
        to_close: List[uuid.UUID] = []

        with self._lock:
            for sid, session in list(self._lru.items()):
                if session.status == SessionStatus.CLOSED:
                    continue
                if session.leased:
                    if session.is_idle(max_lease_age, now=now):
                        logger.warning(
                            "force-closing abandoned leased session "
                            "session_id=%s lease_id=%s idle_for=%.0fs",
                            sid, session.lease_id, now - session.last_activity,
                        )
                        to_close.append(sid)
                    continue
                if session.is_idle(self.idle_timeout, now=now):
                    to_close.append(sid)

        for sid in to_close:
            logger.info("closing idle session session_id=%s", sid)
            try:
                self.close_session(sid, require_lease=False)
            except Exception:
                logger.exception("failed to close idle session session_id=%s", sid)
```

Optionally make `max_lease_age` configurable via `ISABELLE_MAX_LEASE_AGE_SECONDS` in `server/app/core/config.py`.

> Note: also confirm `start_cleanup_task()` is actually invoked at startup — the cleanup coroutine only runs if `start_cleanup_task()` is called (e.g. from the FastAPI lifespan). If it is never started, *no* idle eviction happens at all (leased or not).

---

### Bug 4: TOCTOU on `in_use` Check — OPEN

**File:** `server/app/services/session_manager.py`, `close_session()` (lines ~497–521)
**Severity:** Low
**Status:** ❌ Open — suggestion still valid; tiny residual race.

#### Root cause (still present)

```python
with self._lock:
    session = self._lru.get(sid)
    ...
    if session.in_use:
        raise SessionBusyError(...)
    self._lru.pop(sid, None)        # no re-check after pop
    if session.leased:
        session.release_lease()
```

`in_use` reads `_active_requests` under `_active_requests_lock`, not under `_lock`, so a concurrent handler could call `_acquire_request()` between the check and the `pop()`. The window is microseconds and unlikely in a single-server deployment, but it is a logical correctness issue.

#### Fix

Re-check `in_use` after `pop()`; if a request slipped through, restore the session and raise:

```python
with self._lock:
    session = self._lru.get(sid)
    if session is None:
        raise SessionNotFound(f"Session {sid} not found")
    if session.status == SessionStatus.CLOSED:
        raise SessionNotFound(f"Session {sid} is closed")
    if require_lease:
        session.require_lease(lease_id)
    if session.in_use:
        raise SessionBusyError(f"Session {sid} is busy and cannot be closed")
    self._lru.pop(sid, None)
    if session.in_use:                       # re-check after removing from LRU
        self._lru[sid] = session             # restore
        self._lru.move_to_end(sid)
        raise SessionBusyError(
            f"Session {sid} became busy between in_use check and pop"
        )
    if session.leased:
        session.release_lease()
```

Once `pop()` succeeds, no new `get_session()` can return this session, so no new `_acquire_request()` can start; only a request already past `get_session()` but not yet at `_acquire_request()` remains a (tiny) residual risk.

---

### Bug 5: Isabelle Processes Persist After Close — OPEN

**Files:** `repl/src/main/scala/repl/session_manager.scala` (`shutdown`, `remove_session_async`), `repl/src/main/scala/repl/server_utils.scala` (`stop_server`, `stop_session`)
**Severity:** High (operational — causes OOM over time)
**Status:** ❌ Open / not yet root-caused. This is the observed symptom: after sessions are closed, `isabelle`/`poly` processes keep running in the background and accumulate until the container is OOM-killed (exit 137). Bugs 1–2 being fixed means the Python side now correctly waits for `exit()` to return, so the remaining cause is in the Scala/Isabelle shutdown path.

#### Leads to investigate

1. **Async removals may outrace server stop.** `Session_Manager.shutdown()` forks `remove_session_async` per running session, then joins `pending_removals`, then calls `Server_Utils.stop_server`. Each backend has its **own** `Session_Manager` and therefore its **own** Isabelle server process (`Server.init` in `start_server`). Confirm every `remove_session_async` future is actually in `pending_removals` before the join (it is added synchronously today) and that `stop_session` (`Server_Commands.Session_Stop`) returns a success `return_code` rather than erroring out silently.

2. **`Server.exit(name)` may not kill the OS process.** `stop_server` calls `Server.exit(server_info.name)`, which asks the Isabelle server to shut down over its socket. If the server process (or a child `poly`/ML process) is mid-computation or wedged, it may not terminate. Verify with `ps` inside the container before/after a close that the specific server PID actually exits.

3. **A forked sledgehammer thread can hold the ML process open.** The standalone sledgehammer channel forks an `Isabelle_Thread` (see `claude-work/impl-sledgehammer/`). If a session is closed while that thread is still running a prover, the ML process may refuse to exit until the thread (or its external ATP subprocesses) finishes. Reproduce by closing a session immediately after firing sledgehammer and checking for leftover prover/`poly` processes.

4. **No OS-level reaping.** There is no fallback that force-kills a server PID if graceful `Server.exit` fails. Consider tracking the spawned server PID and `kill`-ing it (and orphaned ATP children) as a last resort during `shutdown()`.

#### Suggested next step

Add `ps -ef | grep -E "isabelle|poly|server"` probes around a single create→close cycle in the container to confirm which process survives, then decide whether the fix belongs in `stop_session` (per-session ML), `stop_server` (`Server.exit`), or a new force-kill fallback. Until then, the operational workaround is to **restart the container between runs** (already the established dev workflow).

---

## Summary of Changes

| File | Change | Status |
|---|---|---|
| `server/app/services/threaded_backend.py` | Queue `exit()` + configurable `EXIT_TIMEOUT`/`JOIN_TIMEOUT` | ✅ Done (Bug 1, Bug 2) |
| `server/app/services/session_manager.py` | `cleanup_idle_sessions()`: add `max_lease_age` force-eviction path | ❌ Pending (Bug 3) |
| `server/app/services/session_manager.py` | `close_session()`: re-check `in_use` after `pop()` | ❌ Pending (Bug 4) |
| `repl/.../session_manager.scala`, `repl/.../server_utils.scala` | Ensure Isabelle server/session OS processes actually terminate on close | ❌ Pending / investigate (Bug 5) |
