'use strict';

// Exercise the real Refresh summary handler and Studio message lifecycle with
// synthetic API responses. Automatic refresh remains a background workflow.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(
  process.argv[2] || path.join(__dirname, '../src/agent_trace_studio/assets/dashboard.js'), 'utf8',
);
function functionSource(name) {
  const declaration = new RegExp('\\n  (?:async )?function ' + name + '\\(');
  const start = source.search(declaration);
  assert.notEqual(start, -1, 'Dashboard function must exist: ' + name);
  const remaining = source.slice(start + 1);
  const end = remaining.search(/\n  (?:async )?function /);
  return remaining.slice(0, end < 0 ? remaining.length : end);
}

function fixture({ fail = false, defer = false, answerOnly = false } = {}) {
  const calls = [];
  const tracked = [];
  const rendered = [];
  let resolveMessage;
  let refreshes = 0;
  let scrolls = 0;
  const state = {
    qaBusy: false, qaConversationOpen: false, qaConversationCollapsed: true,
    qaHistory: [], qaMessages: [], qaConversationBootstrapped: true,
    qaConversationId: 'synthetic-conversation', clientSessionNonce: 'synthetic-client-nonce',
    agentBusy: false, sessionBriefBusy: false, agentRun: null,
    sessionBrief: { available: true, revision: 6, status: 'current', stale: false },
    hasSession: true,
  };
  const runtime = {
    interactive: true,
    qa: { configured: true, agent_harness: 'Codex', agent_model: 'Test model', agent_uses_api_settings: false },
  };
  const run = { run_id: 'synthetic-summary-run', kind: 'checkpoints', status: 'running' };
  const activity = { activity_id: 'synthetic-request', status: fail ? 'failed' : 'completed', activity: [] };
  const response = {
    kind: answerOnly ? 'answer' : 'workflow', answer: answerOnly ? 'Summary explanation.' : 'Starting a session-wide checkpoint summary.',
    run: answerOnly ? null : run, request_activity: activity,
  };
  const terminal = (status) => ['completed', 'failed', 'cancelled'].includes(status);
  const context = vm.createContext({
    state, runtime, AbortController, window: { setTimeout: () => 1 },
    dashboardStateSnapshot: () => ({
      source: { session_id: state.hasSession ? 'synthetic-session' : '' },
      selection: { turn_id: 'synthetic-turn' },
    }),
    newStudioRequestId: () => 'synthetic-request',
    terminalAgentStatus: terminal, terminalActivityStatus: terminal,
    stopSessionBriefAutoRefresh: () => {},
    renderSessionBrief: () => rendered.push({ busy: state.sessionBriefBusy, status: state.sessionBrief.status }),
    renderAgentActions: () => {}, renderQA: () => {}, persistQAConversation: () => {}, markQAUnread: () => {},
    scrollQAConversationToBottom: () => { scrolls += 1; },
    scheduleStudioActivityPoll: () => {}, stopStudioActivityPolling: () => {},
    openQAConversation: () => { state.qaConversationOpen = true; state.qaConversationCollapsed = false; },
    upsertQAActivity: (record) => {
      const index = state.qaMessages.findIndex((item) => item.activity_id === record.activity_id);
      const message = { ...record, role: 'activity' };
      if (index < 0) state.qaMessages.push(message);
      else state.qaMessages[index] = message;
    },
    trackStartedRun: (started, workflow) => {
      if (!started) return;
      tracked.push({ run: started, context: workflow });
      state.agentRun = started;
    },
    refreshSessionBrief: async () => {
      refreshes += 1;
      state.sessionBrief = { ...state.sessionBrief, status: state.agentRun ? 'updating' : 'current' };
    },
    apiJson: async (url, options) => {
      calls.push({ url, body: options?.body ? JSON.parse(options.body) : null });
      if (url === '/api/agent/message') {
        if (fail) throw new Error('Synthetic request failure');
        if (defer) await new Promise((resolve) => { resolveMessage = resolve; });
        return response;
      }
      if (url === '/api/agent/checkpoints') return run;
      if (url === '/api/agent/messages/synthetic-request') return activity;
      throw new Error('Unexpected API call: ' + url);
    },
  });
  vm.runInContext([
    'qaStatusText', 'sendAgentMessage', 'summarizeSessionCheckpoints',
  ].map(functionSource).join('\n'), context);
  return { context, state, runtime, calls, tracked, rendered, resolve: () => resolveMessage(), refreshes: () => refreshes, scrolls: () => scrolls };
}

