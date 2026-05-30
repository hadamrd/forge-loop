# Testing manifesto

Rule IDs in this file use the prefix `TE-`.

## TE-001: Tests must assert behaviour, not mock returns

A test that only asserts the return value of a mock it just configured is a
tautology. Assert on observable behaviour: state changes, emitted events,
side effects against a fake collaborator.

Severity: **sev1**.

## TE-002: Cover the sad path

Every new public function needs at least one adversarial test: bad input,
missing config, partial failure, or empty list. A happy-path-only test
suite ships bugs.

Severity: **sev2**.
