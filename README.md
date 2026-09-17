# Agent Trace Studio

A standalone, local-first workbench for agent execution traces. It reconstructs
turns, presents events and tool calls, and provides agent-assisted Q&A in a
self-contained HTML dashboard that opens directly from disk.

Native file adapters support Codex and Claude Code JSONL journals, while a
provider-neutral event envelope covers SDK and custom agents that expose a
stream instead of a journal. The Python distribution is `agent-trace-studio`,
and its import package is `agent_trace_studio` under `src/`. The existing
`codex-session-dashboard` command remains available as a legacy alias.
Environment variables, saved settings, and the manifest schema are unchanged.

The application runs independently and does not require another project
checkout or organization-specific infrastructure.

See [Agent Trace Studio: Product And System Design](docs/design/agent-trace-studio.md)
for the complete product, architecture, data, agent, security, live-monitoring,
and verified self-update design.

## Product tour

[![Agent Trace Studio: 30-second synthetic product tour](docs/demo/media/agent-trace-studio-demo.gif)](docs/demo/media/agent-trace-studio-demo.mp4)

[Watch the MP4 with sound](docs/demo/media/agent-trace-studio-demo.mp4)

A 30-second tour with AI-generated narration and subtle instrumental music:
load a trace, follow tool calls, and find the agent/API controls. The inline GIF
is silent; open the MP4 for sound. All examples are synthetic; the final chapter
is a labeled control illustration, not a live model request.

## Quick Start

