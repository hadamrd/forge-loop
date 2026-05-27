"""Tests for the pluggable role system (issue #17).

Covers:
- YAML schema validation: required fields, type checks, friendly errors.
- Discovery: 3 built-ins + 2 custom = 5 roles; same-name custom overrides
  the built-in.
- Trigger filter matches PR labels correctly (happy + miss).
- Integration: a custom security-reviewer role is discovered, matches a
  fixture PR event, and exposes its model / brief / mcp_tools for dispatch.
- Adversarial: malformed YAML produces a clean RoleLoadError with
  line/column info and does NOT crash the loader.
- CLI: ``forge-loop roles list`` prints the loaded roles (text + JSON).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from forge_loop.cli import main as cli_main
from forge_loop.roles import (
    Role,
    RoleLoadError,
    discover_roles,
    load_role_file,
)
from forge_loop.roles.role import RoleSchemaError, Trigger


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _write(p: Path, body: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


VALID_YAML = """\
name: security-reviewer
brief_template: .forge/briefs/security-reviewer.md.tmpl
model: claude-opus-4-7
timeout_s: 600
budget_usd: 2.0
triggers:
  - "on": pr_opened
    filter:
      labels: ["security-sensitive"]
actions:
  - mcp_tools: [gh_pr_review_comment, gh_label]
    shell: false
