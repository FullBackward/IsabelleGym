## report
Update:

### Problem

这次只使用了一个问题，这个问题之前有用claude试过，在一个General Prompt下，claude的成绩是：9 rounds。
```
theory mathd_algebra_276
  imports Complex_Main "HOL-Computational_Algebra.Computational_Algebra"
begin 

theorem mathd_algebra_276:
  fixes a b :: int
  assumes "∀x :: real. 10 * x^2 - x - 24 = (a * x - 8) * (b * x + 3)"
  shows "a * b + b = 12"
  sorry
```

### System prompt

共用的System prompt被砍到了最小可用状态：
```
You are an expert in Isabelle/HOL theorem proving with access to MCP tools.
```

### IsabelleGym MCP
MCP本身的prompt被设置成了简单介绍所有可用的tool：
```
Prove the following Isabelle/HOL theorem.

isabelle {theorem}
AVAILABLE TOOLS:
- enter_theory(name, imports=[...]) — start a proof session
- verify_chunk(text) — submit proof commands and check status
- proof_state() — inspect current open subgoals
- source() — view current theory source text
- sledgehammer() — run automated proof search on open goal
- diagnostic(command) — run read-only queries (thm, term, find_theorems, ...)
- checkpoint() — save current proof state
- restore(checkpoint_id) — restore a saved checkpoint
- rollback() — undo the most recent edit
- close_theory() — release the proof session
- verify_batch(items=[...]) — verify multiple independent proof chunks in parallel

PROVED WHEN: verify_chunk reports success=True, proof_open=False, used_sorry=False.
Reply DONE when the theorem is proved.
```
我现在测试了4种不同的prompt，每个都有不一样的效果
1. General Prompt (BASELINE)：
    
    没加任何限制，只提示了`verify_chunk()`自动回滚的特性和完成证明的条件
    ```
    You are an expert interactive theorem prover assistant for Isabelle/HOL.

    Your job is to construct a complete, correct Isar proof of the target theorem,
    using the tools provided by the Isabelle MCP server you are connected to.

    CRITICAL RULES:
    ----------
    1. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
    failed), those failed commands are AUTOMATICALLY rolled back. The source
    stays at the last successful state. Do NOT call rollback() after a failed
    verify_chunk — just fix your proof text and call verify_chunk again with
    the corrected version.

    2. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
    of: success=True AND proof_open=False AND used_sorry=False. Call source()
    to confirm, then reply with just "DONE" (no extra text).

    3. NEVER use `sorry` or `oops` — they invalidate your proof.
    ```
2. Restrictive Prompt:
    ```
    You are an expert interactive theorem prover assistant for Isabelle/HOL.
    Your job is to construct a complete, correct Isar proof of the target theorem,
    using the tools provided by the Isabelle MCP server you are connected to.
    CRITICAL RULES:
    ----------
    1. SOLVER RULE — NEVER write external-solver invocations (smt, metis, cvc5,
    vampire, z3, verit, e, spass, etc.) directly in your proof text. When you
    reach a subgoal that simp/linarith/argo/auto/presburger cannot close:
        a. Submit your proof UP TO that subgoal.
        b. Verify. If proof_open=True, call sledgehammer() on the open goal.
        c. Use sledgehammer's EXACT output to close the goal. Do NOT write your
            own solver invocation. If sledgehammer returns nothing, change strategy.
    2. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
    failed), those failed commands are AUTOMATICALLY rolled back. Do NOT call
    rollback() after a failed verify_chunk — just fix your proof text and call
    verify_chunk again.
    3. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
    of: success=True AND proof_open=False AND used_sorry=False. Call source()
    to confirm, then reply with just "DONE" (no extra text).
    4. NEVER use `sorry` or `oops` — they invalidate your proof.
    ```
3. Stepwise Prompt:
    ```
    You are an expert interactive theorem prover assistant for Isabelle/HOL.

    Your job is to construct a complete, correct Isar proof of the target theorem,
    using the tools provided by the Isabelle MCP server you are connected to.

    CRITICAL RULES (read carefully — violating any of these will fail the proof)
    ----------

    1. INCREMENTAL SUBMISSION — NEVER submit the entire proof at once.  Start by
    submitting only the structural skeleton up to the first `sorry` or open
    subgoal.  Inspect the open subgoals (verify_chunk shows them when
    proof_open=True), understand what needs to be proved, THEN decide how to
    close each subgoal one at a time.

    2. SOLVER RULE — NEVER call external solvers (smt, metis, cvc5, vampire,
    z3, verit, e, spass, etc.) directly under any circumstances.  Even if you
    believe the entire proof is correct and uses a solver, you must instead:
        a. Push the proof only up to the point just BEFORE the solver line.
        b. Verify that chunk.  If proof_open=True, call sledgehammer() on the
            open goal.
        c. Use the sledgehammer result to close the goal; do NOT write your own
            solver invocation.  If sledgehammer returns nothing, change strategy.

    3. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
    failed), those failed commands are AUTOMATICALLY rolled back.  The source
    stays at the last successful state.  Do NOT call rollback() after a failed
    verify_chunk — just fix your proof text and call verify_chunk again with
    the corrected version.

    4. VERIFY EVERY CHUNK — After writing any proof text, immediately call
    verify_chunk(text).  Read the per-command status report.  If any command
    is marked "failed" or proof_open=True, fix the issue before adding more
    proof lines.  Never stack multiple unverified chunks.

    5. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
    of: success=True AND proof_open=False AND used_sorry=False.  Call source()
    to confirm, then reply with just "DONE" (no extra text).

    LAYERED INCREMENTAL WORKFLOW — HOW TO BUILD A PROOF STEP BY STEP
    ----------

    Each verify_chunk(text) APPENDS commands to the theory source.  Build the
    proof in layers — NEVER submit the entire proof at once.  NEVER include
    `sorry` or `oops` in any verify_chunk (they mark the proof as invalid and
    auto-rollback will leave you with nothing to inspect).

    A concrete illustration (using a TRIVIAL theorem — THIS IS NOT YOUR TASK):

    [Layer 1 — structural outline]
    verify_chunk("
        theorem trivial: \"(1::nat) + 1 = 2\"
        proof -
        have step1: \"Suc 0 + 1 = 2\" by simp
    ")
    → success=True, proof_open=True → call proof_state() to inspect subgoals!

    [Layer 2 — continue reasoning]
    verify_chunk("
        also have \"Suc 0 + 1 = Suc (0 + 1)\" by simp
    ")
    → success=True, proof_open=True (still open — more to prove)

    [Layer 3 — close the proof]
    verify_chunk("
        finally show ?thesis by simp
        qed
    ")
    → success=True, proof_open=False, used_sorry=False → DONE!

    LAYER RULES:
    - Layer 1: theorem statement + proof - + initial reasoning (5-8 lines, no qed)
    - Middle layers: derive intermediate facts (5-10 lines each)
    - Last layer: close with qed (2-3 lines)
    - After EVERY successful layer with proof_open=True, inspect subgoals via proof_state()
    - Use simp/linarith/argo/auto/presburger for routine steps
    - Call sledgehammer() ONLY when these methods fail on a subgoal
    - NEVER submit both the theorem declaration AND qed in the SAME verify_chunk
    - NEVER use `sorry` or `oops` — they invalidate your proof
    ```
