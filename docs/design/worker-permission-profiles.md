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
| `standard` | `permission_mode=bypassPermissions` + `sandbox={enabled, autoAllowBashIfSandboxed, network.allowedDomains=lease}` | `-s workspace-write` (+ `network_access`/`allowed_domains` from the lease) |
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

Network egress binding (issue #282, RESOLVED): the `standard` profile now binds
a leased `NetworkPolicy.allow_domains` onto the backend's **native** egress
allow-list — `sandbox.network.allowedDomains` for the Claude SDK and the
`sandbox_workspace_write` network config for Codex. `claude_permission_options`
and `codex_sandbox_args` take the leased `CapabilityPolicy` and render it:

- A lease that grants domains opens exactly those (`allowedDomains=[...]` /
  Codex `network_access=true` + `allowed_domains=[...]`).
- **Fail-safe closed:** `deny_by_default=True` with an empty `allow_domains`
  renders an empty allow-list (Claude) / no extra flags (Codex
  workspace-write denies egress by default) — never open-by-default or
  wildcarded, mirroring the "empty policy → empty allow" rule (#200).
- `full` ignores the policy and stays byte-identical (no sandbox ⇒ nothing to
  bind). A `network` knob an old SDK rejects is stripped via `_OPTIONAL_KNOBS`.

The allow-list is inherently per-project — `github.com`, `api.github.com`,
`*.githubusercontent.com`, **plus the project's package registries**
(pypi/npm/crates/goproxy/apt…). A single global `standard` cannot be both tight
and universally working; the lease (not a global default) carries the set.

Remaining `standard` work (its own follow-up):

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
