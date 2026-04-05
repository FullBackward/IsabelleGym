# Isabelle Server System
This is the implementation to the server-side IsabelleGym based small-step and big-step verifier.
The work is based on IsabelleGym 1.0 by Tom Milan (University of Cambridge) and IsabelleGym 2.0 by Zijing Li (University of Edinburgh).
This iteration focus on serverise the IsabelleGym REPL environment and provide a Isabelle/Scala based stepwise proof verification and whole theory file verification system
for training and evaluating LLM based prover, implemented by Xuanwei Ren (University of Edinburgh).

The system contains:
- a Scala / ML Isabelle REPL backend under `repl/`
- a FastAPI server under `server/`
- an async Python client under `client/`
- evaluation and benchmarking scripts under `evaluation/`

## Requirements

### Containerized stack used by the Dockerfile

- Python 3.12
- OpenJDK 21
- Isabelle 2025-2
- Gradle wrapper build under `repl/`

### Local setup expectations

If you are not using Docker, you will need at least:

- a recent Python 3 version
- JDK 17+
- Isabelle installed and available on `PATH`
- enough memory for multiple live Isabelle processes if you enable pooling

## Quick start with Docker

It is recommanded to run the server with Docker.

### 1. Build and start the container

```bash
docker compose up -d --build
```

or, on older setups:

```bash
docker-compose up -d --build
```

### 2. Open a shell inside the container

```bash
docker compose exec isabelle-gym bash
```

### 3. Confirm Python dependencies are installed

The Dockerfile already installs dependencies from `requirement.txt`.
That filename is **singular** in this repository.

If you need to reinstall them manually:

```bash
pip install -r requirement.txt
```

### 4. Start the server

From /app in docker container:

```bash
python -m server.app.main
```

You can also run it explicitly with Uvicorn:

```bash
uvicorn server.app.main:app --host 0.0.0.0 --port 8000
```

Or from outside the container, in repo_root on host:
```bash
docker compose exec isabelle-gym python -m server.app.main
```

### 5. Check that the server is healthy

From the host:

```bash
curl http://localhost:8000/
```

Expected response shape:

```json
{
  "service": "IsabelleGym Server",
  "version": "0.0.1",
  "status": "healthy",
  "active_sessions": 0,
  "busy_sessions": 0,
  "max_pool_size": 24,
  "timestamp": "..."
}
```

## Local installation

There is an `install.sh` script, but it is **not fully maintained for this server iteration**. If you still want to try a local setup, manual adjustment to the script is mandatory.

## Running the server

The FastAPI application lives in `server/app/main.py`.

Default runtime configuration comes from environment variables in `server/app/core/config.py`.

### Useful environment variables

```bash
ISABELLE_SERVER_HOST=0.0.0.0
ISABELLE_SERVER_PORT=8000
ISABELLE_POOL_SIZE=24
ISABELLE_INITIAL_SESSIONS=8
ISABELLE_IDLE_TIMEOUT=1800
ISABELLE_DEFAULT_FIELD=HOL
ISABELLE_ENABLE_CACHE=false
ISABELLE_MAX_CACHE_SIZE=1
ISABELLE_ENABLE_MEMORY_MANAGEMENT=true
ISABELLE_SHOW_STATES=false
ISABELLE_SERVER_LOG_LEVEL=INFO
```

### Example: start with a smaller pool

```bash
export ISABELLE_POOL_SIZE=4
export ISABELLE_INITIAL_SESSIONS=2
python -m server.app.main
```

## Client setup and import path

From the repo_root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirement.txt
python -m pip install -e .
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
```

## Server API Documentation
see `repo_root/Isabelle Server API Documentation.pdf`

## Python client usage

In `repo_root/client/async_client.py`

### Example: small-step session

```python
import asyncio
from client.async_client import IsabelleGymAsyncClient


async def main() -> None:
    async with IsabelleGymAsyncClient("http://localhost:8000") as client:
        created = await client.create_session(theories=["Main"], field="HOL")
        session_id = created["session_id"]
        lease_id = created["lease_id"]

        await client.enter_theory(session_id, "Scratch", lease_id=lease_id)

        result = await client.execute_command(
            session_id,
            'lemma "A ⟹ A" by simp',
            timeout=30.0,
            lease_id=lease_id,
        )
        print(result)

        await client.close_session(session_id, lease_id=lease_id)