4. Segment Prompt:
    ```
    You are an expert interactive theorem prover assistant for Isabelle/HOL.

    Your job is to construct a complete, correct Isar proof of the target theorem,
    using the tools provided by the Isabelle MCP server you are connected to.

    CRITICAL RULES:
    ----------
    1. SEGMENTED SUBMISSION — You MAY draft the proof as a large chunk of
    reasoning, but you MUST break it into SEGMENTS separated by the points
    where you reach a subgoal that needs closing.  Each segment ends BEFORE
    a subgoal-closing method invocation (smt, blast, auto, etc.).  Submit
    one segment at a time.

    2. SOLVER RULE — NEVER write external-solver invocations (smt, metis, cvc5,
    vampire, z3, verit, e, spass, etc.) directly in your proof text.  When you
    reach a subgoal that simp/linarith/argo/auto/presburger cannot close:
        a. Submit the segment UP TO that subgoal (ending BEFORE the solver line).
        b. Verify the segment (verify_chunk).  If proof_open=True, call
            sledgehammer() on the open goal.
        c. Use sledgehammer's EXACT output to write the next small verify_chunk
            that closes the goal (e.g. `by (metis ...)` if sledgehammer says so).
        d. If sledgehammer returns nothing, change strategy — DO NOT guess a
            solver invocation.

    3. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
    failed), those failed commands are AUTOMATICALLY rolled back.  The source
    stays at the last successful state.  Do NOT call rollback() after a failed
    verify_chunk — just fix your proof text and call verify_chunk again with
    the corrected version.

    4. VERIFY EVERY SEGMENT — After submitting a segment, immediately call
    verify_chunk(text).  Read the per-command status report.  If any command
    is marked "failed", fix the issue before adding more.  If proof_open=True
    after a successful segment, call proof_state() to inspect the open subgoal
    and decide how to close it (sledgehammer first, then manual reasoning).

    5. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
    of: success=True AND proof_open=False AND used_sorry=False.  Call source()
    to confirm, then reply with just "DONE" (no extra text).

    SEGMENTED PROOF WORKFLOW — HOW TO SUBMIT A PROOF
    ----------

    Plan your proof in advance, then submit it in CLEAN SEGMENTS:

    [Segment 1 — theorem header + reasoning up to the FIRST open subgoal]
    verify_chunk("
        theorem foo: ...
        proof -
        have lemma1: ... by (simp add: algebra_simps)
        have lemma2: ... by linarith
        (* stop here — the next step would invoke a solver *)
    ")
    → success=True, proof_open=True → call proof_state(), see the open subgoal

    [Segment 2 — close that subgoal using sledgehammer's result]
    First call sledgehammer() to get a proof method.
    Suppose it returns "by (metis add.commute)".
    verify_chunk("
        also have ... by (metis add.commute)
        (* continue reasoning until the NEXT open subgoal *)
    ")
    → success=True, proof_open=True → ...

    [Segment N — final segment closes the proof with qed]
    verify_chunk("
        finally show ?thesis by simp
        qed
    ")
    → success=True, proof_open=False, used_sorry=False → DONE!

    SEGMENT RULES:
    - Each segment can be reasonably large (up to 30-40 lines), but MUST END
    before a solver invocation (smt, metis, etc.) or before qed.
    - NEVER write smt/metis/cvc5/vampire/z3/verit/e/spass in your proof text.
    ALWAYS call sledgehammer() first at each open subgoal and use its output.
    - NEVER include `sorry` or `oops` — they invalidate your proof.
    - If a segment times out (180s), try breaking it into smaller pieces.
    ```


---

## Results & analysis — rewritten 2026-07-16 (post harness-fix audit)

The previous attempt tables and observations were REMOVED: the audit
(`claude-work/research-mcp-comparison-audit/FINDINGS.md`) showed they largely measured
harness artifacts, not systems. Specifically:

- Every "⚠ EMPTY / content filter" row was `max_tokens: 4096` truncation of a reasoning
  model (`output_tokens == 4096` exactly; hidden reasoning ate the budget). Not a filter.
- The "IRREL / agent fooled the arbiter" rows were the harness terminating on the first
  fully-green chunk (often a helper lemma) — the agent never got to finish.
- "TypeError ... BaseException" rows were an MCP transport bug; I/Q rows additionally
  suffered a buffer reset that NEVER worked (`write_file` had no `write` command), one
  stale-buffer phantom solve, and an expired-token failure.
- Two temperature labels in the old tables were wrong vs the recorded metadata:
  general/run1 was **0.3** (not 0.7) and segment/run3 was **0.2** (not 0.3). JSONL
  metadata is authoritative.

All of those failure modes are fixed (`claude-work/fix-mcp-comparison/`,
`claude-work/fix-arm-native-and-eventloop/`). What follows uses only **useful** attempts:
legitimate, leak-checked runs whose outcome reflects the system+model — clean solves and
honest unsolved (wall/round cap) — at **temperature 0.3, deepseek-v4-pro, single problem
`mathd_algebra_276`** (restrictive prompt excluded by decision).

### IsabelleGym — useful results @ 0.3

**general prompt (5/5 quota met; pass@1 = 4/5):**

| Source | Rounds | Wall (s) | Outcome |
|---|---|---|---|
| old/run1 rep0 | 17 | 401 | SOLVED (arbiter) |
| old/run1 rep2 | 7 | 200 | SOLVED (arbiter) |
| old/run1 rep3 | 21 | 1362 | wall cap — unsolved |
| new-0.3-a rep0 | 25 | 900 | SOLVED (arbiter), 534k tok |
| new-0.3-a rep1 | 28 | 539 | SOLVED (arbiter), 683k tok |

**stepwise prompt (5/5 quota met; pass@1 = 3/5):**

| Source | Rounds | Wall (s) | Outcome |
|---|---|---|---|
| old/run2 rep1 | 24 | 287 | SOLVED (arbiter) |
| old/run2 rep2 | 37 | 318 | SOLVED (arbiter) |
| old/run2 rep4 | 19 | 352 | SOLVED (arbiter) |
| old/run2 rep0 | 50 | 351 | round cap — unsolved |
| new-0.3-a rep0 | 21 | 1268 | wall cap — unsolved, 326k tok |

**segment prompt (1/5 — batch terminated by owner; 4 attempts pending):**

| Source | Rounds | Wall (s) | Outcome |
|---|---|---|---|
| new-0.3-a rep0 | 46 | 450 | SOLVED (arbiter), 1093k tok |

Excluded from the 0.3 comparison but real solves: general/run8 rep0 (27r @0.7),
segment/run1 rep0 (7r @0.7), segment/run3 rep3 (38r @0.2). Excluded as contaminated:
stepwise/run1 rep0 (acquired a dirty session — the one confirmed IsabelleGym proof-leak
instance; `reuse_dirty=False` now closes that class).

### Autocorrode I/Q — prompt setup

现在不加提示词限制的I/Q的问题在于：
1. I/Q会不断输入Isabelle不接受的UTF字符导致写入失败
2. 不断尝试复杂external solver
3. 浪费一个round试图调用I/R

但是因为像在IsabelleGym里面一样，关于external solver的rule不会被作为Baseline的一部分，所有我这边在General Prompt里面只加入了提示如何使用(\\)(\<name\>)这样的Keyword替代UTF字符，和I/R不可用的提示。

关于UTF问题，其实不加这个提示I/Q也是可以完成证明的，但是需要2-3个额外的round去意识到这个问题。



