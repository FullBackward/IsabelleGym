# IsabelleGym Server — Issue Investigation & Fix Plan

**Date:** 2026-06-04 (revised)
**Scope:** Server layer (`server/`), REPL layer (`repl/`)
**Status:** Sledgehammer is implemented and verified — its section has been removed from this file (see `claude-work/impl-sledgehammer/` for the test/demonstration artifacts and notes). What remains below is the memory-management / session-closing review, re-checked against the current code.

---

## Table of Contents

1. [Memory Management / Session Closing](#memory-management--session-closing)
   - [Bug 1: Race Condition in `ThreadedBackend.close()` — RESOLVED](#bug-1-race-condition-in-threadedbackendclose--resolved)
   - [Bug 2: Join Timeout Too Short — RESOLVED](#bug-2-join-timeout-too-short--resolved)
   - [Bug 3: Leased Sessions Never Idle-Evicted — RESOLVED](#bug-3-leased-sessions-never-idle-evicted--open)
   - [Bug 4: TOCTOU on `in_use` Check — RESOLVED](#bug-4-toctou-on-in_use-check--open)
   - [Bug 5: Isabelle Processes Persist After Close — RESOLVED](#bug-5-isabelle-processes-persist-after-close--open)
2. [Bug 6: Gateway OOM Under Concurrent Sledgehammer — RESOLVED](#bug-6-gateway-oom-under-concurrent-sledgehammer--resolved)
3. [Claude Work Log (dated)](#claude-work-log-dated)

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

**Status summary:** Bugs 1–6 are resolved in the current code. Bug 5 (Isabelle OS processes lingering after close) was re-checked on 2026-06-04 and is **not reproducible** — clean reaping verified for normal close and close-during-sledgehammer; the accumulation-to-OOM it described is explained by the now-fixed gateway-orphan-on-shutdown (`killpg`, `claude-work/fix-shutdown/`) and gateway-OOM-under-concurrency (Bug 6) issues. A narrow optional hardening item remains (force-kill fallback for a genuinely wedged ML process). See the [Claude Work Log](#claude-work-log-dated) for the dated history.

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

**File:** `server/app/services/session_manager.py`, `cleanup_idle_sessions()`
**Severity:** Medium
**Status:** ✅ Resolved — current code force-evicts abandoned leased sessions older than `self.max_lease_age` (`Server.MAX_LEASE_AGE`, env `ISABELLE_MAX_LEASE_AGE`), exactly as in the fix below.

#### Original root cause

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

**File:** `server/app/services/session_manager.py`, `close_session()`
**Severity:** Low
**Status:** ✅ Resolved — current code re-checks `in_use` after `pop()` and restores + raises `SessionBusyError` ("became busy between in_use check and pop"), as in the fix below.

#### Original root cause

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
**Status:** ✅ Resolved — re-checked 2026-06-04 and **not reproducible** in current code; the accumulation-to-OOM symptom is explained by two *other* issues fixed since (gateway orphan on shutdown → `killpg`, `claude-work/fix-shutdown/`; gateway OOM under concurrency → Bug 6). See `claude-work/bug5-session-close-leak/NOTES.md`.

#### Verification (2026-06-04, container `isabelle-gym`)

Each `ReplBackend` owns its own Scala `Session_Manager` + Isabelle server, so a close
(`session.close() → ReplBackend.exit() → session_manager_instance.shutdown() → stop_server`)
tears down that backend's whole Isabelle server and its `poly` process. Measured:

- **Normal create → work → close:** poly 2 → 4 (after create) → **2 within ~2 s** of `DELETE`, stable.
- **Close WHILE sledgehammer runs (lead #3):** (poly, provers) = (2,0) baseline → (4,3) mid-run → **(2,0) within 1 s** of `DELETE`, stable for 20 s. The forked sledgehammer thread and its ATP subprocesses (`eprover`/`z3`/`cvc5`/`vampire`) are reaped together with the session.

**Residual (separate hardening, not the OOM symptom):** a session whose ML process is
*genuinely wedged* (a non-terminating tactic that ignores interrupts) may not respond to
`Server.exit`, and there is no OS-level force-kill fallback (`kill -9` on the tracked
server PID). Tracked as an optional follow-up below.

#### Original leads (investigated; symptom no longer present)

1. **Async removals may outrace server stop.** `Session_Manager.shutdown()` forks `remove_session_async` per running session, then joins `pending_removals`, then calls `Server_Utils.stop_server`. Each backend has its **own** `Session_Manager` and therefore its **own** Isabelle server process (`Server.init` in `start_server`). Confirm every `remove_session_async` future is actually in `pending_removals` before the join (it is added synchronously today) and that `stop_session` (`Server_Commands.Session_Stop`) returns a success `return_code` rather than erroring out silently.

2. **`Server.exit(name)` may not kill the OS process.** `stop_server` calls `Server.exit(server_info.name)`, which asks the Isabelle server to shut down over its socket. If the server process (or a child `poly`/ML process) is mid-computation or wedged, it may not terminate. Verify with `ps` inside the container before/after a close that the specific server PID actually exits.

3. **A forked sledgehammer thread can hold the ML process open.** The standalone sledgehammer channel forks an `Isabelle_Thread` (see `claude-work/impl-sledgehammer/`). If a session is closed while that thread is still running a prover, the ML process may refuse to exit until the thread (or its external ATP subprocesses) finishes. Reproduce by closing a session immediately after firing sledgehammer and checking for leftover prover/`poly` processes.

4. **No OS-level reaping.** There is no fallback that force-kills a server PID if graceful `Server.exit` fails. Consider tracking the spawned server PID and `kill`-ing it (and orphaned ATP children) as a last resort during `shutdown()`.

#### Optional follow-up (hardening only)

The poly-probe check above (`claude-work/bug5-session-close-leak/`) confirmed normal and
sledgehammer-interrupt closes reap cleanly. The only remaining gap is a last-resort
**force-kill fallback** in `stop_server`/`shutdown` that `kill -9`s the tracked Isabelle
server PID (and orphaned ATP children) if graceful `Server.exit` does not return within a
timeout — to cover a genuinely wedged ML process. Low priority; not the OOM symptom.

---

### Bug 6: Gateway OOM Under Concurrent Sledgehammer — RESOLVED

**Files:** `server/app/core/config.py`, `server/app/services/session_manager.py`, `server/app/api/v1/router.py`
**Severity:** High (operational — bricked the whole server)
**Status:** ✅ Resolved — fixed and verified 2026-06-04 (see `claude-work/impl-sledgehammer/SCALING_NOTES.md`).

#### Root cause

Found by the scale harness `claude-work/impl-sledgehammer/test_sledgehammer_scaling.py`. At ~16 concurrent `sledgehammer` calls, the single shared Py4J **gateway JVM** was OOM-killed by the kernel:

```
scala: line 68: 15680 Killed   ".../java" -Xmx4g ... repl_backend_gateway.scala
py4j.java_gateway: An error occurred while trying to connect to the Java server (127.0.0.1:39143)
```

Each `sledgehammer` is itself a multi-prover parallel job (balloons its `poly` heap + forks several ATP processes). A burst of them spikes memory; the OOM killer takes the gateway JVM; and because there was **no gateway recovery**, every subsequent request returned HTTP 500 — the server stayed bricked until manual restart. The Python cgroup admission gate (Bug-fix from 2026-06-03, see Work Log) did not prevent this: it gates memory at session-*create* time, but the spike is from *running* a heavy op on already-admitted idle sessions.

#### Fix (two parts)

1. **Concurrency semaphore** — bound in-flight sledgehammers server-wide.
   - `config.py`: `Server.MAX_CONCURRENT_SLEDGEHAMMER` (env `ISABELLE_MAX_CONCURRENT_SLEDGEHAMMER`, default `~cores/8`, i.e. 4 on a 32-core box — tracks the empirical throughput knee).
   - `session_manager.py`: `self.sledgehammer_sem = asyncio.Semaphore(...)`.
   - `router.py`: the sledgehammer endpoint runs under `async with session_manager.sledgehammer_sem:`; excess requests queue (backpressure) instead of oversubscribing.
2. **Gateway health-check + auto-restart** — `session_manager.py`:
   - `gateway_alive()` (via `ReplBackendGatewayProcess.has_terminated()`).
   - `_ensure_gateway()` now detects a dead gateway and calls `_recover_gateway_locked()` — purges the now-invalid sessions and rebuilds the gateway — so the next request recovers instead of 500-ing.
   - the background cleanup loop also recovers a dead gateway proactively.
   - `GET /` reports `gateway_alive` (status `degraded` when false) and `max_concurrent_sledgehammer`.

#### Verification (2026-06-04, container `isabelle-gym`, 32 cores)

- **Semaphore:** re-running the harness at W=16 (which previously OOM-killed the gateway) now completes **32/32** with memory flat at ~3.2 GB; no 500s, isolation still PASS.
- **Auto-restart:** `claude-work/impl-sledgehammer/test_gateway_recovery.sh` SIGKILLs the gateway process group → `GET /` shows `status=degraded, gateway_alive=false` → `POST /sessions` returns **200** (auto-recovered) → `GET /` shows `status=healthy, gateway_alive=true`. Previously this was 500-forever.

### [To-do]Issue 1: How Isabelle do parallel
When have parallel "have x" statements, can we do this in step. And how do we retrive information when one line is stucked in loop. That is, we need error retrieval for a proof chunk, the server should not just return a timeout error, it should tell, when we build the MCP server, the agent what part of that proof chunk just went wrong.


---

## Summary of Changes

| File | Change | Status |
|---|---|---|
| `server/app/services/threaded_backend.py` | Queue `exit()` + configurable `EXIT_TIMEOUT`/`JOIN_TIMEOUT` | ✅ Done (Bug 1, Bug 2) |
| `server/app/services/session_manager.py` | `cleanup_idle_sessions()`: add `max_lease_age` force-eviction path | ✅ Done (Bug 3) |
| `server/app/services/session_manager.py` | `close_session()`: re-check `in_use` after `pop()` | ✅ Done (Bug 4) |
| (none — verification only) | Isabelle server/session OS processes confirmed to terminate on close (incl. mid-sledgehammer) | ✅ Verified not-reproducible (Bug 5); optional force-kill fallback remains |
| `server/app/core/config.py`, `session_manager.py`, `router.py` | Sledgehammer concurrency semaphore + gateway auto-restart | ✅ Done (Bug 6) |

---

## Claude Work Log (dated)

Test/demonstration artifacts live under `claude-work/<task>/` (each with a `NOTES.md`). Summary of work completed:

| Date | Task | What was done | Artifacts |
|---|---|---|---|
| 2026-06-03 | **Shutdown fix** | Two bugs: (1) `SessionManager.shutdown()` let `asyncio.CancelledError` escape (it's a `BaseException`, not caught by `except Exception`) → uvicorn "Application shutdown failed" + teardown skipped; (2) `ReplBackendGatewayProcess.terminate()` signalled only the launcher shell, leaving the JVM orphaned (4 GB-heap leak → container OOM `Exited 137`). Fixed: catch `CancelledError`; `killpg` the gateway process group. | `claude-work/fix-shutdown/` |
| 2026-06-03 | **Memory-mgmt investigation** | Found the Scala memory management was dead three ways: never called by the server, wrong layer (per-backend, 1 session each), wrong metric (JVM heap, not the `poly` processes). | `claude-work/investigate-memory-management/` |
| 2026-06-03 | **Memory mgmt → Python** | Moved memory management into the Python `SessionManager`, measuring real container memory via cgroup v2 (`MemoryMonitor`). Under pressure: evict idle LRU sessions, then 503 (never kill busy/leased). Removed the dead Scala code (rebuilt `repl.jar`) and synced the Py4J layer. Also fixed `PoolExhausted` being mis-mapped to 500. | `claude-work/impl-python-memory-mgmt/` |
| 2026-06-04 | **Sledgehammer concurrency at scale** | Built an N-way isolation + throughput-scaling harness. Result: isolation PASS at N=6 (no crossed channels); throughput knee ≈ 4 on 32 cores; **surfaced Bug 6** (gateway OOM at W=16). | `claude-work/impl-sledgehammer/SCALING_NOTES.md`, `test_sledgehammer_scaling.py` |
| 2026-06-04 | **Bug 6 fix** | Sledgehammer concurrency semaphore + gateway health-check/auto-restart (see Bug 6 above). Verified: W=16 no longer OOMs; killed gateway auto-recovers on next request. | `claude-work/impl-sledgehammer/test_gateway_recovery.sh` |
| 2026-06-04 | **Bug 5 re-check** | Measured poly/prover process counts across create→close and close-during-sledgehammer. Both reap cleanly (≤2 s), so Bug 5 is not reproducible; marked resolved. Optional force-kill fallback for wedged ML processes noted. | `claude-work/bug5-session-close-leak/` |
| 2026-06-04 | **Phase 0 monitoring** | Added a Prometheus `/metrics` endpoint (HTTP histograms via instrumentator + `isabellegym_*` counters/gauges/histogram in `server/app/core/metrics.py`, fed by `get_lru_info()`/`MemoryMonitor`/gateway-recovery), `/healthz`+`/readyz` probes, and a Prometheus+Grafana+cAdvisor stack in `docker-compose.yml` with `mem_limit: 12g` and a starter dashboard. Verified end-to-end: all 3 Prometheus targets UP, Grafana dashboard provisioned, domain counters move, `memory_limit_mb` reflects the 12g cgroup. NB: image must be rebuilt (`docker compose build isabelle-gym`) to bake in the two new pip deps. | `claude-work/impl-monitoring/`, `monitoring/` |

*Last updated: 2026-06-04 02:31 GMT.*
