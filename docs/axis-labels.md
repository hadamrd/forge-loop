# Axis labels (`axis:<slug>`)

> Introduced in [#126](https://github.com/khalidx/forge-loop/issues/126).
> Plumbing for focused-sprint mode: group / filter the loop's work by
> topical concern without changing how issues are queued.

## What it is

A free-form label namespace, `axis:<slug>`, applied to GitHub issues
by the brainstormer / PO / operators. The slug is any lowercase string
(e.g. `dispatch`, `cli`, `observability`, `docs`). Multiple `axis:*`
labels on the same issue are fine; they stack.

There is **no enum, no registry, and no validation**. If you want to
spin up a new axis tonight, just label an issue.

## Why

Operators staring at `forge-loop status` saw a flat list of
`loop:ready` issues with no way to tell whether the backlog leaned
toward dispatcher work, CLI work, or observability work. Likewise,
`forge-loop run` greedily grabbed whatever was ready, so an operator
who wanted to grind one area for a sprint had no knob.

## Surface

### Status grouping

```
$ forge-loop status
... (existing rows)
axes  dispatch (6)  #12, #14, #18, #19, #22, #27
      cli      (5)  #11, #13, #21, #24, #26
      unaligned(3)  #9, #17, #28
warning  3 open issue(s) carry no axis:* label
```

JSON shape (`forge-loop status --json`):

```json
{
  "queue_depth": 14,
  "axes": {
    "dispatch": [{"number": 12, "title": "..."}, ...],
    "cli":      [{"number": 11, "title": "..."}, ...],
    "unaligned": [{"number": 9,  "title": "..."}, ...]
  },
  "unaligned_count": 3,
  "axis_filter": []
}
```

Narrow the view with `--axis`:

```
$ forge-loop status --axis dispatch --json
{ ..., "axes": { "dispatch": [...] }, "axis_filter": ["dispatch"] }
```

### Dispatch filter

```
$ forge-loop run --axis dispatch                # only axis:dispatch issues
$ forge-loop run --axis dispatch --axis cli     # union
$ forge-loop run                                # unchanged behaviour (no filter)
```

The runner logs the active filter at startup and emits
`axis_filter_active` events per tick so a focused-sprint launch is
auditable from `forge-loop events`. If no ready issues match, the tick
exits cleanly (the loop just idles until a matching issue lands).

## Edge cases

| Case | Behaviour |
| ---- | --------- |
| Mixed case label (`Axis:Dispatch`) | Normalised to lowercase before matching. |
| Empty slug label (`axis:`) | Treated as unaligned; logged, no crash. |
| Issue carries multiple `axis:*` labels | Appears under each bucket; counted once in `unaligned_count`. |
| `--axis foo` matches zero issues | Tick idles cleanly with an `axis_filter_empty` event. Exit code 0. |
| `--axis` not passed | Behaviour is byte-identical to the pre-#126 loop. |

## Out of scope

- Automatic axis assignment from issue text (separate brainstormer
  enhancement).
- Closed registry / enum of allowed axes.
- Persisting the filter into config or state — `--axis` is a
  per-invocation knob.
