# Add AutoCorrode I/R to the small-step evaluation

## 1) Clone AutoCorrode
```bash
git clone https://github.com/awslabs/AutoCorrode.git
cd AutoCorrode
```

## 2) Install Isabelle and AFP dependency
AutoCorrode expects Isabelle2025-2 and the AFP `Word_Lib` component.

Set:
```bash
export ISABELLE_HOME=/path/to/Isabelle2025-2/bin
export AFP_COMPONENT_BASE=/path/to/afp
```

Register `Word_Lib`:
```bash
$ISABELLE_HOME/isabelle components -u "$AFP_COMPONENT_BASE/Word_Lib"
```

## 3) Install Python deps for I/R
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r ir/requirements.txt
```

## 4a) For apples-to-apples comparison with your existing HOL small-step corpus
You can run I/R on the `HOL` session directly:
```bash
python3 ir/repl.py \
  --isabelle "$ISABELLE_HOME/isabelle" \
  --session HOL
```

## 4b) For AutoCorrode theories / AutoCorrode session
Build the AutoCorrode session first:
```bash
make build
# or
$ISABELLE_HOME/isabelle build -b -d . AutoCorrode
```

Then run I/R against that session:
```bash
python3 ir/repl.py \
  --isabelle "$ISABELLE_HOME/isabelle" \
  --session AutoCorrode \
  --dir "$(pwd)"
```

## 5) Run the evaluator
The evaluator starts I/R itself, so you do not need to start `repl.py` manually when using the script.

### Existing IsabelleGym small-step corpus
```bash
python3 /mnt/data/eval_smallstep_autocorrode_ir.py \
  --ir-root /path/to/AutoCorrode \
  --isabelle-bin "$ISABELLE_HOME/isabelle" \
  --session-name HOL \
  --corpus /path/to/IsabelleGym/evaluation/HOL_corpus/Examples \
  --output smallstep_autocorrode_ir_results.json
```

### AutoCorrode session / AutoCorrode theory corpus
```bash
python3 /mnt/data/eval_smallstep_autocorrode_ir.py \
  --ir-root /path/to/AutoCorrode \
  --isabelle-bin "$ISABELLE_HOME/isabelle" \
  --session-name AutoCorrode \
  --session-dir /path/to/AutoCorrode \
  --corpus /path/to/your/theories \
  --output smallstep_autocorrode_ir_results.json
```

## Important comparison note
I/R does **not** accept `theory ... begin` as a small-step command. The evaluator therefore compares:
- `Ir.init(imports)`
- replay of top-level body blocks

It does **not** send the theory header or final `end` as steps.
