package repl

import isabelle._

/** PIDE document snapshot utilities beneath [[Repl_Session]]: stable-snapshot
 *  barriers, per-command output extraction, source retrieval, and the
 *  wall-bounded per-command status report (`node_status_report` /
 *  [[Chunk_Report]]) that backs both `verify_chunk` (chunk-centric MCP) and
 *  `step_chunk_report` (LSP-like file-sync via PUT .../document). Shared
 *  infrastructure consumed by BOTH workflows. */

/** Result of a wall-bounded chunk verification: the JSON report `fields` plus a `success`
 *  flag computed under the SAME rule the server uses (router.py): not timed out, at least
 *  one reported command, and every reported command `ok`. `verify_chunk` uses `success` to
 *  decide whether to keep the chunk in the node or roll it back transactionally, and may
 *  enrich `fields` (e.g. with `proof_open`) before serialising. `timed_out` is exposed
 *  separately (not just inside `fields`) so callers like `step_chunk_report` can cancel
 *  runaway commands without re-parsing the JSON. */
case class Chunk_Report(success: Boolean, fields: JSON.Object.T, timed_out: Boolean = false) {
  def json: String = JSON.Format(fields)
}

object Document_Utils {
  def thy_node_name(thy_name: String): Document.Node.Name = {
    val qualifier = Sessions.DRAFT
    Document.Node.Name(s"$qualifier.$thy_name", theory = thy_name)
  }

  // private def stable_node_snapshot(
  //     session: Headless.Session,
  //     node_name: Document.Node.Name,
  //     wait_until_all_commands_processed: Boolean = true
  // ): Document.Snapshot = {
  //   val node_snapshot =
  //     session.await_stable_snapshot().switch(node_name)
  //   val version = node_snapshot.version
  //   var commands_to_process = node_snapshot.node.commands

  //   def all_commands_processed = {
  //     val state = session.get_state()
  //     commands_to_process = commands_to_process.filterNot { command =>
  //       val states = state.command_states(version, command)
  //       // Try with both `maybe_consolidated` and `consolidated` for showing sledgehammer outputs
  //       // states.exists(st => st.maybe_consolidated || st.consolidated)
  //       states.exists(st => st.consolidated)
  //     }
  //     commands_to_process.isEmpty
  //   }

  //   while (wait_until_all_commands_processed && !all_commands_processed)
  //     session.output_delay.sleep()

  //   node_snapshot
  // }

  private def stable_node_snapshot(
      session: Headless.Session,
      node_name: Document.Node.Name,
      wait_until_all_commands_processed: Boolean = true
  ): Document.Snapshot = {
    var node_snapshot =
      session.await_stable_snapshot().switch(node_name)

    def all_commands_processed: Boolean = {
      node_snapshot = session.await_stable_snapshot().switch(node_name)
      val version = node_snapshot.version
      val state   = session.get_state()
      node_snapshot.node.commands.forall { command =>
        scala.util.Try(state.command_states(version, command))
          .fold(
            _   => true,  // version no longer tracked → PIDE advanced → done
            sts => sts.exists(st => st.maybe_consolidated || st.consolidated)
          )
      }
    }

    while (wait_until_all_commands_processed && !all_commands_processed)
      session.output_delay.sleep()

    node_snapshot
  }

  /** Wait until EVERY command in the node is consolidated (maybe_consolidated or
   *  consolidated). Used as a sequencing barrier after ML probes: the probe's
   *  channel reply arrives DURING the probe command's evaluation, so discarding
   *  the probe immediately can cancel its remainder and race the NEXT probe's
   *  evaluation (stale state -> empty results, or a canceled probe -> blind
   *  channel timeout). Awaiting consolidation before the discard serialises
   *  consecutive probes. */
  def await_all_processed(session: Headless.Session, node_name: Document.Node.Name): Unit = {
    stable_node_snapshot(session, node_name)
    ()
  }

  private def pretty_print_results(
      command: Command,
      results: Command.Results,
      hide_state_messages: Boolean
  ): Unit = {

    def output_pretty_if_non_empty(body: XML.Body, output_f: String => Unit): Unit = {
      val pretty_string = Pretty.string_of(body)
      if (pretty_string.nonEmpty) output_f(pretty_string)
    }
    results.iterator
      .foreach(_._2 match {
        case XML.Elem(Markup(markup_type, _), body) =>
          markup_type match {
            case Markup.WRITELN_MESSAGE =>
              if (!hide_state_messages)
                output_pretty_if_non_empty(body, Repl_Output.add_output)
            case Markup.STATE_MESSAGE =>
              if (!(command.is_ignored || hide_state_messages))
                output_pretty_if_non_empty(body, Repl_Output.add_output)
            case Markup.ERROR_MESSAGE | Markup.WARNING_MESSAGE =>
              output_pretty_if_non_empty(body, Repl_Output.add_error)
            case _ => output_pretty_if_non_empty(body, Repl_Output.add_output)
          }
      })
  }