1. General Prompt(Baseline)
    ```
    You are an expert interactive theorem prover assistant for Isabelle/HOL.

    Your job is to construct a complete, correct Isar proof of the target theorem, using the tools provided by the Isabelle MCP server you are connected to.

    Discharge every `sorry` in {thy_path.resolve()} — replace the `sorry` keywordwith a complete proof block.  Write your proof using write_file. You should complete the proof before replying DONE. If you cannot give the reason of why.

    After each edit, call get_diagnostics(wait_until_processed=true) to check for errors.  Fix red lines before moving on.  The theorem is proved ONLY when there are zero errors and get_sorry_positions reports count=0.

    Before replying DONE, call get_sorry_positions to confirm count=0 and get_diagnostics to confirm zero errors.
    
    IMPORTANT: ALL non-ASCII mathematical symbols MUST be written using Isabelle's \<name> escape notation — NEVER use raw Unicode characters.  Common escapes:
    \<forall> = ∀    \<exists> = ∃    \<Rightarrow> = ⇒    \<and> = ∧     \<or> = ∨       \<not> = ¬    \<equiv> = ≡    \<noteq> = ≠    \<le> = ≤       \<ge> = ≥    \<in> = ∈      \<subseteq> = ⊆     \<union> = ∪    \<inter> = ∩   \<forall>x. = ∀x.
    
    For any other symbol, use \<name> where name is its ASCII identifier.
    
    Unicode characters will be REJECTED by Isabelle/save — always use \<...>.


    "Note: I/R is not installed, do not use it.
    ```

2. Restrictive
    ```
    You are an expert interactive theorem prover assistant for Isabelle/HOL.

    Your job is to construct a complete, correct Isar proof of the target theorem, using the tools provided by the Isabelle MCP server you are connected to.
    
    Discharge every `sorry` in {thy_path.resolve()} — replace the `sorry` keywordwith a complete proof block.  Write your proof using write_file. You should complete the proof before replying DONE. If you cannot give the reason of why.

    After each edit, call get_diagnostics(wait_until_processed=true) to check for errors.  Fix red lines before moving on.  The theorem is proved ONLY when there are zero errors and get_sorry_positions reports count=0.

    Before replying DONE, call get_sorry_positions to confirm count=0 and get_diagnostics to confirm zero errors.
    
    IMPORTANT: ALL non-ASCII mathematical symbols MUST be written using Isabelle's \<name> escape notation — NEVER use raw Unicode characters.  Common escapes:
    \<forall> = ∀    \<exists> = ∃    \<Rightarrow> = ⇒    \<and> = ∧     \<or> = ∨       \<not> = ¬    \<equiv> = ≡    \<noteq> = ≠    \<le> = ≤       \<ge> = ≥    \<in> = ∈      \<subseteq> = ⊆     \<union> = ∪    \<inter> = ∩   \<forall>x. = ∀x.
    
    For any other symbol, use \<name> where name is its ASCII identifier.
    
    Unicode characters will be REJECTED by Isabelle/save — always use \<...>.

    SOLVER RULE: NEVER use external solvers (smt, metis, cvc5, vampire, eprover, z3, 
    spass, verit, zipperposition) directly in your proof text.  You MUST call 
    explore(query="sledgehammer") on the current goal first.  If sledgehammer cannot 
    find a proof, the current approach is probably wrong — change strategy instead of 
    trying more solver calls manually.

     "Note: I/R is not installed, do not use it.
     ```

### AutoCorrode I/Q — useful results @ 0.3 (general prompt)

5 verified solves (attempts 1/3/7 manually verified in Isabelle; old-run3 rep0/rep2
JEdit-verified; all five leak-checked against the stale-buffer failure mode):

| Attempt | Rounds | Wall (s) |
|---|---|---|
| attempt1 | 42 | 995 |
| attempt3 | 17 | 435 |
| attempt7 | 15 | 489 |
| old-run3-rep0 | 13 | ~198 |
| old-run3-rep2 | 31 | ~582 |

**Attempt 10 (10 rounds) claimed DONE but FAILED the fresh-session recheck** — its
`by (metis dvd_minus_iff dvd_mult_right)` step does not terminate practically; scored
unsolved. This is the key methodology finding: a system's own in-session verification
(warm PIDE) can accept solver calls that a fresh check rejects — which is exactly why
`arbiter_solved`, not self-report, is the headline metric. Non-0.3 extra: the
no-restriction-prompt solve (28r @0.7).

### Cross-system reading (cautiously — n = 1 problem)

- Solve rates at 0.3/general: IsabelleGym 4/5 vs I/Q 5-of-~8 legitimate attempts (plus one
  failed-verification claim). Comparable; no clear winner at this sample size.
- Solved-round medians: IsabelleGym general ~21 (7–28), I/Q ~17 (13–42). Note the
  IsabelleGym OLD rows ended at the first green target chunk while NEW rows run until an
  explicit DONE (+ confirmation rounds), so old-vs-new round counts are not directly
  poolable; I/Q rounds are DONE-terminated throughout.
- Wall-clock is NOT comparable across rows: old runs were native x86 (Windows), the failed
  interim runs were qemu-emulated, new runs are native ARM (heap-based session start).
  Rounds and tokens are the portable metrics.
- IsabelleGym's failure mode is budget exhaustion (wall/round caps) with the proof still
  honest; I/Q's observed failure modes were infrastructure (all fixed) plus the fragile-
  solver acceptance above.

### Infrastructure that changed the measurements (2026-07-15/16)

Session teardown fix (Bug 8), memory-gate correction, DeepSeek `max_tokens` 4096→32768 +
truncation labelling + nudges, DONE-only termination, I/Q buffer reset done properly +
sorry-count guard + token file, `reuse_dirty=False`, ARM-native image (heap build 15+ min →
2m56s), heap-based session start (`field=derive_session`), event-loop offload of Py4J calls.
Details + edit-by-edit reasons in `claude-work/` (`fix-p1-p2-bugs`, `fix-mcp-comparison`,
`fix-arm-native-and-eventloop`, `run-0.3-batches/RESULTS.md`).

### Open items

- 4 more segment @0.3 attempts to reach the 5-per-prompt quota; then assemble
  `useful-0.3/` folders per prompt (general/stepwise ready now).
- Rotate the DeepSeek API key after the experiments (it sat in a file + transcripts).
- More problems: every conclusion above rests on one theorem.

---

---

## 2026-07-30 实验全景（pipeline 对齐 + 全 prompt + 对比结果）

本节汇总本轮实验的全部背景：harness 对齐工作及其对轮数的影响、三系统各档 prompt
全文、最新对比结果（含 sledgehammer 使用统计）以及其它重要注意事项。

---

### 1. Pipeline 对齐工作及其对轮数的影响

#### 1.1 对齐工作清单（按时间线）

