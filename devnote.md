# Development note for Isabelle server

## Cleanup and fix to previous local gym version

- [SOLVED] Isabelle directory for docker is overwrote by Mount
  - Solved, redirected isabelle dir
- [SOLVED] LRU efficiency benchmark script does not exist.
  - Solved, created and benchmark recorded
- AFP components not added
  - In progress, creating afp init script
- [SOLVED] Due to missing component, process benchmark is not recreated
  - Abandoned old benchmarks due to lack of solid validation
- Sledgehammer function missing
  - Unsolved
- Repl Backend does not support other imports
  - Unsolved, need to refine repo backend create functions
  - Issues: 1. not sure why use_theories from Isabelle is not working. 2. need to a new arguement to pass through start session option on HOL-Analysis. 3. Modify on IsabelleREPL.thy works, but modify on other file doesnt work, need to change the hardcoded parameter.
- Memory cleanup not working
  - Unsolved
- Shutdown not working
  - Probelm specification

## To be implemented features and problems on Isabelle server

### Session manager in Scala end not been used
1. Create with get_repl_backend_with_shared_cache() will create a shared session manager instance in repl_backend_gateway.scala locally.
2. A new session created with get_repl_backend_with_shared_cache() will use the same session_manager, and new session_data will be retrieved directly from ServerUtil.
3. This session is add to cache and a new session is now avaliable.

Issues:
1. In session_manager.scala line 247 in function try_get_from_cache(), queue.head will not pop, so two backends calling the function will get the same session data. And in session_manager.scala there is no storage of currently using sessions.
2. Session_Manager class is not exposed to Python wrapper, all operations related to session manager is through methods in ReplBackend class. This is extremely inconvenient, if we are manageing sessions on a Python based server, a session manager written in Python will be more useful and with better expension capacity and lower maintanence difficulty.

Issue solved by: 


### Rewrite isabelle client using repl backend
The current isabelle_client class is overstaffed, too many methods designed for local MCTS and interface purpose. These methods and implementation are not ideal for implementing server. Thus, a new client/session pool using existing repl backend is needed.

## New design, workflow from install to use
1. Run docker/install.sh
  - Has isabelle, pip ready
2. Download afp, add afp via afp_init.py
  - Has afp as component ready
3. Start server/gym, double option avaliable

## Server idea
1. Server start with 1 default HOL.Main sessions in session pool
2. Server has shared local import memory for .thy artifacts
3. Server take new request from clients with header: theory name, imports, strategy
  - Strategies includes: single session, multi-collaberate, multi-competitive

## Priority
- Server side whole proof verification
- Implement and documentation
- Bugs above
- Scala level optimisation on isabelle heap sharing