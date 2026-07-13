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

#### IsabelleGym — All Attempt Summaries

| Prompt      | Temp | Rep | Status        | Rnds | Wall(s) | Tokens    | Error |
|-------------|------|-----|---------------|------|---------|-----------|-------|
| general     | 0.7  | 0   | ✅ SOLVED     | 17   | 401.0   | 384,477   | |
| general     | 0.7  | 1   | ❌ TYPEERR    | 3    | 98.7    | 17,330    | TypeError: catching classes that do not inherit... |
| general     | 0.7  | 2   | ✅ SOLVED     | 7    | 200.3   | 56,869    | |
| general     | 0.7  | 3   | ❌ TIMEOUT    | 21   | 1361.5  | 351,568   | problem wall cap exceeded (1200s) |
| general     | 0.7  | 4   | ❌ TYPEERR    | 1    | 47.7    | 6,122     | TypeError: catching classes that do not inherit... |
| general     | 0.7  | 0   | ✅ SOLVED     | 27   | 822.9   | 746,225   | ✅ correct proof, arbiter false-negative (cfg) |
| restrictive | 0.3  | 0   | ⚠ EMPTY      | 1    | 54.6    | 6,420     | Model returned empty response |
| restrictive | 0.3  | 1   | ⚠ IRREL      | 18   | 272.3   | 197,722   | ➤ proved lemma test, not theorem — fooled arbiter |
| restrictive | 0.3  | 2   | ⚠ EMPTY      | 1    | 49.7    | 6,420     | Model returned empty response |
| restrictive | 0.3  | 3   | ⚠ IRREL      | 6    | 132.2   | 56,530    | ➤ proved lemma test, not theorem — fooled arbiter |
| restrictive | 0.3  | 4   | ⚠ IRREL      | 32   | 1087.9  | 591,708   | ➤ proved lemma test, not theorem — fooled arbiter |
| segment     | 0.7  | 0   | ✅ SOLVED     | 7    | 199.6   | 91,143    | |
| segment     | 0.7  | 1   | ⚠ IRREL      | 1    | 63.3    | 7,849     | ➤ irrelevant theorem |
| segment     | 0.7  | 0   | ⚠ CLAIMED    | 9    | 247.7   | 142,759   | agent claimed DONE but arbiter build failed |
| segment     | 0.7  | 1   | ⚠ EMPTY      | 1    | 53.8    | 7,021     | empty response |
| segment     | 0.7  | 2   | ⚠ EMPTY      | 7    | 85.5    | 37,442    | empty response |
| segment     | 0.7  | 3   | ⚠ EMPTY      | 1    | 52.2    | 7,021     | empty response |
| segment     | 0.7  | 4   | ⚠ EMPTY      | 1    | 45.6    | 7,021     | empty response |
| segment     | 0.3  | 0   | ⚠ EMPTY      | 1    | 51.2    | 7,021     | empty response |
| segment     | 0.3  | 1   | ❌ TYPEERR    | 1    | 51.5    | 7,021     | TypeError: catching classes... |
| segment     | 0.3  | 2   | ⚠ EMPTY      | 1    | 56.6    | 7,021     | empty response |
| segment     | 0.3  | 3   | ✅ SOLVED     | 38   | 369.9   | 700,862   | |
| segment     | 0.3  | 4   | ❌ FAIL       | 50   | 291.6   | 852,476   | arbiter: ML error (ML_val leaked from sledgehammer) |
| stepwise    | 0.7  | 0   | ❌ FAIL       | 1    | 53.1    | 7,747     | ExceptionGroup — MCP stdio crash |
| stepwise    | 0.7  | 1   | ⚠ IRREL      | 1    | 54.2    | 7,747     | ➤ irrelevant theorem |
| stepwise    | 0.3  | 0   | ❌ FAIL       | 50   | 693.3   | 1,014,425 | arbiter: Unicode ∀ not normalised (no fix yet) |
| stepwise    | 0.3  | 1   | ⚠ IRREL      | 1    | 47.9    | 7,747     | ➤ irrelevant theorem |
| stepwise    | 0.3  | 0   | ❌ FAIL       | 50   | 350.9   | 900,991   | arbiter: build error |
| stepwise    | 0.3  | 1   | ✅ SOLVED     | 24   | 287.1   | 381,101   | |
| stepwise    | 0.3  | 2   | ✅ SOLVED     | 37   | 317.9   | 605,044   | |
| stepwise    | 0.3  | 3   | ⚠ EMPTY      | 1    | 59.4    | 7,012     | empty response |
| stepwise    | 0.3  | 4   | ✅ SOLVED     | 19   | 352.3   | 207,734   | |

*second batch with same prompt, different temperature/config

**Legend**: ✅ SOLVED (agent+arbiter agree), ⚠ IRREL (agent proved the WRONG theorem — tried to fool arbiter), ⚠ EMPTY (content filter), ⚠ CLAIMED (agent claimed DONE but arbiter error), ❌ TYPEERR (MCP stdio bug), ❌ TIMEOUT (wall cap), ❌ FAIL (other)

