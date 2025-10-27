"""Provides a Python interface to interact with Isabelle."""

import atexit
import time

from .repl_backend_gateway import EnvStateID, ReplBackendGatewayProcess, ReplResult


class IsabelleClient:
    """High-level interface to Isabelle."""

    def __init__(
        self, 
        show_states: bool = False,
        enable_cache: bool = False,
        max_cache_size: int = 10,
        enable_memory_management: bool = False,
        shared_cache: bool = False,
        initial_thys: list[str] = None,
    ) -> None:
        """Initialize the Isabelle client."""
        self.repl_backend_gateway_process = ReplBackendGatewayProcess()
        self.repl_backend_gateway_process_init = True
        
        self.show_states = show_states
        self.enable_cache = enable_cache
        self.max_cache_size = max_cache_size
        self.enable_memory_management = enable_memory_management
        self.shared_cache = shared_cache
        
        if initial_thys is None:
            initial_thys = ["$ISABELLE_REPL_HOME/IsabelleREPL"]
        
        self.initial_thys = initial_thys
        
        java_list = self.repl_backend_gateway_process.gateway.jvm.java.util.ArrayList()
        for thy in initial_thys:
            java_list.add(thy)
        
        if shared_cache:
            self.repl_backend = self.repl_backend_gateway_process.get_repl_backend_with_shared_cache(
                show_states, enable_cache, max_cache_size, enable_memory_management, java_list
            )

            self.use_multi_session = True
            self.sessions = {}
            self.current_session_id = None
            self.create_session("default", initial_thys)
        else:

            self.repl_backend = self.repl_backend_gateway_process.get_repl_backend_with_memory_management(
                show_states, enable_cache, max_cache_size, enable_memory_management
            )
            self.use_multi_session = False
            self.sessions = None
            self.current_session_id = None
        
        self.repl_backend_init = True

    def _get_active_backend(self):
        """get the active backend"""
        if self.use_multi_session:
            return self.get_current_session()
        else:
            return self.repl_backend

    def enter_thy(self, thy_name: str) -> ReplResult:
        """Enters a given theory."""
        return self._get_active_backend().enter_thy(thy_name)

    def isar_snippet(self, isar_string: str) -> ReplResult:
        """Adds the given Isar string to the current theory."""
        return self._get_active_backend().step(isar_string)

    def get_current_thy_name(self) -> str:
        """Returns the current theory name."""
        return self._get_active_backend().current_thy_name_string()

    def open_subgoals(self) -> list[str]:
        """Returns the list of currently open subgoals."""
        subgoals = self._get_active_backend().open_subgoals()
        return [subgoal.strip() for subgoal in subgoals]

    def local_facts(self) -> list[str]:
        """Returns the list of local facts in the current proof context."""
        return list(self._get_active_backend().local_facts())

    def global_facts(self, limit: int) -> list[str]:
        """Returns the list of global facts in the current proof context."""
        return list(self._get_active_backend().global_facts(limit))

    def get_source(self) -> ReplResult:
        """Returns the source code of the current theory (if any)."""
        return self._get_active_backend().get_source()

    def rollback(self) -> ReplResult:
        """Undoes the last step in the current theory."""
        return self._get_active_backend().rollback()

    def save_state(self) -> EnvStateID:
        """Saves the current state of the environment, returning an ID for the state."""
        return self._get_active_backend().save_state()

    def restore_state(self, state_id: EnvStateID) -> bool:
        """Restores a previously saved state by the state ID."""
        return self._get_active_backend().restore_state(state_id)

    def reset(self) -> None:
        """Resets the Scala REPL."""
        if self.use_multi_session:
            self._get_active_backend().reset()
        else:
            self.repl_backend.reset()

    def vectorise(self, size: int) -> None:
        """Switches to vector mode with the specified number of environments."""
        return self._get_active_backend().vectorise(size)

    def scalarise(self, index_to_keep: int):
        """Switches from vector mode back to scalar mode"""
        return self._get_active_backend().scalarise(index_to_keep)

    def vector_step(self, isar_strings: list[str]) -> ReplResult:
        """Executes Isar commands in parallel across all environments."""
        return self._get_active_backend().vector_step(isar_strings)

    def cleanup(self) -> None:
        """Cleans up the Scala REPL with proper shutdown sequence"""
    
        if not (self.repl_backend_gateway_process_init 
                and not self.repl_backend_gateway_process.has_terminated()):
            return
    
        print("[Cleanup] Starting Isabelle cleanup sequence...")
    
        # Step 1: Close all sessions/backends gracefully
        if self.use_multi_session:
            print(f"[Cleanup] Closing {len(self.sessions)} sessions...")
            for session_id in list(self.sessions.keys()):
                try:
                    self.close_session(session_id)
                    print(f"  ✓ Closed session: {session_id}")
                except Exception as e:
                    print(f"  ⚠ Error closing session {session_id}: {e}")
        else:
            print("[Cleanup] Closing backend...")
            try:
                self.repl_backend.reset()
                print("  ✓ Backend exited")
            except Exception as e:
                print(f"  ⚠ Error closing backend: {e}")
    
        # Step 2: CRITICAL - Give backends time to cleanup
        print("[Cleanup] Waiting for backends to finish cleanup...")
        time.sleep(0.5)  # 500ms grace period
    
        # Step 3: Terminate gateway process
        print("[Cleanup] Terminating gateway process...")
        try:
            self.repl_backend_gateway_process.terminate()
            print("  ✓ Gateway terminated")
        except Exception as e:
            print(f"  ⚠ Gateway termination warning: {e}")
    
        print("[Cleanup] Cleanup complete")

    def get_cache_stats(self) -> dict:
        """Returns the cache statistics."""
        if self.use_multi_session:
            return self.get_session_cache_stats()
        else:
            return dict(self.repl_backend.get_cache_stats())

    def get_cache_status(self) -> str:
        """Returns the cache status."""
        if self.use_multi_session:
            return self.get_session_cache_status()
        else:
            return self.repl_backend.get_cache_status()

    # only available in multi-session mode
    def create_session(self, session_id: str, initial_thys: list[str] = None) -> str:
        """create a new Session"""
        if not self.use_multi_session:
            raise RuntimeError("create_session is only available in multi-session mode")
        
        if initial_thys is None:
            initial_thys = self.initial_thys
        
        java_list = self.repl_backend_gateway_process.gateway.jvm.java.util.ArrayList()
        for thy in initial_thys:
            java_list.add(thy)
        
        # use shared cache in multi-session mode
        new_backend = self.repl_backend_gateway_process.get_repl_backend_with_shared_cache(
            self.show_states, self.enable_cache, self.max_cache_size, self.enable_memory_management, java_list
        )
        
        self.sessions[session_id] = {
            'backend': new_backend,
            'initial_thys': initial_thys,
            'created_at': time.time()
        }
        
        if self.current_session_id is None:
            self.current_session_id = session_id
        
        print(f"create Session: {session_id}, theories: {initial_thys}")
        return session_id
    
    def switch_session(self, session_id: str) -> bool:
        """switch to the specified Session"""
        if not self.use_multi_session:
            raise RuntimeError("switch_session is only available in multi-session mode")
        
        if session_id not in self.sessions:
            print(f"Session {session_id} does not exist")
            return False
        
        self.current_session_id = session_id
        print(f"switch to Session: {session_id}")
        return True
    
    def get_current_session(self):
        """get the backend of the current active Session"""
        if not self.use_multi_session:
            raise RuntimeError("get_current_session is only available in multi-session mode")
        
        if self.current_session_id is None:
            raise RuntimeError("No active Session")
        return self.sessions[self.current_session_id]['backend']
    
    def list_sessions(self) -> dict:
        """list all Sessions"""
        if not self.use_multi_session:
            raise RuntimeError("list_sessions is only available in multi-session mode")
        
        result = {}
        for session_id, session_info in self.sessions.items():
            result[session_id] = {
                'initial_thys': session_info['initial_thys'],
                'created_at': session_info['created_at'],
                'is_current': session_id == self.current_session_id
            }
        return result
    
    def close_session(self, session_id: str) -> bool:
        """close the specified Session"""
        if not self.use_multi_session:
            raise RuntimeError("close_session is only available in multi-session mode")
        
        if session_id not in self.sessions:
            print(f"Session {session_id} does not exist")
            return False
        
        if session_id == self.current_session_id:
            self.current_session_id = None
        
        session_info = self.sessions.pop(session_id)
        #try:
        #    session_info['backend'].exit()
        #except:
        #    pass
        
        print(f"close Session: {session_id}")
        return True
    
    def get_session_cache_stats(self, session_id: str = None) -> dict:
        """get the cache statistics of the specified Session"""
        if not self.use_multi_session:
            raise RuntimeError("get_session_cache_stats is only available in multi-session mode")
        
        if session_id is None:
            session_id = self.current_session_id
        
        if session_id not in self.sessions:
            return {}
        
        backend = self.sessions[session_id]['backend']
        return dict(backend.get_cache_stats())
    
    def get_session_cache_status(self, session_id: str = None) -> str:
        """get the cache status of the specified Session"""
        if not self.use_multi_session:
            raise RuntimeError("get_session_cache_status is only available in multi-session mode")
        
        if session_id is None:
            session_id = self.current_session_id
        
        if session_id not in self.sessions:
            return "Session does not exist"
        
        backend = self.sessions[session_id]['backend']
        return backend.get_cache_status()
    
    # memory management methods
    def get_memory_report(self) -> str:
        """Get detailed memory report from the backend"""
        if self.use_multi_session:
            backend = self._get_active_backend()
        else:
            backend = self.repl_backend
            
        if backend is None:
            return "Backend not available"
        try:
            return backend.get_memory_report()
        except Exception as e:
            return f"Error getting memory report: {e}"
    
    def get_memory_status(self) -> str:
        """Get current memory status from the backend"""
        if self.use_multi_session:
            backend = self._get_active_backend()
        else:
            backend = self.repl_backend
            
        if backend is None:
            return "Backend not available"
        try:
            return backend.get_memory_status()
        except Exception as e:
            return f"Error getting memory status: {e}"
    
    def can_create_new_session(self) -> bool:
        """Check if memory allows creating a new session"""
        if self.use_multi_session:
            backend = self._get_active_backend()
        else:
            backend = self.repl_backend
            
        if backend is None:
            return False
        try:
            return backend.can_create_new_session()
        except Exception as e:
            print(f"Error checking session creation capability: {e}")
            return False
    
    def perform_memory_cleanup(self) -> None:
        """Perform memory cleanup operations"""
        if self.use_multi_session:
            backend = self._get_active_backend()
        else:
            backend = self.repl_backend
            
        if backend is None:
            print("Backend not available for memory cleanup")
            return
        try:
            backend.perform_memory_cleanup()
        except Exception as e:
            print(f"Error performing memory cleanup: {e}")