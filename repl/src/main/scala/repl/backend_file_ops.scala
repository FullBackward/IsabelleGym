package repl

import isabelle._

/** LSP-like file-sync surface of [[ReplBackend]]: load a chunk (typically a
 *  whole document) and report per-command status WITHOUT rolling back ordinary
 *  failures — the broken state stays in the node so the caller can inspect and
 *  fix it (the dual of `verify_chunk`'s transactional semantics). Consumed via
 *  PUT /api/v1/sessions/{id}/document with report=true — the file-sync
 *  primitive of the planned LSP-like read-only MCP.
 *
 *  Also hosts the LSP-like READ-ONLY line queries (`command_at_line`,
 *  `goals_at_line`): snapshot-based, no edits and no ML probes, so they work at
 *  any document position — including past a trailing theory `end`, where the
 *  channel probes of Backend_Probes would hang.
 *
 *  Semantics: on BUDGET TIMEOUT the chunk IS discarded (to cancel a runaway
 *  command); on ordinary failure it is kept. The `probe_state` flag controls
 *  whether proof_open/pending_qed are probed — callers pass false when the
 *  text ends with theory `end`, where a trailing probe would never execute. */
trait Backend_File_Ops { this: ReplBackend =>

  /**
   * Load a chunk and report per-command status, WITHOUT rollback on ordinary
   * failure (LSP-style: broken state stays in the node so the caller can
   * inspect and fix it — the dual of verify_chunk's transactional semantics).
   * Returns the same JSON per-command report shape as verify_chunk.
   * Used by the LSP-like file-sync mode via load_document
   * (PUT /api/v1/sessions/{id}/document with report=true).
   *
   * Two caveats:
   *  - On BUDGET TIMEOUT the chunk IS discarded (discard_last_edit) to cancel
   *    the runaway command — leaving a looping `metis` churning is never what a
   *    caller wants. The report's `timed_out`/`running` line names the loop.
   *  - `proof_open`/`pending_qed` are only probed when the chunk succeeded AND
   *    `probe_state` is true. Callers pass probe_state=false when the text ends
   *    with theory `end`: a probe appended past `end` never executes, so the
   *    ML channel would hang until timeout. On failure both are false (the
   *    caller inspects `commands[]` instead).
   *  - When the kept chunk leaves the document ending with theory `end`, both
   *    are hardcoded false WITHOUT probing: a successful `end` means no proof
   *    is open (and probing past `end` would hang). The `probe_state` flag is
   *    kept as belt-and-braces for reports generated differently.
   */
  def step_chunk_report(isar_string: String, wall_budget_ms: Long, probe_state: Boolean): String = {
    Repl_Output.reset()
    // Guard on ENTERED (not begun): for a full-file load the theory header is part of
    // the chunk itself, so `current_thy_begun` is still false before this first edit —
    // send_edit processes the header as part of the insertion (need_header_processing).
    if (!repl_session.entered_some_thy)
      Json_Reports.failed_chunk_report("no theory entered")
    else {
      repl_session.send_edit(isar_string)
      val report = repl_session.chunk_status_report(wall_budget_ms)
      val proof_open =
        if (report.timed_out) { repl_session.discard_last_edit(); false }
        else if (!report.success) false
        else if (repl_session.current_thy_ended) false  // trailing `end`: theory closed
        else probe_state && in_proof()
      val pending_qed = proof_open && open_subgoals().isEmpty
      JSON.Format(report.fields + ("proof_open" -> proof_open) + ("pending_qed" -> pending_qed))
    }
  }

  /** Read-only jEdit-style query: the command containing `line` (1-based) of the
   *  current node, as JSON {found, kind, source, range}. Snapshot-based — no edits,
   *  no ML probes — so it is safe at ANY document position, including past a trailing
   *  theory `end` (where the channel probes of Backend_Probes hang). Consumed via
   *  GET /api/v1/sessions/{id}/command_at_line by the LSP-like file-sync mode. */
  def command_at_line(line: Int): String = {
    Repl_Output.reset()
    repl_session.command_at_line(line)
  }

  /** Read-only jEdit-style query: rendered goal state before/after the command
   *  containing `line` (1-based), as JSON
   *  {found, command: {kind, source, range}, goals_before: [str], goals_after: [str]}.
   *  REQUIRES the `show_states` PIDE option (state messages only exist with it on —
   *  ISABELLE_SHOW_STATES, default true since Stage 2.2). Consumed via
   *  GET /api/v1/sessions/{id}/goals by the LSP-like file-sync mode. */
  def goals_at_line(line: Int): String = {
    Repl_Output.reset()
    repl_session.goals_at_line(line)
  }
}
