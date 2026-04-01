# IsabelleGym evaluation setup and minimal scripts

This note is designed to accompany the revised report draft. It separates **big-step** and **small-step** evaluation and gives one minimal script per tool.

## 1. Recommended evaluation structure

# Excluded ML.thy, Commands.thy for smallstep benchmark

### Big-step evaluation
Goal: validate complete theory files.

- **Baseline:** native `isabelle build`
- **System under test:** IsabelleGym Server `/api/v1/sessions/bigstep`
- **Corpus:** `evaluation/HOL_corpus/HOL-Analysis/processed`

This is the cleanest way to test whole-theory verification because both tools operate on full theory files.

### Small-step evaluation
Goal: measure interactive command replay.

- **Tool 1:** local IsabelleGym
- **Tool 2:** QIsabelle
- **Tool 3:** IsabelleGym Server
- **Corpus:** `evaluation/HOL_corpus/Examples`

The scripts replay each theory as:
1. theory header (`theory ... imports ... begin`)
2. a sequence of top-level body blocks
3. final `end`

That gives one comparable command stream across tools.

---

## 2. Shared prerequisites

### Repo-local IsabelleGym / Server
From the repository root:

```bash
chmod +x install.sh
./install.sh
```

or:

```bash
docker-compose up -d
pip install -r requirement.txt
```

If the Scala backend complains about a missing component, the repository README suggests creating an empty main file under `isabelle/Admin/components`.

### Corpora in this repo
- Small-step corpus: `evaluation/HOL_corpus/Examples`
- Big-step corpus: `evaluation/HOL_corpus/HOL-Analysis/processed`

---

## 3. Big-step baseline: native Isabelle build

### Setup
You need a working Isabelle installation and the `HOL-Analysis` session available.

Example sanity check:

```bash
isabelle version
isabelle build -b HOL-Analysis
```

### Minimal script
Use `eval_bigstep_isabelle_build.py`.

Example:

```bash
python /mnt/data/isabellegym_artifacts/eval_bigstep_isabelle_build.py \
  --corpus /tmp/IsabelleGym/evaluation/HOL_corpus/HOL-Analysis/processed \
  --isabelle-bin isabelle \
  --parent-session HOL-Analysis \
  --jobs 8 \
  --output build_bigstep_results.json
```

python ./eval_bigstep_isabelle_build.py --corpus ./HOL_corpus/HOL-Analysis/processed --isabelle-bin isabelle --parent-session HOL-Analysis --jobs 8 --output build_bigstep_results.json

python ./eval_bigstep_isabelle_build.py --corpus ./HOL_corpus/HOL-Analysis/processed --isabelle-bin isabelle --parent-session HOL-Analysis --jobs 8 --output ./runs/isabelle_build_bigstep_results_run_5.json

What it measures:
- success / failure per file
- wall-clock time per file
- total corpus time

---

## 4. Big-step system: IsabelleGym Server

### Setup
From the repository root:

```bash
docker-compose up -d
cd server/app
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Sanity check:

```bash
curl http://localhost:8000/
```

### Minimal script
Use `eval_bigstep_server.py`.

Example:

```bash
python /mnt/data/isabellegym_artifacts/eval_bigstep_server.py \
  --corpus /tmp/IsabelleGym/evaluation/HOL_corpus/HOL-Analysis/processed \
  --server http://localhost:8000 \
  --output server_bigstep_results.json
```

python .\eval_bigstep_server.py --corpus .\HOL_corpus\HOL-Analysis\processed\ --output .\runs\bigstep_server_build\server_build_bigstep_results_run_6.json

What it measures:
- success / failure per file
- server-side execution time returned by the API
- client-observed wall-clock time
- total corpus time

---

## 5. Small-step tool: local IsabelleGym

### Setup
Run from the IsabelleGym repository root so Python can import the local package tree.

### Minimal script
Use `eval_smallstep_isabellegym.py`.

Example:

```bash
python /mnt/data/isabellegym_artifacts/eval_smallstep_isabellegym.py \
  --repo-root /tmp/IsabelleGym \
  --corpus /tmp/IsabelleGym/evaluation/HOL_corpus/Examples \
  --output smallstep_isabellegym_results.json
