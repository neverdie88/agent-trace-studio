import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createReviewCodex, reviewPrompt } from './codex_review.mjs';

const entry = process.env.AGENT_TRACE_STUDIO_CODEX_SDK_ENTRY;
const workspace = process.env.AGENT_TRACE_STUDIO_WORKSPACE;
let confinedReview = false;

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

function safeEnvironment() {
  const result = {};
  for (const name of ['HOME', 'PATH', 'TMPDIR', 'TEMP', 'TMP', 'SHELL', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR', 'CODEX_HOME']) {
    if (process.env[name]) result[name] = process.env[name];
  }
  return result;
}

try {
  if (!entry || !workspace) throw new Error('Codex SDK entry and workspace are required');
  await fs.stat(entry);
  const request = await readInput();
  confinedReview = Boolean(request.confinedReview);
  if (confinedReview && request.threadId) throw new Error('Reviews must start a fresh thread.');
  const { Codex } = await import(pathToFileURL(path.resolve(entry)));
  const codex = confinedReview
    ? await createReviewCodex(Codex, { env: safeEnvironment(), workspace })
    : new Codex({ env: safeEnvironment() });
  const readOnly = confinedReview || Boolean(request.readOnly);
  const options = {
    workingDirectory: workspace,
    skipGitRepoCheck: readOnly,
    modelReasoningEffort: readOnly ? 'medium' : 'high',
    webSearchMode: 'disabled',
    approvalPolicy: 'never',
  };
  if (!confinedReview) {
    // --sandbox would override the named profile with legacy broad read access.
    options.sandboxMode = readOnly ? 'read-only' : 'workspace-write';
    options.networkAccessEnabled = false;
  }
  if (request.model) options.model = request.model;
  const thread = request.threadId ? codex.resumeThread(request.threadId, options) : codex.startThread(options);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), Number(request.timeoutMs) || 295000);
  try {
    const prompt = confinedReview ? reviewPrompt(request.prompt) : String(request.prompt || '');
    const turn = await thread.run(prompt, { signal: controller.signal });
    process.stdout.write(JSON.stringify({ ...turn, threadId: thread.id }));
  } finally {
    clearTimeout(timeout);
  }
} catch (error) {
  const detail = confinedReview ? 'Codex read-only review failed.' : `${error?.name || 'Error'}: ${error?.message || error}`;
  process.stdout.write(JSON.stringify({ error: detail }));
  process.exitCode = 1;
}
