"""Command-line interface for Agent Trace Studio."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path

from agent_trace_studio import __version__
from agent_trace_studio.models import AnalysisResult
from agent_trace_studio.parser import (
    SessionResolutionError,
    analyze_journals,
    discover_journal_paths,
    parse_timestamp,
    resolve_session_ids,
)
from agent_trace_studio.report import write_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='agent-trace-studio',
        description='Build a local Agent Trace Studio dashboard from supported JSON or JSONL traces.',
    )
    parser.add_argument(
        'inputs',
        nargs='*',
        help='Trace file, directory, or recursive glob. Defaults to CODEX_HOME sessions and archived_sessions.',
    )
    parser.add_argument('-o', '--output', type=Path, default=Path('dashboard'), help='Output directory.')
    parser.add_argument(
        '--session-id',
        action='append',
        default=[],
        metavar='ID',
        help='Exact Codex session ID to resolve under CODEX_HOME. Repeatable.',
    )
    parser.add_argument(
        '--session-file',
        action='append',
        default=[],
        type=Path,
        metavar='PATH',
        help='Explicit JSON or JSONL agent trace file. Repeatable.',
    )
    parser.add_argument('--codex-home', type=Path, help='CODEX_HOME override for default discovery or session IDs.')
    parser.add_argument('--from', dest='from_time', help='Inclusive ISO-8601 timestamp or UTC date.')
    parser.add_argument('--to', dest='to_time', help='Inclusive ISO-8601 timestamp or UTC date.')
    parser.add_argument('--limit', type=_positive_int, help='Maximum number of newest journal files to analyze.')
    parser.add_argument(
        '--include-trace',
        action='store_true',
        help='Embed normalized messages and tool payloads in the local dashboard.',
    )
    parser.add_argument(
        '--trace-max-chars',
        type=_positive_int,
        default=20_000,
        metavar='N',
        help='Maximum characters retained in each trace text, input, or output field (default: 20000).',
    )
    parser.add_argument('--serve', action='store_true', help='Run the interactive dashboard on localhost.')
    parser.add_argument(
        '--supervise',
        action='store_true',
        help='Run through the local supervisor for verified restart, health checks, and rollback.',
    )
    parser.add_argument(
        '--no-supervise',
        action='store_true',
        help='Disable the default local supervisor and require manual restart after Python source changes.',
    )
    parser.add_argument('--supervised-child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--agent-state-dir', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--live-state-dir', type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        '--supervisor-health-timeout',
        type=_positive_int,
        default=45,
        metavar='SECONDS',
        help='Maximum candidate startup verification time when supervised (default: 45).',
    )
    parser.add_argument(
        '--live',
        action='store_true',
        help='Run the local server and continuously monitor loaded or hook-registered traces.',
    )
    parser.add_argument(
        '--live-poll-ms',
        type=_positive_int,
        default=750,
        metavar='N',
        help='Live file polling interval in milliseconds (default: 750).',
    )
    parser.add_argument(
        '--assurance-demo',
        action='store_true',
        help='Launch the synthetic execution-contract assurance demo.',
    )
    parser.add_argument('--port', type=_positive_int, default=8765, help='Local server port (default: 8765).')
    parser.add_argument('--qa-model', help='Model override for the configured trace Q&A provider.')
    parser.add_argument(
        '--source-workspace',
        type=Path,
        help='Agent Trace Studio checkout that audit and repair agents may inspect and update.',
    )
    parser.add_argument(
        '--agent-max-attempts',
        type=_repair_attempts,
        default=5,
        metavar='N',
        help='Maximum audit/fix/verify attempts for Fix local source (default: 5).',
    )
    parser.add_argument(
        '--title',
        help='Optional trace-set label shown below the fixed Agent Trace Studio product title.',
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw_arguments = tuple(argv) if argv is not None else tuple(sys.argv[1:])
    args = parser.parse_args(raw_arguments)
    try:
        from_time = _parse_bound(args.from_time, end_of_day=False)
        to_time = _parse_bound(args.to_time, end_of_day=True)
    except ValueError as exc:
        parser.error(str(exc))
    if from_time is not None and to_time is not None and from_time > to_time:
        parser.error('--from must not be after --to')
    if args.assurance_demo and (args.inputs or args.session_file or args.session_id):
        parser.error('--assurance-demo cannot be combined with trace inputs or session IDs')
    if args.supervise and args.no_supervise:
        parser.error('--supervise and --no-supervise cannot be combined')
    if args.supervise and args.supervised_child:
        parser.error('--supervise cannot be used by a supervised child')
    server_requested = args.serve or args.live or args.assurance_demo
    should_supervise = not args.supervised_child and (args.supervise or (server_requested and not args.no_supervise))
    if should_supervise:
        if args.source_workspace:
            source_workspace = args.source_workspace.expanduser().resolve()
            if not _is_source_checkout(source_workspace):
                parser.error('--source-workspace must point to an Agent Trace Studio checkout')
        else:
            source_workspace = _source_checkout()
        from agent_trace_studio.supervisor import run_supervisor

        return run_supervisor(
            child_args=raw_arguments,
            public_port=args.port,
            output_dir=args.output,
            source_workspace=source_workspace,
            health_timeout=args.supervisor_health_timeout,
        )
    if args.assurance_demo:
        from agent_trace_studio.assurance_demo import write_assurance_demo_journal

        demo_path = write_assurance_demo_journal(args.output / '.demo' / 'release-agent.jsonl')
        args.session_file.append(demo_path)
    missing_session_files = [path for path in args.session_file if not path.expanduser().is_file()]
    if missing_session_files:
        parser.error(f'session file not found: {missing_session_files[0]}')
    explicit_inputs = [*args.inputs, *args.session_file]
    try:
        if explicit_inputs or args.session_id:
            discovered = discover_journal_paths(explicit_inputs, codex_home=args.codex_home) if explicit_inputs else ()
            resolved = resolve_session_ids(args.session_id, codex_home=args.codex_home)
            paths = _merge_paths(resolved, discovered, limit=args.limit)
        elif args.live:
            paths = ()
        else:
            paths = discover_journal_paths(codex_home=args.codex_home, limit=args.limit)
    except SessionResolutionError as exc:
        parser.error(str(exc))
    if not paths and not args.live:
        parser.error('no supported JSON or JSONL agent traces matched the requested inputs')
    serve = args.serve or args.live or args.assurance_demo
    include_trace = args.include_trace or serve
    result = analyze_journals(
        paths,
        from_time=from_time,
        to_time=to_time,
        include_trace=include_trace,
        trace_char_limit=args.trace_max_chars,
    )
    assurance = None
    if args.assurance_demo:
        from agent_trace_studio.assurance_demo import build_assurance_demo

        assurance = build_assurance_demo(result)
    title = args.title or ('Agent Assurance Demo' if args.assurance_demo else _default_title(result))
    bundle = write_report(result, output_dir=args.output, title=title, assurance=assurance)
    if args.source_workspace:
        source_workspace = args.source_workspace.expanduser().resolve()
        if not _is_source_checkout(source_workspace):
            parser.error('--source-workspace must point to an Agent Trace Studio checkout')
    else:
        source_workspace = _source_checkout()
    output = {
        'status': 'ok',
        'assurance_demo': bool(args.assurance_demo),
        'source_files': len(result.source_paths),
        'sessions': len(result.sessions),
        'turns': len(result.turns),
        'trace_events': sum(trace.events_total for trace in result.traces),
        'skipped_files': len(result.skipped_files),
        'session_ids': [session.session_id for session in result.sessions[:10]],
        'session_ids_truncated': len(result.sessions) > 10,
        'source_paths': list(result.source_paths[:10]),
        'source_paths_truncated': len(result.source_paths) > 10,
        'index': str(bundle.index_path),
        'manifest': str(bundle.manifest_path),
    }
    if serve:
        return _serve(
            result=result,
            title=title,
            output_dir=bundle.output_dir,
            port=args.port,
            model=args.qa_model,
            source_workspace=source_workspace,
            max_attempts=args.agent_max_attempts,
            live=args.live,
            live_poll_interval=args.live_poll_ms / 1000,
            codex_home=args.codex_home,
            from_time=from_time,
            to_time=to_time,
            trace_char_limit=args.trace_max_chars,
            assurance=assurance,
            agent_state_dir=args.agent_state_dir,
            live_state_dir=args.live_state_dir,
            deployment_managed=args.supervised_child,
            output=output,
        )
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


def _serve(
    *,
    result: AnalysisResult,
    title: str,
    output_dir: Path,
    port: int,
    model: str | None,
    source_workspace: Path | None,
    max_attempts: int,
    live: bool,
    live_poll_interval: float,
    codex_home: Path | None,
    from_time: datetime | None,
    to_time: datetime | None,
    trace_char_limit: int,
    assurance: dict[str, object] | None,
    agent_state_dir: Path | None,
    live_state_dir: Path | None,
    deployment_managed: bool,
    output: dict[str, object],
) -> int:
    from agent_trace_studio.audit_rules import AuditRuleStore
    from agent_trace_studio.credentials import APIConfigStore
    from agent_trace_studio.harness_backend import SelectableRepairAgents
    from agent_trace_studio.qa import JournalQA, QASettings
    from agent_trace_studio.repair import RepairCoordinator
    from agent_trace_studio.server import DashboardState, create_dashboard_server, dashboard_server_url
    from agent_trace_studio.supervisor import child_deployment_context

    resolved_agent_state_dir = (
        agent_state_dir.expanduser().resolve()
        if agent_state_dir is not None
        else output_dir.parent / f'.{output_dir.name}-agent-state'
    )
    resolved_live_state_dir = (
        live_state_dir.expanduser().resolve()
        if live_state_dir is not None
        else output_dir.parent / f'.{output_dir.name}-live-state'
    )
    agent_backend = SelectableRepairAgents(resolved_agent_state_dir)
    repair = RepairCoordinator(
        source_workspace=source_workspace,
        state_dir=resolved_agent_state_dir,
        max_attempts=max_attempts,
        agents=agent_backend,
        deployment_managed=deployment_managed,
    )
    state = DashboardState(
        result,
        title=title,
        qa=JournalQA(
            QASettings.from_environment(model=model),
            config_store=APIConfigStore(),
            agent_backend=agent_backend,
        ),
        repair=repair,
        live=live,
        live_poll_interval=live_poll_interval,
        live_state_dir=resolved_live_state_dir,
        codex_home=codex_home,
        from_time=from_time,
        to_time=to_time,
        trace_char_limit=trace_char_limit,
        assurance=assurance,
        output_dir=output_dir,
        audit_rules=AuditRuleStore(resolved_agent_state_dir / 'audit-rules.json'),
    )
    initial_status = state.status()
    server = create_dashboard_server(output_dir=output_dir, state=state, port=port, attach_live=False)
    output.update(
        {
            'status': 'serving',
            'url': dashboard_server_url(server),
            'qa': initial_status['qa'],
            'live': initial_status['live'],
        }
    )
    print(json.dumps(output, indent=2, sort_keys=True), flush=True)
    deployment = child_deployment_context()
    public_url = deployment.get('public_url')
    state.attach_live_server(str(public_url) if public_url else dashboard_server_url(server))
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.close()
    return 0


def _parse_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    try:
        parsed_date = date.fromisoformat(text)
    except ValueError:
        parsed = parse_timestamp(text)
        if parsed is None:
            raise ValueError(f'invalid ISO-8601 timestamp: {value}') from None
        return parsed
    boundary = time.max if end_of_day else time.min
    return datetime.combine(parsed_date, boundary, tzinfo=UTC)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return parsed


def _repair_attempts(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 10:
        raise argparse.ArgumentTypeError('must be 10 or fewer')
    return parsed


def _merge_paths(*groups: Sequence[Path], limit: int | None) -> tuple[Path, ...]:
    merged = tuple(dict.fromkeys(path.expanduser().resolve() for group in groups for path in group))
    return merged[:limit] if limit is not None else merged


def _default_title(_result: AnalysisResult) -> str:
    return 'Agent Trace Studio'


def _source_checkout() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    return candidate if _is_source_checkout(candidate) else None


def _is_source_checkout(candidate: Path) -> bool:
    required = (
        candidate / 'pyproject.toml',
        candidate / 'src/agent_trace_studio/parser.py',
        candidate / 'tests',
        candidate / 'AGENTS.md',
    )
    return all(path.exists() for path in required)


if __name__ == '__main__':
    sys.exit(main())