Python 3.12 and [uv](https://docs.astral.sh/uv/) are recommended.

Start with the included synthetic examples; no API key or local journal is
required to generate this read-only report:

```bash
uv sync --locked
uv run --frozen agent-trace-studio examples/journals --output ./dashboard
```

Open `dashboard/index.html` in your browser. To inspect your own default Codex
session directories instead:

```bash
uv run agent-trace-studio
```

Analyze one Codex trace by its exact session ID (replace the synthetic ID below
with an ID from your own machine):

```bash
uv run agent-trace-studio \
  --session-id 00000000-0000-4000-8000-000000000001 \
  --include-trace \
  --output ./dashboard
```

Run the interactive dashboard with session-ID, path, and file loading plus the
Studio conversation:

```bash
uv run agent-trace-studio \
  --session-id 00000000-0000-4000-8000-000000000001 \
  --serve \
  --output ./dashboard
```

The server binds only to `127.0.0.1`. The Trace view remains usable from the
generated `index.html`; adding a Codex session ID, path, or file and using the
Studio conversation require the local server. Select **Add a source** to open
the source-loading popup. Select **API settings** in **Choose agent type** to enter
an API key, provider, model, and API base URL.

### Supervised self-update

Run through the cross-platform supervisor when parser or backend repairs should
activate automatically after verification:

```bash
uv run agent-trace-studio \
  --session-file /path/to/session.jsonl \
  --include-trace \
  --serve \
  --supervise \
  --source-workspace /path/to/agent-trace-studio \
  --output ./dashboard
```

The supervisor owns the stable loopback port and runs the dashboard on an
internal port. After a verified Python source change, it starts a candidate,
checks the process, `/api/status`, parsed payload, and generated page, then
switches traffic without changing the browser URL. The previous process remains
available as a rollback target. Failed candidates are stopped, the verified
source backup is restored with hash conflict checks, and the same fixer session
can continue with runtime feedback. CSS and JavaScript changes retain the faster
in-process bundle refresh.

Supervisor generations and logs live under `.<output-name>-supervisor`; repair
state remains under `.<output-name>-agent-state`. Agent patches cannot change
the supervisor, transactional rollback, credential, verifier, or orchestration
kernel. These controls use only Python subprocesses and loopback HTTP and do not
depend on `screen`, systemd, launchd, or Windows services.

## Execution Assurance Demo

Launch the isolated synthetic product demo:

```bash
uv run agent-trace-studio \
  --assurance-demo \
  --output ./dashboard-assurance-demo \
  --port 8767
```

The demo parses a synthetic release-agent journal through the normal trace
pipeline. Three versioned execution contracts show satisfied, violated, and
pending states. **Replay evaluation** follows the relevant events, evidence
buttons select the exact trace event, and **Investigate** places the selected
contract and its evidence into the trace-agent context. No external agent or
model call is made until the user sends a message.

## Session Audit Rules

In interactive mode, **Review rules** opens the local audit-rule catalog. Rules
can forbid an event, require an event, or require one matching event before or
after another. Matchers use normalized trace fields such as tool, kind,
category, role, phase, status, and a case-insensitive text fragment. Rules are
deterministic data, not executable code.

**Review rules** opens each saved rule in a read-only view with its exact
definition, revision metadata, current-session result, and trace evidence.
Choose **Edit rule** only when a change is needed; **New rule** opens a separate
blank editor.

You can edit a rule directly, or ask the selected Studio agent to draft one by
writing an instruction such as `Require a test command after apply_patch`. An
agent draft is only a proposal: review its exact matcher and select **Approve
and save** before it changes the active set. Proposals expire, are bound to the
current browser session and base rule version, and cannot overwrite a newer
manual edit.

Each rule may optionally carry one `send_session_message` automatic action with
an exact, user-reviewed message. The selected Studio agent may draft this field
only when the current user instruction explicitly requests automatic delivery;
the action becomes authoritative only after **Approve and save**.

The catalog is stored in `.<output-name>-agent-state/audit-rules.json`, with an
append-only revision log in `audit-rules-history.jsonl`. Required later events
remain pending while a live session is open and become violations only when the
session is complete. Semantic LLM judgments are not part of the rule engine.

## Live Monitoring

Start an empty dashboard that waits for an agent run, or pass one or more
existing files and continue following them as they grow:

```bash
uv run agent-trace-studio --live --output ./dashboard-live
```

Live mode polls registered JSON/JSONL files and sends revision notifications to
the browser over server-sent events. **Follow live** selects the newest event.
Selecting a source, turn, event, category, tool, or search query turns following
off so later events do not move the current selection or scroll position.

An interactive dashboard that was not launched with `--live` can be opted in at
runtime with **Start live monitor & audit**. The button begins file polling,
publishes the loopback hook descriptor, and evaluates the active deterministic
rule set whenever the selected trace refreshes.

When a rule with an automatic action is violated by new evidence for a Codex
trace with an exact session UUID, the configured message is delivered without a
second click. **Start live monitor & audit** is the explicit arming action;
violations already present when live mode starts or when a rule is saved are
baselined and are not replayed. The server sends only the approved literal
message, trusted rule metadata, and bounded trace anchors in a read-only Codex
turn. Raw trace text cannot author the action or gain source-write/deployment
authority. Identical deliveries are deduplicated, failures can be retried
manually, and non-secret receipts are appended to
`.<output-name>-agent-state/audit-actions.jsonl`. Rules without an automatic
action retain the explicit **Send message to session agent** notification button.

| Agent source | Live adapter |
| --- | --- |
| Codex | `SessionStart` registers its native journal path; `SessionEnd` closes it |
| Claude Code | `SessionStart` registers its native transcript, parsed by the Claude adapter |
| LangGraph, OpenAI Agents SDK, or a custom runner | Publish normalized events through the local Python API |
| Any growing supported JSON/JSONL file | Start with `--session-file PATH` or load the path in the dashboard |

### Codex and Claude Code hooks

Install the two console commands somewhere visible to the agent process:

```bash
uv tool install --editable .
```

For Codex, merge these entries into `~/.codex/hooks.json`. Do not replace other
hooks already in the file:

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "agent-trace-studio-hook codex",
        "timeout": 3
      }]
    }],
    "SessionEnd": [{
      "hooks": [{
        "type": "command",
        "command": "agent-trace-studio-hook codex",
        "timeout": 3
      }]
    }]
  }
}
```

For Claude Code, merge the equivalent entries into
`~/.claude/settings.json`, changing the adapter argument:

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "agent-trace-studio-hook claude-code",
        "timeout": 3
      }]
    }],
    "SessionEnd": [{
      "hooks": [{
        "type": "command",
        "command": "agent-trace-studio-hook claude-code",
        "timeout": 3
      }]
    }]
  }
}
```

The hook reads lifecycle JSON from standard input, contacts only the current
loopback dashboard, produces no output, and always exits successfully. If no
dashboard is running, the agent run continues normally. Codex or Claude Code
must run on the same machine as the dashboard so its transcript path is locally
readable.

### SDK and custom agents

Frameworks that stream events but do not maintain a compatible local journal
can publish a small normalized envelope. Call this from a LangGraph stream
consumer, an OpenAI Agents SDK trace processor, or your runner's callbacks:

```python
from agent_trace_studio import finish_live_session, publish_live_event

publish_live_event(
    adapter="langgraph",
    session_id=run_id,
    turn_id=thread_id,
    cwd=workspace,
    model=model_name,
    event={
        "timestamp": timestamp,
        "category": "tool",
        "kind": "tool_finished",
        "tool_name": "search_docs",
        "call_id": call_id,
        "status": "completed",
        "input": {"query": query},
        "output": {"matches": match_count},
        "duration_secs": duration,
    },
)

finish_live_session(adapter="langgraph", session_id=run_id, turn_id=thread_id)
```

