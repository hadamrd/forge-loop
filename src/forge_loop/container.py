"""DI container for forge-loop adapters (issue #86).

Production code consumes :class:`Container` not the concrete adapter
implementations directly. Tests construct a Container with Fake
adapters; the runner constructs one with the Subprocess/Os/System
defaults.

The container is intentionally a plain dataclass — no service-locator
ceremony, no auto-wiring, no metaclass magic. Three slots, build-once,
pass-by-reference. The "DI framework" pattern at this scale is overkill;
a typed bag-of-clients is enough.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from forge_loop.adapters.clock import Clock, SystemClock
from forge_loop.adapters.fs import FileSystem, OsFileSystem
from forge_loop.adapters.git import GitClient, SubprocessGit


@dataclass
class Container:
    """Bundle of adapter clients passed through the runner.

    Defaults wire the real (subprocess/os/system) implementations.
    Construct with explicit Fakes in tests::

        ct = Container(
            git=FakeGitClient(),
            fs=FakeFileSystem(),
            clock=FakeClock(start=1_700_000_000.0),
        )
    """

    git: GitClient = field(default_factory=SubprocessGit)
    fs: FileSystem = field(default_factory=OsFileSystem)
    clock: Clock = field(default_factory=SystemClock)


# Process-wide default container. Code paths that haven't been migrated
# yet (most of them — #86 ships the framework + first wave) keep calling
# subprocess directly. As call sites adopt the container, they accept it
# as an arg or read the default here.
_DEFAULT = Container()


def get_container() -> Container:
    """Return the process-wide default container.

    Migration policy: new code accepts ``Container`` as an explicit
    arg (preferred — testable). Legacy code that hasn't migrated yet
    can read ``get_container()`` lazily.
    """
    return _DEFAULT


__all__ = ["Container", "get_container"]
