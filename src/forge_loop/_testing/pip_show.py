"""Test fake for the ``pip show`` boundary (#144 boot poison guard)."""

from __future__ import annotations

from dataclasses import dataclass

from forge_loop.runner.poison_guard import PipShowPayload


@dataclass
class FakePipShowReader:
    """In-memory :class:`~forge_loop.runner.poison_guard.PipShowReader`.

    Construct with a canned ``PipShowPayload`` (or use the convenience
    classmethods) to drive the boot guard without a live ``pip`` subprocess.
    """

    payload: PipShowPayload
    calls: list[str] | None = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def pip_show(self, package: str) -> PipShowPayload:
        assert self.calls is not None
        self.calls.append(package)
        return self.payload

    @classmethod
    def editable(cls, location: str) -> FakePipShowReader:
        """A reader reporting an editable install at ``location``."""
        stdout = (
            "Name: forge-loop\n"
            "Version: 0.1.0\n"
            f"Location: {location}\n"
            f"Editable project location: {location}\n"
        )
        return cls(payload=PipShowPayload(returncode=0, stdout=stdout))

    @classmethod
    def installed_at(cls, location: str) -> FakePipShowReader:
        """A reader reporting a non-editable install at ``location``."""
        stdout = f"Name: forge-loop\nVersion: 0.1.0\nLocation: {location}\n"
        return cls(payload=PipShowPayload(returncode=0, stdout=stdout))

    @classmethod
    def not_installed(cls) -> FakePipShowReader:
        """A reader reporting the package is absent (``pip show`` exits 1)."""
        return cls(payload=PipShowPayload(returncode=1, stdout=""))
