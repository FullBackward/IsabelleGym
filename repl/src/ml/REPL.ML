signature REPL =
sig

  val send_open_subgoals: Toplevel.state -> unit
  val send_local_facts: Toplevel.state -> unit
  val send_global_facts: Toplevel.state -> int -> unit

  (* Tagged variants: prepend "CH:<channel_id>" so the Scala side can
     route messages to per-backend queues, enabling safe concurrency. *)
  val send_open_subgoals_tagged: string -> Toplevel.state -> unit
  val send_local_facts_tagged: string -> Toplevel.state -> unit
  val send_global_facts_tagged: string -> Toplevel.state -> int -> unit
  val send_sledgehammer_tagged: string -> int -> Toplevel.state -> unit

end;

structure Repl : REPL =
struct

fun open_subgoals state =
  if (Toplevel.is_proof state) then (
    let 
      val proof = Toplevel.proof_of state;
      val ({context, goal, ...}) = Proof.raw_goal proof;
      val num_goals = goal |> Thm.prop_of |> Logic.strip_horn |> #1 |> length;
      val pretty_goals = if num_goals > 0 then Goal_Display.pretty_goals context goal else []
      fun clean_string s = 
        let val parsed = YXML.parse_body s
        in XML.content_of parsed end
    in
      List.map (clean_string o Pretty.string_of) pretty_goals
    end
  )
  else
    []

fun send_open_subgoals state =
  open_subgoals state |> \<^scala>\<open>add_open_subgoals\<close> |> ignore

fun send_open_subgoals_tagged channel_id state =
  let val goals = open_subgoals state
  in ("CH:" ^ channel_id) :: goals |> \<^scala>\<open>add_open_subgoals\<close> |> ignore end

fun pretty_local_facts verbose ctxt =
  let
    val facts = Proof_Context.facts_of ctxt;
    val props = map #1 (Facts.props facts);
    val local_facts =
      (if null props then [] else [("<unnamed>", props)]) @
      Facts.dest_static verbose [Global_Theory.facts_of (Proof_Context.theory_of ctxt)] facts;
    fun clean_string s = 
      let val parsed = YXML.parse_body s
      in XML.content_of parsed end
  in
    if null local_facts then []
    else
      [List.hd (map #1 (sort_by (#1 o #2) (map (`(Proof_Context.pretty_fact ctxt)) local_facts)))]
      |> List.map (clean_string o Pretty.string_of)
  end;

fun local_facts state =
  if (Toplevel.is_proof state) then 
    state
    |> Toplevel.proof_of
    |> Proof.context_of
    |> pretty_local_facts true
  else
    []

fun send_local_facts state =
  state |> local_facts |> \<^scala>\<open>add_local_facts\<close> |> ignore

fun send_local_facts_tagged channel_id state =
  let val facts = local_facts state
  in ("CH:" ^ channel_id) :: facts |> \<^scala>\<open>add_local_facts\<close> |> ignore end

fun global_facts state limit =
  if (Toplevel.is_proof state) then (
    let
      val ctxt = state |> Toplevel.proof_of |> Proof.context_of;
      val thy = Proof_Context.theory_of ctxt;
      val global_facts = Global_Theory.facts_of thy;
      
      fun collect_facts (name, thms) (count, acc) =
        if count >= limit then (count, acc)
        else (count + 1, (name, thms) :: acc);
      
      val (_, limited_facts) = 
        Facts.fold_static collect_facts global_facts (0, [])
        |> (fn (count, facts) => (count, sort_by #1 facts));
      
      fun clean_string s = 
        let val parsed = YXML.parse_body s
        in XML.content_of parsed end
    in
      limited_facts
      |> map (Proof_Context.pretty_fact ctxt)
      |> List.map (clean_string o Pretty.string_of)
    end
  )
  else
    []

fun send_global_facts state limit =
  let
    val global_facts = global_facts state limit;
  in
    \<^scala>\<open>add_global_facts\<close> global_facts |> ignore
  end

fun send_global_facts_tagged channel_id state limit =
  let
    val facts = global_facts state limit;
  in
    ("CH:" ^ channel_id) :: facts |> \<^scala>\<open>add_global_facts\<close> |> ignore
  end

(* Run sledgehammer on the first subgoal and collect every line it would
   normally print (e.g. "metis found a proof...", "Try this: by (metis ...)",
   "No proof found").  Runs SYNCHRONOUSLY in the caller's thread, so this must
   only ever be invoked from a forked thread (see send_sledgehammer_tagged)
   to avoid blocking the document node. *)
fun sledgehammer_results timeout_s state =
  if Toplevel.is_proof state then
    let
      val proof_state = Toplevel.proof_of state;
      val ctxt        = Proof.context_of proof_state;
      val thy         = Proof_Context.theory_of ctxt;
      (* default_params takes a theory; "timeout" is the Isabelle-level budget
         in seconds.  ("blocking", _) was removed in recent Isabelle and is no
         longer a valid parameter. *)
      val params      = Sledgehammer_Commands.default_params thy
                          [("timeout", string_of_int timeout_s)];
      (* Collect every message via the output callback instead of letting it
         go to the PIDE buffer.  The actual proof methods ("Try this: ...")
         arrive here, not in the return value (which is "" on success).
         Strip the YXML markup (sendback/break/block control codes) so the
         caller gets plain text, consistent with the other channels. *)
      val collected   = Unsynchronized.ref ([] : string list);
      fun clean_string s = XML.content_of (YXML.parse_body s);
      fun on_output s = collected := (clean_string s :: !collected);
      val (_, (_, message)) =
        Sledgehammer.run_sledgehammer
          params
          Sledgehammer_Prover.Normal
          (SOME on_output)                   (* route output here, not to PIDE buffer *)
          1                                  (* first subgoal *)
          Sledgehammer_Fact.no_fact_override (* use all available facts *)
          proof_state;
      val extra = if message = "" then [] else [clean_string message]
    in
      List.rev (!collected) @ extra
    end
  else
    []

(* Standalone, non-blocking sledgehammer channel.

   The ML command that calls this returns immediately after forking, so the
   document node is freed and other ML channels (open_subgoals, local_facts,
   ...) keep working while sledgehammer runs.  When the forked thread finishes
   it pushes the results back through the tagged Scala callback, where the
   Scala side is blocking on the matching per-channel queue. *)
fun send_sledgehammer_tagged channel_id timeout_s state =
  let
    fun run () =
      let val results = sledgehammer_results timeout_s state
      in ("CH:" ^ channel_id) :: results |> \<^scala>\<open>add_sledgehammer_results\<close> |> ignore end
  in
    ignore (Isabelle_Thread.fork (Isabelle_Thread.params "repl_sledgehammer") run)
  end

end

val send_open_subgoals = Repl.send_open_subgoals;
val send_local_facts = Repl.send_local_facts;
val send_global_facts = Repl.send_global_facts;
val send_open_subgoals_tagged = Repl.send_open_subgoals_tagged;
val send_local_facts_tagged = Repl.send_local_facts_tagged;
val send_global_facts_tagged = Repl.send_global_facts_tagged;
val send_sledgehammer_tagged = Repl.send_sledgehammer_tagged;