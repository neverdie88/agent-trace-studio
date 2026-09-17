'use strict';

// Exercise the real renderer with a small DOM double. This checks content and
// interaction contracts; browser layout still needs a visual check.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class TestElement {
  constructor(tag) {
    this.tagName = tag;
    this.className = '';
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this._text = '';
    this._open = false;
    this.classList = {
      add: (name) => { this.className += ' ' + name; },
      toggle: (name, active) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        if (active) names.add(name);
        else names.delete(name);
        this.className = [...names].join(' ');
      },
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  set innerHTML(_value) { throw new Error('Activity content must be inserted as text.'); }
  set open(value) {
    this._open = value;
    this.dispatch('toggle');
  }
  get open() { return this._open; }
  append(...children) {
    this.children.push(...children);
    children.forEach((child) => { child.parentNode = this; });
  }
  appendChild(child) { this.append(child); return child; }
  get firstChild() { return this.children[0]; }
  get isConnected() { return Boolean(this.parentNode); }
  removeChild(child) {
    this.children.splice(this.children.indexOf(child), 1);
    child.parentNode = null;
  }
  remove() { this.parentNode?.removeChild(this); }
  querySelector(selector) { return find(this, selector.slice(1)); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  dispatch(name) { this.listeners[name]?.(); }
  focus() { this.focused = true; }
}

function find(node, className) {
  if (node.className.split(/\s+/).includes(className)) return node;
  for (const child of node.children) {
    const match = find(child, className);
    if (match) return match;
  }
  return null;
}

const source = fs.readFileSync(
  path.join(__dirname, '../src/agent_trace_studio/assets/dashboard.js'), 'utf8',
);
function functionSource(name) {
  const start = source.indexOf('\n  function ' + name + '(');
  const end = source.indexOf('\n  function ', start + 1);
  assert.notEqual(start, -1, 'Renderer function must exist: ' + name);
  return source.slice(start, end < 0 ? source.length : end);
}
let saves = 0;
const context = vm.createContext({
  document: { createElement: (tag) => new TestElement(tag) },
  formatInteger: (value) => String(value),
  persistQAConversation: () => { saves += 1; },
});
vm.runInContext(functionSource('element') + functionSource('renderQAContextCall'), context);

const record = { expanded_steps: [] };
const call = {
  kind: 'context_action',
  id: 'synthetic-call-1',
  tool: 'read_dashboard_resource',
  status: 'running',
  arguments: { resource: 'audit_rules' },
  output: '',
  output_chars: 0,
};
let row = context.renderQAContextCall({ details: call }, record);
assert.equal(find(row, 'qa-tool-verb').textContent, 'Calling');
assert.equal(find(row, 'qa-tool-invocation').textContent, 'read_dashboard_resource({"resource":"audit_rules"})');
assert.ok(row.textContent.includes('Waiting for result…'));
find(row, 'qa-tool-details').open = true;
assert.deepEqual(Array.from(record.expanded_steps), ['synthetic-call-1']);

const output = [
  '<img src=x onerror="throw new Error()">',
  'second line', 'third line', 'fourth line', 'fifth line',
].join('\n');
const completed = {
  ...call, status: 'completed',
  output: 'CONTEXT ACTION: read_dashboard_resource\nDASHBOARD RESOURCE: audit_rules\n'
    + 'classification: bounded server-authoritative dashboard state\n' + output,
  output_chars: 5000,
  duration_ms: 25,
  preview_truncated: true,
  context_truncated: true,
  redacted: true,
};
row = context.renderQAContextCall({ details: completed }, record);
assert.equal(find(row, 'qa-tool-verb').textContent, 'Called');
assert.equal(find(row, 'qa-tool-duration').textContent, '25 ms');
assert.equal(find(row, 'qa-tool-details').open, true, 'Expansion survives completion and polling.');
assert.ok(row.textContent.includes(output), 'Untrusted markup stays literal text in the result.');
assert.equal(row.textContent.includes('CONTEXT ACTION:'), false);
assert.equal(row.textContent.includes('classification:'), false);
assert.ok(row.textContent.includes('5000 characters returned'));
assert.ok(row.textContent.includes('Preview truncated to 4,000 characters'));
assert.ok(row.textContent.includes('Context was truncated before delivery to the agent'));
assert.ok(row.textContent.includes('Sensitive values and local paths omitted'));
const preview = find(row, 'qa-tool-preview');
assert.equal(preview.textContent.includes('fifth line'), false);
find(row, 'qa-tool-details').open = false;
assert.deepEqual(Array.from(record.expanded_steps), []);
find(row, 'qa-tool-more').dispatch('click');
assert.equal(find(row, 'qa-tool-details').open, true);
assert.equal(find(row, 'qa-tool-summary').focused, true);
assert.deepEqual(Array.from(record.expanded_steps), ['synthetic-call-1']);
assert.ok(saves >= 3);

row = context.renderQAContextCall({ details: { ...call, status: 'failed', output: 'Read failed.' } }, record);
assert.equal(row.dataset.status, 'failed');
assert.equal(find(row, 'qa-tool-status').textContent, 'Failed');
row = context.renderQAContextCall({ details: { ...call, status: 'cancelled' } }, record);
assert.equal(find(row, 'qa-tool-verb').textContent, 'Stopped');

const activityNodes = Object.fromEntries(
  ['agent-activity', 'agent-activity-list', 'agent-activity-status'].map((id) => [id, new TestElement('div')]),
);
context.document.getElementById = (id) => activityNodes[id];
context.formatActivityTime = (at) => at || '';
context.activityDurationLabel = () => '';
context.activityStatusLabel = (status) => status;
context.terminalActivityStatus = (status) => ['completed', 'failed', 'cancelled'].includes(status);
vm.runInContext([
  'clear', 'activityPhaseLabel', 'renderAgentActivity', 'renderQAActivityTimeline',
].map(functionSource).join('\n'), context);

for (const [input, expected] of [
  ['Harness', 'Agent'], ['harness', 'Agent'], [' HARNESS ', 'Agent'],
  ['Agent', 'Agent'], ['Model', 'Model'], ['Complete', 'Complete'],
  ['Future phase', 'Future phase'], ['Harness worker', 'Harness worker'], [null, 'Agent'],
]) assert.equal(context.activityPhaseLabel(input), expected);

const legacyActivity = {
  run_id: 'synthetic-summary-run', status: 'completed',
  activity: [
    { sequence: 1, phase: 'Harness', at: '2026-01-01T10:00:00Z', message: 'Synthetic agent is preparing a summary.' },
    { sequence: 2, phase: 'Model', at: '2026-01-01T10:00:01Z', message: 'Synthetic model produced a response.' },
    { sequence: 3, phase: 'Complete', at: '2026-01-01T10:00:02Z', message: 'Synthetic summary completed.' },
  ],
};
const originalActivity = JSON.stringify(legacyActivity);
context.renderAgentActivity(legacyActivity);
const workflowRows = activityNodes['agent-activity-list'].children;
assert.deepEqual(workflowRows.map((item) => find(item, 'agent-activity-phase').textContent), ['Agent', 'Model', 'Complete']);
assert.deepEqual(workflowRows.map((item) => find(item, 'agent-activity-message').textContent), legacyActivity.activity.map((item) => item.message));
assert.deepEqual(workflowRows.map((item) => find(item, 'agent-activity-time').textContent), legacyActivity.activity.map((item) => item.at));
context.renderAgentActivity(legacyActivity);
assert.equal(activityNodes['agent-activity-list'].children.length, 3, 'Polling reuses the same timeline rows.');
assert.equal(activityNodes['agent-activity-list'].children[0], workflowRows[0]);

const transcript = new TestElement('div');
context.renderQAActivityTimeline(transcript, legacyActivity);
const conversationRows = find(transcript, 'qa-activity-list').children;
assert.deepEqual(conversationRows.map((item) => find(item, 'qa-live-activity-phase').textContent), ['Agent', 'Model', 'Complete']);
assert.equal(conversationRows[0].dataset.phase, 'agent');
assert.deepEqual(conversationRows.map((item) => find(item, 'qa-live-activity-message').textContent), legacyActivity.activity.map((item) => item.message));
assert.equal(JSON.stringify(legacyActivity), originalActivity, 'Friendly labels must not rewrite saved events.');

console.log('Activity rendering, tool details, and legacy Harness-to-Agent display checks passed.');
