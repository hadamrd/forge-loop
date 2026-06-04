# Worker permission profiles

**Status:** `full` profile implemented + validated (it's the unchanged default).
`standard` / `readonly` wired but experimental — not yet behaviorally validated
(see "Validation status" below).

## Problem

Workers run with full host access — codex with `danger-full-access`
+ `--dangerously-bypass-approvals-and-sandbox`, claude with
`permission_mode=bypassPermissions` — as the operator's own user, no sandbox.
That is the right default for a trusted single-operator box (the worker should
run like the operator's own interactive agent), but it was **hardcoded**: there
was no way to dial a worker down when running less-trusted issues, and no path
toward the eventual distributable/multi-tenant story.

The `CapabilityPolicy` (filesystem/network/secrets) already existed but is
*advisory only* — it is rendered into the brief as text; nothing enforces it.

## Decision

Introduce a single config knob — `worker.permissions` — with three profiles,
each mapped onto the agent backend's **native** enforcement (no bespoke
sandbox). Reusing the Claude Agent SDK / Codex CLI mechanisms keeps the surface
tiny and correct.

| Profile | Claude SDK | Codex CLI |
|---|---|---|
| `full` *(default)* | `permission_mode=bypassPermissions`, no sandbox | `-s danger-full-access --dangerously-bypass-approvals-and-sandbox` |
| `standard` | `permission_mode=bypassPermissions` + `sandbox={enabled, autoAllowBashIfSandboxed}` | `-s workspace-write` |
| `readonly` | `permission_mode=plan` | `-s read-only` |

`forge_loop.worker_permissions` is the single source of truth
(`claude_permission_options()` / `codex_sandbox_args()` / `normalize_profile()`).

## Guarantees

- **`full` is byte-identical to the historical behaviour.** It emits no
  `sandbox` key and the exact prior codex flags, so existing runs are
  unchanged. `full` stays the default and the only lived-in path until
  forge-loop targets untrusted execution.
- **Unknown/empty profiles degrade to `full`**, never error at runtime
  (config-load validation still rejects a typo'd profile early).
- **Old SDKs degrade gracefully**: `sandbox` is in `_worker_sdk`'s
  `_OPTIONAL_KNOBS`, so an SDK too old to accept it strips the kwarg rather
  than crashing the worker.

## Validation status — READ THIS

Only `full` is **behaviorally validated** (it is the historical path, unchanged).

`standard` and `readonly` are **wired but NOT yet end-to-end validated** — the
tests assert the option *mapping*, not that a sandboxed worker can actually
finish a task. Treat them as **experimental** until a real sandboxed run is
tuned and green.

Known gap: the current `standard` profile sets `sandbox.enabled` +
`autoAllowBashIfSandboxed` but **no `network` allow-list**. The default egress
policy will very likely block `git push` / `gh pr create` (and any dependency
fetch the build needs), so a `standard` worker may complete its edits and then
fail to ship a PR. Making `standard` genuinely usable requires, at minimum:

- `sandbox.network.allowedDomains` covering `github.com`, `api.github.com`,
  `*.githubusercontent.com`, **plus the project's package registries**
  (pypi/npm/crates/goproxy/apt…). This set is inherently per-project — a single
  global `standard` cannot be both tight and universally working.
- writable roots covering the worktree (`/tmp/forge-<repo>/wt-loop-*`), git's
  config, and `/tmp`.

The validation task is its own follow-up: run a worker under `standard` against
a throwaway issue, watch what the sandbox denies, and widen the allow-list until
`commit → push → PR` succeeds — then lock that config in with a real (not
mapping-only) test.

## Out of scope (YAGNI for now)

- Raw `ClaudeAgentOptions` passthrough — profiles cover the need; can be added
  later without breaking the profile API.
- Per-issue profiles / `risk:high` label → auto-restrict. The `risk_gate`
  label already exists, so this is the obvious next extension when wanted.
- Real network egress enforcement beyond what the SDK/codex sandbox provides.