  def output_node_results(
      session: Headless.Session,
      node_name: Document.Node.Name,
      last_insertion_start_line: Int
  ): Unit = {
    val node_snapshot = stable_node_snapshot(session, node_name)
    val node_commands = node_snapshot.node.commands

    node_commands.foreach { command =>
      val command_results = node_snapshot.command_results(command)
      val start_line = node_snapshot.node.command_start_line(command).getOrElse(1)
      val hide_state_messages = start_line < last_insertion_start_line
      pretty_print_results(
        command,
        command_results,
        hide_state_messages = hide_state_messages
      )
    }
  }

  /** Absolute start offset of the node's LAST `end` command (theory or block end),
   *  if any. Used to place probes before a trailing theory `end`. */
  def last_end_offset(session: Headless.Session, node_name: Document.Node.Name): Option[Text.Offset] = {
    val snapshot = stable_node_snapshot(session, node_name)
    snapshot.node.command_iterator().toList
      .collect { case (command, offset) if command.span.name == "end" => offset }
      .lastOption
  }

  /** True when the node's LAST non-ignored command is `end` (theory or block end —
   *  either way no proof can be open immediately after it, and commands appended past
   *  a trailing theory `end` never execute). Drives the post-`end` probe handling in
   *  Backend_Probes / Backend_File_Ops. */
  def node_ends_with_end(session: Headless.Session, node_name: Document.Node.Name): Boolean = {
    val snapshot = stable_node_snapshot(session, node_name)
    snapshot.node.commands.reverse.iterator
      .find(command => !command.is_ignored)
      .exists(_.span.name == "end")
  }

  /** Output the results of the single command STARTING AT `offset` — for mid-document
   *  probes (inserted before a trailing `end`), where output_node_results'
   *  last-insertion-line filter would hide them. */
  def output_command_at_offset(
      session: Headless.Session,
      node_name: Document.Node.Name,
      offset: Text.Offset
  ): Unit = {
    val snapshot = stable_node_snapshot(session, node_name)
    snapshot.node.command_iterator().toList
      .find { case (_, command_offset) => command_offset == offset }
      .foreach { case (command, _) =>
        pretty_print_results(command, snapshot.command_results(command), hide_state_messages = false)
      }
  }

