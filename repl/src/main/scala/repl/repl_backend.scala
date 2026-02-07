package repl

import isabelle._
import scala.jdk.CollectionConverters._

import scala.collection.mutable

class ReplBackend(show_states: Boolean, enable_cache: Boolean = false, max_cache_size: Int = 10, enable_memory_management: Boolean = true, initial_thys: List[String] = List("$ISABELLE_REPL_HOME/thys/IsabelleREPL"), session_manager: Option[Session_Manager] = None, field: String = "HOL") {
  private val session_manager_instance = session_manager.getOrElse(new Session_Manager(show_states, enable_cache, max_cache_size, enable_memory_management))
  private var repl_session = new Repl_Session(session_manager_instance, initial_thys, field)

  def current_thy_name_string: String = repl_session.current_thy_name_string

  def get_cache_status(): String = session_manager_instance.get_cache_status()
  
  def get_cache_stats(): java.util.Map[String, Int] = {
    val stats = session_manager_instance.get_cache_stats()
    stats.asJava
  }

  def build_result[A](command_logic: => A): Repl_Result = {
    Repl_Output.reset()
    command_logic
    Repl_Output.result
  }

  def enter_thy(input_thy_name: String): Repl_Result = build_result {
    Thy_Parsing.extract_thy_name(input_thy_name) match {
      case None           => Repl_Output.add_error(s"Invalid theory name: $input_thy_name")
      case Some(thy_name) => repl_session.enter_thy(thy_name)
    }
  }

  private def send_ml_command(ml_text: String): Unit = repl_session.send_edit(
    s"ML_val ${Symbol.open} $ml_text ${Symbol.close}"
  )

  def open_subgoals(): java.util.List[String] = {
    val subgoals =
      if (!repl_session.current_thy_begun) List()
      else {
        val message = Repl_ML_Communication.waiting_for_subgoals_message {
          send_ml_command("Repl.send_open_subgoals @{Isar.state}")
        }

        // repl_session.rollback_last_text_edit()
        message
      }
    subgoals.asJava
  }

  def local_facts(): java.util.List[String] = {
    val local_facts =
      if (!repl_session.current_thy_begun) List()
      else {
        val message = Repl_ML_Communication.waiting_for_local_facts_message {
          send_ml_command("Repl.send_local_facts @{Isar.state}")
        }

        // repl_session.rollback_last_text_edit()
        message
      }
    local_facts.asJava
  }

  def global_facts(limit: Int): java.util.List[String] = {
    require(limit > 0, "limit must be positive")
    val global_facts =
      if (!repl_session.current_thy_begun) List()
      else {
        val message = Repl_ML_Communication.waiting_for_global_facts_message {
          send_ml_command(s"Repl.send_global_facts @{Isar.state} ${limit}")
        }

        // repl_session.rollback_last_text_edit()
        message
      }
    global_facts.asJava
  }

  def get_proof_state(): Repl_Result = build_result {
    if (!repl_session.current_thy_begun)
      Repl_Output.add_error(
        "Cannot retrieve proof state without beginning theory."
      )
    else {
      send_ml_command("Repl.get_proof_state @{Isar.state}")
      repl_session.output_current_node_results()

      // repl_session.rollback_last_text_edit()
    }
  }

  def get_source(): Repl_Result = build_result {
    Repl_Output.add_output(repl_session.current_source)
  }

  def rollback(): Repl_Result = build_result {
    repl_session.rollback_last_text_edit()
    repl_session.output_current_node_results()
  }

  def step(isar_string: String): Repl_Result = build_result {
    repl_session.send_edit(isar_string)
    repl_session.output_current_node_results()
  }

  def vector_step(isar_strings: java.util.List[String]): Repl_Result = build_result {
    repl_session.send_vector_edit(isar_strings.asScala.toList)
    repl_session.output_current_node_results()
  }

  def reset(): Repl_Result = build_result {

    if (session_manager_instance.get_cache_status().contains("Enabled: true")) {
      // with cache
      repl_session.stop_with_cache()
      repl_session = new Repl_Session(session_manager_instance)
    } else {
      // cache disabled
      repl_session.stop()
      repl_session = new Repl_Session(session_manager_instance)
    }
  }

  def exit(): Unit =
    session_manager_instance.shutdown()

  def save_state(): EnvStateID = repl_session.save_state()

  def restore_state(state_id: EnvStateID): Boolean = repl_session.restore_state(state_id)

  def vectorise(size: Int): Unit =
    repl_session.vectorise(size)

  def scalarise(index_to_keep: Int): Unit =
    repl_session.scalarise(index_to_keep)
  
  // memory management
  def get_memory_report(): String = session_manager_instance.get_memory_report()
  
  def get_memory_status(): String = session_manager_instance.get_memory_status()
  
  def can_create_new_session(): Boolean = session_manager_instance.can_create_new_session()
  
  def perform_memory_cleanup(): Unit = session_manager_instance.perform_memory_cleanup()
  
  // validate session
  def is_session_valid(): Boolean = {
    try {
      
      repl_session.current_thy_name_string
      true
    } catch {
      case _: Throwable => false
    }
  }
  
  def recreate_session_if_needed(): Unit = {
    if (!is_session_valid()) {
      println("Invalid session found, recreating...")
      repl_session = new Repl_Session(session_manager_instance, initial_thys)
    }
  }
}