asyncio.run(main())
```

### Example: big-step verification

```python
import asyncio
from client.async_client import IsabelleGymAsyncClient


THEORY = """
theory Scratch
  imports Main
begin

lemma "A ⟹ A"
  by simp

end
"""


async def main() -> None:
    async with IsabelleGymAsyncClient("http://localhost:8000") as client:
        response = await client.verify_bigstep_text(
            theory_name="Scratch",
            theory_text=THEORY,
            field="HOL",
            timeout=300.0,
        )
        print(response.json())


asyncio.run(main())
```

## Main components

### `repl/`
The Isabelle / Scala / ML backend and Python bridge.

Important pieces:

- `repl/Admin/init`: initializes the Isabelle component
- `repl/gradlew build`: builds the Scala side
- `repl/src/python/`: Python wrapper code around the REPL backend
- `repl/thys/IsabelleREPL.thy`: base theory used by server sessions

### `server/`
The HTTP service.

Important pieces:

- `server/app/main.py`: FastAPI application entrypoint
- `server/app/api/v1/router.py`: REST endpoints
- `server/app/services/session_manager.py`: pooled session lifecycle and lease management
- `server/app/services/session.py`: session-level small-step execution, checkpoints, rollback, proof state access
- `server/app/services/build_verify.py`: big-step verification via `isabelle build`

### `client/`
The async Python client.

- `client/async_client.py` provides an importable `IsabelleGymAsyncClient` for session creation, command execution, and big-step verification.

### `server_gym/`
A copy of old `isabelle_gym.py` and `success_checker.py` but modified as helper scipts for server implementation.

### `evaluation/`
Benchmark and analysis scripts.

This includes:

- local IsabelleGym evaluation
- server-client evaluation
- qIsabelle comparison scripts
- preprocessing helpers for theory corpora
- CSV / JSON exports for later analysis

## Evaluation scripts

### One-time setup for evaluation commands

```bash
cd /path/to/IsabelleGym
source .venv/bin/activate
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
mkdir -p evaluation/results
CORPUS="evaluation/HOL_corpus/Examples/processed"
OUT="evaluation/results"
```

The built-in `evaluation/HOL_corpus/Examples/processed/` directory is the safest small example corpus for smoke tests.

### Preprocessing helpers

#### `evaluation/scripts/process.py`
Rewrites local `Analysis`-style imports to fully qualified `HOL-Analysis.<Theory>` imports.

Example:

```bash
python -m evaluation.scripts.process \
  --analysis-dir /path/to/Isabelle2025-2/src/HOL/Analysis \
  --target evaluation/HOL_corpus/HOL-Analysis/raw \
  --output-dir evaluation/HOL_corpus/HOL-Analysis/processed
```

In-place rewrite example:

```bash
python -m evaluation.scripts.process \
  --analysis-dir /path/to/Isabelle2025-2/src/HOL/Analysis \
  --target evaluation/HOL_corpus/HOL-Analysis/raw \
  --inplace
```

#### `evaluation/scripts/clean_example_dir.py`
Removes document commands such as `text`, `section`, and `subsection` from `.thy` files.

Example:

```bash
python -m evaluation.scripts.clean_example_dir \
  --corpus evaluation/HOL_corpus/Examples/raw \
  --out-corpus evaluation/HOL_corpus/Examples/cleaned \
  --copy-non-thy
```

### Small-step evaluation

#### `evaluation/scripts/eval_smallstep_isabellegym.py`
Runs the local IsabelleGym baseline, aligned with the server small-step workflow.

Example:

```bash
python -m evaluation.scripts.eval_smallstep_isabellegym \
  --repo-root . \
  --corpus "$CORPUS" \
  --output "$OUT/smallstep_isabellegym_aligned.json"
