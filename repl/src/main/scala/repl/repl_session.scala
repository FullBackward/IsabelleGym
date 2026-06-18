package repl

import isabelle._

type EnvStateID = Long

class Repl_Session(session_manager: Session_Manager, initial_thys: List[String] = List("$ISABELLE_REPL_HOME/thys/IsabelleREPL"), field: String = "HOL") {
  //private val helper_thy = "$ISABELLE_REPL_HOME/thys/IsabelleREPL"
  private val helper_thy = initial_thys.headOption.getOrElse("$ISABELLE_REPL_HOME/thys/IsabelleREPL")

  private var session_thys: Map[String, Thy_Info] = Map.empty

  private var current_field: String = field

  private var session_data: Session_Data =
    session_manager.get_new_session(initial_thys, field)
  private var current_thy_info: Option[Thy_Info] = None

  private var initial_theories: List[String] = initial_thys

  private var current_state_id_counter: Long = 0

  def vector_current_duplicates: Option[List[Thy_Info]] = vector_env.flatMap(_.current_duplicates)
  private var vector_env: Option[Vector_Env] = None

  private class StateIDManager {
    private var count: EnvStateID = 0L

    def generate(): EnvStateID = {
      require(count < Long.MaxValue, "state id counter overflow")
      val result = count
      count += 1
      result
    }

    def valid(state_id: EnvStateID): Boolean = state_id >= 0 && state_id <= count
  }

  private val state_id_manager = new StateIDManager

  private def session: Headless.Session = session_data.session

  private def entered_some_thy: Boolean = current_thy_info.isDefined

  def current_thy_begun: Boolean = current_thy_info match {
    case Some(thy_info) if thy_info.header_processed => true
    case _                                           => false
  }

  def current_thy_name_string: String = current_thy_info.map(_.name).getOrElse("")

  private def current_thy_node_name = {
    val thy_info = current_thy_info.getOrElse(
      error("Cannot retrieve current node name if there is no current theory.")
    )
    Document_Utils.thy_node_name(thy_info.name)
  }

  private def update_session_with_edits(
      edits: List[Edit],
      node_name: Option[Document.Node.Name] = None
  ): Unit =
    session.update(
      Document.Blobs.empty,
      edits.map(edit => (node_name.getOrElse(current_thy_node_name), edit))
    )

  def enter_thy(thy_name: String): Unit = {
    if (current_thy_info.isDefined) set_current_required(false)
    val thy_info = session_thys.getOrElse(
      thy_name, {
        val thy_info = new Thy_Info(thy_name)
        session_thys = session_thys + (thy_name -> thy_info)
        thy_info
      }
    )
    current_thy_info = Some(thy_info)
    set_current_required(true)
  }

  private def set_current_required(required: Boolean): Unit = {
    val required_edit = Edit_Utils.set_required_edit(required)
    update_session_with_edits(List(required_edit))
  }

  def output_current_node_results(): Unit =
    current_thy_info.foreach(thy_info =>
      Document_Utils.output_node_results(
        session,
        current_thy_node_name,
        thy_info.last_insertion_line
      )
    )

  /** Wall-bounded per-command status report for the just-inserted chunk: JSON + success. */
  def chunk_status_report(wall_budget_ms: Long): Chunk_Report =
    current_thy_info match {
      case Some(thy_info) =>
        Document_Utils.node_status_report(
          session,
          current_thy_node_name,
          thy_info.last_insertion_line,
          wall_budget_ms
        )
      case None =>
        Chunk_Report(false, JSON.Object(
          "timed_out" -> false, "success" -> false, "used_sorry" -> false,
          "elapsed_ms" -> 0, "commands" -> List.empty[JSON.T]
        ))
    }

