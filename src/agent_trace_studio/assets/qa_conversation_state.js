(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AgentTraceConversationState = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const terminalStatuses = new Set([
    'investigated',
    'extracted',
    'summarized',
    'audited',
    'passed',
    'paused',
    'blocked',
    'failed',
    'cancelled',
    'interrupted',
    'discarded',
  ]);

  function storedText(value, maxChars = null) {
    if (typeof value !== 'string') return '';
    const text = value.trim();
    return Number.isInteger(maxChars) && maxChars >= 0 && text.length > maxChars ? text.slice(0, maxChars) : text;
  }

  function isTerminal(status) {
    return terminalStatuses.has(status);
  }

  function validRunId(value) {
    return typeof value === 'string' && /^[A-Za-z0-9]{1,64}$/.test(value);
  }

  function validAuthorizationId(value) {
    return typeof value === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(value);
  }

  const contextArguments = Object.freeze({
    inspect_current_context: [],
    list_dashboard_resources: [],
    read_dashboard_resource: ['resource', 'resource_id', 'limit'],
    search_trace: ['query', 'turn_id', 'category', 'tool_name', 'limit'],
    read_trace_turn: ['turn_id', 'around_sequence', 'limit'],
  });

  function normalizeActivityDetails(value) {
    if (!value || value.kind !== 'context_action'
      || !Object.hasOwn(contextArguments, value.tool)
      || !['running', 'completed', 'failed', 'cancelled'].includes(value.status)
      || typeof value.id !== 'string' || !/^[A-Za-z0-9_-]{1,96}$/.test(value.id)) return null;
    const args = {};
    contextArguments[value.tool].forEach((key) => {
      const item = value.arguments?.[key];
      if (typeof item === 'string') args[key] = storedText(item, 500);
      else if (Number.isSafeInteger(item) && item > 0) args[key] = item;
    });
    return {
      kind: 'context_action',
      id: value.id,
      tool: value.tool,
      status: value.status,
      arguments: args,
      arguments_truncated: Boolean(value.arguments_truncated),
      output: storedText(value.output, 4000),
      output_chars: Number.isSafeInteger(value.output_chars) && value.output_chars >= 0 ? value.output_chars : 0,
      preview_truncated: Boolean(value.preview_truncated) || (value.output?.length || 0) > 4000,
      context_truncated: Boolean(value.context_truncated),
      redacted: Boolean(value.redacted),
      duration_ms: Number.isFinite(value.duration_ms) && value.duration_ms >= 0 ? value.duration_ms : null,
    };
  }

  function normalizeActivityRecord(value) {
    if (!value || typeof value !== 'object') return null;
    const activityId = storedText(value.activity_id || value.request_id || (value.run_id ? `run:${value.run_id}` : ''), 160);
    if (!activityId || !/^[A-Za-z0-9:_-]+$/.test(activityId)) return null;
    const activity = (Array.isArray(value.activity) ? value.activity : [])
      .filter((item) => item && typeof item === 'object')
      .map((item, index) => {
        const details = normalizeActivityDetails(item.details);
        return {
          sequence: Number.isInteger(item.sequence) && item.sequence > 0 ? item.sequence : index + 1,
          at: storedText(item.at, 80),
          phase: storedText(item.phase, 40) || 'Agent',
          message: storedText(item.message, 400),
          repeat_count: Math.max(Number(item.repeat_count) || 1, 1),
          ...(details ? { details } : {}),
        };
      })
      .filter((item) => item.message);
    return {
      role: 'activity',
      activity_id: activityId,
      request_id: storedText(value.request_id, 80),
      run_id: storedText(value.run_id, 64),
      status: storedText(value.status, 40) || 'running',
      started_at: storedText(value.started_at, 80),
      updated_at: storedText(value.updated_at, 80),
      completed_at: storedText(value.completed_at, 80),
      expanded: typeof value.expanded === 'boolean' ? value.expanded : null,
      expanded_steps: (Array.isArray(value.expanded_steps) ? value.expanded_steps : [])
        .filter((id) => typeof id === 'string' && /^[A-Za-z0-9_-]{1,96}$/.test(id)).slice(-100),
      activity_revision: Number.isSafeInteger(value.activity_revision) ? value.activity_revision : 0,
      activity,
    };
  }

  function contextIndexes(context) {
    const indexes = [];
    if (Number.isInteger(context?.history_index) && context.history_index >= 0) indexes.push(context.history_index);
    (Array.isArray(context?.history_indexes) ? context.history_indexes : []).forEach((index) => {
      if (Number.isInteger(index) && index >= 0) indexes.push(index);
    });
    return indexes;
  }

  function beginWorkflow({ runId, existing = null, context = null, terminal = false, fallbackSessionId = null }) {
    if (!validRunId(runId)) return null;
    const openCycle = Boolean(existing && !existing.completion_key);
    const indexes = new Set(openCycle && Array.isArray(existing.history_indexes) ? existing.history_indexes : []);
    contextIndexes(context).forEach((index) => indexes.add(index));
    return {
      run_id: runId,
      session_id: context?.session_id || existing?.session_id || fallbackSessionId,
      turn_id: context?.turn_id || existing?.turn_id || null,
      history_indexes: Array.from(indexes).slice(-8),
      completion_key: terminal ? String(existing?.completion_key || '') : '',
    };
  }

  function completeWorkflow({ workflow, run, history }) {
    if (!workflow || !run?.run_id || !isTerminal(run.status)) {
      return { appended: false, workflow, history, answer: '' };
    }
    const answer = storedText(run.conversation_answer || run.message, 12000);
    const completionKey = `${run.status || ''}:${run.completed_at || run.updated_at || ''}`;
    if (!answer || workflow.completion_key === completionKey) {
      return { appended: false, workflow, history, answer: '' };
    }
    const nextHistory = Array.isArray(history) ? history.map((item) => ({ ...item })) : [];
    (Array.isArray(workflow.history_indexes) ? workflow.history_indexes : []).forEach((index) => {
      if (nextHistory[index]) nextHistory[index].answer = answer;
    });
    return {
      appended: true,
      answer,
      history: nextHistory,
      workflow: { ...workflow, history_indexes: [], completion_key: completionKey },
    };
  }

  function latestWorkflowActivity({ workflow, run }) {
    if (!workflow || !run?.run_id || isTerminal(run.status)) return null;
    const runId = String(run.run_id);
    if (!validRunId(runId) || workflow.run_id !== runId) return null;
    const activity = (Array.isArray(run.activity) ? run.activity : []).filter(
      (item) => item && typeof item === 'object' && storedText(item.message, 600),
    );
    const latest = activity.at(-1) || null;
    const message = storedText(latest?.message || run.message, 600);
    if (!message) return null;
    return {
      run_id: runId,
      sequence: Number.isInteger(latest?.sequence) ? latest.sequence : null,
      phase: storedText(latest?.phase || run.status || 'Agent', 40) || 'Agent',
      message,
      at: typeof latest?.at === 'string' ? latest.at : '',
      repeat_count: Math.max(Number(latest?.repeat_count) || 1, 1),
    };
  }

  function workflowActivity({ workflow, run }) {
    if (!workflow || !run?.run_id) return null;
    const runId = String(run.run_id);
    if (!validRunId(runId) || workflow.run_id !== runId) return null;
    return normalizeActivityRecord({
      activity_id: `run:${runId}`,
      run_id: runId,
      status: run.status || 'running',
      started_at: run.started_at,
      updated_at: run.updated_at,
      completed_at: run.completed_at,
      activity: run.activity,
    });
  }

  function snapshotContext(context, historyStart) {
    const indexes = contextIndexes(context)
      .filter((index) => index >= historyStart)
      .map((index) => index - historyStart)
      .slice(-8);
    return {
      session_id: typeof context?.session_id === 'string' ? context.session_id : null,
      turn_id: typeof context?.turn_id === 'string' ? context.turn_id : null,
      history_indexes: indexes,
      completion_key: typeof context?.completion_key === 'string' ? context.completion_key : '',
    };
  }

  function createSnapshot({
    traces,
    traceSessionId,
    traceTurnId,
    traceEventSequence,
    followLive,
    messages,
    history,
    workflows,
    pendingSourceActions,
  }) {
    const selectedTrace = (Array.isArray(traces) ? traces : []).find((trace) => trace.session_id === traceSessionId);
    const safeMessages = (Array.isArray(messages) ? messages : [])
      .map((message) => {
        if (message?.role === 'activity') return normalizeActivityRecord(message);
        if (!['user', 'assistant', 'error'].includes(message?.role)) return null;
        const text = storedText(message.text);
        return text ? { role: message.role, text } : null;
      })
      .filter(Boolean);
    const historyValues = Array.isArray(history) ? history : [];
    const historyStart = 0;
    const safeHistory = historyValues
      .slice(historyStart)
      .map((item) => ({
        question: storedText(item?.question),
        answer: storedText(item?.answer),
        session_id: typeof item?.session_id === 'string' ? item.session_id : traceSessionId,
        turn_id: typeof item?.turn_id === 'string' ? item.turn_id : null,
      }))
      .filter((item) => item.question && item.answer);
    const safeWorkflows = {};
    Object.entries(workflows && typeof workflows === 'object' ? workflows : {})
      .slice(-12)
      .forEach(([runId, workflow]) => {
        if (!validRunId(runId) || !workflow || typeof workflow !== 'object') return;
        safeWorkflows[runId] = { run_id: runId, ...snapshotContext(workflow, historyStart) };
      });
    const safePending = {};
    Object.entries(pendingSourceActions && typeof pendingSourceActions === 'object' ? pendingSourceActions : {})
      .slice(-12)
      .forEach(([authorizationId, context]) => {
        if (!validAuthorizationId(authorizationId) || !context || typeof context !== 'object') return;
        safePending[authorizationId] = {
          authorization_id: authorizationId,
          ...snapshotContext(context, historyStart),
        };
      });
    return {
      trace_session_id: traceSessionId,
      trace_source: typeof selectedTrace?.session_file === 'string' ? selectedTrace.session_file : '',
      trace_turn_id: typeof traceTurnId === 'string' ? traceTurnId : null,
      trace_event_sequence: Number.isInteger(traceEventSequence) ? traceEventSequence : null,
      follow_live: Boolean(followLive),
      messages: safeMessages,
      history: safeHistory,
      workflows: safeWorkflows,
      pending_source_actions: safePending,
    };
  }

  function restoreContext(value, historyLength) {
    const indexes = (Array.isArray(value?.history_indexes) ? value.history_indexes : [])
      .filter((index) => Number.isInteger(index) && index >= 0 && index < historyLength)
      .slice(-8);
    return {
      session_id: typeof value?.session_id === 'string' ? value.session_id : null,
      turn_id: typeof value?.turn_id === 'string' ? value.turn_id : null,
      history_indexes: indexes,
      completion_key: typeof value?.completion_key === 'string' ? value.completion_key : '',
    };
  }

  function restoreSnapshot(value, traces) {
    const empty = {
      traceSessionId: null,
      traceTurnId: null,
      traceEventSequence: null,
      followLive: true,
      messages: [],
      history: [],
      workflows: {},
      pendingSourceActions: {},
    };
    if (!value || typeof value !== 'object') return empty;
    const traceSessionId = typeof value.trace_session_id === 'string' ? value.trace_session_id : null;
    const trace = (Array.isArray(traces) ? traces : []).find((item) => item.session_id === traceSessionId);
    const traceSource = typeof value.trace_source === 'string' ? value.trace_source : '';
    if (!trace || (traceSource && trace.session_file !== traceSource)) return empty;
    const events = Array.isArray(trace.events) ? trace.events : [];
    const requestedTurn = typeof value.trace_turn_id === 'string' ? value.trace_turn_id : null;
    const traceTurnId =
      requestedTurn === 'session' || events.some((event) => event.turn_id === requestedTurn) ? requestedTurn : null;
    const requestedSequence = Number.isInteger(value.trace_event_sequence) ? value.trace_event_sequence : null;
    const traceEventSequence = events.some(
      (event) => event.sequence === requestedSequence && (!traceTurnId || event.turn_id === traceTurnId),
    )
      ? requestedSequence
      : null;
    const followLive = typeof value.follow_live === 'boolean' ? value.follow_live : !traceTurnId;
    const messages = (Array.isArray(value.messages) ? value.messages : [])
      .map((message) => {
        if (message?.role === 'activity') return normalizeActivityRecord(message);
        if (!['user', 'assistant', 'error'].includes(message?.role)) return null;
        const text = storedText(message.text);
        return text ? { role: message.role, text } : null;
      })
      .filter(Boolean);
    const history = (Array.isArray(value.history) ? value.history : [])
      .filter((item) => item && typeof item === 'object')
      .map((item) => ({
        question: storedText(item.question),
        answer: storedText(item.answer),
        session_id: typeof item.session_id === 'string' ? item.session_id : traceSessionId,
        turn_id: typeof item.turn_id === 'string' ? item.turn_id : null,
      }))
      .filter((item) => item.question && item.answer);
    const workflows = {};
    Object.entries(value.workflows && typeof value.workflows === 'object' ? value.workflows : {})
      .slice(-12)
      .forEach(([runId, workflow]) => {
        if (!validRunId(runId) || !workflow || typeof workflow !== 'object') return;
        workflows[runId] = { run_id: runId, ...restoreContext(workflow, history.length) };
      });
    const pendingSourceActions = {};
    Object.entries(
      value.pending_source_actions && typeof value.pending_source_actions === 'object'
        ? value.pending_source_actions
        : {},
    )
      .slice(-12)
      .forEach(([authorizationId, context]) => {
        if (!validAuthorizationId(authorizationId) || !context || typeof context !== 'object') return;
        pendingSourceActions[authorizationId] = {
          authorization_id: authorizationId,
          ...restoreContext(context, history.length),
        };
      });
    return {
      traceSessionId,
      traceTurnId,
      traceEventSequence,
      followLive,
      messages,
      history,
      workflows,
      pendingSourceActions,
    };
  }

  function pendingContextForRun(run, pendingSourceActions) {
    const authorizationId = run?.source_authorization?.id;
    if (!validAuthorizationId(authorizationId)) return null;
    const context = pendingSourceActions?.[authorizationId];
    return context ? { authorizationId, context } : null;
  }

  return Object.freeze({
    beginWorkflow,
    completeWorkflow,
    createSnapshot,
    isTerminal,
    latestWorkflowActivity,
    normalizeActivityRecord,
    pendingContextForRun,
    restoreSnapshot,
    storedText,
    workflowActivity,
  });
});