`category` accepts `message`, `tool`, `reasoning`, `lifecycle`, or `context`.
Events may also include `title`, `role`, `phase`, `text`, `input_text`, and
`output_text`. A changed `turn_id` creates a new dashboard turn. Published
events are appended to a replayable Studio-owned journal in the hidden sibling
directory `.<output-name>-live-state`; they are not sent to a model by the live
monitor.

The local server descriptor and ingestion token are stored at
`~/.agent-trace-studio/live-server.json` with user-only permissions where the
platform supports them. Set `AGENT_TRACE_STUDIO_SERVER_FILE` for a different
location or to isolate multiple monitor instances. The implementation uses
`pathlib`, localhost HTTP, and generated console scripts, so the same protocol
works on macOS, Linux, and Windows.

The local server enables **Investigate session** and **Extract memories**.
When it is started from this checkout, it also enables **Audit parser**. If a
completed audit finds supported issues, **Fix it** appears directly below the
audit result. An installed copy can target a downloaded checkout explicitly:

```bash
uv run agent-trace-studio \
  --session-file /path/to/session.jsonl \
  --serve \
  --source-workspace /path/to/agent-trace-studio
```

Session IDs, paths, and uploads are additive while the server is running. Select
**Add a source** in Source workspace, then enter an exact **Codex session ID**,
enter a path, or select several JSON/JSONL files. The popup closes after a
successful load; errors remain visible there for retry. Switch among the loaded
traces from the source area.
Duplicate session IDs are rejected so the selected trace remains unambiguous
across Trace and the Studio conversation.

The ID is matched against `session_meta.payload.id` under
`$CODEX_HOME/sessions` and `$CODEX_HOME/archived_sessions`. If the same exact
session was copied or archived more than once, the newest journal is selected.

Analyze a supplied session file instead:

```bash
uv run agent-trace-studio \
  --session-file /path/to/session.jsonl \
  --include-trace \
  --output ./dashboard
```

With no input arguments, the CLI reads `*.json` and `*.jsonl` recursively from:

- `$CODEX_HOME/sessions`
- `$CODEX_HOME/archived_sessions`
- `~/.codex/sessions` and `~/.codex/archived_sessions` when `CODEX_HOME` is unset

The default report is written to `./dashboard/index.html`.

The browser title and primary heading are always **Agent Trace Studio**. The
backward-compatible `--title` option supplies an optional trace-set label below
that product heading and in the machine-readable exports; it does not rename
the product.

Analyze an explicit file, directory, or recursive glob using the backward-compatible
positional input:

```bash
uv run agent-trace-studio \
  ~/.codex/sessions \
  --from 2026-08-01 \
  --to 2026-08-15 \
  --output ./dashboard
```

```bash
uv run agent-trace-studio \
  'exports/**/*.jsonl' \
  --limit 500 \
  --title 'Production Agent Runs' \
  --output ./runtime-dashboard
```

Build the synthetic demo:

```bash
uv run agent-trace-studio examples/journals --output ./dashboard
```

## Supported Input: Codex

The parser follows the Codex journal structure used by the original analyzer:

- `session_meta` for session, repository, source, and conversation metadata
- `turn_context` for model, effort, and working-directory context
- `event_msg` lifecycle events for started, completed, aborted, and incomplete turns
- `event_msg: token_count` snapshots, de-duplicated by cumulative token totals
- `response_item` function, custom-tool, web-search, and reasoning events
- `event_msg` reasoning and context-compaction events

Session files may be supplied as:

- native line-delimited JSON journal rows
- a JSON array of journal rows
- a JSON object with an `events`, `entries`, `records`, or `items` array
- a JSON object with `content_text`, `journal`, or `jsonl` containing JSONL text

Unknown records are ignored. Malformed and non-object JSONL rows are counted
and shown in the dashboard's Data view and `manifest.json`.

## Supported Input: Claude Code

Claude Code transcripts are detected from their native `sessionId`, `user`,
`assistant`, `tool_use`, and `tool_result` records. The adapter reconstructs
turns, pairs tool calls and outputs, accounts for token usage, and keeps thinking
blocks out of the normalized trace. The source file remains read-only.

## Trace Analysis

`--include-trace` adds a local execution-trace workbench. It groups
events by turn, pairs tool calls with their outputs, suppresses duplicate
message rows, and provides category and text filters over readable event
details. Oversized text, tool input, and tool output fields are truncated with
an explicit marker. Change the per-field limit with `--trace-max-chars`.

