# forge-loop quality manifesto (seed)

This is the dogfood quality manifesto for forge-loop itself. Every future
forge-loop change is gated by these rules. Each rule is followed by a
rationale that names the iteration-probe bug or persistent-worker work
that motivated it, so future contributors understand *why* the rule
exists, not just what it says.

## Rules

### Q1. No shared mutable module-level state. Use a Container or per-instance State.

Module-level globals (caches, counters, "current run" dicts, singleton
queues) make tests order-dependent and make the worker un-restartable in
process. All mutable state MUST hang off a `Container` or a per-instance
`State`/`Runner` object that can be re-created cheaply.

**Rationale:** see #100 (Runner class). The iteration-probe leaked state
between sprints because the runner held onto module-level dicts; the
persistent-worker refactor wedged that into per-instance state and the
class of bug went away.

### Q2. Every external I/O boundary lives behind a typed Protocol with a Fake for tests.

If forge-loop talks to the network, the filesystem, a subprocess, an LLM
API, or `gh`, the call MUST go through a `typing.Protocol` adapter with a
companion `Fake*` implementation in `forge_loop/_testing/`. Tests use the
Fake. Production wires the Real. No ad-hoc `httpx.get` / `subprocess.run`
calls scattered through business logic.

**Rationale:** see #104 (adapters). Untyped boundaries are exactly where
mocks drift away from reality and where flaky tests breed.

### Q3. Single config source of truth via `Settings`. No `os.environ.get` outside `settings.py`.

Configuration is read in exactly one place: `forge_loop/config.py`'s
`Settings` (or its successor `settings.py`). Reaching for
`os.environ.get(...)` anywhere else is a lint-level violation. Tests
override config by constructing a `Settings` instance, not by mutating
`os.environ`.

**Rationale:** see #98. The iteration probe found two different code
paths reading the same env var with different defaults, producing
divergent behaviour between the CLI and the worker.

### Q4. Typed events for every state change. No untyped `**fields` for kinds that have a registered model.

If an event `kind` has a registered pydantic / dataclass model, the
event MUST be constructed via that model. Passing arbitrary `**fields`
into a generic `emit()` for a known kind is forbidden — it defeats
schema validation and lets typos through silently.

**Rationale:** see #99. A misspelled field (`attempt_no` vs `attempt`)
silently fell through `**fields` and produced empty dashboard rows for
two days before anyone noticed.

### Q5. No `subprocess.run` for SDK-able external services.

If a service has a first-class Python SDK (Anthropic, GitHub), use the
SDK. Shelling out to `claude` / `gh` from inside forge-loop is
forbidden in new code. Existing shell-outs must migrate (#103 critic
SDK, #105 GhClient).

**Rationale:** subprocess wrappers hide returncode handling bugs
(#128), eat structured errors, and make tests painful (you end up
mocking argv strings instead of method calls).

## How to apply this manifesto

* When reviewing a forge-loop PR, scan the diff for each rule. A
  violation is a blocking review comment, not a nit.
* When the critic agent reviews a forge-loop PR, it loads this file and
  flags violations as P0 findings.
* When adding a new rule, add a rationale paragraph that names the
  concrete issue or incident. No rationale ⇒ no rule.
