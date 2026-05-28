# forge-loop testing manifesto (seed)

This is the dogfood testing manifesto for forge-loop itself. It exists
because three of this week's bugs (#97, #120, #128) all shared the same
root cause: tests covered the happy edge of a transition and silently
ignored the default / fallthrough branch. The rules below would have
caught all three.

## Rules

### T1. State machine ⇒ ONE TEST PER EDGE + ONE fallthrough adversarial test per default-branch.

If a function is a state machine — `match`, `if/elif/elif/else`, a
dispatch table over an enum — there MUST be one test per edge AND one
adversarial test for each `else` / default arm that asserts the
fallthrough is reached on at least one realistic input that doesn't
match any explicit branch.

**Rationale:** would have caught #97, #120, AND #128. All three shipped
because the test suite covered the explicit cases and assumed the
default arm was unreachable.

### T2. External-dep assumption ⇒ ONE adversarial test for the false case.

Any time the production code asks an external system a yes/no question
— "does `origin/<branch>` exist?", "is the `gh` CLI alive?", "is this
file readable?" — there MUST be a test asserting correct behaviour when
the answer is **no**. The happy "yes" test alone is not sufficient.

**Rationale:** the iteration probe found ~6 places where forge-loop
assumed an external check returned the answer it wanted, with no test
for the negative branch.

### T3. `subprocess.returncode` handling ⇒ test BOTH `==0` and `!=0` branches.

Any call site that inspects `CompletedProcess.returncode` MUST have
explicit tests for both the success (`==0`) and the failure (`!=0`)
branches. Stderr-only failure modes (returncode 0 but stderr non-empty)
also get a test if the production code looks at stderr.

**Rationale:** see #128 specifically. The bug was a silent `!=0` branch
that fell through to "success" because the test only exercised the
returncode=0 path.

### T4. Every Protocol Fake ⇒ a regression test that asserts the Real impl returns the same shape on representative inputs.

For every `Fake*` adapter under `forge_loop/_testing/`, there MUST be a
contract test that runs the **real** implementation on a representative
input (a tmpdir, a tiny mock server, a recorded fixture) and asserts
its output shape matches what the Fake produces. This keeps Fakes from
drifting away from reality.

**Rationale:** Fakes that diverge from Reals produce tests that pass
locally and fail in production. The contract test is cheap insurance.

### T5. Property-based tests on any function with >4 branches OR any function consuming user input.

If a pure function has more than four distinct branches, OR if it
consumes any user-supplied string / bytes (CLI arg, env var, file
content, network payload), it MUST have a `hypothesis` property-based
test. The property need not be deep — "does not raise on any
`st.text()`" is acceptable — but it MUST exist.

**Rationale:** see #102. A property test caught a surrogate-codepoint
crash in the fingerprint function that no example-based test would
have surfaced.

### T6. Every infinite-loop guard ⇒ adversarial test that the guard actually fires.

Any `while True`, recursive descent, retry loop, or polling loop with a
max-iteration / max-attempts / deadline guard MUST have an adversarial
test that drives the function past the guard and asserts the guard
fires (raises, breaks, returns) instead of looping forever. The test
runs under a wall-clock timeout so a regression hangs the test, not
CI.

**Rationale:** "trust me, this loop terminates" has cost the project
two outages. The guard is part of the contract; test it.

## How to apply this manifesto

* Every new test file is reviewed against these six rules. Missing
  coverage of a rule that applies to the diff is a blocking review
  comment.
* The critic agent loads this file when reviewing forge-loop PRs and
  flags missing branches as P0 findings.
* New rules require a named incident or issue number in the rationale,
  same as the quality manifesto.