The trace includes normalized user, assistant, developer, subagent, and tool
content. It excludes encrypted reasoning, binary payloads, and raw journal
rows. Empty encrypted reasoning records and high-volume accounting events such
as token snapshots are collapsed from the trace while remaining represented in
aggregate metrics.

The Turns, Events, and Detail panes are resizable. Drag the separators to adjust
desktop widths or stacked pane heights, and drag below the desktop workbench to
adjust its overall height. Arrow keys resize a focused separator, double-click
restores its default, and the layout is saved locally in the browser.

Dashboard panels can also be moved and resized. **Reset layout** clears their
saved geometry and immediately restores the full-width default panel stack;
panel collapse preferences are preserved.

The Trace view includes total calls, unique-tool counts, top-tool filters, and
individual tool events. Separate aggregate dashboard views are intentionally
omitted.

## Studio conversation

The floating conversation replaces the separate Studio Console panel. Its
**Context** section continuously reflects the current source, turn, and event,
and shows available capabilities. **API settings** in the **Choose agent type**
panel opens the provider settings. **Run details** contains activity, verification results,
approvals, and Continue, Start over, and Discard controls; it opens automatically
when a run needs action. These sections can be collapsed while reading chat.

Each explicit message captures an immutable dashboard snapshot containing those
selection identifiers plus the selected checkpoint, highlighted text, trace view,
search query, event category, and tool filter. The snapshot is sent as
`dashboard_state` alongside the controller request and is authoritative for that
agent turn, even when the selected turn or event changed since the preceding
question. The server adds the latest persisted session brief; client-supplied
brief text cannot replace that record.

The selected agent first chooses one typed action: answer, investigate, extract
memories, update the session brief and checkpoints, audit the parser, repair
the parser, customize the dashboard, or control the current run. For
chat-routed source changes, deterministic code suppresses recognized
high-confidence non-requests such as direct questions, hypotheticals, negations,
reported speech, and trace-text references. That classifier is not the write
authorization boundary.

Source-changing requests do not start immediately. The server prepares a
short-lived approval bound to the action, instruction, current dashboard
selection, source workspace digest, verified-runtime activation mode, and
browser-session nonce. The floating conversation shows an **Approve change and
activate** card that discloses the post-verification activation and rollback
behavior. Only approving that card starts parser repair, dashboard
customization, or a source-changing continue/restart. Trace content and previous
conversation never consume that approval.

Answers and action routing run through the selected **Agent type**. A Studio
turn starts with the current conversation, immutable dashboard state, journal
resource metadata, and available host actions, but no automatically selected
journal excerpts. Pydantic AI invokes native read-only tools to inspect the
current selection, search the active session, or read a turn. OpenCode, Codex
SDK can return the same typed context actions; the host
executes them against normalized events and returns only those bounded results
before asking for the final answer or action. The selected turn does not split
the conversation: navigation changes the current user state while the complete
session conversation remains available. Previous answers never count as
evidence, and no runtime receives the whole raw journal or encrypted reasoning.

Agent answers render as local, sanitized Markdown with headings, lists, links,
tables, quotations, inline code, fenced code blocks, and highlighted turn/line
citations. Raw HTML from model output is never injected into the page.

Studio context actions use compact **Calling / Called** rows with the tool name
and requested arguments, following the Codex tool-call presentation. Each call
updates in place from running to completed or failed, with its elapsed time.
The row shows a short result preview; expand it to inspect arguments and up to
4,000 characters of the bounded evidence returned to the agent. Preview and
context truncation are marked separately. Credentials, local absolute paths,
raw journal records, and encrypted content are omitted from these previews.
The server emits these details only for its allowlisted context actions;
native harness notices and source-changing workflow activity remain status
messages. Expanded call details survive polling and conversation refreshes.

The message composer is the general workflow entry point. The persistent
**Session Brief** panel also has an explicit **Summarize session** command.
Loading a source or changing a selection never starts a workflow automatically.

Eight portable Agent Skills describe trace Q&A, investigation, memory
extraction, session checkpoints, parser audit, parser repair, dashboard
customization, and run control. Pydantic AI loads them on demand with
`load_capability`; OpenCode loads
the same `SKILL.md` files through its native skill tool. Skills provide behavior
guidance only. Typed server code owns workflow execution and safety checks.

## Session Investigation And Memories

Asking the agent to investigate the session produces a structured account of the selected session:
its objective, outcome, key actions, findings, issues, lessons, and grounded
turn/line evidence. Asking it to extract memories returns up to 12 reusable preference,
workflow, technical, decision, failure, or project-context candidates with
confidence and evidence anchors. It does not automatically write those
candidates into another memory system.

