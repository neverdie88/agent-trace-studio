import fs from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';
import { pathToFileURL } from 'node:url';

function write(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function safeEnvironment() {
  const result = {};
  for (const name of ['HOME', 'PATH', 'TMPDIR', 'TEMP', 'TMP', 'SHELL', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR', 'CODEX_HOME']) {
    if (process.env[name]) result[name] = process.env[name];
  }
  return result;
}

function safeName(value, fallback) {
  const normalized = String(value || '').replace(/[^A-Za-z0-9_.:-]/g, '').slice(0, 80);
  return normalized || fallback;
}

function activityForEvent(event) {
  if (!event || typeof event !== 'object') return null;
  if (event.type === 'turn.started') return { phase: 'Turn', message: 'Codex turn started.' };
  if (event.type === 'turn.completed') {
    const usage = event.usage || {};
    const input = Number(usage.input_tokens) || 0;
    const cached = Number(usage.cached_input_tokens) || 0;
    const output = Number(usage.output_tokens) || 0;
    return {
      phase: 'Usage',
      message: `Codex usage · ${input} input tokens · ${cached} cached · ${output} output.`,
    };
  }
  if (event.type === 'turn.failed' || event.type === 'error') {
    return { phase: 'Error', message: 'Codex turn failed.' };
  }
  if (!['item.started', 'item.updated', 'item.completed'].includes(event.type)) return null;
  const item = event.item || {};
  const completed = event.type === 'item.completed';
  if (item.type === 'reasoning') {
    return {
      phase: 'Reasoning',
      message: completed ? 'Codex analysis step completed.' : 'Codex is analyzing the request.',
    };
  }
  if (item.type === 'agent_message') {
    // The host classifies the completed message as either a bounded context
    // request or the final response. Reporting it here would mislabel context
    // requests and duplicate the host's authoritative lifecycle event.
    return null;
  }
  if (item.type === 'command_execution') {
    const status = safeName(item.status, completed ? 'completed' : 'running');
    const exit = Number.isInteger(item.exit_code) ? ` · exit ${item.exit_code}` : '';
    return { phase: 'Tool', message: `Read-only command ${status}${exit}.` };
  }
  if (item.type === 'mcp_tool_call') {
    const tool = safeName(item.tool, 'tool');
    const status = safeName(item.status, completed ? 'completed' : 'running');
    return { phase: 'Tool', message: `MCP tool ${tool} ${status}.` };
  }
  if (item.type === 'web_search') {
    return { phase: 'Tool', message: 'Codex reported a web-search action; query text is hidden.' };
  }
  if (item.type === 'file_change') {
    return { phase: 'Safety', message: 'Codex reported a file-change action in the read-only Studio sandbox.' };
  }
  if (item.type === 'todo_list') {
    const items = Array.isArray(item.items) ? item.items : [];
    const done = items.filter((entry) => entry?.completed).length;
    return { phase: 'Plan', message: `Codex plan updated · ${done} of ${items.length} steps complete.` };
  }
  if (item.type === 'error') return { phase: 'Warning', message: 'Codex reported a non-fatal turn warning.' };
  return null;
}

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let thread = null;

try {
  for await (const line of lines) {
    let request;
    try {
      request = JSON.parse(line);
    } catch (error) {
      write({ type: 'error', id: null, error: `Invalid request JSON: ${error?.message || error}` });
      continue;
    }
    if (request.type === 'init') {
      if (thread) throw new Error('Codex thread worker is already initialized');
      const entry = String(request.entry || '');
      const workspace = String(request.workspace || '');
      if (!entry || !workspace) throw new Error('Codex SDK entry and workspace are required');
      await fs.stat(entry);
      const { Codex } = await import(pathToFileURL(path.resolve(entry)));
      const codex = new Codex({ env: safeEnvironment() });
      const readOnly = Boolean(request.readOnly);
      const options = {
        sandboxMode: readOnly ? 'read-only' : 'workspace-write',
        workingDirectory: workspace,
        skipGitRepoCheck: readOnly,
        modelReasoningEffort: readOnly ? 'medium' : 'high',
        networkAccessEnabled: false,
        webSearchMode: 'disabled',
        approvalPolicy: 'never',
      };
      if (request.model) options.model = String(request.model);
      thread = request.threadId
        ? codex.resumeThread(String(request.threadId), options)
        : codex.startThread(options);
      write({ type: 'ready', threadId: thread.id || request.threadId || null });
      continue;
    }
    if (request.type !== 'run' || !thread) {
      write({ type: 'error', id: request.id || null, error: 'Codex thread worker is not initialized' });
      continue;
    }
    try {
      const streamed = await thread.runStreamed(String(request.prompt || ''));
      let finalResponse = '';
      let usage = null;
      let turnFailure = null;
      for await (const event of streamed.events) {
        const activity = activityForEvent(event);
        if (activity) write({ type: 'activity', id: request.id, ...activity });
        if (event.type === 'item.completed' && event.item?.type === 'agent_message') {
          finalResponse = String(event.item.text || '');
        } else if (event.type === 'turn.completed') {
          usage = event.usage || null;
        } else if (event.type === 'turn.failed') {
          turnFailure = event.error || { message: 'Codex turn failed.' };
        }
      }
      if (turnFailure) throw new Error(String(turnFailure.message || 'Codex turn failed.'));
      write({ type: 'result', id: request.id, finalResponse, usage, threadId: thread.id });
    } catch (error) {
      write({ type: 'error', id: request.id, error: `${error?.name || 'Error'}: ${error?.message || error}` });
    }
  }
} catch (error) {
  write({ type: 'fatal', code: error?.code, error: `${error?.name || 'Error'}: ${error?.message || error}` });
  process.exitCode = 1;
}
