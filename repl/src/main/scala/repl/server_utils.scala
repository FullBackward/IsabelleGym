package repl

import isabelle._

object Server_Utils {
  private def withServerContext(
      server_info: Server.Info,
      server: Server
  )(f: Server.Context => JSON.Object.T): JSON.Object.T =
    using(server_info.connection()) { connection =>

      val ctxClass = Class.forName("isabelle.Server$Context")
      val ctor = ctxClass.getDeclaredConstructors.head
      ctor.setAccessible(true)
      val ctx =
        ctor.newInstance(server, connection).asInstanceOf[Server.Context]
      f(ctx)
    }

  def start_server(): (Server.Info, Server) = {
    def attempt_start_server(): Option[(Server.Info, Server)] = {
      val server_name = UUID.random_string()
      val (server_info, server_opt) = Server.init(name = server_name)
      server_opt.map(server => (server_info, server))
    }

    def retry_attempt_start_server(max_retries: Int): Option[(Server.Info, Server)] =
      LazyList.continually(attempt_start_server()).take(max_retries).find(_.isDefined).flatten

    val max_retries = 5
    retry_attempt_start_server(max_retries) match {
      case None => error(s"Unable to start server after ${max_retries} attempts.")
      case Some((server_info, server)) =>
        Output.writeln(s"Started server ${server_info.name}.")
        (server_info, server)
    }
  }

  def stop_server(server_info: Server.Info): Unit =
    Server.exit(server_info.name) match {
      case true  => Output.writeln(s"Stopped server ${server_info.name}.")
      case false => error(s"Failed to stop server ${server_info.name}.")
    }

  def start_session(server_info: Server.Info, server: Server, options: List[String], field: String = "HOL"): UUID.T = {

    val session_start_json = withServerContext(server_info, server) { context =>

      val result_cell = Synchronized[Option[JSON.Object.T]](None)
      Isabelle_Thread.fork(name = "session_start") {
        val args = Server_Commands.Session_Start.Args(
          build = Server_Commands.Session_Build.Args(session = field, options = options))
        val (res, entry) =
          Server_Commands.Session_Start.command(
            args, progress = context.progress(), log = context.server.log)
        context.server.add_session(entry)     
        result_cell.change(_ => Some(res))    
      }.join()
      result_cell.value.getOrElse(error("Session start failed"))
    }
    val session_id = JSON
      .uuid(session_start_json, "session_id")
      .getOrElse(error("Unable to retrieve session id."))
    Output.writeln(s"Started session ${session_id}.")
    session_id
  }

  def stop_session(
      server_info: Server.Info,
      server: Server,
      session_id: UUID.T
  ): Unit = {
    // grab session, run command, then unregister
    val session = server.remove_session(session_id)
    val (session_stop_json, _) = Server_Commands.Session_Stop.command(session)

    val return_code = session_stop_json.getOrElse(
      "return_code",
      error("Unable to retrieve session stop return code.")
    )
    Output.writeln(s"Stopped session ${session_id} with return code $return_code.")
  }
}