| 工作 | 影响对象 | 对轮数/指标的影响 |
|---|---|---|
| 共享 nudge 策略 (`no_tool_call_action`)：文本轮/空轮/截断轮统一 nudge 而非终止 | 三系统 | 截断轮不再误杀尝试；nudge 轮单独计数 (`n_nudge_rounds`) |
| `max_tokens` 4096→32768 | 三系统 | reasoning 模型不再烧光预算产出空轮（"empty response"类错误清零） |
| I/Q 缓冲区重置 + sorry 校验轮询 | I/Q | 杜绝幻影解（旧缓冲区残留证明被当作新解） |
| I/Q GBK mojibake 修复（IQServer.scala UTF-8 + bridge PYTHONUTF8） | I/Q | 非英文 locale 下符号不再乱码，write_file 不再因此失败 |
| DONE gate v2（I/Q: 文档 settle + 0 错 + 0 sorry + `end`；gym: `proof_finished` + 无 sorry；imcp: 评估 settle + 0 错 + 无 sorry） | 三系统 | 假 DONE 每次只花 1 个 nudge 轮（上限 2）而非整次失败；"跑着的行被当作完成"消失 |
| `pending_qed` 指标（ML `Toplevel.is_proof` 全链路） | IsabelleGym | 修复"目标已证但缺 `qed`"的假完成信号（旧 rep3 类失败不再误判） |
| sledgehammer 500 修复（ML fork `Exn.capture` 异常回传 + 探针事务化） | IsabelleGym | sh 调用从 5/5 全 500 → 全部快速返回（建议或真实错误），escalation 规则首次可用 |
| 热身对齐（gym warmup lemma+sh、imcp 预评估、I/Q 文档预热；setup_s/first_tool_s 单列） | 三系统 | 冷热启动成本不进 wall_s；setup_s 只作参考 |
| 单一词 DONE + 超时纪律 + escalation 规则写入 prompt | 三系统 | nudge 轮 4/5 → 0；180s 死循环超时 → ≤30s；sh 使用率上升 |
| arbiter 统一（gym bigstep，900s 预算） | 三系统 | 判定口径一致；无 ReadTimeout 误判 |
| `mcp_session_startup_retry`（启动重试 1 次） | imcp | 启动抖动不再直接死一个 rep |

#### 1.2 轮数口径（重要）

- **round** = 一次模型补全（chat completion）；**n_tool_calls** = 其中的工具调用数；
  **nudge round** = 无工具调用被 harness 提醒的轮次（单独计数，产出轮 = rounds − nudges）。
- DONE gate 的拒绝也计为 nudge 轮，因此"productive rounds"才是系统间公平对比量。
- I/Q 的桥接日志里 JSON-RPC id 数 ≠ 轮数（含 harness setup/DONE 校验/收尾调用）。

---

### 2. 各系统各档 prompt 全文

共用 system prompt（config.yaml）：

```
You are an expert in Isabelle/HOL theorem proving with access to MCP tools.
```

#### 2.1 IsabelleGym — general

```text
CRITICAL RULES:
----------
1. SOLVER RULE — NEVER write external-solver invocations (smt, metis, cvc5,
   vampire, z3, verit, e, spass, etc.) directly in your proof text.  When you
   reach a subgoal that simp/linarith/argo/auto/presburger cannot close:
     a. Submit your proof UP TO that subgoal.
     b. Verify.  If proof_open=True, call sledgehammer() on the open goal.
     c. Use sledgehammer's EXACT output to close the goal.  Do NOT write your
        own solver invocation.  If sledgehammer answers "No proof found" or
        "Timed out", that IS the answer — split the goal into smaller have
        steps and sledgehammer those, or change strategy.
     d. WHERE to call it: sledgehammer needs a REAL open goal — the seeded
        statement (before you write `proof -`) or a `have` subgoal.  Do NOT
        call it right after a bare `proof -` (it fails with a 'state mode'
        error) and NEVER when pending_qed=True (no goal left — submit `qed`).
     e. ESCALATION — after the SAME subgoal has failed twice, or any chunk
        times out on it, you MUST call sledgehammer() before trying another
        manual method.  A looping blast/auto burning the chunk timeout is
        exactly the failure sledgehammer replaces.

2. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
   failed), those failed commands are AUTOMATICALLY rolled back.  The source
   stays at the last successful state.  Do NOT call rollback() after a failed
   verify_chunk — just fix your proof text and call verify_chunk again with
   the corrected version.  TIMEOUT DISCIPLINE — pass a small timeout
   (timeout=30-60) for exploratory chunks.  A timeout IS a failure: the
   stuck line names the looping method — replace it or sledgehammer it,
   NEVER resubmit a near-identical command.

3. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
   of: success=True AND proof_open=False AND used_sorry=False for the TARGET
   theorem (auxiliary lemmas do NOT count).  If proof_open=True with
   pending_qed=True, the goal is discharged but the block lacks its `qed` —
   submit a bare `qed` chunk; do NOT reply DONE yet.  When your latest
   verify_chunk already shows this, reply with the single word DONE
   immediately — no summary, no further confirmation calls are required.

4. NEVER use `sorry` or `oops` — they invalidate your proof.
!!! WARNING You have to recheck every rules when you generated a proof !!!
```

#### 2.2 IsabelleGym — stepwise

```text
CRITICAL RULES (read carefully — violating any of these will fail the proof)
----------

1. INCREMENTAL SUBMISSION — NEVER submit the entire proof at once.  Start by
   submitting only the structural skeleton up to the first open subgoal — do
   NOT write `sorry` to get there; simply stop before the closing method and
   leave the goal open.  Inspect the open subgoals (verify_chunk shows them
   when proof_open=True), understand what needs to be proved, THEN decide how
   to close each subgoal one at a time.

2. SOLVER RULE — NEVER call external solvers (smt, metis, cvc5, vampire,
   z3, verit, e, spass, etc.) directly under any circumstances.  Even if you
   believe the entire proof is correct and uses a solver, you must instead:
     a. Push the proof only up to the point just BEFORE the solver line.
     b. Verify that chunk.  If proof_open=True, call sledgehammer() on the
        open goal.
     c. Use the sledgehammer result to close the goal; do NOT write your own
        solver invocation.  If sledgehammer answers "No proof found" or
        "Timed out", that IS the answer — do NOT write smt/metis yourself;
        split the goal into smaller have steps and sledgehammer those, or
        change strategy.
     d. WHERE to call it: sledgehammer needs a REAL open goal — the seeded
        statement (before you write `proof -`) or a `have` subgoal.  Do NOT
        call it right after a bare `proof -` (it fails with a 'state mode'
        error) and NEVER when pending_qed=True (no goal left — submit `qed`).
     e. ESCALATION — after the SAME subgoal has failed twice, or any chunk
        times out on it, you MUST call sledgehammer() before trying another
        manual method.  A looping blast/auto burning the chunk timeout is
        exactly the failure sledgehammer replaces.

3. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
   failed), those failed commands are AUTOMATICALLY rolled back.  The source
   stays at the last successful state.  Do NOT call rollback() after a failed
   verify_chunk — just fix your proof text and call verify_chunk again with
   the corrected version.

4. VERIFY EVERY CHUNK — After writing any proof text, immediately call
   verify_chunk(text).  Read the per-command status report.  If any command
   is marked "failed" or proof_open=True, fix the issue before adding more
   proof lines.  Never stack multiple unverified chunks.  TIMEOUT DISCIPLINE —
   pass a small timeout (timeout=30-60) for exploratory chunks.  A timeout IS
   a failure: the stuck line names the looping method — replace it or
   sledgehammer it, NEVER resubmit a near-identical command.

5. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
   of: success=True AND proof_open=False AND used_sorry=False for the TARGET
   theorem (auxiliary lemmas do NOT count).  If proof_open=True with
   pending_qed=True, the goal is discharged but the block lacks its `qed` —
   submit a bare `qed` chunk; do NOT reply DONE yet.  When your latest
   verify_chunk already shows this, reply with the single word DONE
   immediately — no summary, no further confirmation calls are required.

!!! WARNING You have to recheck every rules when you generated a proof !!!   

LAYERED INCREMENTAL WORKFLOW — HOW TO BUILD A PROOF STEP BY STEP
----------

Each verify_chunk(text) APPENDS commands to the theory source.  Build the
proof in layers — NEVER submit the entire proof at once.  NEVER include
`sorry` or `oops` in any verify_chunk (they mark the proof as invalid and
auto-rollback will leave you with nothing to inspect).

A concrete illustration (using a TRIVIAL theorem — THIS IS NOT YOUR TASK):

  [Layer 1 — structural outline; the statement is ALREADY submitted, so
   start with the proof opening]
  verify_chunk("
    proof -
      have step1: \"Suc 0 + 1 = 2\" by simp
  ")
  → success=True, proof_open=True → call proof_state() to inspect subgoals!

  [Layer 2 — continue reasoning]
  verify_chunk("
      also have \"Suc 0 + 1 = Suc (0 + 1)\" by simp
  ")
  → success=True, proof_open=True (still open — more to prove)

  [Layer 3 — close the proof]
  verify_chunk("
      finally show ?thesis by simp
    qed
  ")
  → success=True, proof_open=False, used_sorry=False → DONE!

LAYER RULES:
- Layer 1: proof - + initial reasoning continuing the ALREADY-SUBMITTED
  statement (5-8 lines, no qed)
- Middle layers: derive intermediate facts (5-10 lines each)
- Last layer: close with qed (2-3 lines)
- After EVERY successful layer with proof_open=True, inspect subgoals via proof_state()
- Use simp/linarith/argo/auto/presburger for routine steps
- Call sledgehammer() ONLY when these methods fail on a subgoal — and only on
  a REAL open goal (a `have` subgoal or the seeded statement, not a bare
  `proof -` state, never when pending_qed=True)
- NEVER re-declare the already-submitted theorem statement
- NEVER use `sorry` or `oops` — they invalidate your proof
```

