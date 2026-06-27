# Skill-Tree — learned procedural memory the maestro grows and retrieves

Status: in implementation (branch `feat/skill-tree`). Epic: hadamrd/forge-loop#458.

## Problem

Every worker starts cold: it re-derives repo conventions, file maps, and test
recipes on every ticket — burning tokens, making inconsistent choices, and
repeating known mistakes. The biggest faster+cheaper+more-accurate lever is to
**retrieve learned procedure instead of re-deriving it**.

## What already exists (substrate — reuse, do not duplicate)

- `memory/store.py` `SqliteMemoryStore` at `.forge/memory.db` — `put` / `get` /
  `list_active(kind=)` / `supersede`.
- `memory/models.py` `MemoryItem` (kind/title/body/provenance/tags/superseded_by),
  `MemoryKind.PROCEDURAL`, `derive_skill_key(failing_signal, target)`,
  `skill_tag` / `skill_from_tags`, tag-prefix pattern (`axis:`, `skill:`).
- `runner/learning.py` `record_procedural_skill(...)` — writes a procedural card
  and supersedes the prior card of the same skill-key (lineage preserved).
- Episodic memory is already harvested on merge (`record_merged_outcomes` via
  `runner/tick.py::_record_merged_memory`) and injected into briefs
  (`worker_brief.py::_render_prior_episodes`).

**Gap:** procedural skills are never harvested, never retrieved/injected, and
have no tree structure.

## Design

### Tree structure (leaves + internal nodes)

Each card carries an **area path** as a tag: `area:<path>` (e.g.
`area:pulsar-node/http-route`). The `/`-delimited path IS the tree.

- **Leaf** = a concrete procedure card (has both a `skill:<sig>` tag and an
  `area:<path>` tag). Body schema (plain text sections): `trigger`, `procedure`
  (file map + template + test recipe), `pitfalls`.
- **Internal node** = a generalized card for an area subtree (has an
  `area-node` marker tag + `area:<path>`), distilled from ≥N descendant leaves.

New helpers in `memory/models.py` (mirror `axis_tag`): `AREA_TAG_PREFIX`,
`area_tag(path)`, `area_from_tags(tags)`, `AREA_NODE_TAG`.

### Lifecycle

1. **Harvest** (`skill_librarian.py` + `runner/learning.py`): after a
   critic-clean merge, a one-shot LLM "librarian" distills the merged diff +
   acceptance criteria into a `SkillCard(area, trigger, procedure, pitfalls,
   failing_signal, target, confidence)`. `harvest_skills_from_merge(...)` is pure
   and injectable (both the diff fetch and the LLM call are passed-in callables)
   so the wiring is unit tested without network. The diff is fetched in prod via
   `gh_issues.pr_diff(pr_url, repo)` (forge-loop's githubkit client — the gh CLI
   is deliberately unused and a loop `GH_TOKEN` would break it). It calls
   `record_procedural_skill` with the `area:` tag and `pr:<url>` provenance in
   `evidence_refs`. Emits `skill_harvested`. (Commit-SHA-based age/expiry of
   harvested cards is a follow-up; recipe freshness is held by supersession on
   the skill-key.)
2. **Retrieve + inject** (`memory/skills.py` + `worker_brief.py`):
   `retrieve_skills_for(store, query, *, k)` ranks active procedural cards by
   area/title/tag token overlap with the ticket, walking most-specific-leaf →
   ancestor-node, capped at `k` (token budget). `render_skill_section(...)`
   formats them; injected into `make_brief` and `make_repair_brief`. Emits
   `skill_injected` (rank) per card — enables measuring the turns/tokens win.
   v1 retrieval is deterministic keyword/area ranking; a clean seam is left for
   semantic (Lumen) retrieval as a fast-follow (NOT in this PR).
3. **Curate/evolve** (`memory/skills.py`): `expire_stale_skills(...)` supersedes
   cards whose proof-SHA is absent from the current history / older than a
   horizon; `promote_internal_nodes(store, *, min_leaves)` distills an
   internal-node card once an area has ≥`min_leaves` leaves. Emits
   `skill_expired` / `skill_promoted`.

### Traps engineered around

- **Stale > empty:** every card carries provenance SHA + confidence; expiry is
  mandatory. Adversarial test: an expired card is not retrieved.
- **Retrieval is the hard part:** ranking is the real work; honest v1 is
  keyword/area, semantic is a seam.
- **Contradiction sprawl:** one librarian writes; supersession (existing)
  preserves lineage; no free-for-all.

## Events (new, `events.py`)

`SkillHarvestedEvent`, `SkillInjectedEvent`, `SkillPromotedEvent`,
`SkillExpiredEvent`.

## Testing

Pure modules (models helpers, ranking, expiry, promotion, harvest-wiring with an
injected fake librarian) are fully unit-tested against a real
`SqliteMemoryStore` (`tmp_path`). Each behavior has an adversarial test: remove
the harvest call / the inject / the expiry filter and a test goes red. The
LLM-dependent distiller is isolated behind a callable seam and tested with a fake.

## Out of scope (this PR)

Semantic/Lumen retrieval (seam only); a UI surface; cross-repo skill sharing.
