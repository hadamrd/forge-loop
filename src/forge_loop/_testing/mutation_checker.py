"""Test fake for the scoped mutation-check boundary (issues #379/#380)."""

from __future__ import annotations

from dataclasses import dataclass, field

from forge_loop.control.doctor import DEFAULT_MUTATION_MODULE, MutationCheckResult


@dataclass
class FakeMutationChecker:
    """In-memory :class:`~forge_loop.control.doctor.MutationChecker`.

    Construct with a canned surviving-mutant count to drive the doctor probe
    without running a real mutation pass. ``raises`` simulates a checker that
    blows up (drives the ``warn`` degrade path).
    """

    survivors: int = 0
    module: str = DEFAULT_MUTATION_MODULE
    raises: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def check(self) -> MutationCheckResult:
        self.calls.append(self.module)
        if self.raises is not None:
            raise self.raises
        return MutationCheckResult(module=self.module, survivors=self.survivors)

    @classmethod
    def pinned(cls, module: str = DEFAULT_MUTATION_MODULE) -> FakeMutationChecker:
        """A checker reporting a fully-pinned module (0 survivors → healthy)."""
        return cls(survivors=0, module=module)

    @classmethod
    def with_survivors(
        cls, survivors: int, module: str = DEFAULT_MUTATION_MODULE
    ) -> FakeMutationChecker:
        """A checker reporting ``survivors`` planted faults that slipped past the suite."""
        return cls(survivors=survivors, module=module)
