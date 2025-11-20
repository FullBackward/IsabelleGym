package repl

import isabelle._

import scala.collection.mutable

case class Session_Data(id: UUID.T, session: Headless.Session)

class Session_Manager(show_states: Boolean, enable_cache: Boolean = false, max_cache_size: Int = 10, enable_memory_management: Boolean = true) {
  private val (server_info, server) = Server_Utils.start_server()
  private val running_sessions: Synchronized[mutable.Set[UUID.T]] =
    Synchronized(mutable.Set.empty)
  private val pending_removals: Synchronized[mutable.Set[Future[Unit]]] =
    Synchronized(mutable.Set.empty)

  private val session_cache: Synchronized[mutable.Map[List[String], mutable.Queue[Session_Data]]] =
    Synchronized(mutable.Map.empty)
  
  private val lru_order: Synchronized[mutable.LinkedHashMap[List[String], Long]] = 
    Synchronized(mutable.LinkedHashMap.empty)
  
  private val cache_stats: Synchronized[mutable.Map[String, Int]] =
    Synchronized(mutable.Map("hits" -> 0, "misses" -> 0, "creates" -> 0, "evictions" -> 0))
  
  private val session_refs: Synchronized[mutable.Map[UUID.T, Int]] =
    Synchronized(mutable.Map.empty)
  
  private val evicted_sessions: Synchronized[mutable.Set[UUID.T]] =
    Synchronized(mutable.Set.empty)

  def get_cache_stats(): Map[String, Int] = cache_stats.value.toMap
  
  private def format_bytes_to_mb(bytes: Long): String = {
    val mb = bytes / (1024.0 * 1024.0)
    f"$mb%.1f"
  }
  
  private def get_system_memory_mb(): Long = {
    scala.util.Try {
      val source = scala.io.Source.fromFile("/proc/meminfo")
      try {
        val lines = source.getLines()
        var result: Option[Long] = None
        for (line <- lines if result.isEmpty) {
          if (line.startsWith("MemTotal:")) {
            val parts = line.split("\\s+")
            if (parts.length >= 2) {
              val kb = parts(1).toLong
              result = Some(kb / 1024)  
            }
          }
        }
        result.getOrElse {
          val runtime = Runtime.getRuntime
          val totalMemory = runtime.totalMemory()
          val maxMemory = runtime.maxMemory()
          val estimatedSystemMemory = Math.max(totalMemory, maxMemory) / 0.8
          (estimatedSystemMemory / (1024 * 1024)).toLong
        }
      } finally {
        source.close()
      }
    }.getOrElse(4096L)
  }
  
  private def get_validated_memory_metrics(): (Long, Long, Long, Long, Double, Boolean) = {
    val runtime = Runtime.getRuntime
    val jvm_max = runtime.maxMemory()
    val total = runtime.totalMemory()
    val free = runtime.freeMemory()
    
    val system_memory_mb = get_system_memory_mb()
    val system_memory_bytes = system_memory_mb * 1024 * 1024
    
    val jvm_max_mb = jvm_max / (1024 * 1024)
    val is_jvm_report_suspicious = jvm_max_mb > system_memory_mb * 10 
    
    val effective_max = if (is_jvm_report_suspicious) {
      (system_memory_bytes * 0.8).toLong
    } else {
      jvm_max
    }
    
    val used_in_allocated = total - free
    val unallocated = effective_max - total
    val total_available = free + unallocated
    val memory_pressure = if (effective_max > 0) (used_in_allocated.toDouble / effective_max) * 100 else 0.0
    
    if (is_jvm_report_suspicious) {
      println(s"WARNING: JVM reports ${jvm_max_mb}MB max memory, but system only has ${system_memory_mb}MB")
      println(s"Using corrected limit: ${effective_max / (1024 * 1024)}MB (80% of system memory)")
    }
    
    (used_in_allocated, total_available, effective_max, total, memory_pressure, is_jvm_report_suspicious)
  }