output_schema: critic
"""


# ---------------------------------------------------------------------------
# schema validation
# ---------------------------------------------------------------------------
class TestRoleSchema:
    def test_happy_path(self) -> None:
        import yaml

        role = Role.from_dict(yaml.safe_load(VALID_YAML))
        assert role.name == "security-reviewer"
        assert role.model == "claude-opus-4-7"
        assert role.timeout_s == 600
        assert role.budget_usd == pytest.approx(2.0)
        assert len(role.triggers) == 1
        assert role.triggers[0].on == "pr_opened"
        assert role.triggers[0].filter == {"labels": ["security-sensitive"]}
        assert role.actions[0].mcp_tools == ("gh_pr_review_comment", "gh_label")
        assert role.actions[0].shell is False
        assert role.output_schema == "critic"

    @pytest.mark.parametrize(
        "field,bad",
        [
            ("name", None),  # missing
            ("brief_template", None),
            ("model", None),
            ("timeout_s", None),
        ],
    )
    def test_missing_required(self, field: str, bad: object) -> None:
        import yaml

        data = yaml.safe_load(VALID_YAML)
        del data[field]
        with pytest.raises(RoleSchemaError, match=field):
            Role.from_dict(data)

    def test_invalid_name(self) -> None:
        import yaml

        data = yaml.safe_load(VALID_YAML)
        data["name"] = "bad name with spaces"
        with pytest.raises(RoleSchemaError, match="name"):
            Role.from_dict(data)

    def test_bad_timeout(self) -> None:
        import yaml

        data = yaml.safe_load(VALID_YAML)
        data["timeout_s"] = 0
        with pytest.raises(RoleSchemaError, match="timeout_s"):
            Role.from_dict(data)

    def test_negative_budget(self) -> None:
        import yaml

        data = yaml.safe_load(VALID_YAML)
        data["budget_usd"] = -1
        with pytest.raises(RoleSchemaError, match="budget_usd"):
            Role.from_dict(data)

    def test_top_level_not_mapping(self) -> None:
        with pytest.raises(RoleSchemaError, match="mapping"):
            Role.from_dict(["not", "a", "mapping"])

    def test_triggers_must_be_list(self) -> None:
        import yaml

        data = yaml.safe_load(VALID_YAML)
        data["triggers"] = "pr_opened"
        with pytest.raises(RoleSchemaError, match="triggers"):
            Role.from_dict(data)

    def test_empty_triggers_allowed(self) -> None:
        import yaml

        data = yaml.safe_load(VALID_YAML)
        data["triggers"] = []
        role = Role.from_dict(data)
        assert role.triggers == ()


# ---------------------------------------------------------------------------
# trigger matching
# ---------------------------------------------------------------------------
class TestTriggerMatching:
    def test_label_filter_happy(self) -> None:
        t = Trigger(on="pr_opened", filter={"labels": ["security-sensitive"]})
        assert t.matches("pr_opened", {"labels": ["security-sensitive", "needs-review"]})

    def test_label_filter_miss(self) -> None:
        t = Trigger(on="pr_opened", filter={"labels": ["security-sensitive"]})
        assert not t.matches("pr_opened", {"labels": ["docs"]})

    def test_wrong_event(self) -> None:
        t = Trigger(on="pr_opened", filter={})
        assert not t.matches("pr_updated", {})

    def test_branch_filter(self) -> None:
        t = Trigger(on="pr_opened", filter={"branch": "main"})
        assert t.matches("pr_opened", {"branch": "main"})
        assert not t.matches("pr_opened", {"branch": "feat/x"})

    def test_role_matches_event(self) -> None:
        role = Role(
            name="sec",
            brief_template="x",
            model="m",
            timeout_s=1,
            triggers=(Trigger(on="pr_opened", filter={"labels": ["security-sensitive"]}),),
            actions=(),
        )
        assert role.matches_event("pr_opened", {"labels": ["security-sensitive"]})
        assert not role.matches_event("pr_opened", {"labels": ["other"]})
        assert not role.matches_event("issue_opened", {"labels": ["security-sensitive"]})


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
class TestDiscovery:
    def test_builtins_loaded(self, tmp_path: Path) -> None:
        result = discover_roles(tmp_path)
        names = {r.name for r in result.roles}
        assert {"po", "worker", "critic"}.issubset(names)
        assert result.errors == []

    def test_three_builtin_plus_two_custom(self, tmp_path: Path) -> None:
        # Two custom roles, both with new names.
        _write(
            tmp_path / ".forge" / "roles" / "security-reviewer.yaml",
            VALID_YAML,
        )
        _write(
            tmp_path / ".forge" / "roles" / "docs-author.yaml",
            VALID_YAML.replace("security-reviewer", "docs-author"),
        )
        result = discover_roles(tmp_path)
        names = {r.name for r in result.roles}
        # 3 built-in + 2 custom = 5
        assert names == {"po", "worker", "critic", "security-reviewer", "docs-author"}
        assert result.errors == []

    def test_same_name_override(self, tmp_path: Path) -> None:
        override = VALID_YAML.replace("security-reviewer", "critic").replace(
            "claude-opus-4-7", "claude-sonnet-test"
        )
        _write(tmp_path / ".forge" / "roles" / "critic.yaml", override)
        result = discover_roles(tmp_path)
        critic = result.by_name("critic")
        assert critic is not None
        # Project override wins.
        assert critic.model == "claude-sonnet-test"
        assert ".forge/roles/critic.yaml" in (critic.source_path or "")

    def test_malformed_yaml_does_not_crash(self, tmp_path: Path) -> None:
        bad = tmp_path / ".forge" / "roles" / "broken.yaml"
        _write(bad, "name: foo\n  : : : oops\n")
        result = discover_roles(tmp_path)
        # Built-ins still load fine.
        assert any(r.name == "po" for r in result.roles)
        # Error captured with the offending path.
        assert len(result.errors) == 1
        err = result.errors[0]
        assert str(bad) in err.source
        assert "line" in err.message  # carries line/col diagnostic
        assert "broken" not in {r.name for r in result.roles}

    def test_schema_error_friendly(self, tmp_path: Path) -> None:
        _write(
            tmp_path / ".forge" / "roles" / "no-model.yaml",
            "name: no-model\nbrief_template: x\ntimeout_s: 10\n",
        )
        result = discover_roles(tmp_path)
        assert len(result.errors) == 1
        assert "model" in result.errors[0].message

    def test_load_role_file_directly(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "x.yaml", VALID_YAML)
        role = load_role_file(p)
        assert role.name == "security-reviewer"

    def test_load_role_file_raises_on_bad(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "x.yaml", "::: bad ::: yaml")
        with pytest.raises(RoleLoadError):
            load_role_file(p)


# ---------------------------------------------------------------------------
# integration: custom security-reviewer end-to-end
# ---------------------------------------------------------------------------
class TestSecurityReviewerIntegration:
    def test_dispatch_resolution(self, tmp_path: Path) -> None:
        """A custom security-reviewer role is discovered, matches a labelled
        PR event, exposes its model + mcp_tools so the dispatcher can run it.

        This stops at the dispatch *resolution* boundary — actually invoking
        the SDK is covered by worker/critic tests; here we verify the role
        system delivers the right config to the dispatcher.
        """
        _write(
            tmp_path / ".forge" / "roles" / "security-reviewer.yaml",
            VALID_YAML,
        )

        result = discover_roles(tmp_path)
        role = result.by_name("security-reviewer")
        assert role is not None

        # PR event with the required label fires the role.
        pr_event = {
            "labels": ["security-sensitive", "feature"],
            "branch": "feat/auth",
            "author": "alice",
        }
        assert role.matches_event("pr_opened", pr_event)

        # PR event WITHOUT the label does not fire.
        pr_event_no_label = {"labels": ["feature"], "branch": "feat/auth"}
        assert not role.matches_event("pr_opened", pr_event_no_label)

        # Dispatcher-facing config is intact.
        assert role.model == "claude-opus-4-7"
        assert role.timeout_s == 600
        assert role.allowed_mcp_tools() == (
            "gh_pr_review_comment",
            "gh_label",
        )
        assert role.shell_allowed() is False
        assert role.output_schema == "critic"


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
class TestRolesListCLI:
    def test_text(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write(tmp_path / ".forge" / "roles" / "security-reviewer.yaml", VALID_YAML)
        rc = cli_main(["roles", "list", "--project-dir", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "security-reviewer" in out
        assert "po" in out and "worker" in out and "critic" in out

    def test_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write(tmp_path / ".forge" / "roles" / "security-reviewer.yaml", VALID_YAML)
        rc = cli_main(["roles", "list", "--project-dir", str(tmp_path), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        names = {r["name"] for r in payload["roles"]}
        assert {"po", "worker", "critic", "security-reviewer"}.issubset(names)
        sec = next(r for r in payload["roles"] if r["name"] == "security-reviewer")
        assert sec["model"] == "claude-opus-4-7"
        assert sec["triggers"][0]["filter"] == {"labels": ["security-sensitive"]}

    def test_malformed_reports_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(tmp_path / ".forge" / "roles" / "broken.yaml", "::: bad :::")
        rc = cli_main(["roles", "list", "--project-dir", str(tmp_path)])
        assert rc == 0
        captured = capsys.readouterr()
        # Built-ins still listed on stdout.
        assert "po" in captured.out
        # Warning surfaced on stderr (does not crash).
        assert "warning" in captured.err.lower()
