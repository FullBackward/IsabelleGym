package repl

import isabelle.*
import java.util.concurrent.{ConcurrentHashMap, LinkedBlockingQueue, TimeUnit}

/**
 * Per-session ML communication channels.
 *
 * The original implementation used a single global `Synchronized[Option[…]]`
 * slot for subgoals/local-facts/global-facts.  When multiple ReplBackend
 * instances call `open_subgoals()` concurrently the messages collide:
 *
 *   - "more subgoal messages arrived than requested" – a second session's
 *     response lands in the slot before the first has consumed it.
 *   - "Timeout waiting for subgoals message" – a session's response was
 *     stolen by another session.
 *
 * Fix: every message now carries a `channel_id` tag (the hex session hash
 * that the ML side includes).  A `ConcurrentHashMap` of per-channel queues
 * replaces the single global slot, so sessions can no longer interfere
 * with each other.
 *
 * COMPATIBILITY: If the ML side does NOT send a tagged message, we fall
 * back to a global default channel ("__default__") so that unmodified ML
 * code still works for single-session usage.
 */
object Repl_ML_Communication {
  // -----------------------------------------------------------------------
  // Per-channel infrastructure
  // -----------------------------------------------------------------------

  private val SUBGOALS_TIMEOUT_SECONDS = 20
  private val LOCAL_FACTS_TIMEOUT_SECONDS = 20
  private val GLOBAL_FACTS_TIMEOUT_MINUTES = 5

  private val DEFAULT_CHANNEL = "__default__"

  // Each channel ID maps to a BlockingQueue that holds at most one message.
  private val subgoal_channels =
    new ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]]()
  private val local_fact_channels =
    new ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]]()
  private val global_fact_channels =
    new ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]]()

  private def get_or_create_queue(
    map: ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]],
    channel: String
  ): LinkedBlockingQueue[List[String]] =
    map.computeIfAbsent(channel, _ => new LinkedBlockingQueue[List[String]](1))

  /** Remove the channel's queue so stale data cannot leak across reuses. */
  def clear_channel(channel: String): Unit = {
    subgoal_channels.remove(channel)
    local_fact_channels.remove(channel)
    global_fact_channels.remove(channel)
  }

  // -----------------------------------------------------------------------
  // Scala functions called FROM Isabelle/ML  (via Scala.Fun_Strings)
  // -----------------------------------------------------------------------

  /** Extract a channel tag from the first element if it starts with "CH:" */
  private def extract_channel(msgs: List[String]): (String, List[String]) =
    msgs match {
      case head :: tail if head.startsWith("CH:") => (head.stripPrefix("CH:"), tail)
      case _ => (DEFAULT_CHANNEL, msgs)
    }

  object Open_Subgoals_Function extends Scala.Fun_Strings("add_open_subgoals") {
    val here = Scala_Project.here

    def apply(open_subgoals: List[String]): List[String] = {
      val (channel, goals) = extract_channel(open_subgoals)
      val q = get_or_create_queue(subgoal_channels, channel)
      if (!q.offer(goals))
        error(s"more subgoal messages arrived than requested (channel=$channel)")
      List()
    }
  }

  object Local_Facts_Function extends Scala.Fun_Strings("add_local_facts") {
    val here = Scala_Project.here

    def apply(received_local_facts: List[String]): List[String] = {
      val (channel, facts) = extract_channel(received_local_facts)
      val q = get_or_create_queue(local_fact_channels, channel)
      if (!q.offer(facts))
        error(s"more local facts messages arrived than requested (channel=$channel)")
      List()
    }
  }

  object Global_Facts_Function extends Scala.Fun_Strings("add_global_facts") {
    val here = Scala_Project.here

    def apply(received_global_facts: List[String]): List[String] = {
      val (channel, facts) = extract_channel(received_global_facts)
      val q = get_or_create_queue(global_fact_channels, channel)
      if (!q.offer(facts))
        error(s"more global facts messages arrived than requested (channel=$channel)")
      List()
    }
  }

  // -----------------------------------------------------------------------
  // Blocking receive helpers (called from ReplBackend on the Scala side)
  // -----------------------------------------------------------------------

  def waiting_for_subgoals_message[T](block: => T, channel: String = DEFAULT_CHANNEL): List[String] = {
    val q = get_or_create_queue(subgoal_channels, channel)
    q.clear()   // discard any stale message
    block
    val result = q.poll(SUBGOALS_TIMEOUT_SECONDS, TimeUnit.SECONDS)
    if (result == null) error(s"Timeout waiting for subgoals message (channel=$channel)")
    result
  }

  def waiting_for_local_facts_message[T](block: => T, channel: String = DEFAULT_CHANNEL): List[String] = {
    val q = get_or_create_queue(local_fact_channels, channel)
    q.clear()
    block
    val result = q.poll(LOCAL_FACTS_TIMEOUT_SECONDS, TimeUnit.SECONDS)
    if (result == null) error(s"Timeout waiting for local facts message (channel=$channel)")
    result
  }

  def waiting_for_global_facts_message[T](block: => T, channel: String = DEFAULT_CHANNEL): List[String] = {
    val q = get_or_create_queue(global_fact_channels, channel)
    q.clear()
    block
    val result = q.poll(GLOBAL_FACTS_TIMEOUT_MINUTES * 60, TimeUnit.SECONDS)
    if (result == null) error(s"Timeout waiting for global facts message (channel=$channel)")
    result
  }
}


class Scala_Functions
    extends Scala.Functions(
      Repl_ML_Communication.Open_Subgoals_Function,
      Repl_ML_Communication.Local_Facts_Function,
      Repl_ML_Communication.Global_Facts_Function
    )