  def get_cache_status(): String = {
    val stats = cache_stats.value
    val cache_size = session_cache.value.values.map(_.size).sum
    val memoryInfo = if (enable_memory_management) {
      val (_, _, _, _, pressure, _) = get_validated_memory_metrics()
      s", Memory: ${f"$pressure%.1f"}%"
    } else ""
    s"Cache: ${cache_size} sessions, Hits: ${stats("hits")}, Misses: ${stats("misses")}, Creates: ${stats("creates")}, Evictions: ${stats("evictions")}, Enabled: ${enable_cache}, MaxSize: ${max_cache_size}${memoryInfo}"
  }
  
  def get_memory_report(): String = {
    if (enable_memory_management) {
      val (used, available, max, allocated, pressure, is_corrected) = get_validated_memory_metrics()
      val system_memory_mb = get_system_memory_mb()
      
      val correction_note = if (is_corrected) {
        s"\nNOTE: JVM memory report was corrected (system memory: ${system_memory_mb}MB)"
      } else {
        ""
      }
      
      s"""Memory Report:
         |Memory Pressure: ${f"$pressure%.1f"}% 
         |Used Memory: ${format_bytes_to_mb(used)} MB
         |Available Memory: ${format_bytes_to_mb(available)} MB  
         |Effective Max: ${format_bytes_to_mb(max)} MB
         |System Memory: ${system_memory_mb} MB
         |Total Allocated: ${format_bytes_to_mb(allocated)} MB
         |Memory management enabled: $enable_memory_management
         |Session count: ${running_sessions.value.size}
         |Cache size: ${session_cache.value.values.map(_.size).sum}${correction_note}
         |""".stripMargin
    } else {
      "Memory management disabled"
    }
  }
  
  def get_memory_status(): String = {
    if (enable_memory_management) {
      val (used, available, max, _, pressure, _) = get_validated_memory_metrics()
      s"Memory: ${f"$pressure%.1f"}% (${format_bytes_to_mb(used)}MB used / ${format_bytes_to_mb(available)}MB available / ${format_bytes_to_mb(max)}MB max)"
    } else {
      "Memory management disabled"
    }
  }
  
  def can_create_new_session(): Boolean = {
    if (enable_memory_management) {
      val (used, available, max, _, pressure, _) = get_validated_memory_metrics()
      val system_memory_mb = get_system_memory_mb()
      
      val pressure_threshold = 85.0  
      val min_available_mb = 256L * 1024L * 1024L 
      val session_count_limit = 20  
      
      val system_memory_bytes = system_memory_mb * 1024 * 1024
      val system_memory_safe = used < (system_memory_bytes * 0.8)
      
      val memory_ok = pressure < pressure_threshold && available > min_available_mb && system_memory_safe
      val session_count_ok = running_sessions.value.size < session_count_limit
      
      if (!memory_ok) {
        println(s"Memory pressure too high: ${f"$pressure%.1f"}%, available: ${format_bytes_to_mb(available)}MB")
        if (!system_memory_safe) {
          println(s"System memory protection: using ${used / (1024*1024)}MB of ${system_memory_mb}MB system memory")
        }
      }
      if (!session_count_ok) {
        println(s"Too many active sessions: ${running_sessions.value.size}")
      }
      
      memory_ok && session_count_ok
    } else {
      true // always create new sessions if memory management disabled
    }
  }
  