```

python ./eval_smallstep_isabellegym.py --repo-root ./local_gym/ --corpus ./HOL_corpus/Examples/processed --output ./runs/smallstep_local_gym/local_isabellegym_smallstep_results_run_6.json --print-steps

python ./eval_smallstep_isabellegym.py --repo-root ./local_gym/ --corpus ./HOL_corpus/Examples/tests --output ./runs/smallstep_local_gym/local_isabellegym_smallstep_test.json --print-steps

What it measures:
- backend startup time
- per-step acceptance
- per-step latency
- per-theory completion

---

## 6. Small-step tool: IsabelleGym Server

### Setup
Start the server as above.

### Minimal script
Use `eval_smallstep_server.py`.

Example:

```bash
python /mnt/data/isabellegym_artifacts/eval_smallstep_server.py \
  --corpus /tmp/IsabelleGym/evaluation/HOL_corpus/Examples \
  --server http://localhost:8000 \
  --output smallstep_server_results.json
```

python eval_smallstep_server.py --server http://localhost:8000 --corpus HOL_corpus/Examples/processed --field HOL --timeout 1200 --output ./runs/smallstep_server/worker_8_reuse_true/server_smallstep_results_run_1.json --print-steps

What it measures:
- session creation time
- per-step acceptance
- per-step latency
- per-theory completion

---

## 7. Small-step tool: QIsabelle

### Setup
Clone QIsabelle and follow its heap download instructions.

```bash
git clone https://github.com/marcinwrochna/qisabelle.git
cd qisabelle
source .env
# download AFP files and heaps as described in the QIsabelle README
docker-compose up
```

Then run the Python client from another shell to confirm the service is reachable.

### Minimal script
Use `eval_smallstep_qisabelle.py`.

Example:

```bash
python /mnt/data/isabellegym_artifacts/eval_smallstep_qisabelle.py \
  --qisabelle-root /path/to/qisabelle \
  --corpus /tmp/IsabelleGym/evaluation/HOL_corpus/Examples \
  --session-name HOL \
  --output smallstep_qisabelle_results.json
```
python ./eval_smallstep_qisabelle.py --qisabelle-root ./ --corpus ../IsabelleGym/evaluation/HOL_corpus/Examples/processed --session-name HOL --port 17000 --output ../IsabelleGym/evaluation/runs/smallstep_local_qisabelle/local_qisabelle_smallstep_results_run_1.json


What it measures:
- client startup time
- per-step acceptance
- per-step latency
- per-theory completion

---

## 8. Suggested tables and figures for the report

### Table A: Big-step summary
- tool
- files checked
- successes
- failures
- mean wall-clock time
- median wall-clock time
- total corpus time

### Table B: Small-step summary
- tool
- theories attempted
- theories completed
- commands attempted
- commands accepted
- mean step latency
- mean startup time

### Figure A
Histogram or violin plot of **per-file big-step time**.

### Figure B
Box plot of **per-command small-step latency**.

### Figure C
Throughput vs worker count for the server in:
- fresh-session mode
- reuse mode

---

## 9. Notes from code inspection

- The strongest implementation contribution is in `server/app/`, especially:
  - `session_manager.py`
  - `session.py`
  - `threaded_backend.py`
  - `api/v1/router.py`

- The most reportable design choices are:
  - per-session serialisation via a dedicated worker thread
  - dependency-aware session reuse
  - Python-side LRU pool management
  - checkpoint/restore around big-step verification

- The existing `evaluation/server_benchmark.py` should not be treated as the final benchmark artifact. It currently needs cleaning before use.