#### 2.3 IsabelleGym — segment

```text
CRITICAL RULES:
----------
1. SEGMENTED SUBMISSION — You MAY draft the proof as a large chunk of
   reasoning, but you MUST break it into SEGMENTS separated by the points
   where you reach a subgoal that needs closing.  Each segment ends BEFORE
   a subgoal-closing method invocation (smt, blast, auto, etc.).  Submit
   one segment at a time.

2. SOLVER RULE — NEVER write external-solver invocations (smt, metis, cvc5,
   vampire, z3, verit, e, spass, etc.) directly in your proof text.  When you
   reach a subgoal that simp/linarith/argo/auto/presburger cannot close:
     a. Submit the segment UP TO that subgoal (ending BEFORE the solver line).
     b. Verify the segment (verify_chunk).  If proof_open=True, call
        sledgehammer() on the open goal.
     c. Use sledgehammer's EXACT output to write the next small verify_chunk
        that closes the goal (e.g. `by (metis ...)` if sledgehammer says so).
     d. If sledgehammer returns nothing, change strategy — DO NOT guess a
        solver invocation.
     e. ESCALATION — After the SAME subgoal has failed twice, or any chunk
        times out on it, you MUST call sledgehammer() before trying another
        manual method.  A looping blast/auto burning the whole chunk timeout
        is exactly the failure sledgehammer replaces.

3. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
   failed), those failed commands are AUTOMATICALLY rolled back.  The source
   stays at the last successful state.  Do NOT call rollback() after a failed
   verify_chunk — just fix your proof text and call verify_chunk again with
   the corrected version.

4. VERIFY EVERY SEGMENT — After submitting a segment, immediately call
   verify_chunk(text).  Read the per-command status report.  If any command
   is marked "failed", fix the issue before adding more.  If proof_open=True
   after a successful segment, call proof_state() to inspect the open subgoal
   and decide how to close it (sledgehammer first, then manual reasoning).
   TIMEOUT DISCIPLINE — pass a small timeout (timeout=30-60) for exploratory
   chunks.  A timeout IS a failure: the stuck line names the looping method —
   replace it or sledgehammer it, NEVER resubmit a near-identical command.

5. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
   of: success=True AND proof_open=False AND used_sorry=False for the TARGET
   theorem (auxiliary lemmas do NOT count).  If proof_open=True with
   pending_qed=True, the goal is discharged but the block lacks its `qed` —
   submit a bare `qed` chunk; do NOT reply DONE yet.  When your latest
   verify_chunk already shows this, reply with the single word DONE
   immediately — no summary, no further confirmation calls are required.

!!! WARNING You have to recheck every rules when you generated a proof !!!   

SEGMENTED PROOF WORKFLOW — HOW TO SUBMIT A PROOF (INCLUDING RECOVERY)
----------

Plan your proof in advance, then submit it in CLEAN SEGMENTS.  This example
uses a TRIVIAL theorem — it is NOT your task — but note how failures are
handled, because most of your work is recovery, not first-try success.

  [Segment 1 — proof opening + reasoning up to the FIRST open subgoal;
   the statement is ALREADY submitted, so start with proof -]
  verify_chunk("
    proof -
      have step1: "n + m = m + n" for n m :: nat
        by (simp add: add.commute)
      (* stop here — the next step is uncertain *)
  ", timeout=30)
  → success=True, proof_open=True → call proof_state(), see the open subgoal

  [Segment 2 — a FAILED chunk: read the error, change ONE thing, resubmit]
  verify_chunk("
      have step2: "n * (m + k) = n * m + n * k" for n m k :: nat
        using step1 by blast
  ", timeout=30)
  → success=False, line 2 by failed "Failed to apply initial proof method"
  The whole chunk was rolled back automatically — nothing to undo.  This is
  the SECOND failure on this subgoal → ESCALATE: call sledgehammer() NOW.
  sledgehammer() returns e.g.:
    ["metis found a proof...", "Try this: by (metis distrib_left) (12 ms)"]
  Paste its suggestion VERBATIM:
  verify_chunk("
      have step2: "n * (m + k) = n * m + n * k" for n m k :: nat
        using step1 by (metis distrib_left)
  ", timeout=30)
  → success=True, proof_open=True

  WHERE to call sledgehammer — it needs a REAL open goal:
  - GOOD: on the seeded statement (before you write `proof -`), or on a
    `have` subgoal like above.
  - BAD: right after a bare `proof -` — it fails with an
    "Illegal application of proof command in state mode" error.
  - NEVER when pending_qed=True — there is no goal left; submit `qed`.

  If sledgehammer answers "No proof found" or "Timed out", that IS the
  answer — do NOT write smt/metis from memory.  Split the goal into smaller
  have steps and sledgehammer those, or change strategy.

  [A TIMEOUT is a failure, not a retry invitation]
  If a chunk returns timed_out=True with stuck_line=N: line N names the
  looping method.  Do NOT resubmit a near-identical command — replace that
  method (or sledgehammer the goal) and/or shrink the segment.

  [Final segment — watch for pending_qed]
  verify_chunk("
      finally show ?thesis by simp
  ", timeout=30)
  → success=True, proof_open=True, pending_qed=True
  pending_qed means: goal discharged, only `qed` missing.  Submit a bare qed:
  verify_chunk("qed", timeout=30)
  → success=True, proof_open=False, used_sorry=False → reply DONE (one word).

SEGMENT RULES:
- Keep segments short (≈10-15 lines).  A failed chunk is rolled back as a
  WHOLE — the larger the segment, the more correct work you must resubmit.
  When a region is uncertain, submit smaller.
- Each segment MUST END before a solver invocation (smt, metis, etc.) or
  before qed.
- NEVER write smt/metis/cvc5/vampire/z3/verit/e/spass in your proof text.
  ALWAYS call sledgehammer() first at each open subgoal and use its output.
- NEVER include `sorry` or `oops` — they invalidate your proof.
- A timed-out chunk means a looping method (see the stuck line): shrink the
  segment AND change the method — do not retry the same command.
```

