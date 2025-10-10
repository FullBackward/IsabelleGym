package repl

import isabelle._
import py4j.GatewayServer
import scala.jdk.CollectionConverters._

object ReplBackendGateway {
  def get_repl_backend(show_states: Boolean): ReplBackend = new ReplBackend(show_states)
  def get_repl_backend_with_cache(show_states: Boolean, enable_cache: Boolean): ReplBackend = 
    new ReplBackend(show_states, enable_cache)
    
  def get_repl_backend_with_full_cache_config(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int): ReplBackend = 
    new ReplBackend(show_states, enable_cache, max_cache_size)

  def get_repl_backend_with_memory_management(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, enable_memory_management: Boolean): ReplBackend = 
    new ReplBackend(show_states, enable_cache, max_cache_size, enable_memory_management)

  def get_repl_backend_with_initial_theories(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, enable_memory_management: Boolean, initial_thys: java.util.List[String]): ReplBackend = 
    new ReplBackend(show_states, enable_cache, max_cache_size, enable_memory_management, initial_thys.asScala.toList)
  
  private var shared_session_manager: Option[Session_Manager] = None
  
  def get_shared_session_manager(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, enable_memory_management: Boolean): Session_Manager = {
    if (shared_session_manager.isEmpty) {
      shared_session_manager = Some(new Session_Manager(show_states, enable_cache, max_cache_size, enable_memory_management))
    }
    shared_session_manager.get
  }
  
  def get_repl_backend_with_shared_cache(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, enable_memory_management: Boolean, initial_thys: java.util.List[String]): ReplBackend = {
    val session_manager = get_shared_session_manager(show_states, enable_cache, max_cache_size, enable_memory_management)
    new ReplBackend(show_states, enable_cache, max_cache_size, enable_memory_management, initial_thys.asScala.toList, Some(session_manager))
  }
  
  def clear_shared_session_manager(): Unit = {
    shared_session_manager.foreach(_.shutdown())
    shared_session_manager = None
  }
}

object Gateway_App {
  def main(args: Array[String]): Unit = {
    val gateway = new GatewayServer(ReplBackendGateway, 0)
    gateway.start()
    val port = gateway.getListeningPort
    Output.writeln(port.toString, stdout = true)
  }
}
