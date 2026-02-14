# Local Gym API Reference

API documentation for key modules in the `local_gym/` directory.

---

## isabelle_agent_interface

### ProofStrategy

```python
class ProofStrategy(Enum)
```

Enumeration of proof strategies.

**Members:** `DEPTH_FIRST`, `BREADTH_FIRST`, `PRIORITY`, `MCTS`, `SLEDGEHAMMER`, `COLLABORATIVE`

---

### ProofContext

```python
class ProofContext(subgoals=[], priorities=[], current_theorem="", proof_depth=0, solved=[], attempted_tactics=[], successful_tactics=[], proof_strategy=ProofStrategy.PRIORITY, available_lemmas=[], sledgehammer_suggestions=[], theory_context={}, session_id=None, session_shared=False, training_metadata={})
```

Represents the current state of an Isabelle proof.

**Attributes**

- `subgoals` : list of str
- `priorities` : list of float
- `current_theorem` : str
- `proof_depth` : int
- `solved` : list of bool
- `attempted_tactics` : list of str
- `successful_tactics` : list of str
- `proof_strategy` : ProofStrategy
- `available_lemmas` : list of str
- `sledgehammer_suggestions` : list of str
- `theory_context` : dict
- `session_id` : str, optional
- `session_shared` : bool
- `training_metadata` : dict

**Properties**

- `active_goal_id` : int - Highest priority unsolved goal ID
- `current_subgoal` : str or None - Active subgoal string
- `is_complete` : bool - True if all subgoals solved
- `num_subgoals` : int - Number of remaining subgoals
- `progress_ratio` : float - Successful tactics / total attempts

**Methods**

**update_subgoals**(new_subgoals, new_priorities=None)

Update subgoals and priorities.

- `new_subgoals` : list of str
- `new_priorities` : list of float, optional

**record_tactic_attempt**(tactic, success)

Record tactic attempt and outcome.

- `tactic` : str
- `success` : bool

---

### ProofResult

```python
class ProofResult(success, duration, proof_steps, final_proof_length, final_state, tactic_attempts=0, successful_tactics=0, failed_tactics=0, priority_updates=0, session_reuse_count=0, cache_hit_rate=0.0, memory_usage_mb=0.0, sledgehammer_calls=0, lemma_applications=0, visualization_enabled=False, training_data_collected=False, reward_signal=0.0, guidance_calls=0, tactic_recommendations=[], error_message=None, failure_analysis={})
```

Encapsulates proof outcome and metrics.

**Attributes**

- `success` : bool
- `duration` : float
- `proof_steps` : int
- `final_proof_length` : int
- `final_state` : str
- `tactic_attempts` : int
- `successful_tactics` : int
- `failed_tactics` : int
- `priority_updates` : int
- `session_reuse_count` : int
- `cache_hit_rate` : float
- `memory_usage_mb` : float
- `sledgehammer_calls` : int
- `lemma_applications` : int
- `visualization_enabled` : bool
- `training_data_collected` : bool
- `reward_signal` : float
- `guidance_calls` : int
- `tactic_recommendations` : list of str
- `error_message` : str, optional
- `failure_analysis` : dict

**Properties**

- `success_rate` : float - Percentage of successful tactics
- `performance_score` : float - Comprehensive score (0-150)

---

### IsabelleAgent

```python
class IsabelleAgent(agent_name, strategy=ProofStrategy.PRIORITY)
```

Abstract base class for theorem proving agents.

**Parameters**

- `agent_name` : str
- `strategy` : ProofStrategy, default PRIORITY

**Attributes**

- `total_proofs_attempted` : int
- `total_proofs_succeeded` : int
- `total_tactics_tried` : int
- `total_tactics_succeeded` : int
- `session_step_count` : int
- `session_tactic_history` : list of str
- `session_priority_calls` : int
- `training_collector` : object, optional

**Properties**

- `success_rate` : float - Proof success rate
- `tactic_efficiency` : float - Tactic success rate

**Abstract Methods**

**recommend_tactic**(gym, context, target_goal_id)

Generate next tactic for specified goal.

- `gym` : IsabelleGym
- `context` : ProofContext
- `target_goal_id` : int
- Returns: str or None

**evaluate_priorities**(gym, context)

Return priority scores for subgoals.

- `gym` : IsabelleGym
- `context` : ProofContext
- Returns: list of float

**Methods**