  /**
   * Status-aware, WALL-BOUNDED per-command report for a freshly inserted chunk.
   *
   * Unlike `output_node_results` (which collapses everything into a flat buffer and waits
   * on an UNBOUNDED whole-node barrier), this:
   *   - polls until every command is consolidated/failed OR `wall_budget_ms` elapses
   *     (exactly ONE timeout; no per-command timeouts);
   *   - classifies each command (at/after `since_line`, i.e. the inserted chunk) as
   *     ok | failed | running | unprocessed via `Document_Status.Command_Status`;
   *   - on budget expiry returns the PARTIAL status (the still-`running` line is the loop),
   *     never throws.
   * Parallel proof checking (parallel_proofs) stays on underneath; the report is just
   * enumerated in source order.
   *
   * Each command also carries a `range` — its 1-based line+column start/end (columns are
   * UTF-16 units, i.e. LSP columns). Message-level position offsets do NOT exist for our
   * Sessions.DRAFT nodes (PIDE span tokens are position-less; jEdit underlines at command
   * granularity for the same reason), so diagnostics carry their OWNING COMMAND's range —
   * that is the design, not a workaround.
   *
   * Returns a JSON string: {"timed_out":bool,"elapsed_ms":int,
   *   "commands":[{"i":int,"line":int (chunk-relative, 1-based),"node_line":int (absolute),
   *                "kind":str,"status":str,
   *                "range":{"start":{"line":int,"col":int},"end":{"line":int,"col":int}},
   *                "messages":[{"sev":"error|warning","text":str}]}]}
   */
  def node_status_report(
      session: Headless.Session,
      node_name: Document.Node.Name,
      since_line: Int,
      wall_budget_ms: Long
  ): Chunk_Report = {
    val start_ms = System.currentTimeMillis()
    val deadline = start_ms + wall_budget_ms

    def snap(): Document.Snapshot = session.await_stable_snapshot().switch(node_name)

    def all_settled(snapshot: Document.Snapshot): Boolean = {
      val version = snapshot.version
      val state = session.get_state()
      snapshot.node.commands.forall { command =>
        scala.util.Try(state.command_status(version, command)).fold(
          _ => true, // version no longer tracked -> PIDE advanced -> done
          st => st.maybe_consolidated || st.is_failed
        )
      }
    }

    var snapshot = snap()
    var timed_out = false
    while (!timed_out && !all_settled(snapshot)) {
      if (System.currentTimeMillis() >= deadline) timed_out = true
      else { session.output_delay.sleep(); snapshot = snap() }
    }

    val version = snapshot.version
    val state = session.get_state()

    def message_objs(command: Command): List[JSON.T] = {
      val results = snapshot.command_results(command)
      results.iterator.toList.flatMap {
        case (_, XML.Elem(Markup(markup_type, _), body)) =>
          val sev = markup_type match {
            case Markup.ERROR_MESSAGE   => Some("error")
            case Markup.WARNING_MESSAGE => Some("warning")
            case _                      => None
          }
          sev.flatMap { s =>
            val text = Pretty.string_of(body)
            if (text.nonEmpty) Some(JSON.Object("sev" -> s, "text" -> text): JSON.T)
            else None
          }
        case _ => None
      }
    }

    // One Line.Document for the whole node: converts each command's absolute char-offset
    // extent into a 1-based line/column range (columns = UTF-16 units = LSP columns).
    val line_doc = Line.Document(snapshot.node.source)

    // Per-command range JSON; None if offset/range conversion fails for a command
    // (shouldn't happen — the range is simply omitted rather than failing the report).
    def range_obj(start_offset: Text.Offset, length: Int): Option[JSON.T] =
      try {
        val r = line_doc.range(Text.Range(start_offset, start_offset + length))
        Some(JSON.Object(
          "start" -> JSON.Object("line" -> r.start.line1, "col" -> r.start.column1),
          "end" -> JSON.Object("line" -> r.stop.line1, "col" -> r.stop.column1)))
      } catch { case _: Exception => None }

    // Status per reported command, paired with its JSON object. We keep `status` alongside
    // the JSON so `success` can be computed without re-parsing the JSON we just built.
    // command_iterator preserves source order and yields the same command objects as
    // node.commands, plus each command's node-absolute char-offset start.
    val cmd_pairs: List[(String, JSON.T)] =
      snapshot.node.command_iterator().toList.zipWithIndex.flatMap { case ((command, start_offset), i) =>
        val start_line = snapshot.node.command_start_line(command).getOrElse(1)
        if (start_line < since_line || command.is_ignored) None
        else {
          val st = scala.util.Try(state.command_status(version, command)).toOption
          val status =
            st match {
              case Some(s) if s.is_failed         => "failed"
              case Some(s) if s.maybe_consolidated => "ok"
              case Some(s) if s.is_running        => "running"
              case Some(_)                        => "unprocessed"
              case None                           => "ok" // PIDE advanced past this version
            }
          val base_fields: JSON.Object.T = JSON.Object(
            // chunk-relative line (1-based within the submitted chunk), so stuck_line /
            // failed line maps to the text the caller sent — not the absolute line in the
            // accumulated node. `node_line` keeps the absolute line for debugging.
            "i" -> i,
            "line" -> (start_line - since_line + 1),
            "node_line" -> start_line,
            "kind" -> command.span.name,
            "status" -> status,
            "messages" -> message_objs(command)
          )
          val obj: JSON.T =
            range_obj(start_offset, command.length) match {
              case Some(range_json) => base_fields + ("range" -> range_json)
              case None             => base_fields
            }
          Some((status, obj))
        }
      }

    // Match the server's success rule (router.py): not timed out, at least one reported
    // command, and every reported command `ok`.
    val success = !timed_out && cmd_pairs.nonEmpty && cmd_pairs.forall(_._1 == "ok")

    // Authoritative `sorry`/`oops` detection: scan the chunk's PARSED commands (not the raw
    // text) so a `sorry` in a comment or string literal is NOT a false positive, while a real
    // `sorry`/`oops` command IS caught regardless of spacing. A theorem closed via sorry/oops
    // is not actually proved, so callers must treat used_sorry=true as "not proved".
    val used_sorry = snapshot.node.commands.exists { command =>
      val start_line = snapshot.node.command_start_line(command).getOrElse(1)
      start_line >= since_line && !command.is_ignored &&
        (command.span.name == "sorry" || command.span.name == "oops")
    }

    val fields: JSON.Object.T = JSON.Object(
      "timed_out" -> timed_out,
      "success" -> success,
      "used_sorry" -> used_sorry,
      "elapsed_ms" -> (System.currentTimeMillis() - start_ms).toInt,
      "commands" -> cmd_pairs.map(_._2)
    )
    Chunk_Report(success, fields, timed_out)
  }

