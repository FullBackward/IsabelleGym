package repl

import isabelle._

/** Result of a wall-bounded chunk verification: the JSON report `fields` plus a `success`
 *  flag computed under the SAME rule the server uses (router.py): not timed out, at least
 *  one reported command, and every reported command `ok`. `verify_chunk` uses `success` to
 *  decide whether to keep the chunk in the node or roll it back transactionally, and may
 *  enrich `fields` (e.g. with `proof_open`) before serialising. */
case class Chunk_Report(success: Boolean, fields: JSON.Object.T) {
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

  def output_node_results(
      session: Headless.Session,
      node_name: Document.Node.Name,
      last_insertion_start_line: Int
  ): Unit = {
    def pretty_print_results(
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
   * Returns a JSON string: {"timed_out":bool,"elapsed_ms":int,
   *   "commands":[{"i":int,"line":int (chunk-relative, 1-based),"node_line":int (absolute),
   *                "kind":str,"status":str,
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

    // Status per reported command, paired with its JSON object. We keep `status` alongside
    // the JSON so `success` can be computed without re-parsing the JSON we just built.
    val cmd_pairs: List[(String, JSON.T)] =
      snapshot.node.commands.toList.zipWithIndex.flatMap { case (command, i) =>
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
          val obj: JSON.T = JSON.Object(
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
    Chunk_Report(success, fields)
  }

  def node_source(session: Headless.Session, node_name: Document.Node.Name) = stable_node_snapshot(
    session,
    node_name,
    wait_until_all_commands_processed = false
  ).node.source
}
