"""
Sliding-Puzzle IsabelleGym Demo
Usage
-----
python -m sliding_puzzle.demo            # default 3×3, scramble depth ≤5
python -m sliding_puzzle.demo --size 4   # 4×4 puzzle
python -m sliding_puzzle.demo --scramble 7 --max-steps 80
"""

from __future__ import annotations

import argparse
from typing import Optional

from sliding_puzzle.puzzle_env import PuzzleEnv, generate_scrambled_board
from sliding_puzzle.agent import Agent, Move


def run_demo(
    puzzle_size: int = 3,
    scramble_depth: int = 5,
    max_steps: int = 100,
    num_simulations: Optional[int] = None,
    model_path: Optional[str] = None,
) -> None:
    """Run one end-to-end attempt at solving a random sliding puzzle."""

    env = PuzzleEnv(puzzle_size=puzzle_size)
    agent = Agent(size=puzzle_size, model_path=model_path)

    if num_simulations is not None:
        from sliding_puzzle.mcts import MCTS

        agent.mcts = MCTS(env, agent, num_simulations=num_simulations)

    start_board = generate_scrambled_board(puzzle_size, scramble_depth)
    print(f"Start board (≤{scramble_depth} scrambles): {start_board}")
    env.begin_proof_for_start_state(start_board)

    # loop until solved or run out of steps ------------------------------
    steps = 0
    last_move: Optional[Move] = None

    while steps < max_steps and not env.goal_board_reached():
        board = env.get_current_board()
        move = agent.select_move(env, board, last_move)
        env.make_move(move, board)

        steps += 1
        last_move = move
        print(f"Step {steps:02d} | {move.name:<5} | {env.get_current_board()}")

    # report outcome
    if env.goal_board_reached():
        print(f"\nSolved in {steps} steps")
    else:
        print(f"\nFailed to solve within {max_steps} steps.")

    print("\nIsabelle theory excerpt (truncate):")
    print(env.gym.get_source().total_output().splitlines()[-20:])

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sliding-Puzzle IsabelleGym demo")
    parser.add_argument("--size", type=int, default=3, help="n for n×n puzzle")
    parser.add_argument("--scramble", type=int, default=15, help="scramble depth")
    parser.add_argument("--max-steps", type=int, default=500, help="max search steps")
    parser.add_argument(
        "--sims", type=int, default=None, help="MCTS simulations per move"
    )
    parser.add_argument(
        "--model", type=str, default="agent.pt", help="Path to a trained model checkpoint"
    )

    args = parser.parse_args()
    run_demo(
        puzzle_size=args.size,
        scramble_depth=args.scramble,
        max_steps=args.max_steps,
        num_simulations=args.sims,
        model_path=args.model,
    ) 