(async () => {
  const manual = fixture({ defer: true });
  const pending = manual.context.summarizeSessionCheckpoints();
  assert(manual.state.qaConversationOpen, 'Refresh summary opens the Studio conversation.');
  assert.equal(manual.scrolls(), 1, 'Sending an explicit message scrolls to the latest conversation output.');
  assert(!manual.state.qaConversationCollapsed);
  assert(manual.state.qaBusy && manual.state.sessionBriefBusy);
  assert(!manual.state.agentBusy, 'A manual summary must not disable the Studio Stop control.');
  assert.equal(manual.state.qaMessages[0].role, 'user');
  assert.equal(manual.state.qaMessages[0].text, 'Update the session-wide checkpoint brief from the current trace.');
  assert.equal(manual.calls.length, 1);
  assert.equal(manual.calls[0].url, '/api/agent/message');
  assert.equal(manual.calls[0].body.conversation_id, 'synthetic-conversation');
  assert.equal(manual.calls[0].body.session_id, 'synthetic-session');
  assert.equal(manual.calls[0].body.request_id, 'synthetic-request');
  assert.equal(manual.calls[0].body.message, manual.state.qaMessages[0].text);
  await manual.context.summarizeSessionCheckpoints();
  assert.equal(manual.calls.length, 1, 'Repeated clicks cannot start duplicate refreshes.');
  manual.resolve();
  await pending;
  assert(!manual.state.qaBusy && !manual.state.agentBusy && !manual.state.sessionBriefBusy);
  assert.equal(manual.tracked.length, 1);
  assert.equal(manual.tracked[0].context.session_id, 'synthetic-session');
  assert.equal(manual.tracked[0].context.history_index, 0, 'Workflow completion is linked to the conversation history.');
  assert.equal(manual.state.qaHistory[0].question, manual.calls[0].body.message);
  assert(manual.state.qaMessages.some((item) => item.role === 'assistant'));
  assert.equal(manual.refreshes(), 1);

  const stopping = fixture({ defer: true });
  const finishing = stopping.context.summarizeSessionCheckpoints();
  stopping.state.agentBusy = true;
  stopping.resolve();
  await finishing;
  assert(stopping.state.agentBusy, 'Manual cleanup must not clear a concurrent Stop operation busy flag.');

  for (const options of [{ fail: true }, { answerOnly: true }]) {
    const test = fixture(options);
    await test.context.summarizeSessionCheckpoints();
    assert(!test.state.qaBusy && !test.state.agentBusy && !test.state.sessionBriefBusy);
    assert.equal(test.state.sessionBrief.status, 'current', 'A non-workflow response cannot leave the summary stuck updating.');
    assert.equal(test.state.sessionBrief.revision, 6);
    assert.equal(test.refreshes(), 1);
    if (options.fail) assert(test.state.qaMessages.some((item) => item.role === 'error' && item.text === 'Synthetic request failure'));
  }

  const automatic = fixture();
  await automatic.context.summarizeSessionCheckpoints({ automatic: true });
  assert.deepEqual(automatic.calls.map((call) => call.url), ['/api/agent/checkpoints']);
  assert.equal(automatic.tracked.length, 1);
  assert(!automatic.state.qaConversationOpen, 'Automatic updates do not interrupt the user or send chat messages.');
  assert.equal(automatic.scrolls(), 0, 'Background updates do not move the conversation scroll position.');
  assert.equal(automatic.state.qaMessages.length, 0);

  for (const reason of ['offline', 'unconfigured', 'no-session', 'qa-busy', 'agent-busy', 'summary-busy', 'workflow-running']) {
    const test = fixture();
    if (reason === 'offline') test.runtime.interactive = false;
    if (reason === 'unconfigured') test.runtime.qa.configured = false;
    if (reason === 'no-session') test.state.hasSession = false;
    if (reason === 'qa-busy') test.state.qaBusy = true;
    if (reason === 'agent-busy') test.state.agentBusy = true;
    if (reason === 'summary-busy') test.state.sessionBriefBusy = true;
    if (reason === 'workflow-running') test.state.agentRun = { status: 'running' };
    await test.context.summarizeSessionCheckpoints();
    assert.equal(test.calls.length, 0, reason + ' must not send an action.');
    assert(!test.state.qaConversationOpen);
  }

  const status = fixture();
  assert.equal(status.context.qaStatusText(true), 'Codex · Test model');
  status.runtime.qa.agent_uses_api_settings = true;
  status.runtime.qa.provider = 'Synthetic provider';
  assert.equal(status.context.qaStatusText(true), 'Codex · Synthetic provider · Test model · active for this server');
  assert(!source.includes('harness authentication'), 'The removed phrase must not remain in visible frontend status text.');
  assert(source.includes("document.getElementById('session-brief-button').addEventListener('click', () => summarizeSessionCheckpoints());"));
  console.log('Manual summary conversation, workflow tracking, failure recovery, automatic refresh, and status wording checks passed.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