**prove_theorem**(gym, theorem_statement, max_steps=100, verbose=False, visualizer=None, **kwargs)

Main theorem proving method.

- `gym` : IsabelleGym
- `theorem_statement` : str
- `max_steps` : int, default 100
- `verbose` : bool, default False
- `visualizer` : ProofVisualizer, optional
- Returns: ProofResult

**reset_session**()

Reset agent state between proofs.

**get_performance_summary**()

Get performance statistics.

- Returns: dict

---

### SimpleIsabelleAgent

```python
class SimpleIsabelleAgent(agent_name="SimpleIsabelle")
```

Simple agent with predefined tactic libraries.

**Parameters**

- `agent_name` : str, default "SimpleIsabelle"

**Attributes**

- `basic_tactics` : list of str
- `intro_tactics` : list of str
- `elimination_tactics` : list of str
- `goal_tactic_counters` : dict

**Methods**

**recommend_tactic**(gym, context, target_goal_id)

Pattern-based tactic selection.

- Returns: str or None

**evaluate_priorities**(gym, context)

Assign priorities (first goal highest).

- Returns: list of float

---

### Functions

**create_isabelle_agent**(agent_type='simple', agent_name=None, strategy=PRIORITY)

Factory function to create agent instances.

- `agent_type` : str, default 'simple'
- `agent_name` : str, optional
- `strategy` : ProofStrategy, default PRIORITY
- Returns: IsabelleAgent

**quick_prove**(gym, theorem_statement, agent_type='simple', **kwargs)

Convenient single-theorem proving wrapper.

- `gym` : IsabelleGym
- `theorem_statement` : str
- `agent_type` : str, default 'simple'
- Returns: ProofResult

---

## mcts_agent

### MCTSProofContext

```python
class MCTSProofContext(subgoals, priorities, current_theorem, proof_depth, visit_count=1, total_value=0.0, children=[], parent=None, parent_goal_id=None, exhausted=False, subtree_exhausted=False, tactic_feedback=None, trials=[])
```

Extends ProofContext with MCTS tree search data.

**Additional Attributes**

- `visit_count` : int, default 1
- `total_value` : float, default 0.0
- `children` : list of MCTSProofContext
- `parent` : MCTSProofContext, optional
- `parent_goal_id` : int, optional
- `exhausted` : bool, default False
- `subtree_exhausted` : bool, default False
- `tactic_feedback` : str, optional
- `trials` : list of int

**Properties**

- `is_root` : bool - True if no parent

**Methods**

**get_mean_value**()

Calculate mean Q-value.

- Returns: float

**is_expanded**()

Check if node has children.

- Returns: bool

---

### MCTSIsabelleAgent

```python
class MCTSIsabelleAgent(agent_name="MCTSAgent", c_puct=1.0)
```

Abstract MCTS agent framework.

**Parameters**

- `agent_name` : str, default "MCTSAgent"
- `c_puct` : float, default 1.0 - UCB exploration constant

**Abstract Methods**

**estimate**(state)

Estimate leaf node value.

- `state` : MCTSProofContext
- Returns: MCTSProofContext

**select**(state)

Select search path using UCB.

- `state` : MCTSProofContext
- Returns: list of MCTSProofContext

**Methods**

**backup**(states, value)

Backpropagate value through search path.

- `states` : list of MCTSProofContext
- `value` : float

**prove_theorem**(gym, theorem_statement, max_steps=100, max_trials_per_goal=5, verbose=False, **kwargs)

MCTS proving loop.

- `gym` : IsabelleGym
- `theorem_statement` : str
- `max_steps` : int, default 100
- `max_trials_per_goal` : int, default 5
- `verbose` : bool, default False
- Returns: ProofResult

---

### SimpleMCTSIsabelleAgent

```python
class SimpleMCTSIsabelleAgent(agent_name="SimpleMCTS", c_puct=0.6)
```

Simple MCTS implementation with heuristics.

**Parameters**

- `agent_name` : str, default "SimpleMCTS"
- `c_puct` : float, default 0.6

**Attributes**

- `name` : str
- `goal_tactic_id_map` : dict
- `primary_tactics` : list of str
- `secondary_tactics` : list of str
- `specialized_tactics` : list of str

**Methods**

**estimate**(state)

Heuristic value estimation: `1/(1+num_subgoals) + random noise`.

- Returns: MCTSProofContext

**select**(state)

UCB1 path selection with exhaustion tracking.

