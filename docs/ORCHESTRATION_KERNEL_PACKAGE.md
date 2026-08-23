# Orchestration Kernel Workspace

`packages/orchestration` is a local Python workspace package named
`lumi-orchestration`.  It is the home for deterministic, business-agnostic
orchestration semantics:

```text
packages/orchestration/
  pyproject.toml
  src/lumi_orch/
    dag.py          # graph invariants
    escalation.py   # L1/L2/L3 data protocol
    admission.py    # Redis-agnostic admission lease protocol
    resources.py    # resource-claim and distributed lease protocol
    effects.py      # two-phase effect-journal state transitions
    lifecycle.py    # job lifecycle transition contract
    errors.py        # stable error taxonomy and recovery metadata
    replanning.py    # bounded replan safety decisions
    logical_plan.py  # rolling-plan progress and bounded frontier selection
    validation.py    # backend-neutral validation outcome contract
    manifest.py      # rolling checklist cursor and progress semantics
    plan_dsl.py      # typed input/output/risk contract for static plans
    policy/tca_models.py # constrained TCA weight/threshold schema
    runner.py       # timeout selection and channel lease protocol
    ports.py         # Worker, review and state-store boundaries
    policy/         # typed YAML models, matcher and hook registry
  tests/            # no app imports
```

`app/agents/orchestration` is now the adapter and business-service layer. It
owns Redis client construction, runtime settings, monitoring, office document
hooks, Skills, Worker implementations, state persistence and the concrete node
execution loop. It may import
`lumi_orch`; `lumi_orch` must never import `app`.

The migration is complete at the invariant boundary. The kernel owns DAG
structural validation and dependency scheduling, escalation data validation,
resource and channel lease protocols, admission capacity semantics, two-phase
effect state transitions, timeout selection, logical-plan/manifest contracts,
typed static-plan DSL and the routing-policy matcher. The Lumi side owns only
business adapters: Redis/PostgreSQL clients, settings, monitoring, Worker and
Skill implementations, document hooks and runtime selection. Do not duplicate
an invariant in both layers: it belongs in the package, while infrastructure
code remains in `app`.

## Local commands

```powershell
uv sync
uv run pytest tests packages/orchestration/tests -q
uv run python -c "import lumi_orch; print(lumi_orch.__file__)"
```

`uv.lock` must be regenerated whenever this package's `pyproject.toml` or the
root workspace membership changes.  CI must not use the broad `--no-sources`
flag because it would also ignore this local workspace member; only the
Windows-only Torch sources are disabled there.

The Docker build installs `./packages/orchestration` before the application.
This is required because plain `pip install .` does not understand uv's local
workspace source mapping.
