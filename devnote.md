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

## Challenges
### Scalable and iterable file structure for server
This is trivial, not suitable to write about

### Concurrency-safe and predictablility of session manager
This should be the main focus on challenges

### Protocol between Python end and Scala end, and how it is optimised in Isabelle's end

### LRU optimiseation
Use OrderedDict instead of normal Dict. Every access to session put that session at the tail of the Dict, and the first Dict will be evicted when needed. Normal Dict approach involves scanning and comparision, that takes O(n), but OrderedDict will only take O(1).

The previous iteration of this project uses a sort function to sort the access time of the sessions, which is highly inefficient, and consider it is implemented in Scala end, the management and supervise of the running of the system will be difficult.

Advantages:
1. Simplier, less likely to have bugs in high concurrency situation 
2. Does not rely on system time
3. O(1) vs O(n)


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


theory Seq imports Main begin datatype 'a seq = Empty | Seq 'a "'a seq" fun conc :: "'a seq ⇒ 'a seq ⇒ 'a seq" where "conc Empty ys = ys" | "conc (Seq x xs) ys = Seq x (conc xs ys)" fun reverse :: "'a seq ⇒ 'a seq" where "reverse Empty = Empty" | "reverse (Seq x xs) = conc (reverse xs) (Seq x Empty)" lemma conc_empty: "conc xs Empty = xs" by (induct xs) simp_all lemma conc_assoc: "conc (conc xs ys) zs = conc xs (conc ys zs)" by (induct xs) simp_all lemma reverse_conc: "reverse (conc xs ys) = conc (reverse ys) (reverse xs)" by (induct xs) (simp_all add: conc_empty conc_assoc) lemma reverse_reverse: "reverse (reverse xs) = xs" by (induct xs) (simp_all add: reverse_conc) end

theory Seq imports Main begin datatype 'a seq = Empty | Seq 'a \"'a seq\" fun conc :: \"'a seq ⇒ 'a seq ⇒ 'a seq\" where \"conc Empty ys = ys\" | \"conc (Seq x xs) ys = Seq x (conc xs ys)\" fun reverse :: \"'a seq ⇒ 'a seq\" where \"reverse Empty = Empty\" | \"reverse (Seq x xs) = conc (reverse xs) (Seq x Empty)\" lemma conc_empty: \"conc xs Empty = xs\" by (induct xs) simp_all lemma conc_assoc: \"conc (conc xs ys) zs = conc xs (conc ys zs)\" by (induct xs) simp_all lemma reverse_conc: \"reverse (conc xs ys) = conc (reverse ys) (reverse xs)\" by (induct xs) (simp_all add: conc_empty conc_assoc) lemma reverse_reverse: \"reverse (reverse xs) = xs\" by (induct xs) (simp_all add: reverse_conc) end

theory Seq imports Main begin datatype 'a seq = Empty | Seq 'a "'a seq" fun conc :: "'a seq ⇒ 'a seq ⇒ 'a seq" where "conc Empty ys = ys" | "conc (Seq x xs) ys = Seq x (conc xs ys)" fun reverse :: "'a seq ⇒ 'a seq" where "reverse Empty = Empty" | "reverse (Seq x xs) = conc (reverse xs) (Seq x Empty)" lemma conc_empty: "conc xs Empty = xs" by (induct xs) simp_all lemma conc_assoc: "conc (conc xs ys) zs = conc xs (conc ys zs)" by (induct xs) simp_all lemma reverse_conc: "reverse (conc xs ys) = conc (reverse ys) (reverse xs)" lemma reverse_reverse: "reverse (reverse xs) = xs" by (induct xs) (simp_all add: reverse_conc) end

theory Algebra imports Sylow Chinese_Remainder Zassenhaus Galois_Connection Generated_Fields Free_Abelian_Groups Divisibility Embedded_Algebras IntRing Sym_Groups Exact_Sequence Polynomials Algebraic_Closure Left_Coset SimpleGroups SndIsomorphismGrp begin

Windows:\
python process.py --analysis-dir .\HOL_corpus\raw --target .\HOL_corpus\raw --output-dir .\HOL_corpus\processed

Linux:\
python evaluation/server_benchmark.py --corpus evaluation/HOL_corpus/processed --server-host localhost --server-port 8000 --step bigstep --max-workers 10 --batch-size 8 --output ./outputs/bigstep_results.json

Windows:\
python evaluation/server_benchmark.py --corpus .\HOL_corpus\processed --server-host localhost --server-port 8000 --step bigstep --max-workers 10 --batch-size 8 --output .\outputs\bigstep_results.json