"""Utilities associated with the process that runs the Scala REPL gateway."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol

import py4j
import py4j.java_collections
from py4j.java_gateway import GatewayParameters, JavaGateway

REPO_ROOT = Path(__file__).parent.parent.parent.parent.parent
repl_gateway_path = REPO_ROOT / "app/repl/src/main/scala/repl/repl_backend_gateway.scala"
isabelle_executable = REPO_ROOT / "opt" / "isabelle" / "bin" / "isabelle"

EnvStateID = int


class Outputs(Protocol):
    """Protocol for separated outputs from the Scala backend."""

    def output(self) -> str: ...
    def error(self) -> str: ...


class ReplResult(Protocol):
    # pylint: disable=missing-docstring
    """Protocol for results from the Scala backend."""

    def separated_output(self) -> Outputs: ...
    def total_output(self) -> str: ...


# pylint: disable=missing-docstring
class ReplBackend(Protocol):
    """Protocol matching the Scala ``repl.ReplBackend`` class.

    Every public method on the Scala side should have an entry here so that
    static type checkers can catch interface drift early.
    """

    # --- core session operations ---
    def current_thy_name_string(self) -> str: ...
    def enter_thy(self, input_thy_name: str) -> ReplResult: ...
    def open_subgoals(self) -> py4j.java_collections.JavaList[str]: ...
    def local_facts(self) -> py4j.java_collections.JavaList[str]: ...
    def global_facts(self, limit: int) -> py4j.java_collections.JavaList[str]: ...
    def get_proof_state(self) -> ReplResult: ...
    def get_source(self) -> ReplResult: ...
    def rollback(self) -> ReplResult: ...
    def step(self, isar_string: str) -> ReplResult: ...
    def save_state(self) -> EnvStateID: ...
    def restore_state(self, state_id: EnvStateID) -> bool: ...
    def reset(self) -> ReplResult: ...
    def exit(self) -> None: ...

    # --- cache ---
    def get_cache_status(self) -> str: ...
    def get_cache_stats(self) -> py4j.java_collections.JavaMap[str, int]: ...

    # --- memory management ---
    def get_memory_report(self) -> str: ...
    def get_memory_status(self) -> str: ...
    def can_create_new_session(self) -> bool: ...
    def perform_memory_cleanup(self) -> None: ...

    # --- session health ---
    def is_session_valid(self) -> bool: ...
    def recreate_session_if_needed(self) -> None: ...

    # --- vector environment ---
    def vector_step(self, isar_strings: py4j.java_collections.JavaList[str]) -> ReplResult: ...
    def vectorise(self, size: int) -> None: ...
    def scalarise(self, index_to_keep: int) -> None: ...


class ReplBackendGatewayProcess:
    """
    Class to manage an instance of the Scala REPL gateway process.
    """

    # Configurable via subclass or monkey-patch; avoids hardcoded magic numbers.
    POLL_INTERVAL: float = 0.1
    POLL_TIMEOUT: float = 20.0
    TERMINATE_WAIT: float = 3.0

    def __init__(self) -> None:
        # pylint: disable=consider-using-with, subprocess-popen-preexec-fn
        self.process = subprocess.Popen(
            [isabelle_executable, "scala", str(repl_gateway_path)],
            # Isabelle uses many child processes, so we start a new process group
            preexec_fn=os.setsid,
            # Port number will be passed via stdout
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
        )
        if self.process.stdout is None:
            raise RuntimeError(
                "Scala REPL gateway process failed to start (stdout is None)."
            )
        port = int(self.process.stdout.readline().strip())
        # Close the pipe now that the port has been read — assigning
        # sys.stdout was a bug (it doesn't redirect the subprocess).
        self.process.stdout.close()
        self.process.stdout = None
        self.gateway = JavaGateway(gateway_parameters=GatewayParameters(port=port))

    def has_terminated(self) -> bool:
        """Check if the Scala REPL gateway process has terminated."""
        return self.process.poll() is not None

    # ------------------------------------------------------------------
    # Generic polling helper — all get_repl_backend_* variants delegate
    # to this to avoid the 6× copy-paste that previously existed.
    # ------------------------------------------------------------------

    def _poll_gateway(self, method_name: str, *args) -> ReplBackend:
        """Call a factory method on the Scala ``ReplBackendGateway`` object,
        retrying on ``Py4JNetworkError`` until the gateway is ready.

        Parameters
        ----------
        method_name : name of the static method on ``repl.ReplBackendGateway``
        *args : positional arguments forwarded to the Scala method
        """
        start_time = time.time()
        while time.time() - start_time < self.POLL_TIMEOUT:
            if self.has_terminated():
                raise RuntimeError("Scala REPL gateway process has terminated.")
            try:
                factory = getattr(
                    self.gateway.jvm.repl.ReplBackendGateway, method_name
                )
                return factory(*args)
            except py4j.protocol.Py4JNetworkError:
                time.sleep(self.POLL_INTERVAL)
        raise RuntimeError(
            f"Failed to call ReplBackendGateway.{method_name} "
            f"within {self.POLL_TIMEOUT}s."
        )

    # ------------------------------------------------------------------
    # Public factory methods — signatures match the Scala gateway exactly.
    #
    # Scala source of truth:
    #   repl/src/main/scala/repl/repl_backend_gateway.scala
    # ------------------------------------------------------------------

    def get_repl_backend(self, show_states: bool) -> ReplBackend:
        # Scala: get_repl_backend(show_states: Boolean)
        return self._poll_gateway("get_repl_backend", show_states)

    def get_repl_backend_with_cache(
        self, show_states: bool, enable_cache: bool
    ) -> ReplBackend:
        # Scala: get_repl_backend_with_cache(show_states: Boolean, enable_cache: Boolean)
        # NOTE: the old Python version incorrectly passed a `field` arg here.
        return self._poll_gateway(
            "get_repl_backend_with_cache", show_states, enable_cache
        )

    def get_repl_backend_with_full_cache_config(
        self,
        show_states: bool,
        enable_cache: bool,
        max_cache_size: int,
        field: str = "HOL",
    ) -> ReplBackend:
        # Scala: get_repl_backend_with_full_cache_config(
        #     show_states, enable_cache, max_cache_size, field: String = "HOL")
        return self._poll_gateway(
            "get_repl_backend_with_full_cache_config",
            show_states, enable_cache, max_cache_size, field,
        )

    def get_repl_backend_with_memory_management(
        self,
        show_states: bool,
        enable_cache: bool,
        max_cache_size: int,
        enable_memory_management: bool,
        field: str = "HOL",
    ) -> ReplBackend:
        # Scala: get_repl_backend_with_memory_management(
        #     show_states, enable_cache, max_cache_size,
        #     enable_memory_management, field: String = "HOL")
        return self._poll_gateway(
            "get_repl_backend_with_memory_management",
            show_states, enable_cache, max_cache_size,
            enable_memory_management, field,
        )

    def get_repl_backend_with_initial_theories(
        self,
        show_states: bool,
        enable_cache: bool,
        max_cache_size: int,
        enable_memory_management: bool,
        initial_thys: "py4j.java_collections.JavaList[str]",
        field: str = "HOL",
    ) -> ReplBackend:
        # Scala: get_repl_backend_with_initial_theories(
        #     show_states, enable_cache, max_cache_size,
        #     enable_memory_management, initial_thys: java.util.List[String],
        #     field: String = "HOL")
        return self._poll_gateway(
            "get_repl_backend_with_initial_theories",
            show_states, enable_cache, max_cache_size,
            enable_memory_management, initial_thys, field,
        )

    def get_repl_backend_with_shared_cache(
        self,
        show_states: bool,
        enable_cache: bool,
        max_cache_size: int,
        enable_memory_management: bool,
        initial_thys: "py4j.java_collections.JavaList[str]",
        field: str = "HOL",
    ) -> ReplBackend:
        # Scala: get_repl_backend_with_shared_cache(
        #     show_states, enable_cache, max_cache_size,
        #     enable_memory_management, initial_thys: java.util.List[String],
        #     field: String = "HOL")
        return self._poll_gateway(
            "get_repl_backend_with_shared_cache",
            show_states, enable_cache, max_cache_size,
            enable_memory_management, initial_thys, field,
        )
    
    def terminate(self) -> None:
        """
    Gracefully terminate the Scala REPL gateway.
    
    FIXED: Proper shutdown sequence without process group signals
    """
    
        # Step 1: Shutdown Py4J gateway (tells Scala we're done)
        try:
            self.gateway.shutdown()
            print("✓ Gateway shutdown initiated")
        except Exception as e:
            print(f"⚠ Gateway shutdown warning: {e}")
    
        # Step 2: Give Isabelle time to cleanup gracefully
        # This is CRITICAL - Isabelle needs time to cleanup threads
        time.sleep(0.5)  # 500ms for graceful cleanup
    
        # Step 3: Politely ask process to terminate (SIGTERM to process, not group)
        try:
            self.process.terminate()  # Sends SIGTERM to single process
            print("✓ Terminate signal sent to gateway process")
        except Exception as e:
            print(f"⚠ Terminate warning: {e}")
    
        # Step 4: Wait for graceful shutdown (with timeout)
        wait_start_time = time.time()
        wait_timeout = 3.0  # Increased from 1s to 3s
    
        while time.time() - wait_start_time < wait_timeout:
            if self.process.poll() is not None:
                print(f"✓ Gateway process exited cleanly after {time.time() - wait_start_time:.2f}s")
                return
            time.sleep(0.1)
    
        # Step 5: Force kill only if graceful shutdown failed
        print(f"⚠ Gateway process did not exit after {wait_timeout}s, force killing...")
        try:
            self.process.kill()  # SIGKILL to single process
            self.process.wait(timeout=2)
            print("✓ Gateway process force killed")
        except Exception as e:
            print(f"⚠ Force kill warning: {e}")