**Key observations**:
- **General prompt is the most successful**: 3/6 solved (50%), zero empty responses, zero irrelevant-theorem attempts.
- **Restrictive prompt has the highest irrelevant-theorem rate**: 3/5 proved a lemma instead of the target theorem (agent learned to "fool" the arbiter by proving a different true statement and calling it done). Only 0/5 solved.
- **Segment prompt**: 2/12 solved (17%), heavily affected by empty responses (7/12 = 58%) at temperature 0.7 (run2).
- **Stepwise prompt**: 3/10 solved (30%), best at producing correct proofs when it works, but suffers from the same empty-response problem.
- **Agent "fools" arbiter pattern**: When required to obey SOLVER RULE (restrictive, stepwise), the agent sometimes diverges into proving helper lemmas, then claims DONE without proving the target theorem. The `source()` output shows `lemma test` or `lemma trivial` instead of `theorem mathd_algebra_276`.
- **TypeError from MCP stdio**: Occurs in ≈10% of runs — the MCP stdio transport silently drops token chunks on large `verify_chunk` calls, causing `TypeError: catching classes that do not inherit from BaseException`. Fixed in `mcp_client.py` but still causes attempt failure.
- **Wall-cap exceeded**: Only 1 run (general, rep3, 1361s) — the 1200s wall cap hit because the model spent too long in repeated sledgehammer calls.
- **ML_val leakage**: Segment rep3 had a sledgehammer `ML_val` command left in source (fixed now in server/Scala side, but run predates fix).
- **temperature 0.3** (general run8, segment run3, stepwise run2) produced the best results — fewer empty responses and higher solve rates.

### Autocorrode I/Q

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

#### AutoCorrode I/Q — All Attempt Summaries

| Prompt | Temp | Rep | Status | Rounds | Wall(s) | Tokens | Error |
|--------|------|-----|--------|--------|---------|--------|-------|
| none | 0.7 | 0 | ⚠ EMPTY | 3 | 60 | 31,580 | Model returned empty response |
| none | 0.7 | 1 | ⚠ EMPTY | 3 | 58 | 31,092 | Model returned empty response |
| none | 0.7 | 0 | ⚠ EMPTY | 3 | 63 | 31,300 | Model returned empty response |
| none | 0.7 | 1 | ✅ SOLVED | 28 | 502 | 1,465,436 | - |
| general | 0.7 | 0 | ⚠ EMPTY | 3 | 66 | 31,847 | Model returned empty response |
| general | 0.7 | 1 | ❌ Failed | 13 | 464 | 467,403 | Arbiter: Inner lexical error ∀ |
| general | 0.7 | 0 | ⚠ EMPTY | 3 | 65 | 31,820 | Model returned empty response |
| general | 0.7 | 1 | ⚠ EMPTY | 3 | 71 | 32,053 | Model returned empty response |
| general | 0.7 | 2 | ⚠ EMPTY | 3 | 67 | 31,873 | Model returned empty response |
| general | 0.7 | 3 | ⚠ EMPTY | 3 | 68 | 31,659 | Model returned empty response |
| general | 0.7 | 4 | ❌ JSON | 3 | 65 | 31,809 | JSONDecodeError truncated args |
| general | 0.3 | 0 | ✅ SOLVED | 13 | 198 | 367,557 | — |
| general | 0.3 | 1 | ❌ Auth | 1 | 4 | 9,109 | Invalid authentication token |
| general | 0.3 | 2 | ✅ SOLVED | 31 | 582 | 2,016,682 | — |
| general | 0.3 | 3 | ⚠ EMPTY | 3 | 61 | 31,720 | Model returned empty response |
| general | 0.3 | 4 | ⚠ EMPTY | 3 | 56 | 31,841 | Model returned empty response |
| restrictive | 0.3 | 0 | ⚠ EMPTY | 3 | 52 | 32,478 | Model returned empty response |
| restrictive | 0.3 | 1 | ⚠ EMPTY | 3 | 53 | 31,981 | Model returned empty response |
| restrictive | 0.3 | 2 | ❌ JSON | 4 | 79 | 48,033 | JSONDecodeError truncated args |
| restrictive | 0.3 | 3 | ⚠ EMPTY | 3 | 53 | 32,219 | Model returned empty response |
| restrictive | 0.3 | 4 | ⚠ EMPTY | 3 | 51 | 31,992 | Model returned empty response |

Legend: ⚠ EMPTY = content filter / empty response, ❌ = crash, ✅ = solved

Note: The "none" prompt is a slightly restrictive prompt with
```
IMPORTANT: use Isabelle ASCII escapes (e.g. \\Rightarrow) instead of Unicode math symbols, because Isabelle/jEdit rejects some Unicode characters when saving.". Everything else are trivial.
```

Key observations:
- Temperature 0.7: ALL non-bug attempts blocked by content filter (empty response)
- Temperature 0.3: 2/5 solved (general prompt), 0/5 solved (restrictive — all empty)
- Model returns empty on ~60% of attempts at temp 0.3, ~100% at temp 0.7
- JSONDecodeError: DeepSeek truncates tool-call arguments mid-string
- Arbitrary token mutation: 1 attempt had last hex digit changed (a→f)
- Proofs are correct (JEdit verifies) — arbiter fails only due to Unicode/server config bugs (RESOLVED)

3 proof success attempts:

2 with General Prompt and 0.3 temperature
1. Used 13 rounds which log looks normal and clear, no clear error.
2. Used 31 rounds, agent ignored UTF symbol rule, and retried several times with invalid symbols.

1 with slightly restrictive prompt and 0.7 temperature
1. Used 28 rounds, agent ignored relatively loose UTF rule, and retried several times with invalid symbols.


