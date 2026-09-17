'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/agent_trace_studio/assets/dashboard.js'), 'utf8');
const start = source.indexOf('\n  function renderAgentActions(');
const end = source.indexOf('\n  function ', start + 1);
assert.ok(start >= 0 && end > start);
const selector = { dataset: {}, children: [], appendChild(value) { this.children.push(value); } };
const status = { classList: { add() {}, remove() {} } };
const agent = {
  available: true, harness_available: true, harness_id: 'codex-sdk', harness_label: 'Codex SDK',
  backend: 'Codex SDK', requires_api_settings: false, selection_notice: '',
  source_workspace: '/synthetic/projects/agent-studio',
  harnesses: [
    { id: 'codex-sdk', label: 'Codex SDK', available: true },
    { id: 'opencode', label: 'OpenCode', available: true },
    { id: 'pydantic', label: 'Pydantic AI', available: true },
    { id: 'google-adk', label: 'Google ADK', available: true },
  ],
};
const runtime = { interactive: true, qa: { configured: true, api_configured: false }, agent };
const context = vm.createContext({
  runtime, state: { agentBusy: false, qaBusy: false, agentHarnessBusy: false },
  document: { getElementById: (id) => id === 'agent-harness-select' ? selector : status },
  clear: (node) => { node.children = []; },
  element: (_name, _classes, textContent) => ({ textContent }),
  terminalAgentStatus: () => false, compactPath: (value) => value,
  renderAgentCapabilities() {}, renderAgentRun() {}, renderAgentActivity() {}, renderConversationRunDetails() {},
  latestQAActivity: () => null,
});
vm.runInContext(source.slice(start, end), context);
vm.runInContext('renderAgentActions()', context);
assert.equal(status.textContent, '');
assert.equal(status.hidden, true);
assert.deepEqual(selector.children.map((option) => option.value), ['codex-sdk', 'opencode', 'pydantic', 'google-adk']);
assert.equal(selector.value, 'codex-sdk');
assert.equal(selector.disabled, false);

agent.harness_id = 'pydantic';
agent.harness_label = 'Pydantic AI';
agent.requires_api_settings = true;
vm.runInContext('renderAgentActions()', context);
assert.match(status.textContent, /configure a model API/);
assert.equal(status.hidden, false);

agent.harness_id = 'google-adk';
agent.harness_label = 'Google ADK';
agent.backend = 'Google ADK';
vm.runInContext('renderAgentActions()', context);
assert.equal(selector.value, 'google-adk');
assert.match(status.textContent, /configure a model API/);
runtime.qa.api_configured = true;
vm.runInContext('renderAgentActions()', context);
assert.equal(status.textContent, '');
assert.equal(status.hidden, true);

agent.harness_id = 'opencode';
agent.requires_api_settings = false;
agent.selection_notice = 'Claude Agent SDK was removed. Choose an agent type.';
agent.harness_available = false;
runtime.qa.configured = false;
vm.runInContext('renderAgentActions()', context);
assert.equal(selector.value, '');
assert.equal(selector.children[0].textContent, 'Choose an agent type');
assert.equal(selector.children[0].disabled, true);
assert.equal(status.textContent, agent.selection_notice);
assert.equal(status.hidden, false);

agent.selection_notice = '';
agent.harness_available = true;
runtime.qa.configured = true;
vm.runInContext('renderAgentActions()', context);
assert.equal(selector.value, 'opencode');
assert.equal(selector.children.length, 4);
assert.equal(status.textContent, '');
assert.equal(status.hidden, true);

context.state.agentError = 'Synthetic action failed';
vm.runInContext('renderAgentActions()', context);
assert.equal(status.textContent, 'Synthetic action failed');
assert.equal(status.hidden, false);
context.state.agentError = '';
context.state.agentRun = { status: 'running', message: 'Running synthetic workflow' };
vm.runInContext('renderAgentActions()', context);
assert.equal(status.textContent, 'Running synthetic workflow');
assert.equal(status.hidden, false);
assert.equal(selector.disabled, true);
context.state.agentRun = null;
vm.runInContext('renderAgentActions()', context);
assert.equal(status.textContent, '');
assert.equal(status.hidden, true);
assert.equal(selector.disabled, false);

runtime.qa.configured = false;
runtime.qa.agent_detail = 'Synthetic agent is unavailable';
vm.runInContext('renderAgentActions()', context);
assert.equal(status.textContent, 'Synthetic agent is unavailable');
assert.equal(status.hidden, false);
runtime.interactive = false;
vm.runInContext('renderAgentActions()', context);
assert.equal(status.textContent, 'Available in local server mode');
assert.equal(status.hidden, false);
process.stdout.write('Selected-backend UI tests passed.\n');
