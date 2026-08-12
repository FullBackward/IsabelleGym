# DESIGN_CHOICES.md

Design rationale for IsabelleGym 3.0, written down after the fact so the choices — and the
roads not taken — survive the people who made them. Format per choice: what we chose, what
the old/alternative option was, pros and cons of each, why the chosen option won, and what
possibilities the choice deliberately gave up. Bug numbers refer to ISSUES.md; deeper
histories live in `claude-work/`.

---

## 1. IsabelleGym server — from local REPL gym to a concurrent server

### 1.1 Deployment model: embedded Python library → HTTP service

**Old:** IsabelleGym 2.0 was an in-process Python library. Every training/eval worker
embedded its own REPL and spawned its own Isabelle.
**Chosen:** a FastAPI HTTP service (`server/`) owning all Isabelle state; clients speak REST.

- *Embedded pros:* no network hop, no serialization, trivially simple to start.
- *Embedded cons:* every worker pays full Isabelle startup; no sharing of warm sessions; no
  central resource control (N workers = N uncoordinated multi-GB `poly` processes); Python-only
  clients; nothing to monitor.
- *Server pros:* one warm pool shared by all clients; central admission control, metrics
  (`/metrics`, Grafana), and crash recovery; language-agnostic (the MCP server, eval scripts,
  and curl are all just HTTP clients); containerizable for cluster use.
- *Server cons:* operational surface (leases, pools, timeouts), request/response overhead,
  and a whole class of concurrency bugs we then had to fix (Bugs 1–8).

**Why:** the target workload is many concurrent LLM agents/rollouts against a resource that
costs gigabytes and tens of seconds per instance. Sharing and gating that resource is the
whole game; an embedded library cannot do either. **Abandoned:** the local install path
(`install.sh`) is unmaintained — the server assumes the container layout (`/app`,
`/opt/isabelle`) and that is deliberate: one supported layout instead of N half-working ones.

### 1.2 Prover bridge: Scala-side PIDE via Py4J → one gateway JVM

**Chosen:** a Scala program (`repl/`) using Isabelle's own Scala API (Headless PIDE sessions),
exposed to Python through a single Py4J gateway JVM.
**Alternatives:** (a) the official `isabelle server` JSON/TCP protocol; (b) research bridges
(scala-isabelle / PISA-style); (c) driving `isabelle console` as a subprocess REPL.

- *(a) pros:* stable, documented, no custom Scala. *cons:* too coarse — no per-command status
  inside a document, no transient probes, limited document-edit control; exactly the
  capabilities `verify_chunk`/`diagnostic` are built on.
- *(b) pros:* faster start. *cons:* third-party maintenance risk, and we still would have
  needed custom ML plumbing (subgoal/fact extraction in `REPL.ML`).
- *(c) pros:* trivially simple. *cons:* scraping stdout of a console is fragile, has no
  document model at all, and cannot express rollback/checkpoint semantics.

