'use strict';

const assert = require('node:assert/strict');
const state = require('../src/agent_trace_studio/assets/qa_conversation_state.js');

const traces = [
  {
    session_id: 'session1',
    session_file: '/tmp/session1.jsonl',
    events: [
      { turn_id: 'turn1', sequence: 10 },
      { turn_id: 'turn2', sequence: 20 },
    ],
  },
];

const pendingActivity = state.normalizeActivityRecord({
  activity_id: 'request-pending',
  request_id: 'request-pending',
  status: 'running',
  activity: [],
});
assert.equal(pendingActivity.activity.length, 0);

const toolActivity = state.normalizeActivityRecord({
  request_id: 'request-tools',
  status: 'completed',
  activity_revision: 3,
  expanded_steps: ['synthetic-call-1', '../invalid'],
  activity: [{
    phase: 'Tool',
    message: 'Read rules.',
    at: '2026-01-01T00:00:00Z',
    details: {
      kind: 'context_action',
      id: 'synthetic-call-1',
      tool: 'read_dashboard_resource',
      status: 'completed',
      arguments: { resource: 'audit_rules', limit: 20, api_key: 'PRIVATE_ARGUMENT' },
      output: 'safe result',
      output_chars: 11,
      duration_ms: 25,
      raw_journal: 'PRIVATE_RAW_ROW',
    },
  }],
});
assert.equal(toolActivity.activity[0].details.output, 'safe result');
assert.equal(toolActivity.activity[0].details.duration_ms, 25);
assert.equal(toolActivity.activity_revision, 3);
assert.deepEqual(toolActivity.expanded_steps, ['synthetic-call-1']);
assert.equal(JSON.stringify(toolActivity).includes('PRIVATE_ARGUMENT'), false);
assert.equal(JSON.stringify(toolActivity).includes('PRIVATE_RAW_ROW'), false);
const toolSnapshot = state.createSnapshot({
  traces,
  traceSessionId: 'session1',
  traceTurnId: 'turn1',
  followLive: false,
  messages: [toolActivity],
  history: [],
  workflows: {},
  pendingSourceActions: {},
});
const restoredTool = state.restoreSnapshot(toolSnapshot, traces).messages[0];
assert.deepEqual(restoredTool.activity[0].details, toolActivity.activity[0].details);
assert.deepEqual(restoredTool.expanded_steps, ['synthetic-call-1']);
const unsafeTool = state.normalizeActivityRecord({
  request_id: 'unknown-tools',
  activity: [{ phase: 'Tool', message: 'Legacy status.', details: { ...toolActivity.activity[0].details, tool: 'exec_command' } }],
});
assert.equal(unsafeTool.activity[0].details, undefined);
const longTool = state.normalizeActivityRecord({
  request_id: 'long-tools',
  activity: [{
    message: 'Read a long result.',
    details: { ...toolActivity.activity[0].details, output: 'x'.repeat(5000), context_truncated: true },
  }],
});
assert.equal(longTool.activity[0].details.output.length, 4000);
assert.equal(longTool.activity[0].details.preview_truncated, true);
assert.equal(longTool.activity[0].details.context_truncated, true);

let history = [
  {
    question: 'Review the parser.',
    answer: 'Starting a read-only parser audit.',
    session_id: 'session1',
    turn_id: 'turn1',
  },
];
let workflow = state.beginWorkflow({
  runId: 'run1',
  context: { session_id: 'session1', turn_id: 'turn1', history_index: 0 },
  terminal: false,
  fallbackSessionId: 'session1',
});
const firstActivity = state.latestWorkflowActivity({
  workflow,
  run: {
    run_id: 'run1',
    status: 'auditing',
    message: 'Audit is running.',
    activity: [
      { sequence: 1, at: 'activity-1', phase: 'Queue', message: 'Agent workflow queued.' },
      { sequence: 2, at: 'activity-2', phase: 'Model', message: 'Audit model is analyzing the evidence.' },
    ],
  },
});
assert.equal(firstActivity.sequence, 2);
assert.equal(firstActivity.message, 'Audit model is analyzing the evidence.');
assert.equal(firstActivity.phase, 'Model');
const replacementActivity = state.latestWorkflowActivity({
  workflow,
  run: {
    run_id: 'run1',
    status: 'auditing',
    activity: [
      { sequence: 2, at: 'activity-2', phase: 'Model', message: 'Audit model is analyzing the evidence.' },
      { sequence: 3, at: 'activity-3', phase: 'Tool', message: 'Reading a scoped source file.' },
    ],
  },
});
assert.equal(replacementActivity.sequence, 3);
assert.equal(replacementActivity.message, 'Reading a scoped source file.');
const completeTimeline = state.workflowActivity({
  workflow,
  run: {
    run_id: 'run1',
    status: 'audited',
    completed_at: 'activity-4',
    activity: [
      { sequence: 1, at: 'activity-1', phase: 'Queue', message: 'Agent workflow queued.' },
      { sequence: 2, at: 'activity-2', phase: 'Model', message: 'Audit model reviewed the evidence.' },
      { sequence: 3, at: 'activity-3', phase: 'Complete', message: 'Audit completed.' },
    ],
  },
});
assert.equal(completeTimeline.status, 'audited');
assert.equal(completeTimeline.activity.length, 3);
assert.equal(
  state.latestWorkflowActivity({ workflow, run: { run_id: 'run1', status: 'audited', activity: [] } }),
  null,
);
assert.equal(
  state.latestWorkflowActivity({ workflow: null, run: { run_id: 'run1', status: 'auditing' } }),
  null,
);
let completed = state.completeWorkflow({
  workflow,
  run: {
    run_id: 'run1',
    status: 'paused',
    completed_at: 'pause-1',
    conversation_answer: 'The audit paused after a provider timeout.',
  },
  history,
});
assert.equal(completed.appended, true);
assert.equal(completed.history[0].answer, 'The audit paused after a provider timeout.');
assert.deepEqual(completed.workflow.history_indexes, []);