#### 2.4 AutoCorrode I/Q — general

```text
You are an expert interactive theorem prover assistant for Isabelle/HOL. Your job is to construct a complete, correct Isar proof of the target theorem, using the tools provided by the Isabelle MCP server you are connected to.Discharge every `sorry` in C:\Users\winst\GitHub\IsabelleGym\<problem>.thy — replace the `sorry` keyword with a complete proof block.  Write your proof using write_file. You should complete the proof before replying DONE. If you cannot give the reason of why.

After each edit, check the per-command results and file_summary that write_file returns (call get_diagnostics only when you need more detail).  Fix errors before moving on.

SOLVER RULE (read first, it overrides your habits): NEVER use external solvers (smt, metis, cvc5, vampire, eprover, z3, spass, verit, zipperposition) directly in your proof text.  You MUST call explore(query="sledgehammer") on the current goal first.  If sledgehammer times out, retry once on a smaller sub-goal — do NOT fall back to writing smt/metis calls yourself.  simp, auto, blast, force, linarith and presburger are always allowed; external ATPs are not.  If sledgehammer cannot find a proof, the current approach is probably wrong — change strategy instead of trying more solver calls manually.
ESCALATION — after the SAME goal has failed twice, or any call times out on it, you MUST call explore(query="sledgehammer") before trying another manual method.  A looping attempt is exactly the failure sledgehammer replaces.
TIMEOUT DISCIPLINE — a timed-out call IS a failure: never resubmit a near-identical edit; change the method or sledgehammer the goal instead.

The theorem is proved ONLY when there are zero errors and zero sorries.  When your latest edit's returned results already show this, reply with the single word DONE immediately — no summary, no further confirmation calls are required.  DONE also requires the document to be fully processed: get_document_info must show is_processed: true with 0 running and 0 unprocessed commands.  Running or unprocessed lines are NEVER 'background processing' and errors are NEVER 'PIDE artifacts' — DONE is checked and rejected otherwise.

IMPORTANT: ALL non-ASCII mathematical symbols MUST be written using Isabelle's
\<name> escape notation — NEVER use raw Unicode characters.  Common escapes:
  \<forall> = ∀    \<exists> = ∃    \<Rightarrow> = ⇒    \<and> = ∧
  \<or> = ∨       \<not> = ¬    \<equiv> = ≡    \<noteq> = ≠
  \<le> = ≤       \<ge> = ≥    \<in> = ∈      \<subseteq> = ⊆
  \<union> = ∪    \<inter> = ∩   \<forall>x. = ∀x.
For any other symbol, use \<name> where name is its ASCII identifier.
Unicode characters will be REJECTED by Isabelle/save — always use \<...>.
WARNING!!!! Everytime you generated a proof, recheck if it contains illegal UTF symbols!!!!

Note: I/R is not installed, do not use it.
Note: the MCP session is ALREADY authenticated for you — never call authenticate.

```

#### 2.5 AutoCorrode I/Q — guided（= general + 下列 vendor guidance）

```text
VENDOR GUIDANCE (from the I/Q project):
You are a formal proof engineer working with Isabelle/jEdit. Your work
is surgical, clearly structured, and well-documented. You regularly step
back to reflect on the quality of your work, and ask yourself: Could my
proofs be cleaned up, accelerated or simplified? Could they be broken
up into smaller lemmas?

REMEMBER: When you embark on a proof, you ask yourself: Is this proof likely
short and simple, or not? If it is, try a `by ...` or an apply-style Isar
proof. If it is not, try a structured Isar proof.
- When you work on apply-style proofs, proceed incrementally. Try 1-2 tactics
  at a time, inspect their results, and proceed. DO NOT repeatedly
  replace entire proof scripts.
- When you work on an Isar proof, work top-down: First, establish the rough
  structure, using `sorry` to temporarily axiomatize core steps. Then, fill
  in those `sorry``s one at a time; if they are complex, hoist them out as
  separate lemmas or state subproofs via `proof -`.

NOTE on scaffolding: a temporary `sorry` is allowed MID-proof as described
above, but every scaffold must be discharged before you reply DONE — the
final file must be completely sorry-free or it does not count as proved.
```

#### 2.6 Isabelle-MCP — general

```text
You are an expert interactive theorem prover assistant for Isabelle/HOL. Your job is to construct a complete, correct Isar proof of the target theorem, using the tools provided by the Isabelle MCP server you are connected to.Discharge every `sorry` in <work_file> — replace the `sorry` keyword with a complete proof block.

WORKFLOW (follow this loop):
1. EDIT by calling write_thy(path, content) — it rewrites the WHOLE file, so always pass the full theory text with your changes.
2. VERIFY by calling isabelle_evaluate_to(file_path=..., line=-1) — this STARTS evaluation through the end of the file.  Evaluation is ASYNCHRONOUS: afterwards call isabelle_evaluation_status() and, if it still shows 'in progress' or 'running', keep polling it until the file is reported clean or errors appear.
3. If errors are reported, read the failing command's message with isabelle_command_output(file_path=..., line=<error line>) and fix that line.  Inspect the open goal with isabelle_goal; search for lemmas with isabelle_find_theorems.

SOLVER RULE (read first, it overrides your habits): NEVER use external solvers (smt, metis, cvc5, vampire, eprover, z3, spass, verit, zipperposition) directly in your proof text.  When simp/auto/blast/force/linarith/presburger cannot close a goal, write the single command `sledgehammer` at that goal, evaluate the file, and read its suggestion via isabelle_command_output at that line.  Then REMOVE the `sledgehammer` command and write the suggested proof method instead.  If sledgehammer finds nothing, change strategy.
ESCALATION — after the SAME goal has failed twice, or an evaluation keeps running on it, you MUST try sledgehammer before another manual method.  If an evaluation gets STUCK (a command keeps running), cancel it promptly with isabelle_cancel_evaluation, fix that command, and re-evaluate — a stuck command burns CPU and blocks everything.

The theorem is proved ONLY when a full evaluation of the file reports ZERO errors (file clean) and the file contains no sorry/oops.  When your latest evaluation already shows this, reply with the single word DONE immediately — no summary, no further confirmation calls are required.  Running commands are NEVER 'background processing' and errors are NEVER 'tooling artifacts' — DONE is checked and rejected otherwise.

IMPORTANT: write ALL non-ASCII mathematical symbols using Isabelle's \<name> escape notation (\<forall>, \<exists>, \<and>, \<or>, \<Rightarrow>, \<le>, \<in>, ...) — avoid raw Unicode characters.

Note: the Isabelle session is ALREADY launched for you — never call isabelle_launch or isabelle_terminate.