  def perform_memory_cleanup(): Unit = {
    if (enable_memory_management) {
      val (before_used, _, _, _, before_pressure, _) = get_validated_memory_metrics()
      
      if (enable_cache && session_cache.value.nonEmpty) {
        val cache_size_before = session_cache.value.values.map(_.size).sum
        evict_if_needed()
        val cache_size_after = session_cache.value.values.map(_.size).sum
        if (cache_size_before > cache_size_after) {
          println(s"Cache cleanup: evicted ${cache_size_before - cache_size_after} sessions")
        }
      }
      
      val (after_used, _, _, _, after_pressure, _) = get_validated_memory_metrics()
      if (after_pressure > 90.0) {
        println(s"High memory pressure (${f"$after_pressure%.1f"}%), suggesting GC...")
        System.gc()
        
        Thread.sleep(100)
        val (final_used, _, _, _, final_pressure, _) = get_validated_memory_metrics()
        val memory_freed = before_used - final_used
        println(s"Memory cleanup completed: freed ${format_bytes_to_mb(memory_freed)}MB, pressure: ${f"$before_pressure%.1f"}% → ${f"$final_pressure%.1f"}%")
      } else {
        println(s"Memory cleanup completed: pressure: ${f"$before_pressure%.1f"}% → ${f"$after_pressure%.1f"}%")
      }
    }
  }
  
  private def increment_session_ref(session_id: UUID.T): Unit = {
    session_refs.change(refs => {
      refs.update(session_id, refs.getOrElse(session_id, 0) + 1)
      refs
    })
  }
  
  private def decrement_session_ref(session_id: UUID.T): Unit = {
    session_refs.change(refs => {
      val current_refs = refs.getOrElse(session_id, 0)
      if (current_refs > 1) {
        refs.update(session_id, current_refs - 1)
      } else {
        refs.remove(session_id)
      }
      refs
    })
  }
  
  def is_session_evicted(session_id: UUID.T): Boolean = {
    evicted_sessions.value.contains(session_id)
  }
  
  private def mark_session_evicted(session_id: UUID.T): Unit = {
    evicted_sessions.change(evicted => {
      evicted += session_id
      evicted
    })
  }


  private def get_cache_key(initial_thys: List[String]): List[String] = {
    initial_thys.sorted 
  }
  
  private def try_get_from_cache(key: List[String]): Option[Session_Data] = {
    if (!enable_cache) return None
    
    var session_data: Option[Session_Data] = None
    
    session_cache.change { cache =>
      cache.get(key) match {
        case Some(queue) if queue.nonEmpty =>

          val cached_session = queue.head 
          session_data = Some(cached_session)
          lru_order.change(lru => { lru.put(key, System.currentTimeMillis()); lru })
          cache
        case _ =>
          session_data = None
          cache
      }
    }
    
    if (session_data.isDefined) {
      cache_stats.change(stats => { stats.update("hits", stats("hits") + 1); stats })
      session_data.foreach(sd => increment_session_ref(sd.id))
    } else {
      cache_stats.change(stats => { stats.update("misses", stats("misses") + 1); stats })
    }
    
    session_data
  }
  
  private def put_to_cache(key: List[String], session_data: Session_Data): Unit = {
    if (!enable_cache) return
    
    session_cache.change { cache =>
      val queue = cache.getOrElseUpdate(key, mutable.Queue.empty)
      
      if (!queue.exists(_.id == session_data.id)) {
        queue.enqueue(session_data)
        lru_order.change(lru => { lru.put(key, System.currentTimeMillis()); lru })
        
        evict_if_needed()
      }
      cache
    }
  }

  private def evict_if_needed(): Unit = {
    if (!enable_cache) return
    
    val total_sessions = session_cache.value.values.map(_.size).sum
    if (total_sessions > max_cache_size) {

      lru_order.change { lru =>
        val sorted_keys = lru.toSeq.sortBy(_._2) 
        val keys_to_remove = sorted_keys.take(total_sessions - max_cache_size).map(_._1)
        
        keys_to_remove.foreach { key =>
          session_cache.change { cache =>
            cache.get(key) match {
              case Some(queue) if queue.nonEmpty =>
                val session_to_evict = queue.dequeue()
                mark_session_evicted(session_to_evict.id)
                remove_session_async(session_to_evict.id)
                cache_stats.change(stats => { stats.update("evictions", stats("evictions") + 1); stats })
                println(s"Evicted session for theories: ${key.mkString(", ")}")
                
                if (queue.isEmpty) {
                  cache.remove(key)
                }
                cache
              case _ => cache
            }
          }
        }
        
        keys_to_remove.foreach(key => lru.remove(key))
        lru
      }
    }
  }