```

Verbose step printing:

```bash
python -m evaluation.scripts.eval_smallstep_isabellegym \
  --repo-root . \
  --corpus "$CORPUS" \
  --print-steps \
  --output "$OUT/smallstep_isabellegym_aligned_verbose.json"
```

#### `evaluation/scripts/eval_smallstep_server_client_1_worker_no_reuse.py`
Runs the server-client small-step benchmark with a fresh session per theory.

Start the server first, then run:

```bash
python -m evaluation.scripts.eval_smallstep_server_client_1_worker_no_reuse \
  --corpus "$CORPUS" \
  --server http://localhost:8000 \
  --timeout 1200 \
  --field HOL \
  --output "$OUT/smallstep_server_no_reuse.json"
```

Verbose step printing:

```bash
python -m evaluation.scripts.eval_smallstep_server_client_1_worker_no_reuse \
  --corpus "$CORPUS" \
  --server http://localhost:8000 \
  --print-steps \
  --output "$OUT/smallstep_server_no_reuse_verbose.json"
```

#### `evaluation/scripts/eval_smallstep_server_client_with_reuse.py`
Runs the server-client small-step benchmark with pooled session reuse and parallel workers.

Start the server first, then run:

```bash
python -m evaluation.scripts.eval_smallstep_server_client_with_reuse \
  --corpus "$CORPUS" \
  --server http://localhost:8000 \
  --field HOL \
  --num-workers 4 \
  --output "$OUT/smallstep_server_with_reuse.json"
```

Verbose step printing:

```bash
python -m evaluation.scripts.eval_smallstep_server_client_with_reuse \
  --corpus "$CORPUS" \
  --server http://localhost:8000 \
  --num-workers 4 \
  --print-steps \
  --output "$OUT/smallstep_server_with_reuse_verbose.json"
```

#### `evaluation/scripts/eval_smallstep_qisabelle.py`
Runs the qIsabelle comparison benchmark.

This script assumes a qIsabelle HTTP service is already running on `--port`.

Example:

```bash
python -m evaluation.scripts.eval_smallstep_qisabelle \
  --corpus "$CORPUS" \
  --session-name HOL \
  --port 17000 \
  --master-dir /home/isabelle/ \
  --output "$OUT/smallstep_qisabelle.json"
```

If your theories depend on additional session roots, repeat `--session-root`:

```bash
python -m evaluation.scripts.eval_smallstep_qisabelle \
  --corpus "$CORPUS" \
  --session-name HOL-Analysis \
  --session-root /path/to/AFP/thys \
  --session-root /path/to/other/session/root \
  --port 17000 \
  --master-dir /home/isabelle/ \
  --output "$OUT/smallstep_qisabelle_with_roots.json"
```

### Big-step evaluation

#### `evaluation/scripts/eval_bigstep_isabelle_build.py`
Runs whole-theory verification directly through `isabelle build`.

Example:

```bash
python -m evaluation.scripts.eval_bigstep_isabelle_build \
  --corpus "$CORPUS" \
  --isabelle-bin "$(which isabelle)" \
  --parent-session HOL \
  --jobs 4 \
  --output "$OUT/bigstep_isabelle_build.json"
```

For the processed `HOL-Analysis` corpus, use `--parent-session HOL-Analysis`.

#### `evaluation/scripts/eval_bigstep_server_client_ver.py`
Runs whole-theory verification through the server big-step endpoint.

Start the server first, then run:

```bash
python -m evaluation.scripts.eval_bigstep_server_client_ver \
  --corpus "$CORPUS" \
  --server http://localhost:8000 \
  --timeout 1800 \
  --field HOL \
  --output "$OUT/bigstep_server_client.json"