history = [
  ...completed.history,
  {
    question: 'Continue agent workflow.',
    answer: 'Continuing from the preserved checkpoint.',
    session_id: 'session1',
    turn_id: 'turn1',
  },
];
workflow = state.beginWorkflow({
  runId: 'run1',
  existing: completed.workflow,
  context: { session_id: 'session1', turn_id: 'turn1', history_index: 1 },
  terminal: false,
  fallbackSessionId: 'session1',
});
assert.deepEqual(workflow.history_indexes, [1]);
completed = state.completeWorkflow({
  workflow,
  run: {
    run_id: 'run1',
    status: 'passed',
    completed_at: 'pass-2',
    conversation_answer: 'The resumed repair passed verification.',
  },
  history,
});
assert.equal(completed.history[0].answer, 'The audit paused after a provider timeout.');
assert.equal(completed.history[1].answer, 'The resumed repair passed verification.');
assert.equal(
  state.completeWorkflow({ workflow: completed.workflow, run: {
    run_id: 'run1',
    status: 'passed',
    completed_at: 'pass-2',
    conversation_answer: 'The resumed repair passed verification.',
  }, history: completed.history }).appended,
  false,
);

const pendingSourceActions = {
  'approval-safe_1': {
    authorization_id: 'approval-safe_1',
    session_id: 'session1',
    turn_id: 'turn1',
    history_indexes: [1],
  },
};
const snapshot = state.createSnapshot({
  traces,
  traceSessionId: 'session1',
  traceTurnId: 'turn1',
  traceEventSequence: 10,
  followLive: false,
  messages: [
    { role: 'authorization', text: 'Approve', authorization: { token: 'SECRET_APPROVAL_TOKEN' } },
    { role: 'assistant', text: 'Workflow pending.' },
    {
      role: 'activity',
      activity_id: 'request-safe_1',
      request_id: 'request-safe_1',
      status: 'completed',
      expanded: true,
      raw_prompt: 'SECRET_RAW_PROMPT',
      activity: [
        { sequence: 1, phase: 'Route', message: 'Selected an answer action.', at: 'activity-1' },
        { sequence: 2, phase: 'Complete', message: 'Studio request completed.', at: 'activity-2' },
      ],
    },
  ],
  history: completed.history,
  workflows: { run1: completed.workflow },
  pendingSourceActions,
});
const serialized = JSON.stringify(snapshot);
assert.equal(serialized.includes('SECRET_APPROVAL_TOKEN'), false);
assert.equal(serialized.includes('SECRET_RAW_PROMPT'), false);
assert.equal(snapshot.messages.length, 2);
assert.equal(snapshot.messages[1].role, 'activity');
assert.equal(snapshot.messages[1].activity.length, 2);

const restored = state.restoreSnapshot(snapshot, traces);
assert.equal(restored.traceSessionId, 'session1');
assert.equal(restored.traceTurnId, 'turn1');
assert.equal(restored.traceEventSequence, 10);
assert.equal(restored.followLive, false);
assert.equal(restored.messages[1].activity[1].message, 'Studio request completed.');
assert.equal(restored.messages[1].expanded, true);
assert.deepEqual(restored.pendingSourceActions['approval-safe_1'].history_indexes, [1]);
const legacySnapshot = { ...snapshot };
delete legacySnapshot.follow_live;
assert.equal(state.restoreSnapshot(legacySnapshot, traces).followLive, false);
assert.equal(
  state.restoreSnapshot({ ...snapshot, trace_source: '/tmp/other.jsonl' }, traces).traceSessionId,
  null,
);

const recovered = state.pendingContextForRun(
  { run_id: 'run2', source_authorization: { id: 'approval-safe_1' } },
  restored.pendingSourceActions,
);
assert.equal(recovered.authorizationId, 'approval-safe_1');
const recoveredWorkflow = state.beginWorkflow({
  runId: 'run2',
  context: recovered.context,
  terminal: true,
  fallbackSessionId: 'session1',
});
const recoveredCompletion = state.completeWorkflow({
  workflow: recoveredWorkflow,
  run: {
    run_id: 'run2',
    status: 'passed',
    completed_at: 'pass-recovered',
    conversation_answer: 'The source workflow completed after reconnecting.',
  },
  history: restored.history,
});
assert.equal(recoveredCompletion.appended, true);
assert.equal(recoveredCompletion.history[1].answer, 'The source workflow completed after reconnecting.');

const longConversation = state.createSnapshot({
  traces,
  traceSessionId: 'session1',
  traceTurnId: 'turn2',
  traceEventSequence: 20,
  followLive: false,
  messages: Array.from({ length: 70 }, (_, index) => ({ role: 'assistant', text: `message-${index}` })),
  history: Array.from({ length: 25 }, (_, index) => ({
    question: `question-${index}`,
    answer: `answer-${index}`,
    session_id: 'session1',
    turn_id: index % 2 ? 'turn1' : 'turn2',
  })),
  workflows: {},
  pendingSourceActions: {},
});
assert.equal(longConversation.messages.length, 70);
assert.equal(longConversation.history.length, 25);
assert.equal(state.restoreSnapshot(longConversation, traces).history.length, 25);
