"""Worker permission-profile mapping + back-compat guarantees."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_loop.agent_backend import build_codex_exec_argv
from forge_loop.sandbox import CapabilityPolicy, NetworkPolicy
from forge_loop.worker_permissions import (
    DEFAULT_PROFILE,
    PROFILES,
    claude_permission_options,
    codex_sandbox_args,
    normalize_profile,
)


def test_default_profile_is_full() -> None:
    assert DEFAULT_PROFILE == "full"
    assert set(PROFILES) == {"full", "standard", "readonly"}


@pytest.mark.parametrize(
    "given,expected",
    [
        ("full", "full"),
        ("standard", "standard"),
        ("readonly", "readonly"),
        ("FULL", "full"),
        ("  Standard  ", "standard"),
        ("", "full"),
        (None, "full"),
        ("nonsense", "full"),
        ("bypassPermissions", "full"),  # not a profile name → safe default
    ],
)
def test_normalize_profile(given: str | None, expected: str) -> None:
    assert normalize_profile(given) == expected


def test_full_claude_options_have_no_sandbox() -> None:
    # 'full' must be byte-identical to the historical hardcoded path:
    # permission_mode=bypassPermissions and NO sandbox key.
    opts = claude_permission_options("full")
    assert opts == {"permission_mode": "bypassPermissions"}
    assert "sandbox" not in opts


def test_standard_claude_options_enable_sandbox() -> None:
    opts = claude_permission_options("standard")
    assert opts["permission_mode"] == "bypassPermissions"
    assert opts["sandbox"]["enabled"] is True
    # Bash must auto-allow inside the sandbox or a sandboxed worker can't work.
    assert opts["sandbox"]["autoAllowBashIfSandboxed"] is True


def test_readonly_claude_options_use_plan_mode_and_no_sandbox() -> None:
    opts = claude_permission_options("readonly")
    assert opts == {"permission_mode": "plan"}


def test_unknown_profile_falls_back_to_full_options() -> None:
    assert claude_permission_options("banana") == claude_permission_options("full")
    assert codex_sandbox_args("banana") == codex_sandbox_args("full")


def test_codex_sandbox_args_per_profile() -> None:
    assert codex_sandbox_args("full") == [
        "-s",
        "danger-full-access",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    assert codex_sandbox_args("standard") == ["-s", "workspace-write"]
    assert codex_sandbox_args("readonly") == ["-s", "read-only"]


def test_codex_argv_default_is_unchanged_when_no_sandbox_args(tmp_path: Path) -> None:
    """Regression: omitting sandbox_args reproduces the historical full-access argv."""
    argv = build_codex_exec_argv(cwd=tmp_path, last_message_path=tmp_path / "last.txt")
    assert "danger-full-access" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    # full profile's args are exactly what the default produces.
    full = codex_sandbox_args("full")
    joined = " ".join(argv)
    assert " ".join(full) in joined


def test_codex_argv_threads_profile_sandbox_args(tmp_path: Path) -> None:
    argv = build_codex_exec_argv(
        cwd=tmp_path,
        last_message_path=tmp_path / "last.txt",
        sandbox_args=codex_sandbox_args("standard"),
    )
    assert "workspace-write" in argv
    # The dangerous full-access flag must NOT leak into a sandboxed worker.
    assert "danger-full-access" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


# ---------------------------------------------------------------------------
# Network egress binding — NetworkPolicy.allow_domains → native allow-list (#282)
# ---------------------------------------------------------------------------


def test_standard_claude_options_bind_allow_domains_into_native_egress() -> None:
    """A leased ``allow_domains`` lands in ``sandbox.network.allowedDomains``."""
    policy = CapabilityPolicy(network=NetworkPolicy(allow_domains=("github.com", "api.github.com")))
    opts = claude_permission_options("standard", policy)
    assert opts["sandbox"]["enabled"] is True
    assert opts["sandbox"]["network"]["allowedDomains"] == ["github.com", "api.github.com"]


def test_standard_claude_options_deny_by_default_empty_is_closed() -> None:
    """Fail-safe: deny_by_default + empty allow_domains → CLOSED egress list.

    Adversarial: a non-granted host must NOT appear and the structure must be
    an empty list, never a wildcard / open-by-default value.
    """
    policy = CapabilityPolicy(network=NetworkPolicy(allow_domains=(), deny_by_default=True))
    opts = claude_permission_options("standard", policy)
    net = opts["sandbox"]["network"]
    assert net["allowedDomains"] == []
    assert "evil.example.com" not in net["allowedDomains"]
    assert "*" not in net["allowedDomains"]
    # never an open-by-default knob
    assert net.get("allowAll") is None
    assert net.get("allowManagedDomainsOnly") is None


def test_standard_claude_options_no_policy_keeps_no_network_key() -> None:
    """Back-compat: a ``None`` policy keeps the historical sandbox (no network)."""
    opts = claude_permission_options("standard")
    assert opts["sandbox"] == {"enabled": True, "autoAllowBashIfSandboxed": True}
    assert "network" not in opts["sandbox"]


def test_full_claude_options_ignore_policy_and_stay_byte_identical() -> None:
    """``full`` is byte-identical to today even when a network policy is leased."""
    policy = CapabilityPolicy(network=NetworkPolicy(allow_domains=("github.com",)))
    opts = claude_permission_options("full", policy)
    assert opts == {"permission_mode": "bypassPermissions"}
    assert "sandbox" not in opts


def test_codex_standard_binds_allow_domains_and_enables_network() -> None:
    policy = CapabilityPolicy(network=NetworkPolicy(allow_domains=("github.com",)))
    args = codex_sandbox_args("standard", policy)
    assert args[:2] == ["-s", "workspace-write"]
    joined = " ".join(args)
    assert "sandbox_workspace_write.network_access=true" in joined
    assert "github.com" in joined


def test_codex_standard_deny_by_default_empty_stays_closed() -> None:
    """Fail-safe: empty allow_domains → no egress flags (workspace-write default).

    Adversarial: network_access must NOT be enabled and no host leaks in.
    """
    policy = CapabilityPolicy(network=NetworkPolicy(allow_domains=(), deny_by_default=True))
    args = codex_sandbox_args("standard", policy)
    assert args == ["-s", "workspace-write"]
    joined = " ".join(args)
    assert "network_access=true" not in joined
    assert "evil.example.com" not in joined


def test_codex_full_ignores_policy_and_stays_byte_identical() -> None:
    policy = CapabilityPolicy(network=NetworkPolicy(allow_domains=("github.com",)))
    assert codex_sandbox_args("full", policy) == [
        "-s",
        "danger-full-access",
        "--dangerously-bypass-approvals-and-sandbox",
    ]


def test_old_sdk_strips_sandbox_knob_without_crashing() -> None:
    """AC4: the network knob rides inside ``sandbox``, which ``_OPTIONAL_KNOBS``
    strips wholesale when an old SDK's options class rejects it — never a crash.

    Proxy for the real ``_run_worker_sdk`` degrade path: an options class that
    raises ``TypeError`` on the ``sandbox`` kwarg must be retried without it.
    """

    class ModernOptions:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class OldOptions:
        def __init__(self, **kwargs: object) -> None:
            if "sandbox" in kwargs:
                raise TypeError("unexpected keyword argument 'sandbox'")
            self.kwargs = kwargs

    policy = CapabilityPolicy(network=NetworkPolicy(allow_domains=("github.com",)))
    sandbox = claude_permission_options("standard", policy)["sandbox"]

    # A modern options class accepts the sandbox+network dict unchanged.
    modern = ModernOptions(sandbox=sandbox)
    bound = modern.kwargs["sandbox"]
    assert isinstance(bound, dict)
    assert bound["network"]["allowedDomains"] == ["github.com"]

    # An old one rejects it; the caller must be able to drop it and succeed.
    with pytest.raises(TypeError):
        OldOptions(sandbox=sandbox)
    assert OldOptions().kwargs == {}


def test_e2e_leased_domain_allowed_unleased_denied() -> None:
    """E2E proxy (#282): the rendered native config allows exactly the leased
    domain and denies a non-granted host.

    A live sandboxed connect/deny is infeasible in CI, so this asserts the
    strongest available proxy: the egress allow-list the SDK is handed would
    permit ``github.com`` and never ``evil.example.com``, and is exhaustive
    (no wildcard) rather than open-by-default.
    """
    leased = CapabilityPolicy(
        network=NetworkPolicy(allow_domains=("github.com",), deny_by_default=True)
    )
    allowed = claude_permission_options("standard", leased)["sandbox"]["network"]["allowedDomains"]
    assert "github.com" in allowed  # leased ⇒ reachable
    assert "evil.example.com" not in allowed  # unleased ⇒ denied
    assert allowed == ["github.com"]  # exhaustive, not wildcarded

    # Codex backend mirrors the verdict: granted ⇒ egress enabled + bound;
    # the same lease with the domain dropped ⇒ egress stays closed.
    granted = " ".join(codex_sandbox_args("standard", leased))
    assert "github.com" in granted and "network_access=true" in granted
    denied = codex_sandbox_args(
        "standard", CapabilityPolicy(network=NetworkPolicy(allow_domains=()))
    )
    assert denied == ["-s", "workspace-write"]


def test_integration_policy_network_flows_to_options_and_attestation(tmp_path: Path) -> None:
    """Integration (#282): a populated ``NetworkPolicy`` flows through the
    settings-plant path (event hash) AND the native SDK options (egress list).
    """
    from forge_loop.sandbox import policy_hash
    from forge_loop.worker_worktree import plant_worker_settings

    policy = CapabilityPolicy(
        network=NetworkPolicy(allow_domains=("github.com", "api.github.com")),
    )
    events_file = tmp_path / "events.jsonl"
    wt = tmp_path / "wt"
    wt.mkdir()

    plant_worker_settings(wt, policy, events_file=events_file)

    import json

    lines = [json.loads(x) for x in events_file.read_text().splitlines() if x.strip()]
    enforced = [e for e in lines if e.get("kind") == "worker_policy_enforced"]
    assert enforced and enforced[0]["policy_hash"] == policy_hash(policy)

    # The same lease binds its egress list into the SDK sandbox options.
    opts = claude_permission_options("standard", policy)
    assert opts["sandbox"]["network"]["allowedDomains"] == ["github.com", "api.github.com"]


# --- preserve_on_failure capability flag (#356) ------------------------------


def test_preserve_on_failure_defaults_false() -> None:
    """A default-constructed policy preserves deny-by-default semantics."""
    assert CapabilityPolicy().preserve_on_failure is False


def test_preserve_on_failure_flips_policy_hash() -> None:
    """Two policies differing ONLY in preserve_on_failure hash differently —
    so the saga grant can attest which preserve decision was authorised."""
    from forge_loop.sandbox import policy_hash

    keep = CapabilityPolicy(preserve_on_failure=True)
    drop = CapabilityPolicy(preserve_on_failure=False)
    assert policy_hash(keep) != policy_hash(drop)


def test_to_json_obj_includes_preserve_on_failure() -> None:
    obj = CapabilityPolicy(preserve_on_failure=True).to_json_obj()
    assert obj["preserve_on_failure"] is True
    assert CapabilityPolicy().to_json_obj()["preserve_on_failure"] is False


def test_preserve_on_failure_round_trips() -> None:
    policy = CapabilityPolicy(preserve_on_failure=True)
    assert CapabilityPolicy.from_json_obj(policy.to_json_obj()) == policy


def test_preserve_on_failure_backward_compatible_default() -> None:
    """A grant persisted before this field existed (no key) defaults False."""
    legacy = {
        "filesystem": {"read_roots": [], "write_roots": []},
        "network": {"allow_domains": [], "deny_by_default": True},
        "mcp": [],
        "secret_names": [],
    }
    assert CapabilityPolicy.from_json_obj(legacy).preserve_on_failure is False


def test_render_capability_policy_shows_preserve_line() -> None:
    from forge_loop.sandbox import render_capability_policy

    assert "preserve on failure: yes" in render_capability_policy(
        CapabilityPolicy(preserve_on_failure=True)
    )
    assert "preserve on failure: no" in render_capability_policy(
        CapabilityPolicy(preserve_on_failure=False)
    )


def test_preserve_on_failure_tamper_evident() -> None:
    """Adversarial: flipping the serialised flag breaks the attested digest."""
    from forge_loop.sandbox import canonical_policy_json, policy_hash

    original = CapabilityPolicy(preserve_on_failure=True)
    original_digest = policy_hash(original)

    import json

    obj = json.loads(canonical_policy_json(original))
    obj["preserve_on_failure"] = False
    tampered = CapabilityPolicy.from_json_obj(obj)
    assert policy_hash(tampered) != original_digest


@pytest.mark.parametrize("bad", ["yes", None, 0, 1])
def test_preserve_on_failure_malformed_coerces_via_bool(bad: object) -> None:
    """Malformed serialised values coerce via bool(...) without raising —
    mirrors the deny_by_default handling at policy.py."""
    policy = CapabilityPolicy.from_json_obj({"preserve_on_failure": bad})
    assert policy.preserve_on_failure is bool(bad)


def test_integration_preserve_on_failure_attested_on_grant(tmp_path: Path) -> None:
    """Integration (#356): a populated preserve flag flows through the
    settings-plant attestation path so the saga grant records a digest that
    differs from the same grant with preserve_on_failure=False."""
    from forge_loop.sandbox import policy_hash
    from forge_loop.worker_worktree import plant_worker_settings

    keep = CapabilityPolicy(preserve_on_failure=True)
    events_file = tmp_path / "events.jsonl"
    wt = tmp_path / "wt"
    wt.mkdir()
    plant_worker_settings(wt, keep, events_file=events_file)

    import json

    lines = [json.loads(x) for x in events_file.read_text().splitlines() if x.strip()]
    enforced = [e for e in lines if e.get("kind") == "worker_policy_enforced"]
    assert enforced and enforced[0]["policy_hash"] == policy_hash(keep)
    assert policy_hash(keep) != policy_hash(CapabilityPolicy(preserve_on_failure=False))