**Why:** the product differentiator is *fine-grained feedback* (which line failed, which line
is looping, state probes that don't pollute the script). Only the PIDE document model offers
that, and PIDE is only fully reachable from Isabelle/Scala. Py4J then bridges Scala↔Python
with the least ceremony. **Cost accepted:** a hand-maintained mirror (`ReplBackend` Protocol
in `repl_backend_gateway.py`) that must stay in sync with the Scala class.

### 1.3 One shared gateway JVM vs JVM-per-session

**Chosen:** all sessions live in one JVM; each `ReplBackend` gets a `channel_id` so ML
messages (subgoals/facts) route to the right backend.
**Alternative:** a JVM per session.

- *Per-session pros:* crash isolation; no cross-session interference.
- *Per-session cons:* a JVM heap per session on top of each `poly` process — memory roughly
  doubles; multi-second JVM spawn per session.
- *Shared pros:* amortized JVM cost; instant backend creation.
- *Shared cons:* shared fate — when sledgehammer bursts OOM-killed the JVM (Bug 6), every
  session died at once.

**Why:** memory is the binding constraint (each session's `poly` is already 1.5–2.5 GB); a
per-session JVM would halve pool capacity. The shared-fate risk was made acceptable by
**gateway crash recovery** (detect dead JVM → purge sessions → rebuild, instead of a bricked
server) and by removing the crash *causes* (sledgehammer semaphore, memory admission).
**Note:** each backend still constructs its own Scala `Session_Manager` (own in-JVM Isabelle
server); a shared variant exists but is unused — consolidation is a known open item.

### 1.4 Backend concurrency: one dedicated worker thread per session

**Chosen:** `ThreadedBackend` — every session's Py4J calls are serialized through a single
worker thread and a job queue.
**Alternatives:** a lock around backend calls; or trusting async/await.

- The Scala backend is not thread-safe and Py4J multiplexes one socket, so *some*
  serialization is mandatory. A plain lock serializes too but lets calls run on arbitrary
  threads (including the event loop — see 1.9) and gives no place to implement drain/reject
  semantics on shutdown. Async doesn't help: the underlying socket calls are blocking.

**Why:** a queue-owning thread gives ordering, a natural shutdown protocol, and a single
choke point for instrumentation. **Hard-won lessons encoded here:** `exit()` must go through
the same queue (calling it directly raced in-flight jobs — Bug 1), and the shutdown guard
must not reject the backend's *own* exit job (Bug 8 — the regression that silently leaked
every session's poly process).

### 1.5 Session lifecycle: LRU pool + exclusive leases + dependency-key reuse

**Chosen:** sessions live in a capacity-capped LRU pool; a client *acquires* a session and
gets an exclusive lease token (`X-Lease-Id`) that all session operations must present;
sessions are reusable across clients when their **dependency key** (sorted imports + field
hash) matches; idle and abandoned-lease sessions are reaped by a background sweep.
**Alternatives:** create-and-destroy per request; sticky client→session affinity without
tokens; no reuse at all.

- *Create-per-request:* correctness by construction, but a 30–60 s session start per request
  is unusable interactively.
- *Sticky affinity without leases:* races the moment two workers guess the same session, and
  leaks sessions when clients die silently.
- *Leases pros:* exclusive use is enforced by the server, not by client discipline;
  expiry (`MAX_LEASE_AGE`) plus force-close bounds the damage of a vanished client. There is
  a formal concurrency proof for acquire/release (`evaluation/Server_Concurrency.thy`).
- *Reuse pros:* amortizes the dominant cost. *cons:* a reused session carries document state.

**Why:** leases give multi-client safety with a self-healing failure mode. On reuse, the
proof-leakage incident (an agent reading a previous attempt's proof via `source()`) taught us
the refinement now used by the MCP layer: **clean-only reuse** (`reuse_dirty=False`) — reuse
is only worth its risk when the session has no command history. **Abandoned:** cross-client
sharing of *dirty* sessions, and the Scala-side session cache (`enable_cache`, default off) —
PIDE-level sharing of one session between backends was never made safe enough to trust.

### 1.6 Memory management: JVM-heap heuristics → cgroup admission control

**Old:** the Scala layer measured `Runtime.getRuntime` JVM heap and made "memory decisions"
that were never actually wired to anything — and measured the wrong thing anyway (each
session's memory lives in a separate `poly` OS process the JVM cannot see).
**Chosen:** a Python `MemoryMonitor` reading the **container cgroup** (the number the OOM
killer actually uses), corrected by subtracting reclaimable `inactive_file` page cache;
admission control before session creation; under pressure, evict idle LRU sessions (never
busy/leased ones), settle between evictions, retry admission briefly before refusing with 503.

- *Per-session quotas (alternative):* poly offers no cooperative way to cap or account its
  heap from outside; enforcing per-process rlimits kills sessions mid-proof unpredictably.
- *Do nothing / trust the OOM killer (alternative):* the killer takes the shared JVM first
  (biggest RSS) — one greedy request destroys every session (Bug 6's failure mode).

**Why:** admission at create time plus bounded eviction is the only lever that matches where
the memory actually is. The page-cache correction and settle/retry came from the
close-then-create race: teardown → kernel accounting lags → false "no memory" rejections.

### 1.7 Heavy-operation gating: server-wide sledgehammer semaphore

**Chosen:** a global semaphore (`MAX_CONCURRENT_SLEDGEHAMMER`) queues sledgehammer requests.
**Alternatives:** unbounded (Bug 6: W=16 concurrent sledgehammers OOM-killed the gateway);
per-session limits (useless — the burst comes from *many* sessions at once).

**Why:** sledgehammer is itself a multi-prover parallel job; the measured throughput knee was
~4 on 32 cores, so queueing beyond that costs nothing and prevents the one workload class
that reliably killed the JVM. Backpressure (waiting) beats failing.

### 1.8 Execution granularities: small-step, `verify_chunk`, big-step — all three, with
`verify_chunk` as the centerpiece

**Old:** small-step only (one command per round trip, subgoals after each) — the natural REPL
inheritance.
**Chosen:** keep small-step (checkpoints/rollback/RL uses), add **`verify_chunk`** (a whole
chunk as ONE PIDE edit under a single wall budget, returning per-command
`ok/failed/running/unprocessed` in source order, a partial report naming the `stuck_line` on
budget expiry, and automatic rollback of failed chunks), and keep **big-step**
(`isabelle build` on a temp session) as the strict ground truth.

- *Small-step-only cons:* for an LLM agent, one command per round trip explodes rounds and
  tokens; per-command timeouts surface as opaque failures.
- *Whole-file-only (big-step) cons:* no incremental state, no "which line is looping",
  minutes per iteration.
- *verify_chunk cons:* `success=True` means "no command errored", NOT "theorem proved" — a
  subtlety that must be (and is, aggressively) documented, because agents will happily
  self-deceive. Proved requires `success ∧ ¬proof_open ∧ ¬used_sorry`.

**Why:** the chunk is the natural unit of LLM proof output. One edit checks it with
intra-chunk parallelism, and the per-command report converts "timeout" from a dead end into
actionable feedback (*this* line loops). Big-step stays because a fresh `isabelle build` is
the only verdict immune to warm-session artifacts — the comparison arbiter runs on it, and
the I/Q "attempt 10" incident (in-session check accepted a metis call that fails cold)
proved that independence matters. **Abandoned:** the RL-era vector environment
(`vector_step`/`vectorise`) still exists in the backend but is unused by the server/MCP path.

### 1.9 Event-loop discipline: every blocking call off the loop

**Old (implicit):** router handlers called session methods via `asyncio.to_thread`, but
`_create_session` still did gateway spawn and Py4J calls inline; sledgehammer/metadata paths
similarly leaked blocking work onto the loop.
**Chosen:** all Py4J traffic and process spawning runs in worker threads; the event loop only
schedules.

**Why:** observed failure — a busy JVM turned one inline Py4J call into a frozen server
(every endpoint, including `/healthz`, unresponsive). In an asyncio server there is exactly
one rule: the loop never waits on the prover. The alternative (multi-worker uvicorn) doesn't
fit because the SessionManager is deliberately a single-process singleton owning one gateway.

### 1.10 Diagnostics as transient probes with a syntactic guard

**Chosen:** `POST /diagnostic` runs read-only Isar queries (`thm`, `find_theorems`,
`print_*`, …) as *transient* PIDE edits — output captured, edit discarded, rollback chain and
history untouched — gated by an allowlist (leading keyword) + denylist (`ML`, `setup`,
file-IO anywhere) in `diagnostic_guard.py`.
**Alternatives:** let clients run queries through the normal step path (pollutes the script
and rollback chain; earlier sledgehammer probes literally leaked `ML_val` into saved proofs);
or allow arbitrary commands (a prover command like `ML` is remote code execution).

**Why:** agents need lookups constantly, and lookups must be free of side effects — both on
the proof state and on the host. The conservative guard knowingly rejects a few legitimate
queries (documented trade-off) because the denied class includes code execution.

### 1.11 Per-session PIDE parallelism: modest fixed defaults, env-tunable

**Chosen:** `parallel_proofs=2`, ML `threads` capped at 4 by default (not 0 = auto),
overridable per deployment (`ISABELLE_PARALLEL_PROOFS`, `ISABELLE_SESSION_THREADS`).
**Alternative:** Isabelle's auto settings (each session grabs all cores).

**Why:** with M concurrent sessions, auto-threads oversubscribes cores M-fold and spikes peak
heap (a Bug 6 contributor). The cap trades single-session latency for pool stability. For
single-agent workloads (the MCP comparison), the right tuning inverts: one session, all
cores (`threads=8`) — which is why it's config, not a constant.

### 1.12 Container is the unit of deployment — and must be native-arch

**Chosen:** everything runs in one container (repo volume-mounted for dev, heaps in a named
volume); the image builds for the host architecture, selecting the matching Isabelle tarball
(x86-64 vs ARM) with a mirror-fallback download chain.
**Old:** `platform: linux/amd64` pinned — which silently ran the entire prover stack under
qemu on Apple Silicon at a 5–20× penalty (session creation went from ~1 min to many minutes;
one heap build measured 15+ min emulated vs 2m56s native).

**Why:** Isabelle's path assumptions and component registration make "works on my machine"
expensive; one blessed layout keeps it reproducible. The arch lesson is recorded because it
was invisible: everything *worked*, just absurdly slowly.

---

## 2. MCP server — agent-facing design

### 2.1 Thin layer over the HTTP client — no prover logic in the MCP process

**Chosen:** `mcp_server/` wraps `IsabelleGymAsyncClient` (plain HTTP). No Py4J, no Isabelle,
no core edits.
**Alternative:** embed the gym in the MCP process (one fewer hop), or fork a special agent
server.

**Why:** the server already solved pooling, leases, memory, and recovery — duplicating any of
it in a second process would fork the truth. The MCP process becomes stateless-ish and
restartable at will; the extra HTTP hop is noise next to prover latencies. *Con accepted:*
MCP capabilities are capped by the HTTP API — which turned into a feature: every capability
an agent needed (diagnostic, verify_chunk) had to be added at the API level, where every
other client also benefits.

### 2.2 Sessions and leases are invisible to the agent

**Chosen:** the agent calls `enter_theory(name, imports)`; the pool acquires a leased gym
session behind the scenes, keyed to the MCP connection. `close_theory` (or a new
`enter_theory`) disposes it. The agent never sees `session_id` or lease tokens.
**Alternative:** expose `create_session`/`acquire`/`release` as tools (some Isabelle MCPs
expose their session machinery).

- *Exposed pros:* an agent could in principle juggle several sessions.
- *Exposed cons:* every round trip burns tokens on plumbing; models mismanage handles
  (the I/Q token-mutation incident is the canonical example of making a model carry
  credentials/handles it doesn't need); leaked leases block the pool for hours.

**Why:** the agent's vocabulary should be proof concepts, not infrastructure. The one real
capability lost (multi-session use) was reintroduced deliberately and safely as
`verify_batch` (2.6). **Refinements from incidents:** `_begin_theory` closes the just-acquired
session if `enter_theory` fails (else the lease leaks for `MAX_LEASE_AGE`), and acquisition
uses `reuse_dirty=False` so an agent can never inherit another attempt's document (the
confirmed proof-leak instance).

### 2.3 Connection identity: weak-keyed by the MCP session object (the "stdio key" choice)

**Chosen:** per-connection state lives in a `WeakKeyDictionary` keyed by the MCP
`ctx.session` *object*; stdio (or any transport without a session object) falls back to a
single pool-owned sentinel object (`_StdioKey`).
**Old:** a plain dict keyed by the string `f"conn-{id(ctx.session)}"`.

- *Old cons:* entries were never removed (dropped connections leaked state forever), and
  `id()` is a memory address — after GC, a **new** connection's session object can reuse the
  address and silently inherit the old connection's half-finished proof session.
- *New pros:* identity is bound to the object itself (recycling impossible by construction),
  and weak keys make cleanup automatic — when the transport drops the connection, GC removes
  the entry with no disconnect hook.
- *New cons:* no eager close callback for the underlying gym session; it waits for the
  server's abandoned-lease reaper. (A `weakref.finalize` + async close was judged more
  machinery than the residual risk warranted.)

**Why the sentinel:** weak dict keys must be weak-referenceable (a `str` is not), and a
stdio server has exactly one logical connection for its lifetime — so one strongly-held
sentinel object is both necessary and semantically correct.

### 2.4 `verify_chunk` is the ONLY execution tool

**Chosen:** one tool submits Isar text — one command or a whole proof — and returns the
per-command status report. No `step` tool.
**Alternative:** mirror the REPL (step/undo per command), as several Lean/Isabelle MCPs do.

- *Step-tool cons:* rounds ≈ commands; token cost explodes; the agent must orchestrate its
  own batching anyway.
- *verify_chunk pros:* matches how models naturally emit proofs (blocks); per-line feedback
  (`failed` with messages, `running` with `stuck_line`) tells the agent *where* to intervene
  — the design goal distilled from the Lean ecosystem survey (`lean-lsp-mcp`'s
  multi-attempt/diagnostics tools) in `claude-work/`'s MCP research notes.
- *Renderer choice:* terse summary by default, `detail=True` for the full table — token
  economy with an escape hatch. Failure messages are truncated; `proof_open`/`used_sorry`
  warnings are spelled out in the OUTPUT (not only in docs) because agents act on what they
  see this turn, not what the schema said.

**Why:** granularity is the token budget. One tool with rich output beats five chatty tools.
**Abandoned:** exposing `step`, `checkpoint`-centric workflows as the primary loop
(checkpoint/restore/rollback remain available but secondary), and exposing raw subgoal
objects (state is fetched on demand via `proof_state`).

### 2.5 `diagnostic` as a separate read-only tool

**Chosen:** a dedicated tool for `thm`/`term`/`find_theorems`/`print_*` that returns output
the execution path would discard, running transiently under the server-side guard (1.10).
**Alternative:** let agents put queries inside `verify_chunk` chunks.

**Why:** queries inside chunks pollute the document (and their output is dropped by the
status report), so agents either lost information or corrupted their scripts. Separating
"look" from "touch" gives the agent a safe reflex — and the guard means "look" can never
execute code. The tool description enumerates concrete examples because models use tools
they can pattern-match.

### 2.6 `verify_batch`: bounded inter-session parallelism for a sequential agent

**Chosen:** one tool fans out N independent chunks concurrently, each in its own throwaway
leased session, bounded by a semaphore (`ISABELLE_MCP_MAX_PARALLEL`), returning per-item
summaries.
**Alternatives:** teach agents to hold multiple sessions (rejected in 2.2); or nothing.

**Why:** an MCP agent is a sequential loop, but the server pool is parallel — this tool is
the bridge, letting one round test several lemma variants at once without the agent managing
sessions. The bound respects the memory admission gate; without it, one enthusiastic call
could flood the pool.

### 2.7 Transport: stdio and streamable-HTTP from the same process

**Chosen:** both, switched by env (`ISABELLE_MCP_TRANSPORT`); stdio for local single-agent
use (Claude Desktop/Code, the comparison harness spawns it per attempt), HTTP for remote or
multi-agent setups.
**Why:** stdio is zero-config and process-per-connection (isolation for free); HTTP needs
the per-connection keying of 2.3 to keep agents isolated — which is exactly why that keying
exists. Fresh-process-per-attempt (harness style) also gives clean-slate guarantees that a
long-lived server cannot.

### 2.8 The tool docstrings are the system prompt

**Chosen:** the semantics agents must not get wrong — `success=True ≠ proved`, the
proved-only-when triple, "never sorry/oops to pass", auto-rollback behavior, restart
etiquette — are written into the tool descriptions themselves (and repeated in outputs at the
moment they matter). The bundled `prove_theorem` MCP prompt is minimal and optional.
**Alternative:** rely on each client's system prompt to explain the tools.

**Why:** tool schemas are the only channel guaranteed to reach *every* MCP client unchanged;
harness prompts vary per experiment (four variants exist), but the invariants must not.
The comparison runs validated this: the failure modes that survived all prompt variants were
exactly the ones later fixed in tools/output text, not in prompts. *Con accepted:* longer
schemas cost tokens on every round — mitigated by keeping per-call outputs terse (2.4).

---

## Cross-cutting lesson

Nearly every choice above was refined by an incident rather than foresight: exit-through-queue
(Bug 1/8), crash recovery (Bug 6), cgroup accounting (Bug 5/6), clean-only reuse (the proof
leak), weak connection keys (the id-recycling hazard), fresh-build arbitration (attempt 10),
native-arch containers (the qemu discovery). The meta-choice that made those refinements
cheap: **small, typed, observable layers** — every layer (HTTP API, pool, backend thread,
gateway, ML) can be probed and swapped without rewriting its neighbors, and every incident
above was diagnosed from logs/metrics those layers already emitted.