  def send_edit(isar_string: String, node: Option[Document.Node.Name] = None): Unit = {
    val edits = current_thy_info match {
      case None =>
        Repl_Output.add_error("Cannot make edits without entering theory.")
        List()
      case Some(thy_info) =>
        val text_edit = Edit_Utils.insert_text_edit(isar_string, thy_info)
        val edit_ok = thy_info.update_given_text_edit(text_edit)
        if (!edit_ok) List()
        else {
          val additional_edits: List[Edit] =
            if (thy_info.need_header_processing)
              Edit_Utils
                .get_thy_header_edits(
                  thy_info,
                  session,
                  current_thy_node_name,
                  List(helper_thy)
                )
                .getOrElse(List())
            else List()
          Edit_Utils.edit_from_text_edit(text_edit) :: additional_edits
        }
    }
    if (edits.nonEmpty) update_session_with_edits(edits, node)
  }

  def send_vector_edit(isar_strings: List[String]): Unit =
    vector_current_duplicates.foreach { thy_infos =>
      thy_infos.zip(isar_strings).foreach { case (thy_info, isar_string) =>
        send_edit(isar_string, node = Some(Document_Utils.thy_node_name(thy_info.name)))
      }
    }

  def current_source: String =
    if (entered_some_thy) Document_Utils.node_source(session, current_thy_node_name).strip()
    else ""

  def rollback_last_text_edit(): Unit =
    current_thy_info match {
      case None =>
        Repl_Output.add_error("Cannot rollback without entering theory.")
      case Some(thy_info) =>
        thy_info.last_text_edit match {
          case None => Repl_Output.add_error("No text edits have been made to rollback.")
          case Some(last_edit) =>
            val remove_edit = Edit_Utils.remove_insert_edit(last_edit)
            update_session_with_edits(List(Edit_Utils.edit_from_text_edit(remove_edit)))
            thy_info.rollback_last_text_edit_if_exists()
        }
    }

  /** Silently remove the most recent text edit, if any. Used to make proof-state ML
   *  probes (open_subgoals / facts / sledgehammer / get_proof_state) TRANSIENT: the
   *  `ML_val ‹…›` probe is inserted and evaluated, its result is read, then this drops
   *  it — so probing leaves no trace in the document or the rollback chain. Without this,
   *  every command left a trailing probe edit, so user `rollback` peeled the probe first
   *  and needed two calls to undo one line. Unlike `rollback_last_text_edit`, this never
   *  writes to Repl_Output (safe to call outside `build_result`). */
  def discard_last_edit(): Unit =
    current_thy_info.foreach { thy_info =>
      thy_info.last_text_edit.foreach { last_edit =>
        update_session_with_edits(
          List(Edit_Utils.edit_from_text_edit(Edit_Utils.remove_insert_edit(last_edit)))
        )
        thy_info.rollback_last_text_edit_if_exists()
      }
    }

  def save_state(): EnvStateID = {
    val env_state_id = state_id_manager.generate()
    session_thys.foreach { case (_, thy_info) => thy_info.save_current_state(env_state_id) }
    env_state_id
  }

  def restore_state(state_id: EnvStateID): Boolean =
    if (!state_id_manager.valid(state_id)) false
    else {
      session_thys.foreach { case (_, thy_info) =>
        val thy_node_name = Document_Utils.thy_node_name(thy_info.name)
        val text_edits_required = thy_info.restore_state(state_id)
        val edits = List(Edit_Utils.edit_from_text_edits(text_edits_required))
        update_session_with_edits(edits, node_name = Some(thy_node_name))
      }
      true
    }

  def reset_with_cache(): Unit = {
    val new_session_data = session_manager.get_new_session(initial_theories, current_field)
    
    session_thys = Map.empty
    current_thy_info = None
    vector_env = None
    
    session_data = new_session_data
  }
  def stop(): Unit = session_manager.remove_session_async(session_data)
  

  def stop_with_cache(): Unit = {

    session_manager.release_session_to_cache(session_data, initial_theories)
  }
  def vectorise(size: Int): Unit = {
    require(size > 0, "Vector size must be positive")
    vector_env = Some(Vector_Env(size, current_thy_info))
  }

  def scalarise(env_to_keep: Int): Unit = {
    vector_env
      .getOrElse(error("can't scalarise when already in scalar mode"))
      .scalarise(env_to_keep)
  }

}
