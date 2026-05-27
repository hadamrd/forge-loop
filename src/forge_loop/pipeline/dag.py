"""DAG model + validator for pipeline chains.

Responsibilities:

- Resolve implicit edges: a step with no explicit ``after`` chains off
  the previous step in the YAML list (matching the example in the
  issue body, where ``po`` has ``on:`` instead of ``after:`` and
  everything downstream uses ``after:``).
- Detect cycles via DFS and return the cycle path for actionable error
  messages.
- Detect unknown role refs in ``after:``.
- Produce a deterministic topological order (Kahn's algorithm, breaking
  ties by YAML position) so ``pipeline show`` renders stably.
- Render an ASCII-art view of the resolved DAG.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from forge_loop.pipeline.loader import ChainStep, PipelineSpec


class ValidationError(ValueError):
    """Raised when a pipeline spec cannot be resolved into a valid DAG."""


@dataclass(frozen=True)
class DAGNode:
    step: ChainStep
    parents: tuple[str, ...]
    children: tuple[str, ...]
    depth: int  # 0 = root; longest path from any root

    @property
    def role(self) -> str:
        return self.step.role


@dataclass(frozen=True)
class DAG:
    nodes: dict[str, DAGNode]
    order: tuple[str, ...]  # deterministic topo order
    roots: tuple[str, ...]
    spec: PipelineSpec = field(default=None)  # type: ignore[assignment]

    def parents_of(self, role: str) -> tuple[str, ...]:
        return self.nodes[role].parents

    def children_of(self, role: str) -> tuple[str, ...]:
        return self.nodes[role].children

    def render_ascii(self) -> str:
        """Render the resolved DAG as ASCII art, grouped by depth level.

        Layout:

            [depth 0]  po
                          │
            [depth 1]  worker (parallel=3)
                          │
            [depth 2]  critic
                          │
            [depth 3]  security-reviewer  if labels=[security-sensitive]
                          │
            [depth 4]  merge  if all_approve

        Branches at the same depth are shown comma-separated on the
        same line, then a connector to the next depth.
        """
        levels: dict[int, list[str]] = defaultdict(list)
        for role in self.order:
            levels[self.nodes[role].depth].append(role)

        lines: list[str] = []
        max_depth = max(levels) if levels else -1
        for depth in range(max_depth + 1):
            roles = levels.get(depth, [])
            if not roles:
                continue
            labels = []
            for r in roles:
                n = self.nodes[r]
                tag = r
                if n.step.parallel > 1:
                    tag += f" (parallel={n.step.parallel})"
                if not n.step.condition.is_empty:
                    cond = n.step.condition
                    bits = []
                    if cond.labels:
                        bits.append("labels=[" + ",".join(cond.labels) + "]")
                    if cond.all_approve:
                        bits.append("all_approve")
                    tag += "  if " + " & ".join(bits)
                # show parents for clarity at depth > 0
                if n.parents and depth > 0:
                    tag += f"  ← {','.join(n.parents)}"
                labels.append(tag)
            prefix = f"[depth {depth}]  "
            lines.append(prefix + "  •  ".join(labels))
            if depth < max_depth:
                lines.append(" " * (len(prefix) + 1) + "│")
        return "\n".join(lines)


def _resolve_edges(spec: PipelineSpec) -> dict[str, tuple[str, ...]]:
    """For each step, return the list of parent roles.

    Implicit rule: if ``after`` is empty AND this is not the first step,
    we DO NOT infer an edge. The first step is the root. Any later step
    with no explicit ``after`` is treated as ambiguous and we raise —
    operators must be explicit about ordering after the head, per the
    "ambiguous after" acceptance criterion.

    Exception: a step with an ``on:`` trigger and no ``after:`` is a
    root step regardless of its position in the YAML list (the example
    has ``po`` with ``on: issue_labeled_ready``).
    """
    roles = {s.role for s in spec.steps}
    parents: dict[str, tuple[str, ...]] = {}
    for i, step in enumerate(spec.steps):
        if step.after:
            for ref in step.after:
                if ref not in roles:
                    raise ValidationError(
                        f"step '{step.role}': unknown role reference in 'after': '{ref}' "
                        f"(known roles: {sorted(roles)})"
                    )
            parents[step.role] = step.after
            continue
        # no after
        if step.on:
            parents[step.role] = ()
            continue
        if i == 0:
            parents[step.role] = ()
            continue
        raise ValidationError(
            f"step '{step.role}': ambiguous — no 'after:' and no 'on:' trigger. "
            "Add 'after: <role>' to chain it off a prior step, or 'on: <event>' "
            "to declare it as a root."
        )
    return parents


def _detect_cycle(parents: dict[str, tuple[str, ...]]) -> list[str] | None:
    """Return cycle path as a list of roles if any cycle exists, else None."""
    # Build children adjacency
    children: dict[str, list[str]] = defaultdict(list)
    for role, ps in parents.items():
        for p in ps:
            children[p].append(role)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {r: WHITE for r in parents}
    stack_path: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        stack_path.append(node)
        for nxt in children.get(node, []):
            if color[nxt] == GRAY:
                # found cycle — trim path to start at nxt
                idx = stack_path.index(nxt)
                return stack_path[idx:] + [nxt]
            if color[nxt] == WHITE:
                got = dfs(nxt)
                if got:
                    return got
        stack_path.pop()
        color[node] = BLACK
        return None

    for r in parents:
        if color[r] == WHITE:
            got = dfs(r)
            if got:
                return got
    return None


def build_dag(spec: PipelineSpec) -> DAG:
    """Validate the spec and assemble a DAG.

    Raises :class:`ValidationError` on:
      - unknown role ref in ``after``
      - ambiguous ``after`` (no ``after`` and no ``on`` on a non-head step)
      - cycles (error message names the cycle)
    """
    parents = _resolve_edges(spec)
    cycle = _detect_cycle(parents)
    if cycle:
        raise ValidationError(
            "pipeline contains a cycle: " + " → ".join(cycle)
        )

    # Children index
    children: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {s.role: 0 for s in spec.steps}
    position: dict[str, int] = {s.role: i for i, s in enumerate(spec.steps)}
    for role, ps in parents.items():
        for p in ps:
            children[p].append(role)
            indegree[role] += 1

    # Kahn's, tie-breaking by yaml position for determinism
    order: list[str] = []
    ready = sorted([r for r, d in indegree.items() if d == 0], key=position.__getitem__)
    queue: deque[str] = deque(ready)
    while queue:
        n = queue.popleft()
        order.append(n)
        nexts = sorted(children.get(n, []), key=position.__getitem__)
        for nxt in nexts:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(spec.steps):
        # Should be caught by cycle check above, but defensive.
        missing = set(spec.steps) - set(order)
        raise ValidationError(f"pipeline: failed to topo-sort (unresolved: {missing})")

    # Depth = longest path from any root
    depth: dict[str, int] = {r: 0 for r in order}
    for r in order:
        for p in parents[r]:
            if depth[p] + 1 > depth[r]:
                depth[r] = depth[p] + 1

    nodes = {
        s.role: DAGNode(
            step=s,
            parents=parents[s.role],
            children=tuple(sorted(children.get(s.role, []), key=position.__getitem__)),
            depth=depth[s.role],
        )
        for s in spec.steps
    }
    roots = tuple(r for r in order if not parents[r])
    return DAG(nodes=nodes, order=tuple(order), roots=roots, spec=spec)