Both workflows are state-aware. They start from journal resource metadata and
the selected source, turn, and event coordinates, then direct their own bounded
searches and turn reads. They use normalized trace events rather than raw
journal rows, omit encrypted reasoning and the local journal path, and remain
available without a source checkout.

## Session Brief And Checkpoints

Press **Summarize session** in the persistent panel near the top of the
dashboard, or ask the Studio Agent to update the session brief. The selected
agent reconstructs one chronological account across every turn in the selected
session. It groups meaningful milestones into expandable checkpoints rather
than listing every tool call. The selected turn and event influence focus only;
they do not limit the summary scope.

Each revision stores the session objective, outcome, executive summary,
checkpoint statuses, named artifacts, blockers, next steps, and turn/line
anchors. It also records the covered event cursor, event and turn counts, and a
digest of the normalized trace prefix. Large sessions use bounded, representative
event detail and visibly report sampling or truncation; coverage metadata refers
to the normalized session observed by the host, not a claim that every raw
payload fit in one model prompt.

The host reserves part of the configured evidence budget for agent-directed
retrieval. Pydantic AI can search normalized events and read selected turns with
the same anchored, read-only tools used by Trace Q&A. External agent types first
return a bounded search/turn plan; the host executes that plan against normalized
events and supplies only the bounded results for the final brief. Neither path
exposes the loaded session-file path, encrypted reasoning, or raw JSONL rows.
If an external agent returns JSON that fails the checkpoint schema, the host
returns safe validation paths and the bounded invalid answer to the same agent
for up to two correction attempts. The full trace evidence is not repeated in
those correction prompts, and an uncorrected response fails with visible field
details rather than a generic validation error.

In live mode, **Auto-update** is opt-in. When new events arrive, the workflow
passes the previous persisted brief plus events after its cursor to the same
summary process. Completed checkpoints are retained, an open checkpoint can be
updated, and genuinely new milestones are appended. If the covered trace prefix
was rewritten or truncated, the workflow rebuilds the brief instead of merging
against an invalid history. No model call is made when the cursor has not moved.

Clicking a checkpoint marks it as the active checkpoint in the agent context,
independently of the selected turn. Subsequent questions receive its title,
status, turn IDs, event range, summary, actions, achievements, blockers,
artifacts, next steps, and evidence anchors. The server also injects the latest
persisted session brief into every agent request. Both remain model-generated
derived context, so material claims must still be checked against the anchored
normalized trace.

Text in the trace, checkpoint report, or prior agent answers can be highlighted
and right-clicked to choose **Ask about selection**. This sends a fixed
explanatory question with the bounded excerpt as untrusted UI focus; highlighted
content cannot authorize workflows or source changes.

## Parser Audit And Local Repair

Asking the agent to audit the parser runs the selected backend in a fresh read-only role against the selected
dashboard state, the parser source, tests, and bounded structural journal
evidence. The evidence includes record keys, discriminator values, counts, and
normalized event coverage. It excludes raw journal rows, prompts, responses,
tool payloads, and encrypted reasoning.

An explicit request such as `Fix this parser` starts a maximum five-attempt
loop. A matching completed audit is reused when the source snapshot is
unchanged; otherwise the repair workflow audits before editing:

```text
audit -> fix shadow copy -> tests and journal replay -> read-only verifier
            ^                                      |
            +----------- structured feedback ------+
```

The fixer uses one logical conversation in the selected backend for the whole repair
run and can edit or run allowlisted commands only inside a temporary shadow
copy. Its message history and plan are checkpointed locally. After each
independent verification, structured failures are returned to that same fixer
conversation so it continues from its prior work instead of starting over.
For Pydantic AI, tiered compaction first clears old tool results and then summarizes older
messages when the conversation exceeds 72% of the selected model's context
window, while retaining the recent repair and verification exchange.

Every attempt runs the unit suite, Ruff, a replay of the selected journal
through the candidate parser, and the dashboard JavaScript syntax check when
Node.js is available. The independent verifier receives the complete patch and
host-confirmed check output but has read-only file access. A model verdict
cannot override a failed deterministic gate.

Shadow copies and source snapshots exclude Git metadata, virtual environments,
build outputs, package metadata, dashboards, and `.*-agent-state` runtime data.

Only a passing patch is copied into the configured local checkout. Before
applying, the backend checks file hashes so concurrent user edits cannot be
overwritten. Changed files are backed up, replaced transactionally, and tested
again in the real checkout. A failed post-apply check rolls the source back.
Repeated identical failures stop early; otherwise the run stops after
`--agent-max-attempts` (maximum 10). No branch or commit is created.