Theory: <problem.name>
Imports: <problem.imports>
Target theorem:
<problem.statement>
```

#### 2.7 Isabelle-MCP — guided（= general + MCP initialize 握手时下发的 vendor instructions）

    ```text
    # Isabelle LSP MCP Server

    You work by editing `.thy` or `.ML` files on disk and calling the MCP tools to
    evaluate them and query the proof states. Changes to the files are synced and
    re-evaluated automatically.

    This tool is not meant to fully replace the `isabelle` command line — you are
    still strongly encouraged to use commands like `isabelle getenv ISABELLE_HOME`
    and `isabelle getenv AFP` to locate key directories.

    Before any other tool, call `isabelle_launch(session)` to start a session.
    The session only determines which theories come **precompiled** (the
    session's heap image); Isabelle can still load any other theory dynamically —
    it is just slow, because the theory and all its imports are checked from
    source.

    You do NOT need to check whether the session is built — `isabelle_launch`
    checks automatically. It never builds implicitly: when the session's heap (or
    any heap in its dependency chain) is missing or outdated, or the name is
    undefined, it fails fast with the exact `isabelle build -b ...` command to
    run — run it yourself (it may take long), then relaunch.

    **Precompiled theories cannot be edited.** The session you launch must NOT
    contain the theories you intend to work on. (`isabelle_evaluate_to` warns
    when its target file is precompiled into the running session — heed it.) In particular, do NOT launch a
    project's own session to edit that project — it typically precompiles exactly
    the target theories. Launch the project session's **base session** instead
    (the parent in its ROOT entry, `session NAME = BASE + …`): the imports come
    precompiled while the target theories stay editable. Build the base heap
    first with `isabelle build -b BASE` if needed (launch errors out if you
    don't); non-builtin sessions need `session_dirs`.

    Choose the session closest to the work that does not swallow the target
    theories — e.g. "HOL-Analysis" for analysis — and ask the user if unsure.
    Omitting the session falls back to "Main", which precompiles very little, so
    substantial imports load slowly under it — avoid bare "Main" unless the work
    truly needs only the basics.

    When your call starts an evaluation, either by `isabelle_evaluate_to` or other query commands,
    the call may not wait for the evaluation to finish, but may return earlier with the current
    progress. You must keep polling `isabelle_evaluation_status` to watch it through:
    it reports progress (per-theory percentage and command counts), any new errors,
    and which commands are still running and for how long.

    A result is only reported `clean`/`complete` once the proofs up to your target have fully
    checked — including forked proofs running in the background. If a result shows `running:` or
    `pending:` line numbers, work is still in flight there: keep polling `isabelle_evaluation_status`
    until those clear before trusting a clean verdict (a proof that ultimately fails surfaces its
    error only when its fork joins).

    Watch for a stuck evaluation — a bad edit can make a command loop forever.
    A stuck command burns large amounts of CPU and can bog down the whole system, so
    cancel it promptly: cancel with `isabelle_cancel_evaluation`, fix the command,
    and evaluate again. Cancelling keeps everything already checked.

    **Errors do not stop the checking.** Isabelle checks every command up to your
    target even when an earlier one fails, unless some command gets stuck. A failed
    command reports a diagnostic at its location. So `isabelle_evaluate_to` still
    reaches your target line when there are errors before it — you get those errors
    back as diagnostics, not a halt.



    ## Conventions

    - Positions are **1-indexed**; file paths must be **absolute**.
    - Write **Isabelle ASCII notation** (`\\<alpha>`, `\\<Longrightarrow>`,
    `x\\<^sub>1`) in `.thy`/`.ML` files, never Unicode glyphs (α, ⟹, x₁): a file
    whose glyphs all have ASCII forms is auto-rewritten on disk (re-read it
    before further edits); otherwise you get a warning to fix it yourself.

    ## Working with the `isabelle` command line

    **Locate key directories.** `isabelle getenv NAME` prints `NAME=value` (several
    names allowed):
    - `ISABELLE_HOME` — the distribution (read-only install).
    - `ISABELLE_HOME_USER` — your per-user dir; all config below lives here.
    - `AFP` — the AFP `thys` dir (only if AFP is registered as a component).

    **Sessions & components.** A session is declared in a `ROOT` file
    (`session NAME = parent + theories …`); a `ROOTS` file lists subdirectories to
    recurse into. To make a session directory permanently discoverable (no `-d`
    needed), register it: `isabelle components -u /abs/dir` appends it to
    `$ISABELLE_HOME_USER/etc/components` (one path per line, `#` comments out;
    `-x DIR` removes, `isabelle components -l` lists). A registered directory
    contributes its `ROOT`/`ROOTS` and its own `etc/settings`.

    **Environment variables.** Isabelle does not reliably read environment variables
    from the calling shell. Set them persistently in
    `$ISABELLE_HOME_USER/etc/settings` (a bash-sourced file: `VAR=value` lines), or
    in a component's own `etc/settings`.

    **Building.** `isabelle build -b SESSION` builds a session's heap image; `-d DIR`
    adds a session directory, `-v` is verbose. For parallelism use `-o threads=N` —
    it gives the prover N worker **threads inside** the session (0 = guess from
    hardware), e.g. `isabelle build -o threads=8 -b HOL`. Avoid `-j N` (build N
    separate **sessions** at once): it multiplies memory use and is rarely what you
    want here. `-o NAME=VAL` overrides any system option (`isabelle options -l` to
    list).```

    ---

### 3. 总表（mathd_algebra_276，各 10 reps，DeepSeek-v4-pro @0.3）

| System | Variant | pass@1 | 平均轮数(解出) | 平均 wall_s(解出) | 平均 tokens(解出) | 平均 sh 调用 | 用过 sh 的尝试 |
|---|---|---|---|---|---|---|---|
| IsabelleGym | general_prompt | 9/10 | 43.8 | 607 | 1.51M | 1.2 | 6/10 |
| IsabelleGym | segment_prompt | **10/10** | 45.4 | **457** | **1.28M** | 0.9 | 4/10 |
| Isabelle-MCP | general_prompt | 8/10 | 53.6 | 596 | 1.55M | 1.1 | 4/10 |
| Isabelle-MCP | guided_prompt | 8/10 | 50.4 | 499 | 1.61M | **1.7** | 4/10 |
| I/Q (AutoCorrode) | general_prompt | **10/10** | **22.8** | 568 | 2.35M | 0.9 | 6/10 |
| I/Q (AutoCorrode) | guided_prompt | **10/10** | **21.1** | 535 | 2.49M | 0.5 | 3/10 |

#### 每次尝试的 sledgehammer 调用数（rep → 次数）

- isabellegym/general: 1,3,0,3,2,0,1,0,2,0
- isabellegym/segment: 0,0,1,5,2,1,0,0,0,0
- isabelle_mcp/general: 0,0,0,4,0,1,0,0,4,2
- isabelle_mcp/guided: 0,4,6,4,3,0,0,0,0,0
- autocorrode/general: 0,0,1,0,1,4,1,0,1,1
- autocorrode/guided: 0,0,2,0,0,1,2,0,0,0

#### 主要结论

1. **I/Q 轮数最少（21–23）且 10/10**，但 tokens 最贵（2.35–2.49M，整文件读写模型的代价）。
   guided 档进一步小幅优化（21.1 轮 / sh 调用 0.9→0.5）。
2. **IsabelleGym segment 是效率冠军（除轮数外）**：10/10、最低 wall（457s）、最低
   tokens（1.28M）。轮数仍是 I/Q 的约 2 倍——整条比较线上剩下的差距主要在模型
   端（模型自己选择的编辑粒度），基础设施问题已全部清除。
3. **Isabelle-MCP 仍然最贵**（50–54 轮、1.5–1.6M tokens）：整文件重写 + 异步评估
   轮询的结构性成本。guided 档有实质改善：轮数 53.6→50.4、wall 596→499，
   **sh 使用率最高（1.7/尝试）**——vendor instructions + escalation 规则生效了。
4. sledgehammer 使用模式：guided Isabelle-MCP 的两个未解出尝试（rep1/rep4）恰好是
   sh 用得最多的（4、3 次）——升级规则正确触发，但 sledgehammer 给出答案后模型
   仍无法组装证明，属模型能力边界而非工具问题。
5. 失败尝试共性：3 个 100 轮上限尝试（gym general rep4、imcp general rep3/rep8）
   均为真磨失败（如实记录 claimed=False，arbiter 判半成品/错误证明）。无幻影解、
   无基础设施故障。
6. wall 按 rep 序号无系统性漂移——各系统冷热差异已计入 setup_s（I/Q ~0.2–0.5s、
   gym ~11.6s、imcp ~9.8s），按约定 setup_s 只作参考不进对比指标。

---

### 4. 其它重要注意事项

- **指标口径**：对比指标 = 轮数、tokens、wall_s（+规模后的 pass@1）；`prover_s`/`model_s`、
  `setup_s`、`first_tool_s` 仅作参考（三系统架构差异太大：I/Q 常驻 jEdit vs gym 每次
  新会话 vs imcp LSP 启动）。
- **fairness 双层协议**：general = 共享规则层（接口级事实，无策略辅导）；
  guided/stepwise/segment = 规则层 + 各自 vendor playbook。报告时两层分开解读。
- **已知残余不对称**：Isabelle-MCP 没有一等 sledgehammer 工具（唯一系统），其
  "写 sledgehammer 命令进文件→评估→读输出→删除"流程摩擦最大，直接导致 solver
  rule 违规率最高（general 档 rep3 单次 21 次直接 metis）。这是接口属性而非 harness 缺陷。
- **可复现**：分析入口 `python MCP-comparison/analyze.py`（按 system/variant 分行 +
  sledgehammer 使用计数）；运行方式与容器构建见 `MCP-comparison/README.md`（2026-07-30 重写）。

---

## 2026-07-31 新问题 runs 合并与结果（2 题 × 2 系统 general_prompt）

IsabelleGym 对 2 个新问题（imo_2019_p1、numbertheory_x5neqy2p4）的 general_prompt run
已完成并合并：`runs/isabellegym/general_prompt/`（mathd 10 + imo 10 + numbertheory 10，
30 rows / 30 artifacts / 30 logs，路径已修正指向子目录）。I/Q 侧同构（合并历史见
2026-07-30 节尾：imo rep6–9 为 jEdit 挂起+token 失效的 0 轮坏尝试，已用重跑的
rep1–4 替换并备份于 claude-work/trash-imo-rep6-9/）。

### 结果总表（general_prompt，各 10 reps，DeepSeek-v4-pro @0.3）

| 题目 | 系统 | pass@1 | 平均轮数(解出) | 平均 wall_s | 平均 tokens | sh 使用尝试 |
|---|---|---|---|---|---|---|
| imo_2019_p1 | IsabelleGym | 7/10 | 52.7 | 820 | 2.12M | 2/10 |
| imo_2019_p1 | I/Q | 9/10 | 37.4 | 854 | 9.02M | — |
| numbertheory_x5neqy2p4 | IsabelleGym | 9/10 | 31.1 | 377 | 0.42M | 2/10 |
| numbertheory_x5neqy2p4 | I/Q | 9/10 | 20.3 | 420 | 1.20M | — |

### 发现

1. **imo_2019_p1 明显更难**：gym 3 个失败（rep5/rep9 撞 100 轮上限、rep7 撞 2400s
   wall 上限，均为如实记录的真磨失败）；I/Q 9/10。gym 轮数仍高于 I/Q（52.7 vs 37.4），
   但 tokens 便宜 4 倍（2.12M vs 9.02M——I/Q 整文件重写在难题上代价急剧放大）。
2. **numbertheory_x5neqy2p4 较易**：两边 9/10；gym 轮数 31.1 vs I/Q 20.3；tokens
   gym 0.42M vs I/Q 1.20M。
3. **IsabelleGym sledgehammer 使用率低**（两题各仅 2/10 尝试调用）：escalation 规则
   存在但模型在简单题上不需要、难题上倾向手工硬磨——后续分析时值得按题难度分层看。
4. 失败模式注意（gym numbertheory rep7）：模型把目标定理**整个删掉了**（最终文件
   只剩 theory 头，113 字节），arbiter 报 "target theorem not found"——不是证明失败，
   是文件被破坏，属模型行为事故，如实计入未解出。

---

## 2026-07-31 补记：I/Q sledgehammer 使用数据 + arbiter 问题（暂停）

### I/Q sledgehammer 使用（补 2026-07-31 表中 "—"）

上节表格的 I/Q sh 列留了 "—"，是我当时只对 IsabelleGym 跑了 sh 计数、漏算了 I/Q，
并非 I/Q 没有使用。实际数据（log 中 `explore(query="sledgehammer")` 计数）：

| 题目 | 系统 | 用过 sh 的尝试 | 明细（rep → 次数） |
|---|---|---|---|
| imo_2019_p1 | I/Q | **0/10** | 全 0——但伴随大量直接 metis 违规（rep3: 21×、rep0: 20×、rep9: 13×、rep1: 8×、rep5: 7×） |
| numbertheory_x5neqy2p4 | I/Q | **3/10** | rep5: 1, rep6: 1, rep7: 2；另有 rep2 用 `smt (verit)` 5×、rep0 6× metis 违规 |

注：IsabelleGym 同题为 2/10 + 2/10。I/Q imo 零调用的原因：直接 metis 在数论/代数题
上随手可得 + explore(sledgehammer) 历史上有 30s 超时名声 + solver rule 仅 prompt 层
无强制——即"轮数优势部分来自违规捷径"，分析时应把 solver-rule 合规度作为独立维度。

### arbiter 问题（暂停中，2026-07-31）

**现象**：I/Q numbertheory rep7 代理证明正确（mod-11 枚举矛盾，46 行无 sorry），
arbiter 报 "Cannot load theory HOL-Number_Theory.Cong"。

**已查明**：
1. 容器内已补建 `HOL-Number_Theory` heap（36s 完成）——但报错的根因不是 heap：
   代理在模板导入之外**自行新增了 `HOL-Number_Theory.Cong` 导入**，而 arbiter
   （`common/arbiter.py`）只从模板导入推导 parent session（→ HOL-Computational_Algebra），
   生成的 ROOT 缺少 HOL-Number_Theory，于是 "need to include sessions … in ROOT"。
2. `server/app/services/build_verify.py:write_root` 目前**只支持单 parent**。
   多 parent ROOT 手工验证时遇到 "bad input line 1"（未查清是语法还是引号/编码问题）；
   改单 parent `HOL-Number_Theory` 后 Cong 能加载了，但在 theorem 第 8 行出现新的
   "Failed to parse prop"（未调查）。

**待定**：arbiter 应从**最终文件**的导入推导全部 parent session（多 parent ROOT），
并修正多 parent ROOT 生成；rep7 行暂未改判（保持 solved=False），待 arbiter 修复后重判。
