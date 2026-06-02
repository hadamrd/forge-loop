from __future__ import annotations

# ruff: noqa: F401
import json
import os
import subprocess
import sys
from datetime import UTC
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import typer

from forge_loop.state import tail_events


class ProductCommandsMixin:
    load: Any
    brainstormer_factory: Any
    gh_client_factory: Any

    def _cmd_brainstorm(self, args: SimpleNamespace) -> int:
        """`forge-loop brainstorm` — dry-run by default, files issues with --apply.

        Contract (issue #124):
          * Default (no flags): load ProductVision, run Brainstormer, print
            the BrainstormReport as YAML to stdout. Exit 0. No GitHub calls.
          * --apply: file each proposed epic first, then each ticket with
            ``Parent: #<epic-number>`` cross-link in the body.
          * Missing/invalid vision → exit 2 (no partial state).
          * Partial failure during --apply → exit 1 with per-title reporting.
        """
        import yaml

        from forge_loop.brainstormer import (
            BrainstormReport,
            ProposedEpic,
            ProposedTicket,
            filter_report_for_vision,
        )
        from forge_loop.frontier.decisions import (
            FrontierDecision,
            FrontierDecisionLedger,
            FrontierDecisionOutcome,
            ProposalKind,
            normalize_candidate_key,
        )
        from forge_loop.gh_client import OpenBacklog, list_open_backlog
        from forge_loop.product_vision import MissingVisionError, discover

        # 1. Resolve repo path + GitHub coordinates from the existing config
        #    accessor — same pattern as ``_cmd_init`` / ``_cmd_run``.
        repo_path = Path.cwd()
        owner = ""
        repo_name = ""
        provider = "claude"
        model: str | None = None
        timeout_s = 300
        try:
            cfg = self.load()
            repo_path = Path(cfg.repo).resolve() if getattr(cfg, "repo", None) else repo_path
            gh_repo = getattr(cfg, "github_repo", "") or ""
            if "/" in gh_repo:
                owner, repo_name = gh_repo.split("/", 1)
            po_cfg = getattr(cfg, "po", None)
            provider = getattr(po_cfg, "provider", provider)
            model = getattr(po_cfg, "model", model)
            timeout_s = getattr(po_cfg, "timeout_s", timeout_s)
        except Exception:  # noqa: BLE001 — config-independent: vision discovery still runs
            pass

        # 2. Discover ProductVision. Missing/invalid is a hard exit-2.
        try:
            vision = discover(repo_path)
        except MissingVisionError as exc:
            typer.echo(f"brainstorm: {exc}", err=True)
            return 2
        except Exception as exc:  # noqa: BLE001 — unexpected validator failure
            typer.echo(f"brainstorm: failed to load product vision: {exc}", err=True)
            return 2

        report_path_arg = getattr(args, "report", None)
        output_path_arg = getattr(args, "output", None)
        if report_path_arg and not args.apply:
            typer.echo("brainstorm: --report requires --apply.", err=True)
            return 2
        source_report_path: Path | None = Path(report_path_arg) if report_path_arg else None
        source_report_hash = (
            sha256(source_report_path.read_bytes()).hexdigest()
            if source_report_path is not None and source_report_path.exists()
            else None
        )

        def _revalidate_report(raw: BrainstormReport) -> tuple[BrainstormReport, int]:
            return filter_report_for_vision(raw, vision)

        def _load_report(path: Path) -> tuple[BrainstormReport, int]:
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
                raw = BrainstormReport.model_validate(payload)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"failed to load report {path}: {exc}") from exc
            return _revalidate_report(raw)

        def _drop_duplicate_titles(
            raw: BrainstormReport, backlog: OpenBacklog
        ) -> tuple[BrainstormReport, int, list[FrontierDecision]]:
            existing = {
                normalize_candidate_key(item.title, "")[0]: item
                for item in [*backlog.epics, *backlog.tickets]
                if item.title.strip()
            }
            seen: dict[str, Any] = dict(existing)
            epics: list[ProposedEpic] = []
            tickets: list[ProposedTicket] = []
            duplicate_decisions: list[FrontierDecision] = []
            dropped = 0
            for epic in raw.proposed_epics:
                key = normalize_candidate_key(epic.title, "")[0]
                if key in seen:
                    dropped += 1
                    duplicate_decisions.append(
                        _rejected_duplicate_decision(
                            epic,
                            ProposalKind.EPIC,
                            seen[key],
                        )
                    )
                    continue
                seen[key] = epic
                epics.append(epic)
            for ticket in raw.proposed_tickets:
                key = normalize_candidate_key(ticket.title, "")[0]
                if key in seen:
                    dropped += 1
                    duplicate_decisions.append(
                        _rejected_duplicate_decision(
                            ticket,
                            ProposalKind.TICKET,
                            seen[key],
                        )
                    )
                    continue
                seen[key] = ticket
                tickets.append(ticket)
            return (
                BrainstormReport(proposed_epics=epics, proposed_tickets=tickets),
                dropped,
                duplicate_decisions,
            )

        def _source_key(kind: ProposalKind, title: str, axis: str) -> str:
            title_key, axis_key = normalize_candidate_key(title, axis)
            source = source_report_hash or str(source_report_path or "brainstorm")
            return f"{source}:{kind.value}:{axis_key}:{title_key}"

        def _rejected_duplicate_decision(
            proposal: ProposedEpic | ProposedTicket,
            kind: ProposalKind,
            duplicate: Any,
        ) -> FrontierDecision:
            duplicate_title = getattr(duplicate, "title", None)
            duplicate_issue = getattr(duplicate, "number", None)
            return FrontierDecision(
                proposal_title=proposal.title,
                proposal_kind=kind,
                axis=proposal.axis,
                outcome=FrontierDecisionOutcome.REJECTED,
                rationale="dropped during reviewed-report apply because an open backlog item already has this title and axis",
                source_key=_source_key(kind, proposal.title, proposal.axis),
                duplicate_of_title=duplicate_title if isinstance(duplicate_title, str) else None,
                duplicate_of_issue=duplicate_issue if isinstance(duplicate_issue, int) else None,
                source_report_path=str(source_report_path)
                if source_report_path is not None
                else None,
                source_report_hash=source_report_hash,
            )

        def _accepted_decision(
            proposal: ProposedEpic | ProposedTicket,
            kind: ProposalKind,
            issue_number: int,
        ) -> FrontierDecision:
            return FrontierDecision(
                proposal_title=proposal.title,
                proposal_kind=kind,
                axis=proposal.axis,
                outcome=FrontierDecisionOutcome.ACCEPTED,
                rationale="filed from reviewed brainstorm report",
                source_key=_source_key(kind, proposal.title, proposal.axis),
                issue_number=issue_number,
                source_report_path=str(source_report_path)
                if source_report_path is not None
                else None,
                source_report_hash=source_report_hash,
            )

        dropped_count = 0
        if report_path_arg:
            try:
                report, dropped_count = _load_report(Path(report_path_arg))
            except ValueError as exc:
                typer.echo(f"brainstorm: {exc}", err=True)
                return 2
        else:
            # 3. Run the brainstormer. Tests monkeypatch ``cli._brainstormer_factory``
            #    to inject a stub that skips the real SDK session.
            brainstormer = self.brainstormer_factory(
                repo_path,
                owner,
                repo_name,
                provider=provider,
                model=model,
                timeout_s=timeout_s,
            )
            try:
                report = brainstormer.run(vision)
            except Exception as exc:  # noqa: BLE001 — propagate as runtime error to operator
                typer.echo(f"brainstorm: brainstormer run failed: {exc}", err=True)
                return 1

        # 4. Dry-run path: YAML-dump the report; never touch GitHub.
        if not args.apply:
            payload = report.model_dump(mode="json")
            rendered = yaml.safe_dump(payload, sort_keys=False).rstrip()
            if output_path_arg:
                Path(output_path_arg).write_text(f"{rendered}\n", encoding="utf-8")
            typer.echo(rendered)
            return 0

        # 5. --apply path: epics first, then tickets cross-linked to the epic
        #    that was just filed in *this* run.
        try:
            gh_client = self.gh_client_factory()
        except Exception as exc:  # noqa: BLE001
            typer.echo(
                f"brainstorm: cannot construct GhClient ({exc}); set GH_TOKEN or monkeypatch _gh_client_factory.",
                err=True,
            )
            return 1

        if not owner or not repo_name:
            typer.echo(
                "brainstorm: --apply requires a configured GitHub repo (owner/name).",
                err=True,
            )
            return 2

        if report_path_arg:
            try:
                backlog = list_open_backlog(gh_client, owner, repo_name)
            except Exception as exc:  # noqa: BLE001
                typer.echo(
                    f"brainstorm: failed to scan open backlog before applying report: {exc}",
                    err=True,
                )
                return 1
            report, duplicate_count, duplicate_decisions = _drop_duplicate_titles(report, backlog)
            dropped_count += duplicate_count
            if dropped_count:
                typer.echo(
                    f"brainstorm: dropped {dropped_count} proposal(s) during report validation."
                )
        else:
            duplicate_decisions = []

        if not report.proposed_epics and not report.proposed_tickets:
            if source_report_path is not None:
                ledger = FrontierDecisionLedger(repo_path / ".forge" / "frontier-decisions.yaml")
                for decision in duplicate_decisions:
                    ledger.record(decision)
            typer.echo("brainstorm: no proposals — nothing to file.")
            return 0

        epic_axis_to_number: dict[str, int] = {}
        succeeded: list[tuple[str, int]] = []
        failed: list[tuple[str, str]] = []
        decision_ledger = (
            FrontierDecisionLedger(repo_path / ".forge" / "frontier-decisions.yaml")
            if source_report_path is not None
            else None
        )
        if decision_ledger is not None:
            for decision in duplicate_decisions:
                decision_ledger.record(decision)

        def _render_epic_body(epic: ProposedEpic) -> str:
            parts = [epic.body.strip()] if epic.body else []
            if epic.customer_story:
                parts.append(f"\n## Customer story\n\n{epic.customer_story.strip()}")
            return "\n\n".join(p for p in parts if p) or epic.title

        def _render_ticket_body(ticket: ProposedTicket, parent: int | None) -> str:
            parts: list[str] = []
            if parent is not None:
                parts.append(f"Parent: #{parent}")
            if ticket.body:
                parts.append(ticket.body.strip())
            if ticket.customer_story:
                parts.append(f"\n## Customer story\n\n{ticket.customer_story.strip()}")
            return "\n\n".join(parts) or ticket.title

        # Epics first — their numbers are threaded into ticket bodies.
        for epic in report.proposed_epics:
            labels = [f"axis:{epic.axis}", "epic"]
            body = _render_epic_body(epic)
            try:
                issue = gh_client.create_issue(
                    owner=owner,
                    repo=repo_name,
                    title=epic.title,
                    body=body,
                    labels=labels,
                )
                epic_axis_to_number[epic.axis] = issue.number
                succeeded.append((epic.title, issue.number))
                if decision_ledger is not None:
                    decision_ledger.record(
                        _accepted_decision(epic, ProposalKind.EPIC, issue.number)
                    )
            except Exception as exc:  # noqa: BLE001
                failed.append((epic.title, str(exc)))

        # Tickets — cross-link to the same-axis epic that was just filed.
        for ticket in report.proposed_tickets:
            labels = [f"axis:{ticket.axis}", "loop:ready"]
            parent = epic_axis_to_number.get(ticket.axis)
            body = _render_ticket_body(ticket, parent)
            try:
                issue = gh_client.create_issue(
                    owner=owner,
                    repo=repo_name,
                    title=ticket.title,
                    body=body,
                    labels=labels,
                )
                succeeded.append((ticket.title, issue.number))
                if decision_ledger is not None:
                    decision_ledger.record(
                        _accepted_decision(ticket, ProposalKind.TICKET, issue.number)
                    )
            except Exception as exc:  # noqa: BLE001
                failed.append((ticket.title, str(exc)))

        typer.echo("brainstorm: filed:")
        for title, number in succeeded:
            typer.echo(f"  + #{number}: {title}")
        if failed:
            typer.echo("brainstorm: failed:", err=True)
            for title, err in failed:
                typer.echo(f"  ! {title}: {err}", err=True)
            return 1
        return 0

    def _cmd_audit(self, args: SimpleNamespace) -> int:
        """`forge-loop audit` — codebase-state audit (issue #156).

        Contract:
          * Default (no flags): walk the repo, run every default probe,
            print a human-readable summary, exit 0. NO GitHub calls.
          * --apply: file one ticket per violation (idempotent — existing
            open tickets for the same probe+target are skipped).
          * --json: emit the report as JSON for scripting / dashboards.

        Errors:
          * No probe failure can cause exit != 0 on the dry-run path —
            ``audit.errors`` is surfaced as a warning but isn't a gate.
          * ``--apply`` exits 1 if ANY ticket-create call raised.
        """
        import json as _json

        from forge_loop.codebase_audit import audit, file_violations

        repo_path = Path.cwd()
        owner = ""
        repo_name = ""
        extra_labels: tuple[str, ...] = ()
        cfg: Any | None = None
        try:
            cfg = self.load()
            repo_path = Path(cfg.repo).resolve() if getattr(cfg, "repo", None) else repo_path
            gh_repo = getattr(cfg, "github_repo", "") or ""
            if "/" in gh_repo:
                owner, repo_name = gh_repo.split("/", 1)
        except Exception:  # noqa: BLE001 — audit must work even without a config
            pass

        report = audit(repo_path)

        if getattr(args, "json", False):
            payload = {
                "probes_run": report.probes_run,
                "violations": [
                    {
                        "probe": v.probe,
                        "target": v.target,
                        "severity": v.severity,
                        "title": v.title,
                        "metrics": v.metrics,
                    }
                    for v in report.violations
                ],
                "errors": report.errors,
            }
            typer.echo(_json.dumps(payload, indent=2, sort_keys=True))
        else:
            typer.echo(
                f"audit: probes_run={report.probes_run} "
                f"violations={len(report.violations)} errors={list(report.errors)}"
            )
            for v in report.violations:
                typer.echo(f"  [P{v.severity}] {v.probe}: {v.target}")
                typer.echo(f"        {v.title}")
            for probe_name, err in report.errors.items():
                typer.echo(f"  ! probe {probe_name} crashed: {err}", err=True)

        if not getattr(args, "apply", False):
            return 0

        if not owner or not repo_name:
            typer.echo(
                "audit: --apply requires github_repo configured (owner/repo)",
                err=True,
            )
            return 2

        if report.is_clean:
            typer.echo("audit: clean — nothing to file.")
            return 0

        try:
            gh_client = self.gh_client_factory()
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"audit: gh client init failed: {exc}", err=True)
            return 1

        # Wire events file from the resolved config (best-effort).
        events_file: Path | None = None
        events_file = getattr(cfg, "events_file", None)

        def _emit_filed(v: Any, number: int) -> None:
            if events_file is None:
                return
            try:
                from forge_loop.events import AuditViolationFiledEvent, emit

                emit(
                    events_file,
                    AuditViolationFiledEvent(
                        probe=v.probe,
                        target=v.target,
                        severity=v.severity,
                        issue_number=number,
                        title=v.title,
                    ),
                )
            except Exception:  # noqa: BLE001 — never let event emit kill --apply
                pass

        outcome = file_violations(
            report,
            gh_client,
            owner=owner,
            repo=repo_name,
            extra_labels=extra_labels,
            emit_filed=_emit_filed,
        )
        for v, number in outcome.filed:
            typer.echo(f"audit: filed #{number}: {v.title}")
        for v in outcome.skipped:
            typer.echo(f"audit: skipped (already filed): {v.probe}:{v.target}")
        if outcome.errors:
            for key, err in outcome.errors.items():
                typer.echo(f"audit: ERROR filing {key}: {err}", err=True)
            return 1
        return 0