If a candidate changes a protected control-plane file, creates a symbolic-link
change, or exceeds the workspace limits, the host records that candidate as a
rejected attempt before running project code or the independent verifier. It
then resets the shadow workspace to the immutable baseline and returns the
exact policy failure to the same logical fixer session for the next attempt.

Run history and redacted audit artifacts are stored in a hidden sibling of the
report directory, such as `.dashboard-agent-state`. API keys are never written
into that history. A running job can be asked to stop from the dashboard;
cancellation takes effect after the current model step.

Each model request has a five-minute timeout and two bounded transport retries
with backoff. Transport retries do not consume repair attempts. If they are
exhausted, or a run stops for another reason, the dashboard displays the stop
reason and recovery actions. A repair checkpoint can be continued, started
over, or discarded. You can change the provider or model in **API settings**
before continuing; the fallback is recorded and the preserved fixer
conversation resumes. You can also change the agent type before continuing a
paused repair; the new runtime receives the preserved workspace and verifier
feedback as an explicit fallback. Workflows without a resumable source
checkpoint can be started over.

The current-run panel includes a bounded live activity window. It persists
workspace, evidence, model, and tool lifecycle messages across refreshes, but
never stores prompts, generated text, tool arguments, tool results, or dynamic
tool names in that feed. For repair runs its summary shows the issues being
fixed, independently verified fixed items grouped by attempt, and candidate
files. Verification attempts are collapsed by default; opening one reveals its
repair summary, changed files, deterministic checks, independent verdict, and
unresolved findings.

For workflows started from the **Studio conversation**, the conversation also shows one
temporary activity row. It updates in place with only the latest lifecycle
message, then disappears when the final workflow answer is added.

An empty **Studio conversation** presents context-aware suggested prompts for
session investigation, brief updates, memory extraction, parser audit, and the
current event or checkpoint. These use the normal message route. **Customize the
dashboard...** only prefills the composer, and any resulting source change still
requires the explicit approval card.

## Dashboard Customization

An explicit request such as `Make the turns panel wider` sends the exact current
message to the approval card first, then to the coding agent after approval.
Customization uses the same isolated workspace, persistent fixer session,
deterministic checks, independent verifier, transactional apply, rollback,
timeout recovery, and provider fallback as parser repair. The local source
changes only after verification passes; no branch or commit is created. When
the server was launched with `--supervise`, the same consumed approval permits
the coordinator to health-check and activate the verified version automatically.
The coding model cannot restart or promote a runtime directly.

## Agent Type

The **Agent type** selector controls every workflow: chat and action routing,
session investigation, memory extraction, session summaries, parser audit,
source editing, and independent verification. It selects
the same runtime, but not the same permissions: Q&A is read-only and receives
only bounded trace evidence, while source changes are restricted to the shadow
workspace and must pass independent verification. The selection is stored as
non-secret local state in `.dashboard-agent-state/agent-harness.json` and
survives page and server restarts. OpenCode is selected by default.

Available choices are:

- **OpenCode** uses its own credential store and is the default.
- **Pydantic AI** uses the provider configured in **API settings** for all roles.
- **Google ADK** uses the provider configured in **API settings** for all roles,
  including OpenAI, Anthropic, and Google Gemini. Its native ADK agent loop calls
  the same bounded dashboard tools and returns the same validated action types.
- **Codex SDK** uses existing Codex authentication for all roles.

Audit and verification use fresh read-only sessions in that same backend, never
the fixer's conversation. Deterministic checks and transactional application
remain mandatory. Codex SDK and OpenCode workflows do not require a separate dashboard API key
and never silently fall back to Pydantic AI.

To use Google ADK, run `uv sync`, restart the dashboard, select **Google ADK**
in **Choose agent type**, and open **API settings** in that panel. Select the
provider, model and provider-specific key; an existing saved configuration works
without re-entering it. OpenAI uses the Responses API through LiteLLM, Anthropic
uses its Messages API through LiteLLM, and Google uses ADK's native Gemini
adapter. Keys are supplied directly to the selected provider client, never to
agent subprocess environments. No Google key is needed when using OpenAI.

The ADK dependency and LiteLLM adapter are version-locked in `uv.lock`. ADK
owns the model/tool loop; Studio owns bounded retrieval, structured action
validation, approvals, cancellation, and usage reporting. Its source fixer can
read/edit files only through workspace-scoped tools and run the fixed Python
test command. Its auditor/verifier have read-only file tools and fresh sessions.
All source changes still pass the same deterministic gates and host transaction.

