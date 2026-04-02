theory Server_Concurrency
  imports Main
begin

text ‹
  This theory gives a small abstract model of the current lease-atomic server:

  ▪ each session has exactly one worker thread (modelled by a single @{term running} slot),
  ▪ session reuse is protected by an exclusive lease @{term owner},
  ▪ only the lease holder may enqueue work, and
  ▪ release is allowed only when the session is idle.

  The main result is a commutativity theorem for actions on distinct sessions.
  That is the standard interleaving proof obligation for concurrency: if two
  actions touch different sessions, the server imposes no order between them.
›

typedecl sid
text ‹Abstract session identifiers.›

typedecl cid
text ‹Abstract client / lease-holder identifiers.›

typedecl req
text ‹Abstract requests submitted to a session.›

typedecl lst
text ‹Abstract local backend state of one session.›

record session_state =
  owner   :: "cid option"
  q       :: "req list"
  running :: "req option"
  local   :: lst

type_synonym system_state = "sid ⇒ session_state"

definition idle :: "system_state ⇒ sid ⇒ bool" where
  "idle σ s ⟷ q (σ s) = [] ∧ running (σ s) = None"

definition upd_sess :: "sid ⇒ (session_state ⇒ session_state) ⇒ system_state ⇒ system_state" where
  "upd_sess s f σ = σ(s := f (σ s))"

datatype action =
    Acquire cid sid
  | Release cid sid
  | Enqueue cid sid req
  | Start sid
  | Finish sid lst

fun acts_on :: "action ⇒ sid" where
  "acts_on (Acquire c s) = s"
| "acts_on (Release c s) = s"
| "acts_on (Enqueue c s r) = s"
| "acts_on (Start s) = s"
| "acts_on (Finish s st) = s"

fun enabled :: "action ⇒ system_state ⇒ bool" where
  "enabled (Acquire c s) σ ⟷ owner (σ s) = None"
| "enabled (Release c s) σ ⟷ owner (σ s) = Some c ∧ idle σ s"
| "enabled (Enqueue c s r) σ ⟷ owner (σ s) = Some c"
| "enabled (Start s) σ ⟷ owner (σ s) ≠ None ∧ running (σ s) = None ∧ q (σ s) ≠ []"
| "enabled (Finish s st) σ ⟷ owner (σ s) ≠ None ∧ running (σ s) ≠ None"

fun exec :: "action ⇒ system_state ⇒ system_state" where
  "exec (Acquire c s) σ = upd_sess s (λx. x⦇owner := Some c⦈) σ"
| "exec (Release c s) σ = upd_sess s (λx. x⦇owner := None⦈) σ"
| "exec (Enqueue c s r) σ = upd_sess s (λx. x⦇q := q x @ [r]⦈) σ"
| "exec (Start s) σ = upd_sess s (λx. x⦇running := Some (hd (q x)), q := tl (q x)⦈) σ"
| "exec (Finish s st) σ = upd_sess s (λx. x⦇running := None, local := st⦈) σ"

text ‹Basic safety facts corresponding to the implementation.›

lemma only_owner_can_enqueue:
  "enabled (Enqueue c s r) σ ⟷ owner (σ s) = Some c"
  by simp

lemma release_only_when_idle:
  "enabled (Release c s) σ ⟷ owner (σ s) = Some c ∧ q (σ s) = [] ∧ running (σ s) = None"
  by (simp add: idle_def)

lemma start_requires_no_running_job:
  "enabled (Start s) σ ⟹ running (σ s) = None"
  by simp

lemma start_blocks_second_start_same_session:
  assumes "enabled (Start s) σ"
  shows "¬ enabled (Start s) (exec (Start s) σ)"
  proof -
  have run_after: "running ((exec (Start s) σ) s) ≠ None"
    by (simp add: upd_sess_def)
  then show ?thesis
    by simp
qed
  

text ‹
  The previous lemma is the formal version of “one worker thread per session”:
  after a start step, the same session cannot start a second job until a finish
  step clears @{term running} again.
›

lemma exec_other_session[simp]:
  assumes "acts_on a ≠ s"
  shows "exec a σ s = σ s"
  using assms
  by (cases a; simp add: upd_sess_def)

lemma enabled_unchanged_by_other_session_update:
  assumes "acts_on a ≠ t"
  shows "enabled a (upd_sess t f σ) = enabled a σ"
  using assms
  by (cases a; simp add: upd_sess_def idle_def)

lemma enabled_preserved_by_disjoint_exec:
  assumes "acts_on a ≠ acts_on b"
  shows "enabled a (exec b σ) = enabled a σ"
  using assms
  by (cases b; simp add: enabled_unchanged_by_other_session_update)

lemma exec_commute_disjoint:
  assumes "acts_on a ≠ acts_on b"
  shows "exec a (exec b σ) = exec b (exec a σ)"
  using assms
  by (cases a; cases b; simp add: upd_sess_def fun_upd_twist)

text ‹
  Main concurrency theorem: if two actions on distinct sessions are both enabled,
  then each remains enabled after the other, and the two execution orders yield
  the same global state.
›

theorem disjoint_enabled_actions_commute:
  assumes diff: "acts_on a ≠ acts_on b"
    and ena: "enabled a σ"
    and enb: "enabled b σ"
  shows "enabled a (exec b σ)"
    and "enabled b (exec a σ)"
    and "exec a (exec b σ) = exec b (exec a σ)"
  using diff ena enb
  by (simp_all add: enabled_preserved_by_disjoint_exec exec_commute_disjoint)

text ‹A concrete corollary specialised to two leased sessions with pending work.›

theorem two_distinct_leased_sessions_can_progress_independently:
  assumes sid_diff: "s1 ≠ s2"
    and own1: "owner (σ s1) = Some c1"
    and own2: "owner (σ s2) = Some c2"
    and run1: "running (σ s1) = None"
    and run2: "running (σ s2) = None"
    and q1: "q (σ s1) ≠ []"
    and q2: "q (σ s2) ≠ []"
  shows "enabled (Start s1) σ"
    and "enabled (Start s2) σ"
    and "exec (Start s1) (exec (Start s2) σ) = exec (Start s2) (exec (Start s1) σ)"
proof -
  have e1: "enabled (Start s1) σ"
    using own1 run1 q1 by simp
  have e2: "enabled (Start s2) σ"
    using own2 run2 q2 by simp
  have comm:
    "exec (Start s1) (exec (Start s2) σ) = exec (Start s2) (exec (Start s1) σ)"
    using sid_diff
    by (metis sid_diff exec_commute_disjoint acts_on.simps(4))
  show "enabled (Start s1) σ"
    using e1 .
  show "enabled (Start s2) σ"
    using e2 .
  show "exec (Start s1) (exec (Start s2) σ) = exec (Start s2) (exec (Start s1) σ)"
    using comm .
qed

text ‹
  Interpretation for the implementation:

  ▪ @{const Acquire} models the atomic lease attachment done under the manager lock.
  ▪ @{const Enqueue} models an API call that passed X-Lease-Id checks.
  ▪ @{const Start}/@{const Finish} model the single worker thread of one
    @{text ThreadedBackend} consuming its private queue.
  ▪ @{thm [source] disjoint_enabled_actions_commute} states the server-level
    concurrency property: distinct leased sessions are independent and their
    steps commute.

  This proves a safety property of the protocol.  It does not prove a wall-clock
  speedup or that the operating system schedules the two worker threads on
  different cores.
›
end