```

For the processed `HOL-Analysis` corpus, use `--field HOL-Analysis`.

### Helper modules used by the evaluators

These files are imported by the other scripts and do not currently expose a standalone CLI entrypoint:

- `evaluation/scripts/eval_stats.py`
- `evaluation/scripts/theory_splitter.py`

## Troubleshooting

### “Bad component...” during backend setup

A repository note already mentions this failure mode during Scala / Isabelle component compilation. If it happens, one workaround noted in the project is to create an empty main file under Isabelle’s `Admin/components` path before rebuilding.

### Server starts but commands fail immediately

Check the following inside the container:

```bash
which isabelle
java -version
python --version
./repl/gradlew build
```

Also make sure the Isabelle component initialization step completed successfully:

```bash
./repl/Admin/init
```

### `pip install -r requirements.txt` fails

This repository uses `requirement.txt`, not `requirements.txt`.

### Docker container is up but the API is not responding

The compose setup does not automatically launch Uvicorn. Open a shell in the container and start the server manually.


## Comprehensive detailed file Structure
A comprehensive file structure summary, this is due to the fact that no complete file structure specification was found in the previous iterations. Therefore, I feel it necessary to illustrate the whole picture and highlight key directories and files to increase the readability of this repo.

```
repo_root/
├── client/
|   ├── __init__.py
|   └── async_client.py
├── evaluation/
|   ├── analysis_exports/
|   ├── benchmark/
|   ├── HOL_corpus/
|   ├── local_gym/
|   ├── runs/
|   ├── scripts/
|   |   ├── clean_example_dir.py
|   |   ├── eval_bigstep_isabelle_build.py
|   |   ├── eval_bigstep_server_client_ver.py
|   |   ├── eval_smallstep_isabellegym.py
|   |   ├── eval_smallstep_qisabelle.py
|   |   ├── eval_smallstep_server_client_1_worker_no_reuse.py
|   |   ├── eval_smallstep_server_client_with_reuse.py
|   |   ├── eval_stats.py
|   |   ├── process.py
|   |   └── theory_splitter.py
|   ├── runs_analysis.ipynb
|   └── Server_Concurrency.thy
├── previous works/
|   ├── IsabelleGym1.0/
|   |   ├── IsabelleGym/
|   |   └── IsabelleGym.pdf
|   └── msc_20257720.pdf
├── repl/
|   ├── Admin/
|   ├── etc/
|   ├── gradle/
|   ├── python/
|   ├── src/
|   |   ├── main/
|   |   |   └── scala/
|   |   |   |   └── repl/
|   |   |   |       ├── document_utils.scala
|   |   |   |       ├── edit_utils.scala
|   |   |   |       ├── repl_backend_gateway.scala
|   |   |   |       ├── repl_backend.scala
|   |   |   |       ├── repl_ml_communication.scala
|   |   |   |       ├── repl_output.scala
|   |   |   |       ├── repl_session.scala
|   |   |   |       ├── server_utils.scala
|   |   |   |       ├── session_manager.scala
|   |   |   |       ├── thy_info.scala
|   |   |   |       ├── thy_parsing.scala
|   |   |   |       ├── thy_status.scala
|   |   |   |       └── vector_env.scala
|   |   ├── ml/
|   |   |   └── REPL.ML
|   |   └── python/
|   |       ├── isabelle_client.py
|   |       ├── isabelle_repl.py
|   |       ├── operation.py
|   |       ├── repl_backend_gateway.py
|   |       ├── session_manager.py
|   |       └── thy_init.py
|   ├── thys/
|   ├── __init__.py
|   ├── build.gradle
|   ├── demo_repl.py
|   ├── gradlew
|   ├── gradlew.bat
|   ├── README.md
|   ├── settings.gradle
|   └── temp.txt
├── server/
|   └── app/
|       ├── api/
|       |   └── v1/
|       |   |   ├── schemas
|       |   |   ├── router.py
|       |   |   └── ws.py
|       ├── core/
|       |   ├── config.py
|       |   └── logging.py
|       ├── services/
|       |   ├── build_verify.py
|       |   ├── internal_models.py
|       |   ├── session_manager.py
|       |   ├── session.py
|       |   ├── theory_chunks.py
|       |   ├── theory_parsing.py
|       |   └── threaded_backend.py
|       ├── dependencies.py
|       ├── errors.py
|       └── main.py
├── server_gym/
|   ├── isabelle_gym.py
|   └── success_checker.py
├── docker-compose.yml
├── Dockerfile
├── install.sh
├── pyproject.toml
├── README.md
└── requirement.txt
```