Codex source reviews require a CLI supporting named permission profiles (0.147+).
Before each review, a no-model sandbox probe must prove that the shadow is
readable but not writable, a synthetic sibling file cannot be read, and network
access is denied. Inherited connectors, plugins, hooks, memory, and subagents
are disabled; explicit skill syntax in the review input is neutralized. Native
authentication is preserved. MCP configuration discovery may check OAuth status,
but its transport/credential output is never exposed. Unsupported or unprovable
confinement fails closed; it does not retry with broader permissions. A platform
that blocks the probe itself, or uses different network-denial errors, may need
an explicitly selected Pydantic AI backend instead.

OpenCode reviews require a supported 1.x release (1.18.18+), a symlink-free
temporary review copy outside Git ancestors, and isolated configuration. Native
credentials remain in the original data directory. Project configuration, LSP,
and external skills are disabled; home-directory custom configuration that
cannot be isolated causes the review to refuse rather than loading extra tools.
Remote account/organization configuration, well-known configuration providers,
and managed policy files also refuse native OpenCode review: they can change
between configuration inspection and the model turn. Only local auth types and
account-presence metadata are checked; keys/tokens are never returned or copied.
The final resolved permissions are checked before starting the fresh agent.

Claude Agent SDK is no longer a runnable choice. A saved selection of that
backend displays a removal notice and requires a new selection; old run and
conversation history is retained. Select a supported backend before continuing
an old source-change run through its normal approval flow. Claude Code trace
imports and Anthropic API models remain supported.

Studio conversations persist independently for each loaded trace and agent type.
While the dashboard server is running, Codex SDK keeps one live `Thread` object
per Studio conversation and calls `thread.run()` again for every new turn.
OpenCode keeps one loopback-only headless server per Studio conversation and
sends each turn directly to the same native session. After a dashboard restart,
both harnesses reconstruct their live runtime from the persisted Codex thread ID
or OpenCode session ID. Pydantic AI and Google ADK store provider-neutral
user/assistant messages. ADK seeds a fresh request-local session from completed
exchanges; it does not persist provider reasoning or tool payloads. The ADK fixer
separately persists completed exchanges across repair attempts and inspects the
preserved candidate before continuing an interrupted turn.
This state lives under `.dashboard-agent-state/studio-conversations`, uses
owner-only permissions, and survives page and server restarts. **New chat**
closes the live harness runtime and clears the current Studio conversation
across all agent types. Trace evidence is still selected on demand through the
bounded read-only context tools; raw journal rows are not stored in conversation
state.

Unavailable harnesses remain visible with their readiness reason. The selector
distinguishes a missing OpenCode login from a failed native startup/credential
check. A log-file or filesystem-access error is not evidence that the saved key
is missing: OpenCode must be able to use its native log and data directories.
If the dashboard was launched inside a restricted agent session, relaunch it
from a normal local terminal with those permissions. Do not delete credentials,
copy keys into a new data directory, or add them to subprocess environments to
work around the restriction. A successful `opencode auth list` reporting zero
credentials is the case that requires `opencode auth login`.

The selector
cannot change while a workflow is active, but it can be changed before starting
or continuing a paused source-change run. Each runtime preserves its resumable
conversation state so independent verification can return feedback to the same
logical coding session.

Use `AGENT_TRACE_STUDIO_AGENT_TYPE` to override the initial selection. The old
`AGENT_TRACE_STUDIO_CODING_HARNESS` name remains a compatibility fallback. The
supported values are `opencode`, `pydantic`, `google-adk`, and `codex-sdk`.
Optional executable/model overrides are
`AGENT_TRACE_STUDIO_OPENCODE_BIN`, `AGENT_TRACE_STUDIO_OPENCODE_MODEL`,
`AGENT_TRACE_STUDIO_CODEX_SDK_ENTRY`, and `AGENT_TRACE_STUDIO_CODEX_MODEL`.

The configured model API powers workflows when Pydantic AI or Google ADK is selected.
API keys are removed from external agent subprocess
environments.

