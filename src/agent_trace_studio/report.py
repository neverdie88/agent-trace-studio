"""Write a self-contained static dashboard and machine-readable exports."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from dataclasses import asdict
from html import escape
from importlib import resources
from pathlib import Path

from agent_trace_studio.analytics import PRODUCT_TITLE, build_dashboard_payload, metric_rows, overview_metrics
from agent_trace_studio.models import AnalysisResult, ReportBundle, SessionSummary, TurnSummary


def write_report(
    result: AnalysisResult,
    *,
    output_dir: Path,
    title: str = 'Agent Trace Studio',
    assurance: dict[str, object] | None = None,
) -> ReportBundle:
    """Write HTML, JSON, manifest, and CSV files into ``output_dir``."""

    resolved_output = output_dir.expanduser().resolve()
    resolved_output.mkdir(parents=True, exist_ok=True)
    bundle = ReportBundle(
        output_dir=resolved_output,
        index_path=resolved_output / 'index.html',
        manifest_path=resolved_output / 'manifest.json',
        analysis_path=resolved_output / 'analysis.json',
        sessions_csv_path=resolved_output / 'sessions.csv',
        turns_csv_path=resolved_output / 'turns.csv',
        metrics_csv_path=resolved_output / 'metrics.csv',
    )
    payload = build_dashboard_payload(result, title=title, assurance=assurance)
    bundle.analysis_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    _write_csv(bundle.sessions_csv_path, (_session_csv_row(item) for item in result.sessions))
    _write_csv(bundle.turns_csv_path, (_turn_csv_row(item) for item in result.turns))
    _write_csv(bundle.metrics_csv_path, metric_rows(result))
    manifest = {
        'schema_version': 'codex-session-dashboard.v2',
        'generated_at': result.generated_at,
        'title': PRODUCT_TITLE,
        'trace_set_title': title if title != PRODUCT_TITLE else '',
        'source_paths': list(result.source_paths),
        'skipped_files': list(result.skipped_files),
        'issues': [asdict(issue) for issue in result.issues],
        'sessions_total': len(result.sessions),
        'turns_total': len(result.turns),
        'overview': overview_metrics(result),
        'outputs': {
            'index': bundle.index_path.name,
            'analysis': bundle.analysis_path.name,
            'sessions_csv': bundle.sessions_csv_path.name,
            'turns_csv': bundle.turns_csv_path.name,
            'metrics_csv': bundle.metrics_csv_path.name,
        },
        'content_policy': {
            'raw_journal_rows_embedded': False,
            'normalized_trace_events_embedded': bool(result.traces),
            'prompts_embedded': bool(result.traces),
            'responses_embedded': bool(result.traces),
            'tool_payloads_embedded': bool(result.traces),
            'encrypted_reasoning_embedded': False,
        },
    }
    bundle.manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    bundle.index_path.write_text(_render_html(payload, title=title), encoding='utf-8')
    return bundle


def _render_html(payload: dict[str, object], *, title: str) -> str:
    package_root = resources.files('agent_trace_studio')
    css = package_root.joinpath('assets/dashboard.css').read_text(encoding='utf-8')
    conversation_state = package_root.joinpath('assets/qa_conversation_state.js').read_text(encoding='utf-8')
    dashboard = package_root.joinpath('assets/dashboard.js').read_text(encoding='utf-8')
    javascript = f'{conversation_state}\n{dashboard}'
    serialized = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    safe_payload = serialized.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>{escape(PRODUCT_TITLE)}</title>
  <style>{css}</style>
</head>
<body>
  <header class="topbar">
    <div class="brand-lockup">
      <span class="product-kicker">Local agent trace workbench</span>
      <h1 id="dashboard-title">{escape(PRODUCT_TITLE)}</h1>
    </div>
    <div class="topbar-context">
      <p id="dashboard-trace-label" class="muted" hidden></p>
      <p id="generated-label" class="muted"></p>
      <button id="reset-layout" class="command-button compact secondary" type="button"
              title="Restore full-width default panel positions and sizes">Reset layout</button>
    </div>
  </header>
  <main id="floating-workspace" aria-label="Movable dashboard panels">
    <section id="agent-selection-panel" class="agent-selection-panel" aria-labelledby="agent-selection-title"
             data-floating-panel>
      <div class="agent-selection-heading floating-panel-handle" data-floating-handle tabindex="0"
           aria-label="Move agent selection panel" title="Drag to move; use arrow keys to reposition">
        <span class="control-label">Studio agent</span>
        <h2 id="agent-selection-title">Choose agent type</h2>
      </div>
      <label class="agent-harness-control">
        <span class="agent-context-label">Agent type</span>
        <select id="agent-harness-select" aria-describedby="agent-actions-status" disabled></select>
      </label>
      <div id="agent-selection-settings" class="agent-selection-settings">
        <button id="qa-configure-button" class="command-button compact secondary" type="button"
                aria-haspopup="dialog" aria-controls="qa-config-dialog" disabled>API settings</button>
        <span id="agent-actions-status" class="muted" role="status">Available in local server mode</span>
      </div>
    </section>
    <section id="source-workspace" class="source-workspace" aria-labelledby="source-workspace-title"
             data-floating-panel>
      <div class="workspace-heading floating-panel-handle" data-floating-handle tabindex="0"
           aria-label="Move source workspace panel" title="Drag to move; use arrow keys to reposition">
        <div>
          <span class="control-label">Trace sources</span>
          <h2 id="source-workspace-title">Source workspace</h2>
        </div>
        <button id="add-source-button" class="command-button secondary" type="button"
                aria-haspopup="dialog" aria-controls="source-dialog">Add a source</button>
      </div>
      <div class="source-workspace-grid">
        <section id="source-context" class="source-context" aria-label="Loaded trace set">
          <div class="source-panel-heading">
            <h3>Loaded traces</h3>
          </div>
          <label id="loaded-session-field" class="select-field loaded-session-field">
            <span>Loaded trace</span>
            <select id="trace-session-select"></select>
          </label>
          <div class="source-current">
            <span class="control-label">Current source</span>
            <strong id="current-source"></strong>
            <span id="source-status" class="muted" aria-live="polite"></span>
            <div class="source-facts" aria-label="Current trace metadata">
              <div>
                <span class="control-label">Adapter</span>
                <strong id="current-source-adapter"></strong>
              </div>
              <div>
                <span class="control-label">Turns</span>
                <strong id="current-source-turn-count"></strong>
              </div>
              <div>
                <span class="control-label">Events</span>
                <strong id="current-source-event-count"></strong>
              </div>
            </div>
            <div id="live-audit-control" class="live-audit-control" hidden>
              <button id="live-audit-start" class="command-button secondary compact" type="button">
                Start live monitor &amp; audit
              </button>
              <span id="live-audit-status" class="muted" role="status"></span>
            </div>
            <div id="live-monitor" class="live-monitor" hidden>
              <span id="live-state" class="live-state" data-state="starting">
                <span class="live-dot" aria-hidden="true"></span>
                <strong id="live-state-label">Connecting</strong>
              </span>
              <span id="live-message" class="muted" aria-live="polite"></span>
              <label class="live-follow-control">
                <input id="follow-live" type="checkbox" checked>
                <span>Follow live</span>
              </label>
            </div>
          </div>
        </section>
      </div>
    </section>

    <section id="session-brief-panel" class="session-brief-panel" aria-labelledby="session-brief-title"
             data-floating-panel hidden>
      <div class="session-brief-heading floating-panel-handle" data-floating-handle tabindex="0"
           aria-label="Move session brief panel" title="Drag to move; use arrow keys to reposition">
        <div>
          <span class="control-label">Session understanding</span>
          <h2 id="session-brief-title">Session brief</h2>
          <p id="session-brief-status" class="muted" aria-live="polite"></p>
        </div>
        <div class="session-brief-controls">
          <label id="session-brief-auto-field" class="session-brief-auto" hidden>
            <input id="session-brief-auto" type="checkbox">
            <span>Auto-update live</span>
          </label>
          <button id="session-brief-button" class="command-button secondary" type="button">
            Summarize session
          </button>
          <button id="session-brief-toggle" class="icon-button collapse-toggle session-brief-toggle" type="button"
                  aria-label="Collapse session brief" aria-controls="session-brief-content"
                  aria-expanded="true" title="Collapse session brief">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="m6 9 6 6 6-6"></path>
            </svg>
          </button>
        </div>
      </div>
      <div id="session-brief-content" class="session-brief-content"></div>
    </section>

    <section id="assurance-panel" class="assurance-panel" aria-labelledby="assurance-title"
             data-floating-panel hidden>
      <div class="assurance-heading floating-panel-handle" data-floating-handle tabindex="0"
           aria-label="Move session audit panel" title="Drag to move; use arrow keys to reposition">
        <div>
          <span id="assurance-label" class="control-label">Audit rules</span>
          <h2 id="assurance-title">Session audit</h2>
          <p id="assurance-summary" class="muted"></p>
        </div>
        <div class="assurance-heading-actions">
          <button id="assurance-manage" class="command-button secondary" type="button" hidden>Review rules</button>
          <button id="assurance-replay" class="command-button secondary" type="button">Replay evaluation</button>
          <button id="assurance-toggle" class="icon-button collapse-toggle assurance-toggle" type="button"
                  aria-label="Collapse session audit" aria-controls="assurance-content"
                  aria-expanded="true" title="Collapse session audit">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="m6 9 6 6 6-6"></path>
            </svg>
          </button>
        </div>
      </div>
      <div id="assurance-content">
        <div class="assurance-overview" aria-live="polite">
          <div class="assurance-kpi">
            <span>Active rules</span>
            <strong id="assurance-active-count">0</strong>
          </div>
          <div class="assurance-kpi violation">
            <span>Open violations</span>
            <strong id="assurance-violation-count">0</strong>
          </div>
          <div class="assurance-kpi pending">
            <span>Pending obligations</span>
            <strong id="assurance-pending-count">0</strong>
          </div>
          <p id="assurance-replay-status" class="assurance-replay-status muted"></p>
        </div>
        <div id="assurance-contract-list" class="assurance-contract-list"></div>
      </div>
    </section>

    <section id="trace" data-floating-panel>

      <div class="section-heading trace-heading floating-panel-handle" data-floating-handle tabindex="0"
           aria-label="Move trace workbench panel" title="Drag to move; use arrow keys to reposition">
        <div>
          <span class="control-label">Trace workbench</span>
          <h2>Agent execution trace</h2>
          <p id="trace-summary" class="muted"></p>
        </div>
      </div>

      <section class="tool-summary-band" aria-label="Tool summary">
        <div class="tool-kpi"><span>Total calls</span><strong id="trace-tool-total"></strong></div>
        <div class="tool-kpi"><span>Unique tools</span><strong id="trace-tool-unique"></strong></div>
        <div id="trace-tool-chips" class="tool-chips"></div>
      </section>

      <div class="trace-toolbar">
        <label class="search-field trace-search-field">
          <span>Search trace</span>
          <input id="trace-search" type="search" autocomplete="off">
        </label>
        <div id="trace-category-filters" class="segmented-control trace-category-filters"
             role="group" aria-label="Trace event category"></div>
      </div>

      <div id="trace-workbench" class="trace-workbench">
        <section class="trace-pane trace-turn-pane" aria-label="Turns">
          <div class="trace-pane-heading"><h3>Turns</h3><span id="trace-turn-count" class="muted"></span></div>
          <div id="trace-turn-list" class="trace-list trace-turn-list"></div>
        </section>
        <div id="trace-turn-resizer" class="trace-splitter" role="separator" tabindex="0"
             aria-label="Resize turns panel" aria-orientation="vertical" title="Resize turns panel"></div>
        <section class="trace-pane trace-event-pane" aria-label="Events">
          <div class="trace-pane-heading"><h3>Events</h3><span id="trace-event-count" class="muted"></span></div>
          <div id="trace-event-list" class="trace-list trace-event-list"></div>
        </section>
        <div id="trace-event-resizer" class="trace-splitter" role="separator" tabindex="0"
             aria-label="Resize events panel" aria-orientation="vertical" title="Resize events panel"></div>
        <section id="trace-detail" class="trace-pane trace-detail" aria-live="polite"></section>
      </div>
      <div id="trace-height-resizer" class="trace-height-resizer" role="separator" tabindex="0"
           aria-label="Resize trace workbench height" aria-orientation="horizontal"
           title="Resize trace workbench height"></div>
    </section>



  </main>
  <dialog id="source-dialog" class="source-dialog" aria-labelledby="source-dialog-title">
        <section class="source-loader" aria-label="Load agent traces">
          <div class="source-panel-heading">
            <h2 id="source-dialog-title">Add a source</h2>
            <button id="source-dialog-close" class="icon-button" type="button"
                    aria-label="Close Add a source" title="Close">&times;</button>
          </div>
          <p id="source-loader-status" class="source-loader-status" role="status">
            Read-only report. Open Agent Trace Studio through its local server to add sources.
          </p>
          <p id="source-dialog-status" class="source-dialog-status" role="status" hidden></p>
          <div class="source-adapter-catalog" aria-label="Supported trace adapters">
            <span class="control-label">Trace adapters</span>
            <div class="source-adapter-list">
              <span title="Native session lookup and JSONL traces">Codex</span>
              <span title="Native transcript files">Claude Code</span>
              <span title="Canonical live events">LangGraph</span>
              <span title="Canonical live events">OpenAI Agents SDK</span>
              <span title="Canonical live events">Custom runner</span>
            </div>
          </div>
          <form id="session-id-form" class="path-form">
            <label class="path-field">
              <span class="control-label">Codex session ID</span>
              <input id="session-id" type="text" autocomplete="off" autocapitalize="none" spellcheck="false"
                     maxlength="128" placeholder="00000000-0000-4000-8000-000000000001">
            </label>
            <button id="load-session-id-button" class="command-button" type="submit">Add session</button>
          </form>
          <form id="path-form" class="path-form">
            <label class="path-field">
              <span class="control-label">Trace path</span>
              <input id="session-path" type="text" autocomplete="off" placeholder="/path/to/agent-trace.jsonl">
            </label>
            <button id="load-path-button" class="command-button" type="submit">Add path</button>
          </form>
          <button id="upload-button" class="command-button secondary" type="button">Add files</button>
          <input id="session-upload" type="file" accept=".json,.jsonl,application/json" multiple hidden>
        </section>
  </dialog>
  <button id="qa-floating-button" class="qa-floating-button" type="button"
          aria-label="Open Studio conversation" aria-controls="qa-conversation-popover"
          aria-expanded="false" title="Open Studio conversation; drag to move">
    <svg class="qa-floating-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"></path>
    </svg>
    <span id="qa-unread-count" class="qa-unread-count" hidden></span>
  </button>
  <section id="qa-conversation-popover" class="qa-conversation-popover" role="dialog"
           aria-labelledby="qa-conversation-title" hidden>
    <header class="qa-conversation-header">
      <div>
        <h2 id="qa-conversation-title">Studio conversation</h2>
        <p id="qa-conversation-status" class="muted"></p>
      </div>
      <div class="qa-conversation-header-actions">
        <button id="qa-new-conversation" class="command-button compact secondary" type="button"
                aria-label="Start a new Studio conversation" title="Clear persisted agent context">New chat</button>
        <button id="qa-conversation-toggle" class="icon-button collapse-toggle" type="button"
                aria-label="Collapse Studio conversation"
                aria-controls="qa-context-details qa-conversation-body qa-highlight-context qa-form"
                aria-expanded="true" title="Collapse Studio conversation">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="m6 9 6 6 6-6"></path>
          </svg>
        </button>
        <button id="qa-conversation-close" class="icon-button" type="button"
                aria-label="Close Studio conversation" title="Close Studio conversation">&times;</button>
      </div>
    </header>
    <details id="qa-context-details" class="qa-context-details">
      <summary>Context <span id="qa-context-summary">No source loaded</span></summary>
        <section class="agent-context" aria-label="Current dashboard context">
          <div class="agent-context-item">
            <span class="agent-context-label">Source</span>
            <strong id="agent-context-source"></strong>
            <span id="agent-context-source-meta" class="agent-context-meta"></span>
          </div>
          <div class="agent-context-item">
            <span class="agent-context-label">Turn</span>
            <strong id="agent-context-turn"></strong>
            <span id="agent-context-turn-meta" class="agent-context-meta"></span>
          </div>
          <div class="agent-context-item">
            <span class="agent-context-label">Event</span>
            <strong id="agent-context-event"></strong>
            <span id="agent-context-event-meta" class="agent-context-meta"></span>
          </div>
          <div class="agent-context-item">
            <span id="agent-context-analysis-label" class="agent-context-label">Checkpoint</span>
            <strong id="agent-context-checkpoint"></strong>
            <span id="agent-context-checkpoint-meta" class="agent-context-meta"></span>
          </div>
          <div class="agent-context-item">
            <span id="agent-context-focus-label" class="agent-context-label">Focus</span>
            <strong id="agent-context-focus"></strong>
            <span id="agent-context-focus-meta" class="agent-context-meta"></span>
          </div>
        </section>
          <div class="agent-capability-row">
            <span class="control-label">Studio capabilities</span>
            <div id="agent-capability-list" class="agent-capability-list"
                 aria-label="Studio capability availability"></div>
          </div>
    </details>
    <div id="qa-conversation-body" class="qa-conversation-body">
      <div id="qa-messages" class="qa-messages" aria-live="polite"></div>
      <details id="qa-run-details" class="qa-run-details" hidden>
        <summary>Run details <span id="qa-run-summary"></span></summary>
          <div id="agent-activity" class="agent-activity" hidden>
            <div class="agent-activity-heading">
              <span class="control-label">Agent activity</span>
              <span id="agent-activity-status" class="muted"></span>
            </div>
            <ol id="agent-activity-list" class="agent-activity-list" role="log"
                aria-live="polite" aria-relevant="additions text"></ol>
          </div>
          <section id="agent-run-panel" class="agent-run-panel" hidden>
            <div class="agent-run-heading">
              <div>
                <span id="agent-run-label" class="control-label">Current run</span>
                <strong id="agent-run-title"></strong>
                <span id="agent-run-session-meta" class="agent-run-session-meta muted"></span>
              </div>
              <div class="agent-run-controls">
                <span id="agent-run-state" class="run-state"></span>
                <button id="agent-run-cancel" class="command-button secondary danger" type="button" hidden>Stop</button>
              </div>
            </div>
            <p id="agent-run-message" class="agent-run-message"></p>
            <div class="agent-run-progress">
              <span id="agent-run-attempt" class="muted"></span>
              <div class="run-progress-track"><span id="agent-run-progress-fill"></span></div>
            </div>
            <div id="agent-repair-summary" class="agent-repair-summary" hidden></div>
            <section id="agent-run-recovery" class="agent-run-recovery" role="alert" hidden>
              <div class="agent-recovery-heading">
                <div>
                  <span class="control-label">Action needed</span>
                  <strong id="agent-recovery-title">Agent stopped</strong>
                </div>
                <button id="agent-run-configure-api" class="command-button secondary" type="button">
                  API settings
                </button>
              </div>
              <p id="agent-recovery-reason"></p>
              <label id="agent-recovery-instruction-field" class="agent-recovery-instruction">
                <span class="control-label">Additional direction</span>
                <textarea id="agent-recovery-instruction" rows="2" maxlength="2000"></textarea>
              </label>
              <div class="agent-recovery-actions">
                <button id="agent-run-activate" class="command-button" type="button">Approve activation</button>
                <button id="agent-run-continue" class="command-button" type="button">Continue</button>
                <button id="agent-run-restart" class="command-button secondary" type="button">Start over</button>
                <button id="agent-run-discard" class="command-button secondary danger" type="button">
                  Discard candidate
                </button>
              </div>
              <p id="agent-recovery-feedback" class="agent-recovery-feedback muted" aria-live="polite"></p>
            </section>
            <div id="agent-run-audit" class="agent-run-section"></div>
            <div id="agent-run-attempts" class="agent-run-section"></div>
            <div id="agent-run-result" class="agent-run-section"></div>
          </section>
      </details>
    </div>
    <div id="qa-highlight-context" class="qa-highlight-context" aria-live="polite" hidden>
      <div>
        <span id="qa-highlight-label" class="control-label">Highlighted text</span>
        <span id="qa-highlight-text"></span>
      </div>
      <button id="qa-highlight-clear" class="icon-button" type="button"
              aria-label="Clear ask context" title="Clear ask context">&times;</button>
    </div>
    <form id="qa-form" class="qa-composer">
      <label>
        <span class="control-label">Message</span>
        <textarea id="qa-question" rows="3" maxlength="4000"></textarea>
      </label>
      <div class="qa-composer-actions">
        <button id="qa-stop" class="command-button secondary danger" type="button" hidden>Stop</button>
        <button id="qa-submit" class="command-button" type="submit">Send</button>
      </div>
    </form>
  </section>
  <div id="studio-context-menu" class="studio-context-menu" role="menu"
       aria-label="Studio actions" hidden>
    <button id="studio-context-menu-ask" type="button" role="menuitem">Ask agent</button>
  </div>
  <dialog id="qa-config-dialog" class="qa-config-dialog" aria-labelledby="qa-config-title">
    <form id="qa-config-form" class="qa-config-form">
      <div class="qa-config-header">
        <div>
          <h2 id="qa-config-title">Configure model API</h2>
          <p class="muted">
            Used for all workflows when Agent type is Pydantic AI or Google ADK.
            Codex SDK and OpenCode use their own authentication.
          </p>
        </div>
      </div>
      <label class="qa-config-field">
        <span class="control-label">Provider</span>
        <select id="qa-api-provider" required></select>
      </label>
      <label class="qa-config-field">
        <span class="control-label">API key</span>
        <input id="qa-api-key" type="password" maxlength="8192" autocomplete="off" spellcheck="false">
        <small id="qa-api-key-hint" class="muted"></small>
      </label>
      <label class="qa-config-field">
        <span class="control-label">Model</span>
        <select id="qa-api-model-preset" required></select>
        <input id="qa-api-model" type="text" maxlength="200" autocomplete="off" spellcheck="false"
               placeholder="Custom model ID" hidden>
      </label>
      <label class="qa-config-field">
        <span class="control-label">API base URL</span>
        <input id="qa-api-base-url" type="url" maxlength="2048" autocomplete="off" spellcheck="false" required>
        <small class="muted">Bounded trace evidence, source audit artifacts, and the key may be sent here.</small>
      </label>
      <label class="qa-remember-option">
        <input id="qa-remember-key" type="checkbox" checked>
        <span>
          <strong>Remember on this device</strong>
          <small id="qa-remember-key-hint" class="muted">Stored in the system credential store.</small>
        </span>
      </label>
      <div id="qa-vault-fields" class="qa-vault-fields" hidden>
        <label class="qa-config-field">
          <span class="control-label">Vault password</span>
          <input id="qa-vault-password" type="password" minlength="12" maxlength="1024"
                 autocomplete="current-password" spellcheck="false">
          <small id="qa-vault-password-hint" class="muted"></small>
        </label>
        <label id="qa-vault-confirm-field" class="qa-config-field">
          <span class="control-label">Confirm vault password</span>
          <input id="qa-vault-password-confirm" type="password" minlength="12" maxlength="1024"
                 autocomplete="new-password" spellcheck="false">
        </label>
      </div>
      <p id="qa-config-feedback" class="qa-config-feedback" aria-live="polite"></p>
      <div class="qa-config-actions">
        <button id="qa-clear-key" class="command-button secondary danger" type="button">Clear key</button>
        <div class="qa-config-primary-actions">
          <button id="qa-config-cancel" class="command-button secondary" type="button">Cancel</button>
          <button id="qa-config-save" class="command-button" type="submit">Save API</button>
        </div>
      </div>
    </form>
  </dialog>
  <dialog id="audit-rules-dialog" class="audit-rules-dialog" aria-labelledby="audit-rules-title">
    <div class="audit-rules-shell">
      <header class="audit-rules-header">
        <div>
          <span class="control-label">Session assurance</span>
          <h2 id="audit-rules-title">Audit rules</h2>
          <p id="audit-rules-revision" class="muted"></p>
        </div>
        <button id="audit-rules-close" class="icon-button" type="button"
                aria-label="Close audit rules" title="Close audit rules">&times;</button>
      </header>
      <p id="audit-rules-feedback" class="audit-rules-feedback muted" aria-live="polite"></p>
      <div class="audit-rules-workspace">
        <aside class="audit-rule-index" aria-label="Saved audit rules">
          <div class="audit-rule-index-heading">
            <span class="control-label">Saved rules</span>
          </div>
          <div id="audit-rule-list" class="audit-rule-list"></div>
        </aside>
        <section id="audit-rule-review" class="audit-rule-review" aria-label="Selected audit rule">
          <div id="audit-rule-review-content" class="audit-rule-review-content"></div>
          <div class="audit-rule-review-actions">
            <button id="audit-rule-edit" class="command-button" type="button">Edit rule</button>
          </div>
        </section>
        <form id="audit-rule-form" class="audit-rule-form" hidden>
          <input id="audit-rule-version" type="hidden">
          <div class="audit-rule-form-row audit-rule-form-row-title">
            <label>
              <span class="control-label">Rule ID</span>
              <input id="audit-rule-id" type="text" maxlength="64" pattern="[a-z][a-z0-9\\-]{{2,63}}" required>
            </label>
            <label>
              <span class="control-label">Severity</span>
              <select id="audit-rule-severity" required>
                <option value="critical">Critical</option>
                <option value="high">High</option>
                <option value="medium" selected>Medium</option>
                <option value="low">Low</option>
              </select>
            </label>
          </div>
          <label>
            <span class="control-label">Title</span>
            <input id="audit-rule-title" type="text" maxlength="160" required>
          </label>
          <label>
            <span class="control-label">Expected behavior</span>
            <textarea id="audit-rule-expectation" rows="3" maxlength="2000" required></textarea>
          </label>
          <div class="audit-rule-form-row">
            <label>
              <span class="control-label">Rule type</span>
              <select id="audit-rule-type" required>
                <option value="forbid_event">Event must not occur</option>
                <option value="require_event">Event must occur</option>
                <option value="require_before">Required event before trigger</option>
                <option value="require_after">Required event after trigger</option>
              </select>
            </label>
            <div class="audit-rule-switches">
              <label><input id="audit-rule-enabled" type="checkbox" checked> Enabled</label>
              <label id="audit-rule-same-turn-field" hidden>
                <input id="audit-rule-same-turn" type="checkbox"> Same turn
              </label>
            </div>
          </div>
          <fieldset class="audit-matcher-fields">
            <legend id="audit-rule-event-legend">Event matcher</legend>
            <div class="audit-matcher-grid">
              <label><span>Tool</span><input id="audit-rule-event-tool" type="text" maxlength="200"></label>
              <label><span>Kind</span><input id="audit-rule-event-kind" type="text" maxlength="120"></label>
              <label><span>Category</span><input id="audit-rule-event-category" type="text" maxlength="80"></label>
              <label><span>Role</span><input id="audit-rule-event-role" type="text" maxlength="80"></label>
              <label><span>Phase</span><input id="audit-rule-event-phase" type="text" maxlength="120"></label>
              <label><span>Status</span><input id="audit-rule-event-status" type="text" maxlength="80"></label>
              <label><span>Contains</span><input id="audit-rule-event-contains" type="text" maxlength="500"></label>
            </div>
          </fieldset>
          <fieldset id="audit-rule-required-fields" class="audit-matcher-fields" hidden>
            <legend>Required event</legend>
            <div class="audit-matcher-grid">
              <label><span>Tool</span><input id="audit-rule-required-tool" type="text" maxlength="200"></label>
              <label><span>Kind</span><input id="audit-rule-required-kind" type="text" maxlength="120"></label>
              <label><span>Category</span><input id="audit-rule-required-category" type="text" maxlength="80"></label>
              <label><span>Role</span><input id="audit-rule-required-role" type="text" maxlength="80"></label>
              <label><span>Phase</span><input id="audit-rule-required-phase" type="text" maxlength="120"></label>
              <label><span>Status</span><input id="audit-rule-required-status" type="text" maxlength="80"></label>
              <label><span>Contains</span><input id="audit-rule-required-contains" type="text" maxlength="500"></label>
            </div>
          </fieldset>
          <fieldset class="audit-matcher-fields audit-action-fields">
            <legend>Automatic action</legend>
            <label class="audit-action-toggle">
              <input id="audit-rule-action-enabled" type="checkbox">
              <span>Send a message to the monitored Codex session when new evidence violates this rule</span>
            </label>
            <label id="audit-rule-action-message-field" hidden>
              <span class="control-label">Exact session message</span>
              <textarea id="audit-rule-action-message" rows="3" maxlength="1000"></textarea>
            </label>
            <small class="muted">
              Runs read-only after live monitoring is armed. Existing historical violations are not sent.
            </small>
          </fieldset>
          <div class="audit-rule-form-actions">
            <button id="audit-rule-archive" class="command-button secondary danger" type="button" hidden>
              Disable rule
            </button>
            <div class="audit-rule-form-primary-actions">
              <button id="audit-rule-cancel-edit" class="command-button secondary" type="button">Cancel</button>
              <button id="audit-rule-save" class="command-button" type="submit">Save rule</button>
            </div>
          </div>
        </form>
      </div>
    </div>
  </dialog>
  <script id="dashboard-data" type="application/json">{safe_payload}</script>
  <script>{javascript}</script>
</body>
</html>
"""


def _session_csv_row(session: SessionSummary) -> dict[str, object]:
    return asdict(session)


def _turn_csv_row(turn: TurnSummary) -> dict[str, object]:
    row = asdict(turn)
    row['tool_breakdown'] = json.dumps(dict(turn.tool_breakdown), sort_keys=True)
    return row


def _write_csv(path: Path, rows: Iterable[dict[str, object]]) -> None:
    materialized = list(rows)
    if not materialized:
        path.write_text('', encoding='utf-8')
        return
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(materialized[0]))
    writer.writeheader()
    writer.writerows(materialized)
    path.write_text(stream.getvalue(), encoding='utf-8')
