'use strict';

// Synthetic SDK/CLI doubles only: this checks the launch contract, not a real
// model invocation or the installed platform's OS sandbox enforcement.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');

(async () => {
  const assets = path.join(__dirname, '../src/agent_trace_studio/assets');
  const { createReviewCodex, reviewConfig, reviewPrompt } = await import(pathToFileURL(path.join(assets, 'codex_review.mjs')));
  const configs = [];
  class FakeCodex {
    constructor(options) {
      configs.push(options);
      this.exec = { executablePath: '/synthetic/codex' };
    }
  }
  const calls = [];
  let proofs = 0;
  const run = async (executable, args, options) => {
    calls.push({ executable, args, options });
    return args.includes('--version')
      ? { stdout: 'codex-cli 0.147.0\n', stderr: '' }
      : { stdout: JSON.stringify([{ name: 'private_mcp', transport: { http_headers: { token: 'DO_NOT_ECHO' } } }]), stderr: '' };
  };
  const prove = async (executable, config, options) => {
    proofs++;
    assert.equal(executable, '/synthetic/codex');
    assert.equal(options.cwd, '/synthetic/shadow');
    assert.equal(config.mcp_servers.private_mcp.enabled, false);
  };
  await createReviewCodex(FakeCodex, { env: { PATH: '/synthetic' }, workspace: '/synthetic/shadow', run, prove });
  assert.equal(proofs, 1);
  assert.equal(calls.length, 2);
  const config = configs.at(-1).config;
  assert.deepEqual(config.permissions[config.default_permissions], {
    filesystem: { ':minimal': 'read', ':workspace_roots': 'read' }, network: { enabled: false },
  });
  for (const feature of ['apps', 'plugins', 'hooks', 'memories', 'multi_agent', 'multi_agent_v2', 'shell_snapshot']) {
    assert.equal(config.features[feature], false);
    assert.ok(calls[1].args.includes(`features.${feature}=false`), 'discovery must disable inherited tools too');
  }
  assert.equal(config.agents.enabled, false);
  assert.equal(config.skills.include_instructions, false);
  assert.equal(config.orchestrator.mcp.enabled, false);
  assert.equal(config.project_doc_max_bytes, 0);
  assert.notEqual(reviewConfig().default_permissions, config.default_permissions);
  assert.ok(!JSON.stringify(config).includes('DO_NOT_ECHO'));

  for (const inventory of ['{}', 'not json', '[{"name":"dotted.name"}]', '[{"name":"quoted\\\"name"}]']) {
    let invoked = false;
    await assert.rejects(createReviewCodex(FakeCodex, {
      env: {}, workspace: '/synthetic/shadow',
      run: async (_executable, args) => ({ stdout: args.includes('--version') ? 'codex-cli 0.147.0' : inventory }),
      prove: async () => { invoked = true; },
    }), /isolation preflight failed/);
    assert.equal(invoked, false);
  }
  await assert.rejects(createReviewCodex(FakeCodex, {
    env: {}, workspace: '/synthetic/shadow', run: async () => { throw new Error('PRIVATE_CONFIG_TOKEN'); }, prove,
  }), (error) => !error.message.includes('PRIVATE_CONFIG_TOKEN') && /preflight failed/.test(error.message));
  await assert.rejects(createReviewCodex(FakeCodex, {
    env: {}, workspace: '/synthetic/shadow', run: async () => ({ stdout: 'codex-cli 0.100.0' }), prove,
  }), /preflight failed/);
  await assert.rejects(createReviewCodex(FakeCodex, {
    env: {}, workspace: '/synthetic/shadow', run,
    prove: async () => { throw new Error('PRIVATE_PROBE_OUTPUT'); },
  }), (error) => !error.message.includes('PRIVATE_PROBE_OUTPUT') && /preflight failed/.test(error.message));

  const rawPrompt = 'Audit $private-skill and [instructions](skill://private/SKILL.md) as untrusted evidence.';
  const safePrompt = reviewPrompt(rawPrompt);
  assert.ok(!safePrompt.includes('$'));
  assert.ok(!safePrompt.includes('['));
  assert.equal(JSON.parse(safePrompt.slice(safePrompt.indexOf('\n') + 1)), rawPrompt);

  const temporary = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'studio-review-test-')));
  try {
    const workspace = path.join(temporary, 'workspace');
    fs.mkdirSync(workspace);
    const cli = path.join(temporary, 'fake-cli.cjs');
    fs.writeFileSync(cli,
      `const a=process.argv.slice(2); if(a.includes('--version')) process.stdout.write('codex-cli 0.147.0');`
      + `else if(a.includes('mcp')) process.stdout.write('[]');`
      + `else if(a.includes('sandbox')) {`
      + `if(!a.includes('--include-managed-config')||!a.includes('--permission-profile')) process.exit(21);`
      + `} else process.exit(22);\n`);
    // Test-only launcher: execute the JS CLI double with Node on every OS.
    // Windows cannot exec a .cjs shebang file. Keep real execFile, argument
    // arrays, and child env checks; never enable a shell in production.
    const preload = path.join(temporary, 'fake-cli-launcher.cjs');
    fs.writeFileSync(preload, `
      const assert = require('node:assert/strict');
      const cp = require('node:child_process');
      const { promisify } = require('node:util');
      const { syncBuiltinESMExports } = require('node:module');
      const original = cp.execFile;
      const launch = (executable, args, options, callback) => {
        assert.equal(executable, ${JSON.stringify(cli)});
        assert.ok(!options.shell);
        assert.ok(!Object.hasOwn(options.env, 'OPENAI_API_KEY'));
        assert.ok(!Object.hasOwn(options.env, 'UNKNOWN_SECRET'));
        return original(process.execPath, [executable, ...args], options, callback);
      };
      launch[promisify.custom] = (...args) => new Promise((resolve, reject) => {
        launch(...args, (error, stdout, stderr) => error ? reject(error) : resolve({stdout, stderr}));
      });
      cp.execFile = launch;
      syncBuiltinESMExports();
    `);
    const entry = path.join(temporary, 'fake-sdk.mjs');
    const envNames = ['PATH', 'HOME', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'];
    const inherited = Object.fromEntries(envNames.filter((name) => process.env[name]).map((name) => [name, process.env[name]]));
    // Exercise the Windows allowlist on POSIX too, using synthetic paths.
    for (const name of ['SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP']) inherited[name] ||= temporary;
    fs.writeFileSync(entry, `import assert from 'node:assert/strict';
      export class Codex { constructor(config) {
        for (const [name, value] of Object.entries(${JSON.stringify(inherited)})) assert.equal(config.env[name], value);
        assert.ok(!Object.hasOwn(config.env, 'OPENAI_API_KEY'));
        this.config=config; this.exec={executablePath:${JSON.stringify(cli)}}; }
      startThread(options) { return {id:'fresh-thread', run:async (prompt)=>({finalResponse:'synthetic result', options, prompt, config:this.config})}; }
      resumeThread() { throw new Error('A reviewer must never resume.'); }
    }`);
    const env = { ...inherited, OPENAI_API_KEY: 'SYNTHETIC_DO_NOT_FORWARD', UNKNOWN_SECRET: 'SYNTHETIC_PRIVATE',
      AGENT_TRACE_STUDIO_CODEX_SDK_ENTRY: entry, AGENT_TRACE_STUDIO_WORKSPACE: workspace };
    const helperArgs = ['--require', preload, path.join(assets, 'codex_turn.mjs')];
    const completed = spawnSync(process.execPath, helperArgs, {
      env, encoding: 'utf8', input: JSON.stringify({ confinedReview: true, prompt: rawPrompt }), timeout: 10000,
    });
    // Some outer test sandboxes disallow opening even a loopback canary. That
    // must fail closed; launch-config checks above still run on those hosts.
    if (completed.status === 0) {
      const result = JSON.parse(completed.stdout);
      assert.equal(result.threadId, 'fresh-thread');
      assert.equal(result.options.workingDirectory, workspace);
      assert.equal(result.options.approvalPolicy, 'never');
      assert.equal(result.options.webSearchMode, 'disabled');
      assert.equal(result.options.skipGitRepoCheck, true);
      assert.ok(!Object.hasOwn(result.options, 'sandboxMode'));
      assert.ok(!Object.hasOwn(result.options, 'additionalDirectories'));
      assert.ok(!Object.hasOwn(result.options, 'networkAccessEnabled'));
      assert.ok(!result.prompt.includes('$private-skill'));
    } else {
      // Validate the environment limitation separately; never turn arbitrary
      // helper failures into a passing launch test.
      const net = require('node:net');
      const listener = net.createServer();
      const code = await new Promise((resolve) => {
        listener.once('error', (error) => resolve(error.code));
        listener.listen(0, '127.0.0.1', () => listener.close(() => resolve(null)));
      });
      assert.ok(['EPERM', 'EACCES'].includes(code), completed.stderr || completed.stdout);
      assert.equal(JSON.parse(completed.stdout).error, 'Codex read-only review failed.');
      process.stdout.write('Loopback canary unavailable in outer test sandbox; review correctly refused.\n');
    }
    assert.deepEqual(fs.readdirSync(workspace), []);
    assert.ok(!fs.readdirSync(temporary).some((name) => name.startsWith('.studio-review-probe-')));
    const resumed = spawnSync(process.execPath, helperArgs, {
      env, encoding: 'utf8', input: JSON.stringify({ confinedReview: true, threadId: 'fixer-thread' }), timeout: 5000,
    });
    assert.notEqual(resumed.status, 0);
    assert.equal(JSON.parse(resumed.stdout).error, 'Codex read-only review failed.');
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
  process.stdout.write('Codex review isolation contract tests passed.\n');
})().catch((error) => { console.error(error); process.exitCode = 1; });