Use `CODEX_SESSION_DASHBOARD_QA_PROVIDER` to select `openai`, `anthropic`, or
`google` from the environment. Provider keys are read from `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, or `GEMINI_API_KEY`/`GOOGLE_API_KEY`; the generic
`CODEX_SESSION_DASHBOARD_QA_API_KEY` takes precedence. Use `--qa-model` or
`CODEX_SESSION_DASHBOARD_QA_MODEL` to override the provider default. A custom
base URL can be supplied with `CODEX_SESSION_DASHBOARD_QA_BASE_URL`; the
existing `OPENAI_BASE_URL` fallback applies to OpenAI.

You can also configure these values from the dashboard while the local server
is running. **Remember on this device** prefers macOS Keychain, Windows
Credential Locker, or Linux Secret Service/KWallet. If no recognized native
backend is available, the dialog stores the key in an AES-GCM encrypted local
vault derived from a user-supplied password with scrypt. The vault password is
never stored, so it must be entered again after a server restart. A page refresh
does not require another unlock while the server remains running.

For unattended headless Linux or container use, set
`AGENT_TRACE_STUDIO_VAULT_PASSWORD` in the server environment to unlock the
encrypted vault at startup. It is treated as a secret and removed from agent
subprocess environments. Uncheck **Remember on this device** to keep the API key
in process memory only. **Clear key** removes both the active and remembered
credential. There is no plaintext file fallback.

The key is never returned by an API endpoint, embedded in generated files, or
logged. Set `AGENT_TRACE_STUDIO_CONFIG_DIR` to override the non-secret settings
directory. Remote base URLs must use HTTPS; HTTP is accepted only for loopback
test providers. Changing providers requires entering that provider's key so a
key is never silently reused across providers.

## Outputs

Every report directory contains:

| File | Purpose |
| --- | --- |
| `index.html` | Self-contained interactive dashboard |
| `analysis.json` | Normalized dashboard payload, including opt-in trace events |
| `manifest.json` | Input, output, counts, parse health, and schema metadata |
| `sessions.csv` | One row per parsed session |
| `turns.csv` | One row per reconstructed turn |
| `metrics.csv` | Long-form daily and tool metrics |

The HTML uses no network resources for read-only inspection. Interactive source
loading and Q&A use the optional loopback server.

## Privacy Boundary

Agent traces can contain prompts, responses, tool inputs, tool outputs,
credentials, and customer context. The default report reads those rows only to
derive structural metrics and does **not** embed their content.

`--include-trace` is an explicit local-sensitive mode. It embeds normalized
messages and tool payloads in `index.html` and `analysis.json`. It still excludes
raw journal rows and encrypted reasoning, but it is not a redaction or secret
scanner. Treat a trace report with the same care as its source journal.

Q&A sends retrieved normalized evidence to the configured model provider only
after a user asks a question. API credentials remain server-side, are never
returned in public status, and are never embedded in the report payload. A
remembered credential is held by a native system store or as authenticated
ciphertext in the password-protected local vault.

Every agent workflow requires an explicit message or command press. Investigation,
memory extraction, and checkpoint summaries use bounded normalized trace evidence. Parser audit and
repair use structural journal evidence and source-code artifacts, not the raw
journal. Repair commands receive an environment with common secret-bearing
variables removed.

Derived files still contain local paths, repository metadata, session IDs,
conversation IDs, and aggregate token/tool usage. Treat the report directory
as local sensitive data unless you review or redact it for another audience.

## Architecture

```text
session ID or JSON/JSONL files/directories/globs
        |
        v
parser.py        tolerant event parsing, turn reconstruction, optional trace normalization
        |
        v
analytics.py     aggregate metrics and normalized dashboard payload
        |
        v
report.py        HTML, JSON, manifest, and CSV exports
        |
        +--> index.html       read-only dashboard, directly openable from disk
        |
        +--> server.py        loopback path/upload and Q&A API
                  |
                  +--> qa.py             Studio Agent and read-only retrieval tools
                  +--> credentials.py    native keyrings and encrypted vault fallback
                  |
                  +--> repair.py         persisted analysis/audit/fix workflow runner
                           |
                           +--> agent_backend.py  structured schemas and Pydantic AI roles
                           +--> adk_backend.py    native Google ADK loop and provider adapters
                           +--> harness_backend.py selected runtime for every workflow role
                           +--> workflow_roles.py native result validation and read-only integrity gates
                           +--> workspace.py      shadow copy, hash checks, apply/rollback
                           +--> repair_probe.py   privacy-safe parser replay metrics
```

The trace parser and static report remain dependency-light. Structured agent
roles use the selected runtime; Pydantic AI and Google ADK use the OpenAI,
Anthropic, and Google provider adapters locked in `uv.lock`. OpenCode and Codex SDK are
optional local agent runtimes and expose readiness in the dashboard.

## Development

Application code lives in `src/agent_trace_studio/`; tests and documentation
remain in the top-level `tests/` and `docs/` folders. Python integrations should
import from `agent_trace_studio`. After pulling the package rename, run
`uv sync --locked` to refresh the editable installation and CLI entry points.

```bash
uv run python -m unittest discover -s tests
uv run ruff check .
uv run ruff format --check .
```

The tests and examples use synthetic journals only. The cross-platform GitHub
Actions workflow runs the locked suite on macOS, Windows, and Ubuntu.