  def release_session(session_data: Session_Data, theories: List[String]): Unit = {
    decrement_session_ref(session_data.id)
    
    val current_refs = session_refs.value.getOrElse(session_data.id, 0)
    
    if (current_refs > 0) {
      println(s"Session ${session_data.id} still has ${current_refs} references, keeping alive")
      return
    }
    
    if (!enable_cache || is_session_evicted(session_data.id)) {
      remove_session_async(session_data.id)
      println(s"Session ${session_data.id} destroyed (cache disabled or evicted)")
    } else {
      println(s"Session ${session_data.id} released but kept in cache for theories: ${theories.mkString(", ")}")
    }
  }
  
  def release_session_to_cache(session_data: Session_Data, theories: List[String]): Unit = {
    release_session(session_data, theories)
  }
  def get_session_with_cache(initial_thys: List[String], field: String = "HOL"): Session_Data = {
    val key = get_cache_key(initial_thys)
    
    try_get_from_cache(key) match {
      case Some(session_data) =>
        println(s"Cache hit for theories: ${key.mkString(", ")} - sharing session ${session_data.id}")
        session_data 
      case None =>
        println(s"Cache miss for theories: ${key.mkString(", ")}. Creating new session.")
        val session_data = create_new_session_internal(initial_thys, field)
        put_to_cache(key, session_data) 
        session_data
    }
  }

  private def create_new_session_internal(initial_thys: List[String], field: String = "HOL"): Session_Data = {
    val session_delay_options_to_minimise =
      List("headless_consolidate_delay", "headless_check_delay", "headless_nodes_status_delay")
    val min_delay = "0.1"
    val session_option_pairs =
      ("show_states", show_states.toString) ::
        session_delay_options_to_minimise.map(option_name => (option_name, min_delay))
    val session_options = session_option_pairs.map { case (name, value) => s"${name}=${value}" }
    val session_id = Server_Utils.start_session(server_info, server, session_options, field)
    running_sessions.change(_ += session_id)
    val session = server.the_session(session_id)
    if (initial_thys.nonEmpty) {
      session.use_theories(initial_thys)
    }
    cache_stats.change(stats => { stats.update("creates", stats("creates") + 1); stats })
    increment_session_ref(session_id)
    Session_Data(session_id, session)
  }
  
  def get_new_session(initial_thys: List[String], field: String = "HOL"): Session_Data = {
    if (enable_cache) {
      get_session_with_cache(initial_thys, field)
    } else {
      create_new_session_internal(initial_thys, field)
    }
  }

  private def remove_session_sync(session_id: UUID.T): Unit = {
    Server_Utils.stop_session(server_info, server, session_id)
    running_sessions.change(_ -= session_id)
  }

  def remove_session_async(session_id: UUID.T): Unit = {
    val removal_future = Future.fork[Unit](remove_session_sync(session_id))
    pending_removals.change(_ += removal_future)
    removal_future.map(_ => pending_removals.change(_ -= removal_future))
  }

  def remove_session_async(session_data: Session_Data): Unit =
    remove_session_async(session_data.id)

  def shutdown(): Unit = {
    def apply_foreach[A](mutable_set: Synchronized[mutable.Set[A]])(f: A => Unit): Unit =
      mutable_set.guarded_access(set => Some((set.toList, set))).foreach(f)

    apply_foreach(running_sessions)(remove_session_async)

    apply_foreach(pending_removals) { removal_future =>
      try removal_future.join
      catch {
        case e: Throwable =>
          Output.error_message(s"Error during session removal: ${Exn.message(e)}")
      }
    }
    session_cache.change { cache =>
      cache.values.foreach(_.foreach(session_data => remove_session_async(session_data.id)))
      cache.clear()
      cache
    }
    
    lru_order.change(lru => { lru.clear(); lru })

    Server_Utils.stop_server(server_info)
  }
}

