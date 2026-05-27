# IsabelleGym Server — Issue Investigation & Fix Plan

**Date:** 2026-05-24  
**Scope:** Server layer (`server/`), REPL layer (`repl/`), client layer (`client/`)  
**Status:** Investigation complete — no code modified

---

## Table of Contents

1. [Problem 1: Sledgehammer — Completely Unimplemented](#problem-1-sledgehammer)
   - [Current State](#current-state)
   - [Approach A: Step Passthrough (Minimal)](#approach-a-step-passthrough)
   - [Approach B: Dedicated ML Channel (Recommended)](#approach-b-dedicated-ml-channel)
2. [Problem 2: Memory Management / Session Closing](#problem-2-memory-management--session-closing)
   - [Bug 1: Race Condition in `ThreadedBackend.close()`](#bug-1-race-condition-in-threadedbackendclose)
   - [Bug 2: Join Timeout Too Short](#bug-2-join-timeout-too-short)
   - [Bug 3: Leased Sessions Never Idle-Evicted](#bug-3-leased-sessions-never-idle-evicted)
   - [Bug 4: TOCTOU on `in_use` Check](#bug-4-toctou-on-in_use-check)

---

## Problem 1: Sledgehammer

### Current State

Sledgehammer is entirely absent at every layer of the stack. `devnote.md` explicitly marks it "Unsolved". The `HOL.Sledgehammer` library is imported into the REPL theory files so the kernel has it available — but nothing exposes it.

**Missing at every layer:**

| Layer | File | Gap |
|---|---|---|
| ML | `repl/src/ml/REPL.ML` | No `send_sledgehammer_tagged` function |
| Scala comms | `repl/src/main/scala/repl/repl_ml_communication.scala` | No sledgehammer channel, no `Scala.Fun_Strings` for it |
| Scala backend | `repl/src/main/scala/repl/repl_backend.scala` | No `sledgehammer()` method |
| Python protocol | `repl/src/python/repl_backend_gateway.py` | Not in `ReplBackend` Protocol |
| Server session | `server/app/services/session.py` | No `sledgehammer()` method |
| API schemas | `server/app/api/v1/schemas/API_models.py` | No request/response models |
| Router | `server/app/api/v1/router.py` | No endpoint |
| Client | `client/async_client.py` | No client method |

---

### Approach A: Step Passthrough

Sledgehammer is a valid Isabelle Isar command. In proof mode you can write `sledgehammer [timeout = N]` and it will print `Try this: by (metis ...)` lines in the normal proof output. The existing `step()` infrastructure in Scala already handles arbitrary Isar strings. Approach A adds a dedicated endpoint that calls `step("sledgehammer [timeout = N]")` and returns the output.

**Pros:** No changes to Scala or ML. Reuses all existing infrastructure.  
**Cons:** Output is a flat string requiring fragile text parsing. Cannot distinguish "no proof found" from "timed out". Blocks the session for the full search duration. Output format varies by Isabelle locale and which ATPs respond.

**Files to change: 4**

---

#### A.1 — `server/app/api/v1/schemas/API_models.py`

Add two models at the end of the file:

```python
class SledgehammerRequest(BaseModel):
    timeout_s: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Isabelle sledgehammer timeout in seconds (1–300).",
    )


class SledgehammerResponse(BaseModel):
    success: bool
    suggestions: List[str]
    raw_output: str
    execution_time: float
```

---

#### A.2 — `server/app/services/session.py`

Add one method to `_Isabelle_Session`, after the existing `rollback()` method (~line 184):

```python
def sledgehammer(
    self,
    timeout_s: int = 30,
    http_timeout: Optional[float] = None,
):
    """Run Isabelle's sledgehammer tactic and return raw output.

    Uses the built-in Isar command syntax: sledgehammer [timeout = N].
    The session must be in an active proof state before calling this.
    """
    command = f"sledgehammer [timeout = {timeout_s}]"
    logger.info("running sledgehammer timeout_s=%s", timeout_s)
    # http_timeout must be longer than the isabelle-level timeout
    effective_http_timeout = http_timeout or (timeout_s + 30.0)
    return self._call_backend(
        lambda: self.backend.raw.step(command),
        timeout=effective_http_timeout,
    )
```

---

#### A.3 — `server/app/api/v1/router.py`

**Step 1.** Add the two new models to the import block at the top of the file:

```python
from .schemas.API_models import (
    BigStepTheoryRequest,
    CommandRequest,
    CommandResponse,
    ProofStateResponse,
    SessionAcquireRequest,
    SessionAcquireResponse,
    SessionCreateRequest,
    SessionResponse,
    SledgehammerRequest,      # add
    SledgehammerResponse,     # add
    StateCheckpoint,
)
```

**Step 2.** Add the endpoint after the `rollback` endpoint (~line 283):

```python
@router.post(
    "/api/v1/sessions/{session_id}/sledgehammer",
    response_model=SledgehammerResponse,
)
async def sledgehammer(
    session_id: str,
    request: SledgehammerRequest,
    x_lease_id: str | None = Header(None, alias="X-Lease-Id"),
    session_manager=Depends(get_session_manager),
):
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(
            session_id, lease_id=lease_id, require_lease=True
        )
        logger.info("sledgehammer requested timeout_s=%s", request.timeout_s)
        start = time.time()
        result = await asyncio.to_thread(
            session.sledgehammer,
            request.timeout_s,
        )
        elapsed = time.time() - start

        raw = result.total_output() if hasattr(result, "total_output") else str(result)
        # Parse "Try this: ..." lines out of the flat output
        suggestions = [
            line.replace("Try this:", "").strip()
            for line in raw.splitlines()
            if line.strip().startswith("Try this:")
        ]
        found = len(suggestions) > 0
        logger.info(
            "sledgehammer finished found=%s suggestions=%s elapsed=%.2f",
            found, len(suggestions), elapsed,
        )
        return SledgehammerResponse(
            success=found,
            suggestions=suggestions,
            raw_output=raw,
            execution_time=elapsed,
        )
```

---

#### A.4 — `client/async_client.py`

Add one method to `IsabelleGymAsyncClient`, after `execute_command()`:

```python
async def sledgehammer(
    self,
    session_id: str,
    timeout_s: int = 30,
    *,
    lease_id: str | None = None,
) -> dict[str, Any]:
    """Run Isabelle's sledgehammer on the current proof goal.

    Returns a dict with keys: success, suggestions (list), raw_output, execution_time.
    The session must already be in an active proof state (theory entered, theorem
    stated, inside a proof block).
    """
    http_timeout = timeout_s + 30.0  # always longer than the isabelle-side timeout
    response = await self._request(
        "POST",
        f"{BASE_URL}/{session_id}/sledgehammer",
        json_body={"timeout_s": timeout_s},
        headers=self._lease_headers(lease_id),
        timeout=http_timeout,
    )
    response.raise_for_status()
    return response.json()
```

---

### Approach B: Dedicated ML Channel

This is the recommended production approach. It follows the identical pattern used by `open_subgoals`, `local_facts`, and `global_facts`: an ML function sends structured results back to Scala over a per-session tagged channel. The output is a proper list of proof method strings, not a flat string requiring fragile parsing.

**Pros:** Structured output (list of proof methods). No output parsing. Channel-isolated per session. Results are available programmatically.  
**Cons:** Requires Scala changes and a `./gradlew build` before deploying.

**Files to change: 8**

---

#### B.1 — `repl/src/ml/REPL.ML`

The ML signature and structure both need a new function. Sledgehammer in Isabelle/ML is accessed via the `Sledgehammer` structure. The key function is `Sledgehammer.run` which returns a list of `(string * Sledgehammer_Proof_Methods.proof_method)` pairs. We extract the method strings and send them back over the tagged channel.

**Add to the signature block** (after the last `send_global_facts_tagged` line):

```ml
  val send_sledgehammer_tagged: string -> int -> Toplevel.state -> unit
```

**Add to the structure body** (after the `send_global_facts_tagged` function, before the closing `end`):

```ml
fun run_sledgehammer timeout_s state =
  if not (Toplevel.is_proof state) then []
  else
    let
      val proof = Toplevel.proof_of state;
      val ctxt  = Proof.context_of proof;
      val ({goal, ...}) = Proof.raw_goal proof;

      (* Build a minimal sledgehammer configuration *)
      val thy      = Proof_Context.theory_of ctxt;
      val name     = Context.theory_name thy;
      val params   = Sledgehammer_Commands.default_params thy
                       [("timeout", string_of_int timeout_s)];
      val state_ref = Unsynchronized.ref (Proof.state proof);

      (* Run sledgehammer; collect the proof-method strings *)
      val results =
        Sledgehammer.run_sledgehammer
          params
          Sledgehammer_Prover.Normal
          NONE      (* no override prover list *)
          1         (* first subgoal *)
          proof
        |> map (fn (_, (outcome, _)) =>
             case outcome of
               Sledgehammer.Proof (_, str, _) => SOME str
             | _                              => NONE)
        |> map_filter I;
    in
      results
    end
    handle exn =>
      ( warning ("sledgehammer ML exception: " ^ Runtime.exn_message exn)
      ; [] );

fun send_sledgehammer_tagged channel_id timeout_s state =
  let val results = run_sledgehammer timeout_s state
  in ("CH:" ^ channel_id) :: results |> \<^scala>\<open>add_sledgehammer_results\<close> |> ignore end
```

**Add top-level binding** after the `send_global_facts_tagged` alias at the very end of the file:

```ml
val send_sledgehammer_tagged = Repl.send_sledgehammer_tagged;
```

> **Note:** The exact Sledgehammer ML API (`Sledgehammer.run_sledgehammer`, result type shape) should be verified against the Isabelle 2025-2 source under `$ISABELLE_HOME/src/HOL/Tools/Sledgehammer/`. The function name and signature can differ across Isabelle versions. If `run_sledgehammer` is not available, use `Sledgehammer.run` or the tactic-level `Sledgehammer_Tactics.sledgehammer_tac`.

---

#### B.2 — `repl/src/main/scala/repl/repl_ml_communication.scala`

Four additions:

**1. Add the timeout constant** at the top of the `object Repl_ML_Communication` block, alongside the other constants:

```scala
private val SLEDGEHAMMER_TIMEOUT_SECONDS = 300
```

**2. Add the sledgehammer channel map** after the `global_fact_channels` declaration:

```scala
private val sledgehammer_channels =
  new ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]]()
```

**3. Update `clear_channel()`** to also remove the sledgehammer channel:

```scala
def clear_channel(channel: String): Unit = {
  subgoal_channels.remove(channel)
  local_fact_channels.remove(channel)
  global_fact_channels.remove(channel)
  sledgehammer_channels.remove(channel)   // add this line
}
```

**4. Add the `Scala.Fun_Strings` handler** after `Global_Facts_Function`:

```scala
object Sledgehammer_Results_Function extends Scala.Fun_Strings("add_sledgehammer_results") {
  val here = Scala_Project.here

  def apply(received_results: List[String]): List[String] = {
    val (channel, results) = extract_channel(received_results)
    val q = get_or_create_queue(sledgehammer_channels, channel)
    if (!q.offer(results))
      error(s"more sledgehammer result messages arrived than requested (channel=$channel)")
    List()
  }
}
```

**5. Add the blocking receive helper** after `waiting_for_global_facts_message`:

```scala
def waiting_for_sledgehammer_message[T](block: => T, channel: String = DEFAULT_CHANNEL, timeout_s: Int = SLEDGEHAMMER_TIMEOUT_SECONDS): List[String] = {
  val q = get_or_create_queue(sledgehammer_channels, channel)
  q.clear()
  block
  val result = q.poll((timeout_s + 10).toLong, TimeUnit.SECONDS)
  if (result == null) error(s"Timeout waiting for sledgehammer message (channel=$channel)")
  result
}
```

**6. Register the new function** in the `Scala_Functions` class at the bottom of the file:

```scala
class Scala_Functions
    extends Scala.Functions(
      Repl_ML_Communication.Open_Subgoals_Function,
      Repl_ML_Communication.Local_Facts_Function,
      Repl_ML_Communication.Global_Facts_Function,
      Repl_ML_Communication.Sledgehammer_Results_Function   // add this line
    )
```

---

#### B.3 — `repl/src/main/scala/repl/repl_backend.scala`

Add one method after `global_facts()` (~line 95):

```scala
def sledgehammer(timeout_s: Int): java.util.List[String] = {
  val suggestions =
    if (!repl_session.current_thy_begun) List()
    else {
      Repl_ML_Communication.waiting_for_sledgehammer_message(
        {
          send_ml_command(
            s"""Repl.send_sledgehammer_tagged "${channel_id}" ${timeout_s} @{Isar.state}"""
          )
        },
        channel_id,
        timeout_s
      )
    }
  suggestions.asJava
}
```

---

#### B.4 — `repl/src/python/repl_backend_gateway.py`

Add one entry to the `ReplBackend` Protocol, after `global_facts`:

```python
def sledgehammer(self, timeout_s: int) -> py4j.java_collections.JavaList[str]: ...
```

---

#### B.5 — `server/app/api/v1/schemas/API_models.py`

Add two models at the end of the file (same models as Approach A, reproduced here for completeness):

```python
class SledgehammerRequest(BaseModel):
    timeout_s: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Isabelle sledgehammer timeout in seconds (1–300).",
    )


class SledgehammerResponse(BaseModel):
    success: bool
    suggestions: List[str]
    raw_output: str
    execution_time: float
```

---

#### B.6 — `server/app/services/session.py`

Add one method to `_Isabelle_Session`, after the existing `rollback()` method (~line 184):

```python
def sledgehammer(
    self,
    timeout_s: int = 30,
    http_timeout: Optional[float] = None,
) -> list:
    """Call Isabelle's sledgehammer via the dedicated ML channel.

    Returns a list of proof method strings (e.g. ['by (metis foo)',
    'by (simp add: bar)']).  Returns an empty list if no proof is found
    within timeout_s or if the session is not in a proof state.
    """
    logger.info("running sledgehammer timeout_s=%s", timeout_s)
    effective_http_timeout = http_timeout or (timeout_s + 30.0)
    raw: "py4j.java_collections.JavaList[str]" = self._call_backend(
        lambda: self.backend.raw.sledgehammer(timeout_s),
        timeout=effective_http_timeout,
    )
    return list(raw) if raw is not None else []
```

---

#### B.7 — `server/app/api/v1/router.py`

**Step 1.** Add the two new models to the import block:

```python
from .schemas.API_models import (
    BigStepTheoryRequest,
    CommandRequest,
    CommandResponse,
    ProofStateResponse,
    SessionAcquireRequest,
    SessionAcquireResponse,
    SessionCreateRequest,
    SessionResponse,
    SledgehammerRequest,      # add
    SledgehammerResponse,     # add
    StateCheckpoint,
)
```

**Step 2.** Add the endpoint after the `rollback` endpoint (~line 283):

```python
@router.post(
    "/api/v1/sessions/{session_id}/sledgehammer",
    response_model=SledgehammerResponse,
)
async def sledgehammer(
    session_id: str,
    request: SledgehammerRequest,
    x_lease_id: str | None = Header(None, alias="X-Lease-Id"),
    session_manager=Depends(get_session_manager),
):
    with logging_context(session_id=session_id):
        lease_id = _require_lease_id(x_lease_id)
        session = session_manager.get_session(
            session_id, lease_id=lease_id, require_lease=True
        )
        logger.info("sledgehammer requested timeout_s=%s", request.timeout_s)
        start = time.time()
        suggestions: list = await asyncio.to_thread(
            session.sledgehammer, request.timeout_s
        )
        elapsed = time.time() - start
        found = len(suggestions) > 0
        logger.info(
            "sledgehammer finished found=%s suggestions=%s elapsed=%.2f",
            found, len(suggestions), elapsed,
        )
        return SledgehammerResponse(
            success=found,
            suggestions=suggestions,
            raw_output="\n".join(suggestions),
            execution_time=elapsed,
        )
```

---

#### B.8 — `client/async_client.py`

Add one method to `IsabelleGymAsyncClient`, after `execute_command()`:

```python
async def sledgehammer(
    self,
    session_id: str,
    timeout_s: int = 30,
    *,
    lease_id: str | None = None,
) -> dict[str, Any]:
    """Run Isabelle's sledgehammer on the current proof goal.

    Returns a dict with keys: success (bool), suggestions (list of strings),
    raw_output (str), execution_time (float).

    The session must already be in an active proof state.
    """
    http_timeout = timeout_s + 30.0
    response = await self._request(
        "POST",
        f"{BASE_URL}/{session_id}/sledgehammer",
        json_body={"timeout_s": timeout_s},
        headers=self._lease_headers(lease_id),
        timeout=http_timeout,
    )
    response.raise_for_status()
    return response.json()
```

#### B.9 — Build step

After all code changes, rebuild the Scala JAR before starting the server:

```bash
cd repl
./gradlew build
```

---

### Approach Comparison

| Criterion | Approach A | Approach B |
|---|---|---|
| Scala/ML changes | None | Yes (requires rebuild) |
| Output structure | Flat string, parsed with regex | Native `List[String]` |
| Output reliability | Fragile (locale/ATP-dependent format) | Robust |
| Timeout control | Via Isabelle command syntax | Per-channel blocking queue with configurable timeout |
| Session blocking | Yes, same as any `step()` call | Yes, same behaviour |
| Concurrent safety | Same as `step()` (serialised via worker queue) | Same pattern as `open_subgoals` (channel-isolated) |
| Suggested for | Quick prototype / testing | Production ML training |

---

## Problem 2: Memory Management / Session Closing

The full close call chain is:

```
HTTP DELETE /sessions/{id}
  → asyncio.to_thread(session_manager.close_session(...))   [session_manager.py:495]
  → session.close()                                         [session.py:540]
  → threaded_backend.close()                                [threaded_backend.py:58]
  → self._backend.exit()   ← Py4J call to Scala             [repl_backend.scala:140]
  → session_manager_instance.shutdown()                     [Scala]
```

There are two high-severity bugs and two lower-severity issues.

---

### Bug 1: Race Condition in `ThreadedBackend.close()`

**File:** `server/app/services/threaded_backend.py`, lines 58–65  
**Severity:** High

#### Root cause

```python
def close(self) -> None:
    try:
        self._backend.exit()   # ← called directly from the calling thread
    finally:
        self._stop.set()
        self._t.join(timeout=2.0)
```

`self._backend.exit()` is called from whatever thread calls `close()` — NOT through the job queue. The worker thread `_t` is simultaneously alive and may be executing a job that calls another method on the same `self._backend` Py4J object.

`ThreadedBackend` was designed to serialise all backend calls through a single worker thread precisely because the Py4J gateway uses a single socket connection and the underlying Scala `ReplBackend` is not thread-safe. By calling `exit()` directly from the calling thread, this serialisation guarantee is broken. Two concurrent Py4J method calls over the same gateway connection can corrupt the in-flight call, cause a `Py4JNetworkError`, or trigger undefined behaviour in the Scala object's mutable state.

The `_lru.pop()` + `in_use` check in `close_session()` reduces but does not eliminate the window — there is a TOCTOU between the `in_use` check and the moment `close()` is called.

#### Fix

Submit `exit()` through the job queue. Wait for the future to confirm it ran. Only then signal the worker to stop.

**Replace the current `close()` method entirely:**

```python
def close(self) -> None:
    logger.info("closing threaded backend worker=%s", self._name)
    # Submit exit() through the queue so it is serialised after any pending job.
    # Do NOT call self._backend.exit() directly from this thread.
    exit_fut = self.submit(self._backend.exit)
    try:
        exit_fut.result(timeout=60.0)
    except Exception:
        logger.exception(
            "backend exit raised during close worker=%s", self._name
        )
    finally:
        self._stop.set()
        self._t.join(timeout=5.0)
        logger.info("threaded backend closed worker=%s", self._name)
```

**Why this is correct:**
1. `exit()` is queued and will run only after any in-progress job finishes — no concurrent call.
2. `exit_fut.result(timeout=60.0)` blocks until `exit()` returns (or until 60 seconds, preventing a permanent hang).
3. After `exit()` completes, `_stop.set()` signals the loop to stop. The loop checks `_stop` at the top of every iteration so it stops almost immediately (at most one 0.1 s `_q.get` timeout).
4. `_t.join(timeout=5.0)` is now waiting for a loop that has nothing left to do — it exits quickly.

---

### Bug 2: Join Timeout Too Short

**File:** `server/app/services/threaded_backend.py`, line 64  
**Severity:** High

#### Root cause

The original code has `self._t.join(timeout=2.0)`. The `_backend.exit()` call triggers `session_manager_instance.shutdown()` in Scala, which:

1. Removes all running Isabelle sessions asynchronously.
2. Waits for those async removal futures to complete.
3. Clears the session cache.
4. Calls `Server_Utils.stop_server(server_info)` — stops the Isabelle subprocess.

For any session that has loaded a non-trivial theory (anything beyond base HOL), this sequence routinely takes 5–30 seconds. With the 2-second timeout, the `join()` times out and the method logs "threaded backend closed" even though the worker thread is still alive, and the Scala shutdown has not completed. The worker thread will eventually finish (it is `daemon=True`), but any resource it holds — the Isabelle server subprocess, file handles, Py4J callback threads — is not yet released at the time `close()` returns.

The fix is already embedded in Bug 1's corrected code above: the `exit_fut.result(timeout=60.0)` call waits up to 60 seconds for the Scala shutdown to complete, and the subsequent join is for a loop that has stopped normally, so `timeout=5.0` is sufficient there.

If 60 seconds is too long for your use case, make the timeout configurable. Add a class-level constant:

```python
class ThreadedBackend:
    EXIT_TIMEOUT: float = 60.0   # seconds to wait for Scala shutdown
    JOIN_TIMEOUT: float = 5.0    # seconds to wait for worker loop exit after _stop
```

And reference these in `close()`:

```python
    exit_fut.result(timeout=self.EXIT_TIMEOUT)
    ...
    self._t.join(timeout=self.JOIN_TIMEOUT)
```

---

### Bug 3: Leased Sessions Never Idle-Evicted

**File:** `server/app/services/session_manager.py`, lines 551–553  
**Severity:** Medium

#### Root cause

```python
async def cleanup_idle_sessions(self) -> None:
    while True:
        await asyncio.sleep(60)
        ...
        for sid, session in list(self._lru.items()):
            if session.leased:
                continue  # never evict a leased session
```

Once a session is leased, the idle cleanup task skips it unconditionally. If a client acquires a lease and then disconnects (crash, network failure, process kill), the session remains in the pool forever — or until the server is restarted. For ML training loops that create many sessions, this is a steady memory leak.

#### Fix

Add a `max_lease_age` threshold. Sessions leased for longer than `idle_timeout * 2` are force-closed with a warning. Replace the current `cleanup_idle_sessions` method:

```python
async def cleanup_idle_sessions(self) -> None:
    # Abandoned leases are force-closed after this many seconds of inactivity.
    max_lease_age = self.idle_timeout * 2

    while True:
        await asyncio.sleep(60)
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
                            sid,
                            session.lease_id,
                            now - session.last_activity,
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
                logger.exception(
                    "failed to close idle session session_id=%s", sid
                )
```

If you want to make `max_lease_age` configurable via an environment variable, add it to `server/app/core/config.py` alongside the other `ISABELLE_*` variables (e.g. `ISABELLE_MAX_LEASE_AGE_SECONDS`, defaulting to `idle_timeout * 2`), and pass it as a parameter to `SessionManager.__init__`.

---

### Bug 4: TOCTOU on `in_use` Check

**File:** `server/app/services/session_manager.py`, lines 509–513  
**Severity:** Low

#### Root cause

```python
with self._lock:
    session = self._lru.get(sid)
    ...
    if session.in_use:
        raise SessionBusyError(...)
    self._lru.pop(sid, None)
```

The `in_use` property reads `_active_requests` under `_active_requests_lock`, not under `_lock`. A concurrent request handler could call `_acquire_request()` between the `in_use` check and the `_lru.pop()` and slip through — the session would be closed while a request is executing against it.

In practice, the window is microseconds and requires the OS to schedule a context switch at exactly the wrong moment. It is unlikely to manifest in a single-server deployment. However, it is a logical correctness issue.

#### Fix

After `_lru.pop()`, double-check `in_use` before proceeding. If a request slipped through, re-add the session and raise:

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
    # Re-check after pop: a request that was about to be acquired
    # cannot reach _acquire_request now that the session is out of the LRU,
    # but one that was already mid-acquisition may have incremented the counter.
    if session.in_use:
        self._lru[sid] = session          # restore
        self._lru.move_to_end(sid)
        raise SessionBusyError(
            f"Session {sid} became busy between in_use check and pop"
        )
    if session.leased:
        session.release_lease()
```

This is belt-and-suspenders: once `_lru.pop()` succeeds, no new `get_session()` call can return this session (they check the LRU), so no new `_acquire_request()` can be called. The only risk is a request that completed `get_session()` but has not yet called `_acquire_request()` — catching that requires either a longer lock scope or accepting the tiny residual risk.

---

## Summary of Changes

### Sledgehammer — Approach A (4 files, no rebuild)

| File | Change |
|---|---|
| `server/app/api/v1/schemas/API_models.py` | Add `SledgehammerRequest`, `SledgehammerResponse` |
| `server/app/services/session.py` | Add `sledgehammer()` calling `step()` |
| `server/app/api/v1/router.py` | Add import, add `POST /sledgehammer` endpoint |
| `client/async_client.py` | Add `sledgehammer()` client method |

### Sledgehammer — Approach B (8 files + rebuild)

| File | Change |
|---|---|
| `repl/src/ml/REPL.ML` | Add `send_sledgehammer_tagged` signature + implementation |
| `repl/src/main/scala/repl/repl_ml_communication.scala` | Add channel map, `Fun_Strings`, blocking helper, register in `Scala_Functions` |
| `repl/src/main/scala/repl/repl_backend.scala` | Add `sledgehammer()` method |
| `repl/src/python/repl_backend_gateway.py` | Add `sledgehammer()` to `ReplBackend` Protocol |
| `server/app/api/v1/schemas/API_models.py` | Add `SledgehammerRequest`, `SledgehammerResponse` |
| `server/app/services/session.py` | Add `sledgehammer()` calling `backend.raw.sledgehammer()` |
| `server/app/api/v1/router.py` | Add import, add `POST /sledgehammer` endpoint |
| `client/async_client.py` | Add `sledgehammer()` client method |

### Memory Management (2 files)

| File | Change | Bug fixed |
|---|---|---|
| `server/app/services/threaded_backend.py` | Replace `close()`: submit `exit()` via queue, `exit_fut.result(timeout=60)`, then set stop + join | Bug 1 (race), Bug 2 (timeout) |
| `server/app/services/session_manager.py` | Replace `cleanup_idle_sessions()`: add `max_lease_age` force-eviction path | Bug 3 (lease leak) |
