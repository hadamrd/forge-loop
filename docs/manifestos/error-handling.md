# Error-handling manifesto

Rule IDs in this file use the prefix `EH-`.

## EH-001: No silent broad excepts

`except Exception: pass` (or `except BaseException: pass`) is forbidden in
committed code. It hides real bugs behind a green CI. If you genuinely want
to swallow an exception, you must:

1. catch the *specific* exception class you expect, and
2. log it (or otherwise surface it) with enough context to debug.

Severity: **sev1** — blocks auto-merge.

## EH-002: No bare `except:`

`except:` (no exception class) catches `KeyboardInterrupt` and `SystemExit`
and is almost never what you want. Use `except Exception:` at minimum, and
prefer a specific class.

Severity: **sev1**.

## EH-003: No `print` debugging in committed code

Leftover `print(...)` statements used for ad-hoc debugging should not ship.
Use the project's logger.

Severity: **sev2**.