- Returns: list of MCTSProofContext

**recommend_tactic**(gym, context, target_goal_id)

Pattern-based tactic selection with trial tracking.

- Returns: str or None

**evaluate_priorities**(gym, context)

Zero priorities (all goals equal).

- Returns: list of float

---

## proof_visualizer

### TacticResult

```python
class TacticResult(Enum)
```

Tactic execution result types.

**Members:** `SUCCESS`, `FAILURE`, `PARTIAL`, `PENDING`

---

### TacticExecution

```python
class TacticExecution(tactic, timestamp, result, before_subgoals, after_subgoals, error_message=None, execution_time=0.0)
```

Single tactic execution record.

**Attributes**

- `tactic` : str
- `timestamp` : float
- `result` : TacticResult
- `before_subgoals` : list of str
- `after_subgoals` : list of str
- `error_message` : str, optional
- `execution_time` : float

**Properties**

- `subgoals_changed` : bool - Check if subgoals modified
- `progress_made` : bool - Check if progress made (subgoals reduced or changed)

---

### ProofSession

```python
class ProofSession(theorem_statement, start_time, agent_name, executions=[], final_success=False, total_steps=0)
```

Complete proof session record.

**Attributes**

- `theorem_statement` : str
- `start_time` : float
- `agent_name` : str
- `executions` : list of TacticExecution
- `final_success` : bool
- `total_steps` : int

**Properties**

- `duration` : float - Total session time
- `success_rate` : float - Percentage of successful tactics

**Methods**

**add_execution**(execution)

Add tactic execution to session.

- `execution` : TacticExecution

---

### ProofVisualizer

```python
class ProofVisualizer(max_subgoal_display=3, max_history_display=10)
```

Proof state visualizer with session management.

**Parameters**

- `max_subgoal_display` : int, default 3
- `max_history_display` : int, default 10

**Attributes**

- `current_session` : ProofSession, optional

**Methods**

**start_session**(theorem_statement, agent_name)

Initialize new proof session.

- `theorem_statement` : str
- `agent_name` : str

**record_tactic_execution**(tactic, before_subgoals, after_subgoals, success, error_message=None, execution_time=0.0)

Record and display tactic execution.

- `tactic` : str
- `before_subgoals` : list of str
- `after_subgoals` : list of str
- `success` : bool
- `error_message` : str, optional
- `execution_time` : float, default 0.0

**display_current_state**(subgoals, proof_depth=0, tested_tactics=None)

Display current proof state.

- `subgoals` : list of str
- `proof_depth` : int, default 0
- `tested_tactics` : list of str, optional

**display_tactics_history**(limit=None)

Show tactic history.

- `limit` : int, optional

**display_session_summary**()

Print session summary.

**end_session**(success=False)

Finalize session.

- `success` : bool, default False

**export_session_data**(filepath)

Export session to JSON.

- `filepath` : str

---

### Functions

**create_visualizer**(max_subgoal_display=3, max_history_display=10)

Create ProofVisualizer instance.

- Returns: ProofVisualizer

---

## success_checker

### Functions

**has_error_output**(result)

Check if ReplResult contains actual error output.

- `result` : ReplResult
- Returns: bool

**is_syntax_successful**(result)

Check if syntax/parsing succeeded.

- `result` : ReplResult
- Returns: bool

**is_proof_progress**(before_subgoals, after_subgoals)

Determine if real proof progress was made.

- `before_subgoals` : list of str
- `after_subgoals` : list of str
- Returns: bool

**is_tactic_successful**(gym, result, before_subgoals=None)

Comprehensive success check (syntax + progress).

- `gym` : IsabelleGym
- `result` : ReplResult
- `before_subgoals` : list of str, optional
- Returns: bool

**get_error_message**(result)

Extract error message from ReplResult.

- `result` : ReplResult
- Returns: str

**get_output_message**(result)

Extract normal output message.

- `result` : ReplResult
- Returns: str

---

### SuccessResult

```python
class SuccessResult(result, gym, before_subgoals=None)
```

ReplResult wrapper with success checking.

**Parameters**

- `result` : ReplResult
- `gym` : IsabelleGym
- `before_subgoals` : list of str, optional

**Properties**

- `success` : bool - Automatically calls is_tactic_successful()

**Methods**

**separated_output**()

Proxy to result.separated_output().

- Returns: object

**total_output**()

Proxy to result.total_output().

- Returns: str