  // -----------------------------------------------------------------------
  // Read-only line-based queries (command_at_line / goals_at_line), consumed
  // via GET .../command_at_line and GET .../goals by the LSP-like file-sync
  // mode. Fully read-only: immutable snapshot, no edits, no ML probes — so
  // they work at ANY document position, including past a trailing theory `end`
  // (where the channel probes of Backend_Probes would hang).
  // -----------------------------------------------------------------------

  /** Shared core: a fresh stable snapshot of the node plus the command containing
   *  `line` (1-based, matching the chunk-report convention), resolved with jEdit
   *  cursor semantics (Snapshot.current_command: the command at the line's start
   *  offset, else the nearest non-ignored command before it — comment and blank
   *  lines map to the preceding command). None when `line` is out of range. */
  private def command_containing_line(
      session: Headless.Session,
      node_name: Document.Node.Name,
      line: Int
  ): Option[(Document.Snapshot, Line.Document, Command, Text.Offset)] =
    if (line < 1) None
    else {
      val snapshot = stable_node_snapshot(session, node_name)
      val line_doc = Line.Document(snapshot.node.source)
      for {
        offset <- line_doc.offset(Line.Position(line = line - 1, column = 0))
        command <- snapshot.current_command(node_name, offset)
        start_offset <- snapshot.node.command_start(command)
      } yield (snapshot, line_doc, command, start_offset)
    }

  /** JSON {kind, source, range} for one command; `range` omitted if the offset/range
   *  conversion fails (shouldn't — the offset comes from the node itself). */
  private def command_info_json(
      line_doc: Line.Document,
      command: Command,
      start_offset: Text.Offset
  ): JSON.Object.T = {
    val base: JSON.Object.T = JSON.Object(
      "kind" -> command.span.name,
      "source" -> command.source)
    try {
      val r = line_doc.range(Text.Range(start_offset, start_offset + command.length))
      base + ("range" -> JSON.Object(
        "start" -> JSON.Object("line" -> r.start.line1, "col" -> r.start.column1),
        "end" -> JSON.Object("line" -> r.stop.line1, "col" -> r.stop.column1)))
    } catch { case _: Exception => base }
  }

  /** Last STATE_MESSAGE (rendered proof state) a command produced, if any. State
   *  messages only exist when the `show_states` PIDE option is on
   *  (ISABELLE_SHOW_STATES). */
  private def last_state_message(snapshot: Document.Snapshot, command: Command): Option[String] =
    snapshot.command_results(command).iterator.toList.collect {
      case (_, XML.Elem(Markup(markup_type, _), body)) if markup_type == Markup.STATE_MESSAGE =>
        Pretty.string_of(body)
    }.lastOption

  /** The command containing `line` (1-based) of the node, as JSON
   *  {found, kind, source, range}; {found:false, error} when out of range. */
  def command_at_line_json(
      session: Headless.Session,
      node_name: Document.Node.Name,
      line: Int
  ): String =
    command_containing_line(session, node_name, line) match {
      case Some((_, line_doc, command, start_offset)) =>
        JSON.Format(command_info_json(line_doc, command, start_offset) + ("found" -> true))
      case None =>
        Json_Reports.line_query_not_found(s"no command at line $line")
    }

  /** Rendered goal state before/after the command containing `line` (1-based), as JSON
   *  {found, command: {kind, source, range}, goals_before: [str], goals_after: [str]}.
   *
   *  `goals_after` is the command's LAST state message as ONE raw text element (no
   *  subgoal splitting — the numbered-subgoal format is not parsed). `goals_before`
   *  is the last state message of the immediately preceding non-ignored command, or
   *  [] when that command produced none (e.g. outside a proof). Both are empty when
   *  `show_states` is off. */
  def goals_at_line_json(
      session: Headless.Session,
      node_name: Document.Node.Name,
      line: Int
  ): String =
    command_containing_line(session, node_name, line) match {
      case Some((snapshot, line_doc, command, start_offset)) =>
        val goals_after = last_state_message(snapshot, command).toList
        val commands = snapshot.node.command_iterator().toList
        val index = commands.indexWhere { case (c, _) => c == command }
        val previous_command =
          if (index <= 0) None
          else commands.take(index).reverse.collectFirst { case (c, _) if !c.is_ignored => c }
        val goals_before = previous_command.flatMap(last_state_message(snapshot, _)).toList
        JSON.Format(JSON.Object(
          "found" -> true,
          "command" -> command_info_json(line_doc, command, start_offset),
          "goals_before" -> goals_before,
          "goals_after" -> goals_after))
      case None =>
        Json_Reports.line_query_not_found(s"no command at line $line")
    }

  def node_source(session: Headless.Session, node_name: Document.Node.Name) = stable_node_snapshot(
    session,
    node_name,
    wait_until_all_commands_processed = false
  ).node.source
}
