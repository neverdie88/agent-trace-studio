(() => {
  'use strict';

  const dataNode = document.getElementById('dashboard-data');
  const traceLayoutStorageKey = 'agent-trace-studio.trace-layout.v1';
  const floatingPanelLayoutStorageKey = 'agent-trace-studio.floating-panels.v1';
  const qaFloatingButtonPositionStorageKey = 'agent-trace-studio.qa-floating-button-position.v1';
  const qaConversationStorageKey = 'agent-trace-studio.qa-conversation.v1';
  const qaConversationIdStorageKey = 'agent-trace-studio.qa-conversation-id.v1';
  const clientSessionNonceStorageKey = 'agent-trace-studio.client-session-nonce.v1';
  const sessionBriefAutoUpdateStorageKey = 'agent-trace-studio.session-brief-auto-update.v1';
  const sessionBriefCollapsedStorageKey = 'agent-trace-studio.session-brief-collapsed.v1';
  const assuranceCollapsedStorageKey = 'agent-trace-studio.session-audit-collapsed.v1';
  const qaConversationCollapsedStorageKey = 'agent-trace-studio.studio-console-collapsed.v1';
  const qaConversationState = globalThis.AgentTraceConversationState;
  const traceStackMedia = '(max-width: 1050px)';
  const floatingPanelStackMedia = '(max-width: 780px)';
  const qaFloatingViewportMargin = 12;
  const qaConversationGap = 10;
  const dashboardReloadMarkers = new Set();
  let data = JSON.parse(dataNode.textContent);
  let runtime = {
    interactive: false,
    qa: {
      agent_enabled: true,
      agent_type_id: 'opencode',
      agent_harness: 'OpenCode',
      agent_model: '',
      agent_detail: '',
      agent_uses_api_settings: false,
      api_configured: false,
      configured: false,
      remembered: false,
      credential_store_available: false,
      native_credential_store_available: false,
      credential_store: 'system credential store',
      credential_mode: 'memory',
      save_credential_mode: 'memory',
      credential_locked: false,
      vault_password_required: false,
      vault_password_from_environment: false,
      model: '',
      base_url: '',
      provider: '',
      provider_id: 'openai',
      providers: [],
    },
    agent: {
      available: false,
      active_run_id: null,
      latest_run: null,
      harness_id: 'opencode',
      harnesses: [],
    },
    live: {
      enabled: false,
      state: 'disabled',
      revision: 0,
      monitored_files: 0,
      sources: [],
    },
    deployment: {
      supervised: false,
      generation: '',
    },
    audit_rules: {
      available: false,
      revision: 0,
      active_count: 0,
    },
  };
  const restoredQAConversation = loadQAConversationState();
  const state = {
    traceSessionId:
      restoredQAConversation.traceSessionId || (data.traces.length ? data.traces[0].session_id : null),
    traceTurnId: restoredQAConversation.traceTurnId,
    traceEventSequence: restoredQAConversation.traceEventSequence,
    traceCheckpoint: null,
    assuranceContract: null,
    assuranceReplayStep: null,
    assuranceReplayTimer: null,
    assuranceCollapsed: loadAssuranceCollapsed(),
    floatingPanelLayout: loadFloatingPanelLayout(),
    floatingPanelsReady: false,
    auditRules: null,
    auditRulesBusy: false,
    auditRulesRefreshPending: false,
    auditRuleSelectedId: null,
    auditRuleEditing: false,
    auditRulesError: '',
    liveControlBusy: false,
    liveControlError: '',
    traceCategory: 'all',
    traceTool: '',
    highlightedText: '',
    highlightOrigin: '',
    highlightTruncated: false,
    askTarget: null,
    studioAskContext: null,
    qaConversationOpen: false,
    qaConversationCollapsed: loadQAConversationCollapsed(),
    qaUnreadCount: 0,
    qaFloatingButtonPosition: loadQAFloatingButtonPosition(),
    qaBusy: false,
    qaRequestId: null,
    qaRequestController: null,
    qaRequestPollTimer: null,
    qaStoppedRequestId: null,
    qaStopBusy: false,
    qaConversationId: loadQAConversationId(),
    qaConversationBootstrapped: false,
    qaConfigBusy: false,
    qaHistory: restoredQAConversation.history,
    qaMessages: restoredQAConversation.messages,
    qaWorkflowRuns: restoredQAConversation.workflows,
    qaPendingSourceActions: restoredQAConversation.pendingSourceActions,
    qaActivity: null,
    clientSessionNonce: loadClientSessionNonce(),
    sourceBusy: false,
    agentBusy: false,
    agentHarnessBusy: false,
    agentRun: null,
    agentError: '',
    agentPollTimer: null,
    sessionBrief: null,
    sessionBriefBusy: false,
    sessionBriefFetchBusy: false,
    sessionBriefFetchPending: false,
    sessionBriefError: '',
    sessionBriefAutoUpdate: loadSessionBriefAutoUpdate(),
    sessionBriefCollapsed: loadSessionBriefCollapsed(),
    sessionBriefRefreshTimer: null,
    runtimeRetryTimer: null,
    deploymentPollTimer: null,
    deploymentGeneration: '',
    liveEventSource: null,
    liveConnected: false,
    liveRevision: 0,
    liveRefreshBusy: false,
    liveRefreshPending: false,
    followLive: restoredQAConversation.followLive,
    liveNewEvents: 0,
    agentNotificationKey: '',
    traceLayout: loadTraceLayout(),
  };
  let floatingPanelZCounter = 20;

  function loadSessionBriefAutoUpdate() {
    try {
      return window.localStorage.getItem(sessionBriefAutoUpdateStorageKey) === 'true';
    } catch (_error) {
      return false;
    }
  }

  function persistSessionBriefAutoUpdate() {
    try {
      window.localStorage.setItem(sessionBriefAutoUpdateStorageKey, String(state.sessionBriefAutoUpdate));
    } catch (_error) {
      // Auto-update remains active for this page when browser storage is unavailable.
    }
  }

  function loadSessionBriefCollapsed() {
    try {
      return window.localStorage.getItem(sessionBriefCollapsedStorageKey) === 'true';
    } catch (_error) {
      return false;
    }
  }

  function persistSessionBriefCollapsed() {
    try {
      window.localStorage.setItem(sessionBriefCollapsedStorageKey, String(state.sessionBriefCollapsed));
    } catch (_error) {
      // The current page still keeps the user's collapse preference when browser storage is unavailable.
    }
  }

  function loadAssuranceCollapsed() {
    try {
      return window.localStorage.getItem(assuranceCollapsedStorageKey) === 'true';
    } catch (_error) {
      return false;
    }
  }

  function persistAssuranceCollapsed() {
    try {
      window.localStorage.setItem(assuranceCollapsedStorageKey, String(state.assuranceCollapsed));
    } catch (_error) {
      // The current page still keeps the user's collapse preference when browser storage is unavailable.
    }
  }

  function loadQAConversationCollapsed() {
    try {
      return window.localStorage.getItem(qaConversationCollapsedStorageKey) === 'true';
    } catch (_error) {
      return false;
    }
  }

  function persistQAConversationCollapsed() {
    try {
      window.localStorage.setItem(qaConversationCollapsedStorageKey, String(state.qaConversationCollapsed));
    } catch (_error) {
      // The current page still keeps the user's collapse preference when browser storage is unavailable.
    }
  }

  function renderCollapseToggle(toggle, collapsed, label) {
    const action = collapsed ? 'Expand' : 'Collapse';
    toggle.setAttribute('aria-expanded', String(!collapsed));
    toggle.setAttribute('aria-label', `${action} ${label}`);
    toggle.title = `${action} ${label}`;
  }

  function syncFloatingPanelCollapse(panel, collapsed) {
    // The collapsed CSS overrides height without changing the expanded sizing mode.
    panel.classList.toggle('floating-panel-collapsed', collapsed);
    recoverFloatingPanelHeight(panel);
    if (state.floatingPanelsReady) window.requestAnimationFrame(updateFloatingWorkspaceHeight);
  }

  function recoverFloatingPanelHeight(panel) {
    if (panel.classList.contains('floating-panel-collapsed') || window.matchMedia(floatingPanelStackMedia).matches) return;
    const explicitHeight = Number.parseFloat(panel.style.height);
    if (!Number.isFinite(explicitHeight)) return;
    const handle = panel.querySelector('[data-floating-handle]');
    if (!handle?.querySelector('.collapse-toggle')) return;
    // Older collapse logic saved the header height, including the CSS minimum.
    const minimum = Number.parseFloat(window.getComputedStyle(panel).minHeight) || 0;
    const headerHeight = handle.getBoundingClientRect().height + panel.offsetHeight - panel.clientHeight;
    if (explicitHeight <= Math.max(minimum, headerHeight)) panel.style.removeProperty('height');
  }

  function loadQAConversationState() {
    try {
      const raw = window.sessionStorage.getItem(qaConversationStorageKey);
      const restored = qaConversationState.restoreSnapshot(raw ? JSON.parse(raw) : null, data.traces);
      if (raw && !restored.traceSessionId) {
        window.sessionStorage.removeItem(qaConversationStorageKey);
      }
      return restored;
    } catch (_error) {
      return qaConversationState.restoreSnapshot(null, data.traces);
    }
  }

  function storedQAText(value, maxChars = 20000) {
    return qaConversationState.storedText(value, maxChars);
  }

  function persistQAConversation() {
    try {
      const snapshot = qaConversationState.createSnapshot({
        traces: data.traces,
        traceSessionId: state.traceSessionId,
        traceTurnId: state.traceTurnId,
        traceEventSequence: state.traceEventSequence,
        followLive: state.followLive,
        messages: state.qaMessages,
        history: state.qaHistory,
        workflows: state.qaWorkflowRuns,
        pendingSourceActions: state.qaPendingSourceActions,
      });
      window.sessionStorage.setItem(
        qaConversationStorageKey,
        JSON.stringify(snapshot),
      );
    } catch (_error) {
      // Conversation handoff is best effort; workflow state remains durable on the server.
    }
  }

  function clearStoredQAConversation() {
    try {
      window.sessionStorage.removeItem(qaConversationStorageKey);
    } catch (_error) {
      // Storage may be disabled by browser policy.
    }
  }

  const numberFormatter = new Intl.NumberFormat('en-US');
  const decimalFormatter = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });
  const dateFormatter = new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
  const activityTimeFormatter = new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  });
  const traceCategories = [
    ['all', 'All'],
    ['message', 'Messages'],
    ['tool', 'Tools'],
    ['reasoning', 'Reasoning'],
    ['lifecycle', 'Lifecycle'],
    ['context', 'Context'],
  ];

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function parseTraceAnchor(value) {
    const match = String(value || '').trim().match(/^\[turn ([^,\]]+), line (\d+)\]$/);
    if (!match) return null;
    return { turnId: match[1], lineNumber: Number(match[2]) };
  }

  function traceAnchorTarget(turnId, lineNumber) {
    const current = data.traces.find((trace) => trace.session_id === state.traceSessionId);
    const traces = current ? [current, ...data.traces.filter((trace) => trace !== current)] : data.traces;
    for (const trace of traces) {
      const event = trace.events.find(
        (candidate) =>
          candidate.turn_id === turnId &&
          (Number(candidate.line_number) === lineNumber || Number(candidate.output_line_number) === lineNumber),
      );
      if (event) return { trace, event };
    }
    return null;
  }

  function navigateToTraceAnchor(turnId, lineNumber) {
    const target = traceAnchorTarget(turnId, lineNumber);
    if (!target) return false;
    pauseLiveFollow();
    const changedSession = target.trace.session_id !== state.traceSessionId;
    resetAgentFocus({ checkpoint: changedSession });
    state.traceSessionId = target.trace.session_id;
    state.traceTurnId = target.event.turn_id;
    state.traceEventSequence = target.event.sequence;
    state.traceCategory = 'all';
    state.traceTool = '';
    const search = document.getElementById('trace-search');
    if (search) search.value = '';
    renderDashboard();
    if (changedSession) refreshAuditRules();
    window.requestAnimationFrame(() => {
      document.getElementById('trace-workbench')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      const activeEvent = document.querySelector('.trace-event-row.active');
      activeEvent?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      activeEvent?.focus({ preventScroll: true });
    });
    return true;
  }

  function traceEvidenceLink(value) {
    const text = String(value || '');
    const anchor = parseTraceAnchor(text);
    const link = element('a', 'trace-evidence-link', text);
    link.href = '#trace-workbench';
    const target = anchor ? traceAnchorTarget(anchor.turnId, anchor.lineNumber) : null;
    if (!target) {
      link.classList.add('unavailable');
      link.setAttribute('aria-disabled', 'true');
      link.title = 'This evidence line is not available in the loaded trace';
      link.addEventListener('click', (event) => event.preventDefault());
      return link;
    }
    link.title = `Jump to ${anchor.turnId}, line ${formatInteger(anchor.lineNumber)}`;
    link.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      navigateToTraceAnchor(anchor.turnId, anchor.lineNumber);
    });
    return link;
  }

  function appendTraceEvidenceAnchors(parent, anchors) {
    const values = Array.isArray(anchors) ? anchors.map(String).filter(Boolean) : [];
    values.forEach((value, index) => {
      if (index) parent.appendChild(document.createTextNode(' · '));
      parent.appendChild(traceEvidenceLink(value));
    });
  }

  function appendTraceAnchoredText(parent, value) {
    const text = String(value || '');
    const pattern = /\[turn ([^,\]]+), line (\d+)\]/g;
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      parent.appendChild(document.createTextNode(text.slice(cursor, match.index)));
      parent.appendChild(traceEvidenceLink(match[0]));
      cursor = Number(match.index) + match[0].length;
    }
    parent.appendChild(document.createTextNode(text.slice(cursor)));
  }

  function traceAnchoredElement(tag, className, value) {
    const node = element(tag, className);
    appendTraceAnchoredText(node, value);
    return node;
  }

  function safeMarkdownHref(value) {
    try {
      const url = new URL(value, window.location.href);
      return ['https:', 'http:', 'mailto:'].includes(url.protocol) ? url.href : null;
    } catch (_error) {
      return null;
    }
  }

  function appendInlineMarkdown(parent, value) {
    const text = String(value || '');
    let index = 0;
    const appendText = (content) => parent.appendChild(document.createTextNode(content));
    while (index < text.length) {
      const rest = text.slice(index);
      const citation = rest.match(/^\[turn ([^\],]+), line (\d+)\]/);
      if (citation) {
        const link = traceEvidenceLink(citation[0]);
        link.classList.add('qa-citation');
        parent.appendChild(link);
        index += citation[0].length;
        continue;
      }
      if (text[index] === '`') {
        const end = text.indexOf('`', index + 1);
        if (end > index + 1) {
          parent.appendChild(element('code', 'qa-inline-code', text.slice(index + 1, end)));
          index = end + 1;
          continue;
        }
      }
      if (text[index] === '[') {
        const labelEnd = text.indexOf('](', index + 1);
        const urlEnd = labelEnd >= 0 ? text.indexOf(')', labelEnd + 2) : -1;
        if (labelEnd > index + 1 && urlEnd > labelEnd + 2) {
          const href = safeMarkdownHref(text.slice(labelEnd + 2, urlEnd).trim());
          if (href) {
            const link = element('a', 'qa-markdown-link');
            link.href = href;
            if (href.startsWith('http')) {
              link.target = '_blank';
              link.rel = 'noreferrer noopener';
            }
            appendInlineMarkdown(link, text.slice(index + 1, labelEnd));
            parent.appendChild(link);
            index = urlEnd + 1;
            continue;
          }
        }
      }
      if (text.startsWith('**', index)) {
        const end = text.indexOf('**', index + 2);
        if (end > index + 2) {
          const strong = element('strong', '');
          appendInlineMarkdown(strong, text.slice(index + 2, end));
          parent.appendChild(strong);
          index = end + 2;
          continue;
        }
      }
      if (text.startsWith('~~', index)) {
        const end = text.indexOf('~~', index + 2);
        if (end > index + 2) {
          const deleted = element('del', '');
          appendInlineMarkdown(deleted, text.slice(index + 2, end));
          parent.appendChild(deleted);
          index = end + 2;
          continue;
        }
      }
      if (text[index] === '*' && text[index + 1] !== '*') {
        const end = text.indexOf('*', index + 1);
        if (end > index + 1) {
          const emphasis = element('em', '');
          appendInlineMarkdown(emphasis, text.slice(index + 1, end));
          parent.appendChild(emphasis);
          index = end + 1;
          continue;
        }
      }
      if (text[index] === '\n') {
        parent.appendChild(document.createElement('br'));
        index += 1;
        continue;
      }
      const nextMarker = ['`', '[', '*', '~', '\n']
        .map((marker) => text.indexOf(marker, index + 1))
        .filter((position) => position >= 0)
        .sort((left, right) => left - right)[0];
      const end = nextMarker === undefined ? text.length : nextMarker;
      appendText(text.slice(index, end));
      index = end;
    }
  }

  function splitMarkdownTableRow(line) {
    const value = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    const cells = [];
    let current = '';
    let escaped = false;
    let inCode = false;
    for (const character of value) {
      if (escaped) {
        current += character;
        escaped = false;
      } else if (character === '\\') {
        escaped = true;
      } else if (character === '`') {
        inCode = !inCode;
        current += character;
      } else if (character === '|' && !inCode) {
        cells.push(current.trim());
        current = '';
      } else {
        current += character;
      }
    }
    cells.push(current.trim());
    return cells;
  }

  function markdownTableAlignment(cell) {
    const value = cell.trim();
    if (value.startsWith(':') && value.endsWith(':')) return 'center';
    if (value.endsWith(':')) return 'right';
    return 'left';
  }

  function isMarkdownTable(lines, index) {
    if (!lines[index]?.includes('|') || !lines[index + 1]?.includes('-')) return false;
    const divider = splitMarkdownTableRow(lines[index + 1]);
    return divider.length > 0 && divider.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
  }

  function appendMarkdownTable(root, lines, start) {
    const headers = splitMarkdownTableRow(lines[start]);
    const alignments = splitMarkdownTableRow(lines[start + 1]).map(markdownTableAlignment);
    const table = element('table', 'qa-markdown-table');
    const header = document.createElement('thead');
    const headerRow = document.createElement('tr');
    headers.forEach((value, index) => {
      const cell = element('th', `align-${alignments[index] || 'left'}`);
      appendInlineMarkdown(cell, value);
      headerRow.appendChild(cell);
    });
    header.appendChild(headerRow);
    table.appendChild(header);
    const body = document.createElement('tbody');
    let index = start + 2;
    while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
      const row = document.createElement('tr');
      splitMarkdownTableRow(lines[index]).forEach((value, cellIndex) => {
        const cell = element('td', `align-${alignments[cellIndex] || 'left'}`);
        appendInlineMarkdown(cell, value);
        row.appendChild(cell);
      });
      body.appendChild(row);
      index += 1;
    }
    table.appendChild(body);
    const wrapper = element('div', 'qa-markdown-table-wrap');
    wrapper.appendChild(table);
    root.appendChild(wrapper);
    return index;
  }

  function markdownBlockStart(lines, index) {
    const line = lines[index] || '';
    return (
      !line.trim() ||
      /^\s*```/.test(line) ||
      /^#{1,4}\s+/.test(line) ||
      /^\s*>\s?/.test(line) ||
      /^\s*(?:[-+*]|\d+[.)])\s+/.test(line) ||
      /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line) ||
      isMarkdownTable(lines, index)
    );
  }

  function renderMarkdown(value) {
    const root = element('div', 'qa-markdown');
    const lines = String(value || '').replace(/\r\n?/g, '\n').split('\n');
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }
      const fence = line.match(/^\s*```([\w.+-]*)\s*$/);
      if (fence) {
        const codeLines = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
          codeLines.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const block = element('div', 'qa-code-block');
        if (fence[1]) block.appendChild(element('div', 'qa-code-language', fence[1]));
        const pre = document.createElement('pre');
        pre.appendChild(element('code', '', codeLines.join('\n')));
        block.appendChild(pre);
        root.appendChild(block);
        continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.+)$/);
      if (heading) {
        const node = element('h3', `qa-markdown-heading level-${heading[1].length}`);
        appendInlineMarkdown(node, heading[2]);
        root.appendChild(node);
        index += 1;
        continue;
      }
      if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        root.appendChild(document.createElement('hr'));
        index += 1;
        continue;
      }
      if (isMarkdownTable(lines, index)) {
        index = appendMarkdownTable(root, lines, index);
        continue;
      }
      if (/^\s*>\s?/.test(line)) {
        const quote = element('blockquote', 'qa-markdown-quote');
        const values = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
          values.push(lines[index].replace(/^\s*>\s?/, ''));
          index += 1;
        }
        appendInlineMarkdown(quote, values.join('\n'));
        root.appendChild(quote);
        continue;
      }
      const listItem = line.match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
      if (listItem) {
        const ordered = Boolean(listItem[2]);
        const list = document.createElement(ordered ? 'ol' : 'ul');
        if (ordered && Number(listItem[2]) > 1) list.start = Number(listItem[2]);
        while (index < lines.length) {
          const itemMatch = lines[index].match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
          if (!itemMatch || Boolean(itemMatch[2]) !== ordered) break;
          const item = document.createElement('li');
          const task = itemMatch[3].match(/^\[([ xX])\]\s+(.+)$/);
          if (task) {
            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.checked = task[1].toLowerCase() === 'x';
            checkbox.disabled = true;
            checkbox.setAttribute('aria-hidden', 'true');
            item.className = 'qa-task-item';
            item.appendChild(checkbox);
            appendInlineMarkdown(item, task[2]);
          } else {
            appendInlineMarkdown(item, itemMatch[3]);
          }
          list.appendChild(item);
          index += 1;
        }
        root.appendChild(list);
        continue;
      }
      const paragraphLines = [line];
      index += 1;
      while (index < lines.length && !markdownBlockStart(lines, index)) {
        paragraphLines.push(lines[index]);
        index += 1;
      }
      const paragraph = document.createElement('p');
      appendInlineMarkdown(paragraph, paragraphLines.join('\n'));
      root.appendChild(paragraph);
    }
    return root;
  }

  function formatInteger(value) {
    return numberFormatter.format(Number(value) || 0);
  }

  function formatDecimal(value) {
    return decimalFormatter.format(Number(value) || 0);
  }

  function formatDuration(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds)) return 'n/a';
    if (seconds < 60) return `${formatDecimal(seconds)}s`;
    if (seconds < 3600) return `${formatDecimal(seconds / 60)}m`;
    return `${formatDecimal(seconds / 3600)}h`;
  }

  function formatDate(value) {
    if (!value) return 'n/a';
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? String(value) : dateFormatter.format(parsed);
  }

  function formatActivityTime(value) {
    if (!value) return '';
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? '' : activityTimeFormatter.format(parsed);
  }

  function shortId(value) {
    const text = String(value || 'unknown');
    return text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-7)}` : text;
  }

  function compactPath(value) {
    const text = String(value || 'unknown').replaceAll('\\', '/');
    const parts = text.split('/').filter(Boolean);
    if (parts.length <= 4) return text;
    return `…/${parts.slice(-4).join('/')}`;
  }

  function compactText(value, limit = 120) {
    const text = String(value || '').replaceAll(/\s+/g, ' ').trim();
    return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
  }

  function normalizedSearch(value) {
    return String(value || '').toLocaleLowerCase();
  }

  function bounded(value, minimum, maximum) {
    return Math.min(Math.max(Number(value) || minimum, minimum), maximum);
  }

  function floatingPanelElements() {
    return Array.from(document.querySelectorAll('#floating-workspace > [data-floating-panel]'));
  }

  function loadFloatingPanelLayout() {
    const fallback = { panels: {} };
    try {
      const saved = JSON.parse(window.localStorage.getItem(floatingPanelLayoutStorageKey) || 'null');
      if (!saved?.panels || typeof saved.panels !== 'object') return fallback;
      const panels = {};
      Object.entries(saved.panels).forEach(([panelId, geometry]) => {
        if (!geometry || typeof geometry !== 'object') return;
        if (!Number.isFinite(geometry.left) || !Number.isFinite(geometry.top)) return;
        if (!Number.isFinite(geometry.width)) return;
        panels[panelId] = {
          left: geometry.left,
          top: geometry.top,
          width: geometry.width,
          height: Number.isFinite(geometry.height) ? geometry.height : null,
          z: Number.isFinite(geometry.z) ? geometry.z : 10,
        };
      });
      return { panels };
    } catch (_error) {
      return fallback;
    }
  }

  function floatingWorkspaceBounds() {
    const workspace = document.getElementById('floating-workspace');
    const style = window.getComputedStyle(workspace);
    const left = Number.parseFloat(style.paddingLeft) || 0;
    const top = Number.parseFloat(style.paddingTop) || 0;
    const right = workspace.clientWidth - (Number.parseFloat(style.paddingRight) || 0);
    const bottomPadding = Number.parseFloat(style.paddingBottom) || 0;
    return { left, top, right, bottomPadding };
  }

  function constrainedFloatingPanelGeometry(geometry) {
    const bounds = floatingWorkspaceBounds();
    const maximumWidth = Math.max(320, bounds.right - bounds.left);
    const width = bounded(geometry.width, 320, maximumWidth);
    const left = bounded(geometry.left, bounds.left, Math.max(bounds.left, bounds.right - width));
    return {
      left,
      top: Math.max(bounds.top, Number(geometry.top) || bounds.top),
      width,
      height: Number.isFinite(geometry.height) ? Math.max(100, geometry.height) : null,
      z: Number.isFinite(geometry.z) ? geometry.z : 10,
    };
  }

  function applyFloatingPanelGeometry(panel, geometry) {
    const next = constrainedFloatingPanelGeometry(geometry);
    panel.style.left = `${next.left}px`;
    panel.style.top = `${next.top}px`;
    panel.style.width = `${next.width}px`;
    panel.style.height = next.height === null ? '' : `${next.height}px`;
    panel.style.zIndex = String(next.z);
    panel.dataset.floatingReady = 'true';
    floatingPanelZCounter = Math.max(floatingPanelZCounter, next.z);
    recoverFloatingPanelHeight(panel);
  }

  function floatingPanelCurrentGeometry(panel) {
    const workspaceRect = document.getElementById('floating-workspace').getBoundingClientRect();
    const rect = panel.getBoundingClientRect();
    return {
      left: Number.parseFloat(panel.style.left) || rect.left - workspaceRect.left,
      top: Number.parseFloat(panel.style.top) || rect.top - workspaceRect.top,
      width: rect.width,
      height: rect.height,
      z: Number.parseInt(panel.style.zIndex, 10) || 10,
    };
  }

  function updateFloatingWorkspaceHeight() {
    const workspace = document.getElementById('floating-workspace');
    if (!state.floatingPanelsReady || window.matchMedia(floatingPanelStackMedia).matches) {
      workspace.style.minHeight = '';
      return;
    }
    const bounds = floatingWorkspaceBounds();
    let panelBottom = bounds.top;
    floatingPanelElements().forEach((panel) => {
      if (panel.hidden || panel.dataset.floatingReady !== 'true') return;
      panelBottom = Math.max(panelBottom, panel.offsetTop + panel.getBoundingClientRect().height);
    });
    workspace.style.minHeight = `${Math.ceil(panelBottom + bounds.bottomPadding)}px`;
  }

  function saveFloatingPanelLayout() {
    if (!state.floatingPanelsReady || window.matchMedia(floatingPanelStackMedia).matches) return;
    const panels = {};
    floatingPanelElements().forEach((panel) => {
      if (panel.dataset.floatingReady !== 'true') return;
      const geometry = floatingPanelCurrentGeometry(panel);
      const explicitHeight = Number.parseFloat(panel.style.height);
      panels[panel.id] = {
        ...geometry,
        height: Number.isFinite(explicitHeight)
          ? panel.classList.contains('floating-panel-collapsed') ? explicitHeight : geometry.height
          : null,
      };
    });
    state.floatingPanelLayout = { panels };
    try {
      window.localStorage.setItem(floatingPanelLayoutStorageKey, JSON.stringify(state.floatingPanelLayout));
    } catch (_error) {
      // Panel positions remain active for this page when browser storage is unavailable.
    }
  }

  function resetFloatingPanelLayout({ clearSaved = true } = {}) {
    state.floatingPanelLayout = { panels: {} };
    if (clearSaved) {
      try {
        window.localStorage.removeItem(floatingPanelLayoutStorageKey);
      } catch (_error) {
        // The active page can still restore the default layout when browser storage is unavailable.
      }
    }
    if (!state.floatingPanelsReady) return;
    const bounds = floatingWorkspaceBounds();
    const fullWidth = Math.max(320, bounds.right - bounds.left);
    let top = bounds.top;
    floatingPanelZCounter = 20;
    floatingPanelElements().forEach((panel) => {
      panel.style.removeProperty('left');
      panel.style.removeProperty('top');
      panel.style.removeProperty('width');
      panel.style.removeProperty('height');
      panel.style.removeProperty('z-index');
      delete panel.dataset.floatingReady;
      if (panel.hidden) return;
      applyFloatingPanelGeometry(panel, {
        left: bounds.left,
        top,
        width: fullWidth,
        height: null,
        z: ++floatingPanelZCounter,
      });
      top = panel.offsetTop + panel.getBoundingClientRect().height + 16;
    });
    updateFloatingWorkspaceHeight();
  }

  function bringFloatingPanelToFront(panel) {
    floatingPanelZCounter += 1;
    panel.style.zIndex = String(floatingPanelZCounter);
  }

  function ensureFloatingPanelPlaced(panel) {
    if (!state.floatingPanelsReady || panel.hidden || panel.dataset.floatingReady === 'true') return;
    const stored = state.floatingPanelLayout.panels?.[panel.id];
    if (stored) {
      applyFloatingPanelGeometry(panel, stored);
      updateFloatingWorkspaceHeight();
      return;
    }
    const bounds = floatingWorkspaceBounds();
    let top = bounds.top;
    floatingPanelElements().forEach((candidate) => {
      if (candidate === panel || candidate.hidden || candidate.dataset.floatingReady !== 'true') return;
      top = Math.max(top, candidate.offsetTop + candidate.getBoundingClientRect().height + 16);
    });
    applyFloatingPanelGeometry(panel, {
      left: bounds.left,
      top,
      width: bounds.right - bounds.left,
      height: null,
      z: ++floatingPanelZCounter,
    });
    updateFloatingWorkspaceHeight();
  }

  function bindFloatingPanel(panel) {
    const handle = panel.querySelector('[data-floating-handle]');
    if (!handle) return;
    panel.addEventListener('pointerdown', (event) => {
      bringFloatingPanelToFront(panel);
      const rect = panel.getBoundingClientRect();
      const resizeGesture =
        !panel.classList.contains('floating-panel-collapsed') &&
        event.clientX >= rect.right - 20 &&
        event.clientY >= rect.bottom - 20;
      if (resizeGesture) {
        const finishResize = () => {
          window.removeEventListener('pointerup', finishResize);
          window.removeEventListener('pointercancel', finishResize);
          applyFloatingPanelGeometry(panel, floatingPanelCurrentGeometry(panel));
          updateFloatingWorkspaceHeight();
          saveFloatingPanelLayout();
        };
        window.addEventListener('pointerup', finishResize);
        window.addEventListener('pointercancel', finishResize);
      } else {
        window.setTimeout(saveFloatingPanelLayout, 0);
      }
    }, { capture: true });

    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || window.matchMedia(floatingPanelStackMedia).matches) return;
      if (event.target.closest('button, input, select, textarea, label, a, [contenteditable="true"]')) return;
      event.preventDefault();
      const start = floatingPanelCurrentGeometry(panel);
      const startX = event.clientX;
      const startY = event.clientY;
      bringFloatingPanelToFront(panel);
      handle.setPointerCapture(event.pointerId);
      panel.classList.add('floating-panel-moving');
      const move = (moveEvent) => {
        const next = constrainedFloatingPanelGeometry({
          ...start,
          left: start.left + moveEvent.clientX - startX,
          top: start.top + moveEvent.clientY - startY,
          z: floatingPanelZCounter,
        });
        panel.style.left = `${next.left}px`;
        panel.style.top = `${next.top}px`;
        updateFloatingWorkspaceHeight();
      };
      const finish = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', finish);
        handle.removeEventListener('pointercancel', finish);
        panel.classList.remove('floating-panel-moving');
        saveFloatingPanelLayout();
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', finish);
      handle.addEventListener('pointercancel', finish);
    });

    handle.addEventListener('keydown', (event) => {
      if (event.target !== handle || window.matchMedia(floatingPanelStackMedia).matches) return;
      const movements = {
        ArrowLeft: [-16, 0],
        ArrowRight: [16, 0],
        ArrowUp: [0, -16],
        ArrowDown: [0, 16],
      };
      const movement = movements[event.key];
      if (!movement) return;
      event.preventDefault();
      const current = floatingPanelCurrentGeometry(panel);
      const multiplier = event.shiftKey ? 3 : 1;
      const next = constrainedFloatingPanelGeometry({
        ...current,
        left: current.left + movement[0] * multiplier,
        top: current.top + movement[1] * multiplier,
      });
      panel.style.left = `${next.left}px`;
      panel.style.top = `${next.top}px`;
      bringFloatingPanelToFront(panel);
      updateFloatingWorkspaceHeight();
      saveFloatingPanelLayout();
    });
  }

  function setupFloatingPanels() {
    if (state.floatingPanelsReady) return;
    const workspace = document.getElementById('floating-workspace');
    const workspaceRect = workspace.getBoundingClientRect();
    const naturalGeometry = new Map();
    floatingPanelElements().forEach((panel, index) => {
      bindFloatingPanel(panel);
      if (panel.hidden) return;
      const rect = panel.getBoundingClientRect();
      naturalGeometry.set(panel.id, {
        left: rect.left - workspaceRect.left,
        top: rect.top - workspaceRect.top,
        width: rect.width,
        height: null,
        z: index + 10,
      });
    });
    workspace.classList.add('floating-panels-enabled');
    state.floatingPanelsReady = true;
    floatingPanelElements().forEach((panel) => {
      if (panel.hidden) return;
      applyFloatingPanelGeometry(
        panel,
        state.floatingPanelLayout.panels?.[panel.id] || naturalGeometry.get(panel.id),
      );
    });
    if ('ResizeObserver' in window) {
      const observer = new ResizeObserver(updateFloatingWorkspaceHeight);
      floatingPanelElements().forEach((panel) => observer.observe(panel));
    }
    let resizeFrame = null;
    window.addEventListener('resize', () => {
      if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = null;
        if (!window.matchMedia(floatingPanelStackMedia).matches) {
          if (!Object.keys(state.floatingPanelLayout.panels || {}).length) {
            resetFloatingPanelLayout({ clearSaved: false });
            return;
          }
          floatingPanelElements().forEach((panel) => {
            if (panel.hidden || panel.dataset.floatingReady !== 'true') return;
            const geometry = floatingPanelCurrentGeometry(panel);
            if (panel.classList.contains('floating-panel-collapsed')) {
              geometry.height = Number.parseFloat(panel.style.height) || null;
            } else if (!Number.isFinite(Number.parseFloat(panel.style.height))) {
              geometry.height = null;
            }
            applyFloatingPanelGeometry(panel, geometry);
          });
        }
        updateFloatingWorkspaceHeight();
      });
    });
    updateFloatingWorkspaceHeight();
  }

  function loadTraceLayout() {
    const fallback = {
      turnRatio: null,
      eventRatio: null,
      workbenchHeight: null,
      stackedTurnHeight: 390,
      stackedEventHeight: 390,
    };
    try {
      const saved = JSON.parse(window.localStorage.getItem(traceLayoutStorageKey) || 'null');
      if (!saved || typeof saved !== 'object') return fallback;
      return {
        turnRatio: Number.isFinite(saved.turnRatio) ? saved.turnRatio : null,
        eventRatio: Number.isFinite(saved.eventRatio) ? saved.eventRatio : null,
        workbenchHeight: Number.isFinite(saved.workbenchHeight) ? saved.workbenchHeight : null,
        stackedTurnHeight: Number.isFinite(saved.stackedTurnHeight) ? saved.stackedTurnHeight : 390,
        stackedEventHeight: Number.isFinite(saved.stackedEventHeight) ? saved.stackedEventHeight : 390,
      };
    } catch (_error) {
      return fallback;
    }
  }

  function saveTraceLayout() {
    try {
      window.localStorage.setItem(traceLayoutStorageKey, JSON.stringify(state.traceLayout));
    } catch (_error) {
      // Read-only and privacy-restricted browser modes may reject local storage.
    }
  }

  function loadClientSessionNonce() {
    try {
      const saved = window.sessionStorage.getItem(clientSessionNonceStorageKey) || '';
      if (/^[A-Za-z0-9_-]{24,160}$/.test(saved)) return saved;
    } catch (_error) {
      // Continue with an in-memory nonce when session storage is unavailable.
    }
    const bytes = new Uint8Array(24);
    window.crypto.getRandomValues(bytes);
    const nonce = [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('');
    try {
      window.sessionStorage.setItem(clientSessionNonceStorageKey, nonce);
    } catch (_error) {
      // The in-memory value still binds approvals for this page lifetime.
    }
    return nonce;
  }

  function loadQAConversationId() {
    try {
      const saved = window.localStorage.getItem(qaConversationIdStorageKey) || '';
      if (/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(saved)) {
        return saved.toLowerCase();
      }
    } catch (_error) {
      // Continue with an in-memory conversation when local storage is unavailable.
    }
    const conversationId = newStudioRequestId();
    try {
      window.localStorage.setItem(qaConversationIdStorageKey, conversationId);
    } catch (_error) {
      // The in-memory ID still scopes agent context for this page lifetime.
    }
    return conversationId;
  }

  function rotateQAConversationId() {
    state.qaConversationId = newStudioRequestId();
    state.qaConversationBootstrapped = false;
    try {
      window.localStorage.setItem(qaConversationIdStorageKey, state.qaConversationId);
    } catch (_error) {
      // The in-memory ID still starts a fresh agent conversation.
    }
  }

  function newStudioRequestId() {
    if (typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function loadQAFloatingButtonPosition() {
    try {
      const saved = JSON.parse(
        window.localStorage.getItem(qaFloatingButtonPositionStorageKey) || 'null',
      );
      if (!saved || !Number.isFinite(saved.left) || !Number.isFinite(saved.top)) return null;
      return { left: saved.left, top: saved.top };
    } catch (_error) {
      return null;
    }
  }

  function saveQAFloatingButtonPosition() {
    try {
      if (state.qaFloatingButtonPosition) {
        window.localStorage.setItem(
          qaFloatingButtonPositionStorageKey,
          JSON.stringify(state.qaFloatingButtonPosition),
        );
      } else {
        window.localStorage.removeItem(qaFloatingButtonPositionStorageKey);
      }
    } catch (_error) {
      // Read-only and privacy-restricted browser modes may reject local storage.
    }
  }

  function tracePaneSizes() {
    return {
      turn: document.querySelector('.trace-turn-pane').getBoundingClientRect().width,
      event: document.querySelector('.trace-event-pane').getBoundingClientRect().width,
      detail: document.getElementById('trace-detail').getBoundingClientRect().width,
    };
  }

  function constrainedTraceWidths(total, turn, event) {
    const minimumTurn = 180;
    const minimumEvent = 240;
    const minimumDetail = 320;
    let nextTurn = bounded(turn, minimumTurn, total - minimumEvent - minimumDetail);
    let nextEvent = bounded(event, minimumEvent, total - nextTurn - minimumDetail);
    let detail = total - nextTurn - nextEvent;
    if (detail < minimumDetail) {
      let deficit = minimumDetail - detail;
      const eventReduction = Math.min(deficit, nextEvent - minimumEvent);
      nextEvent -= eventReduction;
      deficit -= eventReduction;
      nextTurn -= Math.min(deficit, nextTurn - minimumTurn);
      detail = total - nextTurn - nextEvent;
    }
    return { turn: nextTurn, event: nextEvent, detail };
  }

  function setDesktopTraceWidths(sizes, persist = true) {
    const workbench = document.getElementById('trace-workbench');
    const total = sizes.turn + sizes.event + sizes.detail;
    const next = constrainedTraceWidths(total, sizes.turn, sizes.event);
    workbench.style.gridTemplateColumns =
      `${next.turn}px 8px ${next.event}px 8px ${next.detail}px`;
    if (persist) {
      state.traceLayout.turnRatio = next.turn / total;
      state.traceLayout.eventRatio = next.event / total;
    }
  }

  function applyTraceLayout() {
    const workbench = document.getElementById('trace-workbench');
    const stacked = window.matchMedia(traceStackMedia).matches;
    for (const splitter of document.querySelectorAll('.trace-splitter')) {
      splitter.setAttribute('aria-orientation', stacked ? 'horizontal' : 'vertical');
    }
    if (stacked) {
      workbench.style.gridTemplateColumns = '';
      workbench.style.height = '';
      const turnHeight = bounded(state.traceLayout.stackedTurnHeight, 220, 900);
      const eventHeight = bounded(state.traceLayout.stackedEventHeight, 220, 900);
      workbench.style.gridTemplateRows = `${turnHeight}px 8px ${eventHeight}px 8px auto`;
      return;
    }
    workbench.style.gridTemplateRows = '';
    workbench.style.height = state.traceLayout.workbenchHeight
      ? `${bounded(state.traceLayout.workbenchHeight, 420, 1400)}px`
      : '';
    if (state.traceLayout.turnRatio && state.traceLayout.eventRatio) {
      const total = workbench.getBoundingClientRect().width - 18;
      if (total >= 740) {
        setDesktopTraceWidths(
          {
            turn: total * state.traceLayout.turnRatio,
            event: total * state.traceLayout.eventRatio,
            detail: total * (1 - state.traceLayout.turnRatio - state.traceLayout.eventRatio),
          },
          false,
        );
        return;
      }
    }
    workbench.style.gridTemplateColumns = '';
  }

  function resizedDesktopPanes(start, boundary, delta) {
    if (boundary === 0) {
      const combined = start.turn + start.event;
      const turn = bounded(start.turn + delta, 180, combined - 240);
      return { turn, event: combined - turn, detail: start.detail };
    }
    const combined = start.event + start.detail;
    const event = bounded(start.event + delta, 240, combined - 320);
    return { turn: start.turn, event, detail: combined - event };
  }

  function bindTraceSplitter(splitter, boundary) {
    splitter.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const stacked = window.matchMedia(traceStackMedia).matches;
      const startCoordinate = stacked ? event.clientY : event.clientX;
      const startSizes = tracePaneSizes();
      const startHeight = boundary === 0
        ? document.querySelector('.trace-turn-pane').getBoundingClientRect().height
        : document.querySelector('.trace-event-pane').getBoundingClientRect().height;
      splitter.setPointerCapture(event.pointerId);

      const move = (moveEvent) => {
        const coordinate = stacked ? moveEvent.clientY : moveEvent.clientX;
        const delta = coordinate - startCoordinate;
        if (stacked) {
          const key = boundary === 0 ? 'stackedTurnHeight' : 'stackedEventHeight';
          state.traceLayout[key] = bounded(startHeight + delta, 220, 900);
          applyTraceLayout();
        } else {
          setDesktopTraceWidths(resizedDesktopPanes(startSizes, boundary, delta));
        }
      };
      const finish = () => {
        splitter.removeEventListener('pointermove', move);
        splitter.removeEventListener('pointerup', finish);
        splitter.removeEventListener('pointercancel', finish);
        saveTraceLayout();
      };
      splitter.addEventListener('pointermove', move);
      splitter.addEventListener('pointerup', finish);
      splitter.addEventListener('pointercancel', finish);
    });

    splitter.addEventListener('keydown', (event) => {
      const stacked = window.matchMedia(traceStackMedia).matches;
      const negativeKey = stacked ? 'ArrowUp' : 'ArrowLeft';
      const positiveKey = stacked ? 'ArrowDown' : 'ArrowRight';
      if (![negativeKey, positiveKey].includes(event.key)) return;
      event.preventDefault();
      const delta = event.key === negativeKey ? -20 : 20;
      if (stacked) {
        const key = boundary === 0 ? 'stackedTurnHeight' : 'stackedEventHeight';
        state.traceLayout[key] = bounded(state.traceLayout[key] + delta, 220, 900);
        applyTraceLayout();
      } else {
        setDesktopTraceWidths(resizedDesktopPanes(tracePaneSizes(), boundary, delta));
      }
      saveTraceLayout();
    });

    splitter.addEventListener('dblclick', () => {
      if (window.matchMedia(traceStackMedia).matches) {
        state.traceLayout[boundary === 0 ? 'stackedTurnHeight' : 'stackedEventHeight'] = 390;
      } else {
        state.traceLayout.turnRatio = null;
        state.traceLayout.eventRatio = null;
      }
      applyTraceLayout();
      saveTraceLayout();
    });
  }

  function setupTraceResizing() {
    bindTraceSplitter(document.getElementById('trace-turn-resizer'), 0);
    bindTraceSplitter(document.getElementById('trace-event-resizer'), 1);
    const heightResizer = document.getElementById('trace-height-resizer');
    heightResizer.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || window.matchMedia(traceStackMedia).matches) return;
      event.preventDefault();
      const workbench = document.getElementById('trace-workbench');
      const startY = event.clientY;
      const startHeight = workbench.getBoundingClientRect().height;
      heightResizer.setPointerCapture(event.pointerId);
      const move = (moveEvent) => {
        state.traceLayout.workbenchHeight = bounded(startHeight + moveEvent.clientY - startY, 420, 1400);
        applyTraceLayout();
      };
      const finish = () => {
        heightResizer.removeEventListener('pointermove', move);
        heightResizer.removeEventListener('pointerup', finish);
        heightResizer.removeEventListener('pointercancel', finish);
        saveTraceLayout();
      };
      heightResizer.addEventListener('pointermove', move);
      heightResizer.addEventListener('pointerup', finish);
      heightResizer.addEventListener('pointercancel', finish);
    });
    heightResizer.addEventListener('keydown', (event) => {
      if (!['ArrowUp', 'ArrowDown'].includes(event.key) || window.matchMedia(traceStackMedia).matches) return;
      event.preventDefault();
      const current = document.getElementById('trace-workbench').getBoundingClientRect().height;
      state.traceLayout.workbenchHeight = bounded(current + (event.key === 'ArrowUp' ? -24 : 24), 420, 1400);
      applyTraceLayout();
      saveTraceLayout();
    });
    heightResizer.addEventListener('dblclick', () => {
      state.traceLayout.workbenchHeight = null;
      applyTraceLayout();
      saveTraceLayout();
    });
    let resizeFrame = null;
    window.addEventListener('resize', () => {
      if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = null;
        applyTraceLayout();
      });
    });
    applyTraceLayout();
  }

  function renderHeader() {
    const sourceCount = Number(data.meta.source_count) || 0;
    document.getElementById('dashboard-title').textContent = data.meta.title || 'Agent Trace Studio';
    const traceLabel = document.getElementById('dashboard-trace-label');
    const traceSetTitle = String(data.meta.trace_set_title || '').trim();
    traceLabel.textContent = traceSetTitle;
    traceLabel.hidden = !traceSetTitle;
    document.getElementById('generated-label').textContent =
      `Generated ${formatDate(data.meta.generated_at)} · ${formatInteger(sourceCount)} trace file${sourceCount === 1 ? '' : 's'}`;
  }

  function traceKey(trace) {
    return `${trace.session_id}\u0000${trace.session_file}`;
  }

  function loadedTraceKeys() {
    return new Set(data.traces.map(traceKey));
  }

  function sessionSummary(trace) {
    if (!trace) return null;
    return data.sessions.find(
      (session) => session.session_id === trace.session_id && session.session_file === trace.session_file,
    ) || null;
  }

  function sessionLabel(trace) {
    const summary = sessionSummary(trace);
    const rawContext = summary?.repository && summary.repository !== 'unknown' ? summary.repository : summary?.cwd;
    const parts = String(rawContext || '').replaceAll('\\', '/').split('/').filter(Boolean);
    const context = parts.at(-1) || 'Agent trace';
    const timestamp = summary?.first_turn_started_at || summary?.session_started_at;
    return `${context} · ${formatDate(timestamp)} · ${shortId(trace.session_id)}`;
  }

  function traceAdapterLabel(trace) {
    if (!trace) return 'Pending';
    const liveSources = Array.isArray(runtime.live?.sources) ? runtime.live.sources : [];
    const liveSource = liveSources.find((item) => item.path === trace.session_file)
      || liveSources.find((item) => item.session_id === trace.session_id);
    const summary = sessionSummary(trace);
    const candidates = [liveSource?.adapter, summary?.originator, summary?.source]
      .map((value) => String(value || '').trim())
      .filter(Boolean);
    for (const candidate of candidates) {
      const normalized = candidate.toLowerCase().replaceAll('_', '-');
      if (normalized.includes('codex')) return 'Codex';
      if (normalized.includes('claude')) return 'Claude Code';
      if (normalized === 'langgraph') return 'LangGraph';
      if (normalized.includes('openai') && normalized.includes('agent')) return 'OpenAI Agents SDK';
    }
    const raw = candidates[0] || '';
    const normalized = raw.toLowerCase().replaceAll('_', '-');
    if (!raw || normalized === 'unknown' || normalized === 'file') return 'Trace file';
    return raw
      .split(/[\s._-]+/)
      .filter(Boolean)
      .map((part) => (part.toLowerCase() === 'sdk' ? 'SDK' : `${part[0].toUpperCase()}${part.slice(1)}`))
      .join(' ');
  }

  function renderSourceContext() {
    const trace = selectedTrace();
    const hasSessions = Boolean(data.traces.length);
    const liveEnabled = Boolean(runtime.live?.enabled);
    document.getElementById('source-context').hidden = !hasSessions && !liveEnabled;
    document.getElementById('trace').hidden = !hasSessions;
    const source = trace?.session_file || '';
    const currentSource = document.getElementById('current-source');
    currentSource.textContent = source ? compactPath(source) : 'Waiting for a live trace';
    currentSource.title = source;
    const turnCount = trace ? data.turns.filter((turn) => turn.session_id === trace.session_id).length : 0;
    document.getElementById('current-source-adapter').textContent = traceAdapterLabel(trace);
    document.getElementById('current-source-turn-count').textContent = formatInteger(turnCount);
    document.getElementById('current-source-event-count').textContent = formatInteger(
      Number(trace?.events_total ?? trace?.events?.length) || 0,
    );
    const sessionSelect = document.getElementById('trace-session-select');
    document.getElementById('loaded-session-field').hidden = !hasSessions;
    clear(sessionSelect);
    data.traces.forEach((item) => {
      const option = element('option', '', sessionLabel(item));
      option.value = item.session_id;
      option.title = `${item.session_id} · ${item.session_file}`;
      sessionSelect.appendChild(option);
    });
    sessionSelect.value = state.traceSessionId || '';
    const status = document.getElementById('source-status');
    status.textContent = hasSessions
      ? `${formatInteger(data.traces.length)} trace${data.traces.length === 1 ? '' : 's'} loaded`
      : 'No trace has registered yet';
    status.classList.remove('error-text');
    renderLiveMonitor();
  }

  function renderLiveMonitor() {
    const control = document.getElementById('live-audit-control');
    const startButton = document.getElementById('live-audit-start');
    const controlStatus = document.getElementById('live-audit-status');
    control.hidden = !runtime.interactive;
    if (runtime.interactive) {
      const enabled = Boolean(runtime.live?.enabled);
      startButton.disabled = state.liveControlBusy || enabled;
      startButton.textContent = state.liveControlBusy
        ? 'Starting live monitor & audit…'
        : enabled
          ? 'Live monitor & audit active'
          : 'Start live monitor & audit';
      const activeRules = Number(runtime.live?.audit?.active_rules) || Number(runtime.audit_rules?.active_count) || 0;
      const automaticActions = Number(runtime.live?.audit?.automatic_actions) || 0;
      controlStatus.textContent =
        state.liveControlError ||
        (enabled
          ? `${formatInteger(activeRules)} active rules · ${formatInteger(automaticActions)} automatic actions`
          : 'Opt-in');
      controlStatus.classList.toggle('error-text', Boolean(state.liveControlError));
    }
    const panel = document.getElementById('live-monitor');
    const live = runtime.live || {};
    panel.hidden = !runtime.interactive || !live.enabled;
    if (panel.hidden) return;
    const stateNode = document.getElementById('live-state');
    let connectionState = live.error ? 'error' : live.state || 'starting';
    if (!state.liveConnected && connectionState === 'watching') connectionState = 'disconnected';
    stateNode.dataset.state = connectionState;
    const labels = {
      starting: 'Connecting',
      watching: 'Live',
      disconnected: 'Disconnected',
      error: 'Monitor error',
    };
    document.getElementById('live-state-label').textContent = labels[connectionState] || 'Live';
    const parts = [`${formatInteger(live.monitored_files)} file${live.monitored_files === 1 ? '' : 's'}`];
    if (live.updated_at) parts.push(`updated ${formatActivityTime(live.updated_at)}`);
    if (state.liveNewEvents) parts.push(`${formatInteger(state.liveNewEvents)} new events`);
    if (live.error) parts.push(live.error);
    document.getElementById('live-message').textContent = parts.join(' · ');
    document.getElementById('follow-live').checked = state.followLive;
  }

  async function startLiveMonitorAndAudit() {
    if (!runtime.interactive || runtime.live?.enabled || state.liveControlBusy) return;
    state.liveControlBusy = true;
    state.liveControlError = '';
    renderLiveMonitor();
    try {
      const response = await apiJson('/api/live/start', { method: 'POST' });
      runtime.live = response.live || runtime.live;
      state.liveRevision = Math.max(state.liveRevision, Number(runtime.live?.revision) || 0);
      setupLiveStream();
      await refreshAuditRules();
    } catch (error) {
      state.liveControlError = error.message;
    } finally {
      state.liveControlBusy = false;
      renderLiveMonitor();
    }
  }

  function assuranceStatusLabel(status) {
    return {
      watching: 'Watching',
      satisfied: 'Satisfied',
      pending: 'Pending',
      violated: 'Violated',
    }[status] || 'Unclear';
  }

  function assuranceContractStatus(contract) {
    if (state.assuranceReplayStep === null) return contract.status || 'watching';
    let status = 'watching';
    const transitions = Array.isArray(contract.replay) ? contract.replay : [];
    transitions.forEach((transition) => {
      if (Number(transition.step) <= state.assuranceReplayStep) status = transition.status || status;
    });
    return status;
  }

  function activeAssurance() {
    const dynamic = state.auditRules?.assurance;
    const savedRules = state.auditRules?.rule_set?.rules;
    if (data.assurance?.mode === 'demo' && (!Array.isArray(savedRules) || !savedRules.length)) return data.assurance;
    return dynamic || data.assurance || null;
  }

  function assuranceAnchor(evidence) {
    return `[turn ${evidence.turn_id || 'unknown'}, line ${formatInteger(evidence.line_number)}]`;
  }

  function selectAssuranceEvidence(contract, status, evidence, { scroll = true } = {}) {
    const trace = data.traces.find((item) => item.session_id === evidence.session_id);
    if (!trace) return;
    const exists = trace.events.some(
      (event) => event.turn_id === evidence.turn_id && event.sequence === Number(evidence.event_sequence),
    );
    if (!exists) return;
    resetAgentFocus({ checkpoint: true });
    state.traceSessionId = evidence.session_id;
    state.traceTurnId = evidence.turn_id;
    state.traceEventSequence = Number(evidence.event_sequence);
    state.traceCategory = 'all';
    state.traceTool = '';
    state.assuranceContract = {
      id: String(contract.id || ''),
      version: String(contract.version || ''),
      turn_id: String(evidence.turn_id || ''),
      title: String(contract.title || 'Audit rule'),
      severity: String(contract.severity || 'medium'),
      status: String(status || contract.status || 'unclear'),
      expectation: String(contract.expectation || ''),
      observation: String(contract.observation || ''),
      evidence_anchors: (Array.isArray(contract.evidence) ? contract.evidence : []).map(assuranceAnchor).slice(0, 8),
    };
    renderDashboard();
    if (scroll) {
      window.requestAnimationFrame(() => {
        document.getElementById('trace-workbench')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
    }
  }

  function prepareAssuranceInvestigation(contract, status) {
    const evidence = Array.isArray(contract.evidence) ? contract.evidence.at(-1) : null;
    if (!evidence) return;
    selectAssuranceEvidence(contract, status, evidence, { scroll: false });
    const input = document.getElementById('qa-question');
    input.value = `Investigate the selected audit rule ${contract.id} and explain the finding, evidence, and safest next action.`;
    input.focus({ preventScroll: true });
    document.querySelector('.qa-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function appendAssuranceDefinition(container, label, value) {
    const item = element('div', 'assurance-definition');
    item.append(element('span', 'control-label', label), element('p', '', value || 'Not available.'));
    container.appendChild(item);
  }

  function renderAssurance() {
    const assurance = activeAssurance();
    const panel = document.getElementById('assurance-panel');
    const rulesAvailable = Boolean(runtime.interactive && runtime.audit_rules?.available);
    panel.hidden = !assurance?.enabled && !rulesAvailable;
    if (panel.hidden) return;
    ensureFloatingPanelPlaced(panel);
    const content = document.getElementById('assurance-content');
    const toggle = document.getElementById('assurance-toggle');
    content.hidden = state.assuranceCollapsed;
    renderCollapseToggle(toggle, state.assuranceCollapsed, 'session audit');
    syncFloatingPanelCollapse(panel, state.assuranceCollapsed);
    document.getElementById('assurance-label').textContent = assurance?.mode === 'demo' ? 'Execution contracts' : 'Audit rules';
    document.getElementById('assurance-title').textContent = assurance?.title || 'Session audit';
    document.getElementById('assurance-summary').textContent =
      state.auditRulesError || assurance?.summary || 'No active audit rules for this session.';
    const contracts = Array.isArray(assurance?.contracts) ? assurance.contracts : [];
    const statuses = contracts.map((contract) => assuranceContractStatus(contract));
    document.getElementById('assurance-active-count').textContent = formatInteger(contracts.length);
    document.getElementById('assurance-violation-count').textContent = formatInteger(
      statuses.filter((status) => status === 'violated').length,
    );
    document.getElementById('assurance-pending-count').textContent = formatInteger(
      statuses.filter((status) => status === 'pending').length,
    );
    const steps = Array.isArray(assurance?.replay_steps) ? assurance.replay_steps : [];
    const replaying = state.assuranceReplayTimer !== null;
    const replayButton = document.getElementById('assurance-replay');
    const demoMode = assurance?.mode === 'demo';
    replayButton.disabled = state.auditRulesBusy || replaying || (demoMode && !steps.length);
    replayButton.textContent = demoMode
      ? replaying
        ? 'Replaying...'
        : 'Replay evaluation'
      : state.auditRulesBusy
        ? 'Evaluating...'
        : 'Evaluate now';
    const manageButton = document.getElementById('assurance-manage');
    manageButton.hidden = !rulesAvailable;
    manageButton.disabled = state.auditRulesBusy;
    const replayStatus = document.getElementById('assurance-replay-status');
    if (!demoMode) {
      const revision = state.auditRules?.rule_set?.revision;
      replayStatus.textContent = state.auditRulesError
        ? state.auditRulesError
        : `Rule set revision ${formatInteger(revision || 0)} · deterministic evaluation`;
    } else if (state.assuranceReplayStep === null) {
      replayStatus.textContent = 'Evaluation complete';
    } else {
      const step = steps[state.assuranceReplayStep];
      replayStatus.textContent = step
        ? `Step ${formatInteger(state.assuranceReplayStep + 1)} of ${formatInteger(steps.length)} · ${step.message}`
        : 'Watching the execution path';
    }

    const list = document.getElementById('assurance-contract-list');
    const firstRender = !list.dataset.rendered;
    const openContracts = new Set(
      Array.from(list.querySelectorAll('.assurance-contract[open]')).map((node) => node.dataset.contractId),
    );
    clear(list);
    if (!contracts.length) {
      list.appendChild(element('p', 'empty-state', 'No active audit rules.'));
    }
    contracts.forEach((contract, index) => {
      const status = statuses[index];
      const details = element('details', 'assurance-contract');
      details.dataset.contractId = contract.id || '';
      const active = state.assuranceContract?.id === contract.id;
      details.classList.toggle('active', active);
      details.open = openContracts.has(contract.id) || (firstRender && status === 'violated');
      const summary = element('summary', 'assurance-contract-summary');
      if (active) summary.setAttribute('aria-current', 'true');
      const heading = element('span', 'assurance-contract-heading');
      const title = element('span', 'assurance-contract-title');
      title.append(
        element('strong', '', contract.title || 'Execution contract'),
        element('small', 'muted', `${contract.id || 'contract'} · v${contract.version || '1'}`),
      );
      heading.append(
        title,
        element('span', `assurance-severity ${contract.severity || 'medium'}`, contract.severity || 'medium'),
        element('span', `assurance-status ${status}`, assuranceStatusLabel(status)),
      );
      summary.appendChild(heading);
      details.appendChild(summary);

      const body = element('div', 'assurance-contract-body');
      const definitions = element('div', 'assurance-definitions');
      appendAssuranceDefinition(definitions, 'Expected', contract.expectation);
      appendAssuranceDefinition(
        definitions,
        'Observed',
        status === 'watching' ? 'Waiting for relevant execution evidence.' : contract.observation,
      );
      body.appendChild(definitions);
      const evidence = Array.isArray(contract.evidence) ? contract.evidence : [];
      const evidenceSection = element('section', 'assurance-evidence');
      evidenceSection.appendChild(element('h4', '', 'Evidence'));
      const evidenceList = element('div', 'assurance-evidence-list');
      evidence.forEach((item) => {
        const button = element('button', 'assurance-evidence-button');
        button.type = 'button';
        button.append(
          element('strong', '', item.label || item.event_title || 'Trace event'),
          element('span', '', `${item.event_title || 'Event'} · line ${formatInteger(item.line_number)}`),
        );
        button.addEventListener('click', () => selectAssuranceEvidence(contract, status, item));
        evidenceList.appendChild(button);
      });
      evidenceSection.appendChild(evidenceList);
      body.appendChild(evidenceSection);
      if (status === 'violated') {
        const actions = element('div', 'assurance-contract-actions');
        const investigate = element('button', 'command-button secondary compact', 'Investigate');
        investigate.type = 'button';
        investigate.addEventListener('click', () => prepareAssuranceInvestigation(contract, status));
        actions.appendChild(investigate);
        const auditAction = contract.action || {};
        const receipt = auditAction.receipt || null;
        const receiptStatus = receipt?.status || '';
        if (auditAction.available || receipt) {
          const send = element(
            'button',
            'command-button compact',
            receiptStatus === 'queued' || receiptStatus === 'running'
              ? 'Sending message…'
              : receiptStatus === 'delivered'
                ? 'Message delivered'
                : auditAction.automatic
                  ? 'Retry automatic action'
                  : 'Send message to session agent',
          );
          send.type = 'button';
          send.disabled =
            state.auditRulesBusy ||
            !auditAction.available ||
            ['queued', 'running', 'delivered'].includes(receiptStatus);
          send.addEventListener('click', () => sendSessionAgentAuditMessage(contract));
          actions.appendChild(send);
        }
        if (receipt?.message) actions.appendChild(element('span', 'audit-action-status muted', receipt.message));
        else if (!auditAction.available && auditAction.reason) {
          actions.appendChild(element('span', 'audit-action-status muted', auditAction.reason));
        }
        body.appendChild(actions);
      }
      details.appendChild(body);
      list.appendChild(details);
    });
    list.dataset.rendered = 'true';
  }

  async function sendSessionAgentAuditMessage(contract) {
    const sessionId = state.traceSessionId;
    const ruleVersion = Number(contract?.version);
    if (!sessionId || !contract?.id || !ruleVersion || state.auditRulesBusy) return;
    state.auditRulesBusy = true;
    state.auditRulesError = '';
    renderAssurance();
    try {
      await apiJson('/api/live/audit-actions/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, rule_id: contract.id, rule_version: ruleVersion }),
      });
    } catch (error) {
      state.auditRulesError = error.message;
    } finally {
      state.auditRulesBusy = false;
      await refreshAuditRules();
    }
  }

  function focusAssuranceReplayStep(step) {
    const assurance = activeAssurance();
    const contracts = Array.isArray(assurance?.contracts) ? assurance.contracts : [];
    const evidence = contracts
      .flatMap((contract) => (Array.isArray(contract.evidence) ? contract.evidence : []))
      .find((item) => Number(item.event_sequence) === Number(step.event_sequence));
    if (!evidence) return;
    const trace = data.traces.find((item) => item.session_id === evidence.session_id);
    if (!trace) return;
    resetAgentFocus({ checkpoint: true });
    state.traceSessionId = evidence.session_id;
    state.traceTurnId = evidence.turn_id;
    state.traceEventSequence = Number(evidence.event_sequence);
    state.traceCategory = 'all';
    state.traceTool = '';
  }

  function replayAssurance() {
    const assurance = activeAssurance();
    if (assurance?.mode !== 'demo') {
      refreshAuditRules();
      return;
    }
    const steps = Array.isArray(assurance?.replay_steps) ? assurance.replay_steps : [];
    if (!steps.length || state.assuranceReplayTimer !== null) return;
    state.assuranceReplayStep = -1;
    const advance = () => {
      state.assuranceReplayStep += 1;
      const step = steps[state.assuranceReplayStep];
      if (!step) {
        state.assuranceReplayStep = steps.length - 1;
        state.assuranceReplayTimer = null;
        renderAssurance();
        return;
      }
      focusAssuranceReplayStep(step);
      renderDashboard();
      state.assuranceReplayTimer = window.setTimeout(advance, 1100);
    };
    state.assuranceReplayTimer = window.setTimeout(advance, 350);
    renderAssurance();
  }

  function selectedTrace() {
    return data.traces.find((trace) => trace.session_id === state.traceSessionId) || null;
  }

  function traceTurnRows(trace) {
    const sessionTurns = data.turns
      .filter((turn) => turn.session_id === trace.session_id)
      .sort((left, right) => String(left.started_at).localeCompare(String(right.started_at)));
    const rows = sessionTurns.map((turn, index) => ({ ...turn, trace_label: `Turn ${index + 1}` })).reverse();
    if (trace.events.some((event) => event.turn_id === 'session')) {
      rows.push({
        session_id: trace.session_id,
        turn_id: 'session',
        trace_label: 'Trace context',
        status: 'context',
        started_at: '',
        duration_secs: null,
      });
    }
    return rows;
  }

  function eventsByTurn(trace) {
    const grouped = new Map();
    trace.events.forEach((event) => {
      const events = grouped.get(event.turn_id) || [];
      events.push(event);
      grouped.set(event.turn_id, events);
    });
    return grouped;
  }

  function selectedTraceTurn(trace) {
    if (!trace) return null;
    return traceTurnRows(trace).find((turn) => turn.turn_id === state.traceTurnId) || null;
  }

  function selectedTraceEvent(trace) {
    if (!trace) return null;
    return trace.events.find(
      (event) => event.turn_id === state.traceTurnId && event.sequence === state.traceEventSequence,
    ) || null;
  }

  function dashboardStateSnapshot() {
    const trace = selectedTrace();
    const checkpoint = state.traceCheckpoint
      ? Object.freeze({
            run_id: state.traceCheckpoint.run_id,
            index: state.traceCheckpoint.index,
            title: state.traceCheckpoint.title,
            status: state.traceCheckpoint.status,
            summary: state.traceCheckpoint.summary,
            turn_ids: Object.freeze([...state.traceCheckpoint.turn_ids]),
            start_event_sequence: state.traceCheckpoint.start_event_sequence,
            end_event_sequence: state.traceCheckpoint.end_event_sequence,
            actions: Object.freeze([...state.traceCheckpoint.actions]),
            achievements: Object.freeze([...state.traceCheckpoint.achievements]),
            blockers: Object.freeze([...state.traceCheckpoint.blockers]),
            artifacts: Object.freeze([...state.traceCheckpoint.artifacts]),
            next_steps: Object.freeze([...state.traceCheckpoint.next_steps]),
            evidence_anchors: Object.freeze([...state.traceCheckpoint.evidence_anchors]),
          })
      : null;
    const contract =
      state.assuranceContract && state.assuranceContract.turn_id === state.traceTurnId
        ? Object.freeze({
            id: state.assuranceContract.id,
            version: state.assuranceContract.version,
            turn_id: state.assuranceContract.turn_id,
            title: state.assuranceContract.title,
            severity: state.assuranceContract.severity,
            status: state.assuranceContract.status,
            expectation: state.assuranceContract.expectation,
            observation: state.assuranceContract.observation,
            evidence_anchors: Object.freeze([...state.assuranceContract.evidence_anchors]),
          })
        : null;
    const highlight = state.highlightedText
      ? Object.freeze({ text: state.highlightedText, origin: state.highlightOrigin || 'dashboard' })
      : null;
    const askTarget =
      state.askTarget && state.askTarget.turn_id === state.traceTurnId
        ? Object.freeze({ ...state.askTarget })
        : null;
    return Object.freeze({
      source: Object.freeze({ session_id: trace?.session_id || null }),
      selection: Object.freeze({
        turn_id: state.traceTurnId,
        event_sequence: state.traceEventSequence,
        ask_target: askTarget,
        checkpoint,
        contract,
        highlight,
      }),
      view: Object.freeze({
        active_tab: 'trace',
        search_query: document.getElementById('trace-search')?.value || '',
        category: state.traceCategory,
        tool: state.traceTool,
      }),
    });
  }

  function setAgentContextValue(id, metaId, value, meta, title = '') {
    const node = document.getElementById(id);
    const metaNode = document.getElementById(metaId);
    node.textContent = value;
    node.title = title || value;
    metaNode.textContent = meta;
    metaNode.title = title || meta;
  }

  function renderAgentContext() {
    const trace = selectedTrace();
    const turn = selectedTraceTurn(trace);
    const event = selectedTraceEvent(trace);
    const checkpoint = state.traceCheckpoint;
    const contract = state.assuranceContract?.turn_id === turn?.turn_id ? state.assuranceContract : null;
    const askTarget = state.askTarget?.turn_id === turn?.turn_id ? state.askTarget : null;
    const turnEvents = trace && turn ? eventsByTurn(trace).get(turn.turn_id) || [] : [];
    const contextSummary = document.getElementById('qa-context-summary');
    if (contextSummary) {
      contextSummary.textContent = trace
        ? [`Session ${shortId(trace.session_id)}`, turn?.trace_label, event ? `Event #${event.sequence}` : ''].filter(Boolean).join(' · ')
        : 'No source loaded';
    }

    setAgentContextValue(
      'agent-context-source',
      'agent-context-source-meta',
      trace ? sessionLabel(trace) : 'No source loaded',
      trace ? compactPath(trace.session_file) : 'Not selected',
      trace ? `${trace.session_id} · ${trace.session_file}` : '',
    );
    setAgentContextValue(
      'agent-context-turn',
      'agent-context-turn-meta',
      turn?.trace_label || 'No turn selected',
      turn ? `${shortId(turn.turn_id)} · ${traceTurnSummary(turn, turnEvents)}` : 'Not selected',
      turn?.turn_id || '',
    );
    setAgentContextValue(
      'agent-context-event',
      'agent-context-event-meta',
      event?.title || 'No event selected',
      event ? `#${event.sequence} · ${event.category} · line ${event.line_number}` : 'Not selected',
      event ? `${event.kind} · ${event.timestamp || 'no timestamp'}` : '',
    );
    document.getElementById('agent-context-analysis-label').textContent = contract ? 'Contract' : 'Checkpoint';
    if (contract) {
      setAgentContextValue(
        'agent-context-checkpoint',
        'agent-context-checkpoint-meta',
        contract.title,
        `${contract.status} · ${contract.severity} severity · v${contract.version}`,
        `${contract.expectation}\n${contract.observation}`,
      );
    } else {
      setAgentContextValue(
        'agent-context-checkpoint',
        'agent-context-checkpoint-meta',
        checkpoint ? `Checkpoint ${checkpoint.index}: ${checkpoint.title}` : 'No checkpoint selected',
        checkpoint
          ? `${checkpoint.status || 'unclear'} · ${formatInteger(checkpoint.turn_ids.length)} turn${checkpoint.turn_ids.length === 1 ? '' : 's'} · events ${checkpoint.start_event_sequence || '?'}–${checkpoint.end_event_sequence || '?'}`
          : 'Not selected',
        checkpoint?.summary || '',
      );
    }
    if (askTarget) {
      const targetType = askTarget.kind === 'turn' ? 'Turn' : askTarget.kind === 'message' ? 'Message' : 'Event';
      const targetDetail = askTarget.summary || askTarget.text || 'Selected trace item';
      setAgentContextValue(
        'agent-context-focus',
        'agent-context-focus-meta',
        `${targetType}: ${askTarget.label || 'Selected item'}`,
        compactText(targetDetail, 110),
        askTarget.text || targetDetail,
      );
    } else if (state.highlightedText) {
      setAgentContextValue(
        'agent-context-focus',
        'agent-context-focus-meta',
        'Highlighted text',
        compactText(state.highlightedText, 110),
        state.highlightedText,
      );
    } else {
      setAgentContextValue(
        'agent-context-focus',
        'agent-context-focus-meta',
        'No focused text',
        'Not selected',
      );
    }
  }

  function renderSessionBrief() {
    const panel = document.getElementById('session-brief-panel');
    const content = document.getElementById('session-brief-content');
    const statusNode = document.getElementById('session-brief-status');
    const button = document.getElementById('session-brief-button');
    const toggle = document.getElementById('session-brief-toggle');
    const autoField = document.getElementById('session-brief-auto-field');
    const autoToggle = document.getElementById('session-brief-auto');
    const trace = selectedTrace();
    panel.hidden = !runtime.interactive || !trace;
    if (panel.hidden) return;

    ensureFloatingPanelPlaced(panel);
    content.hidden = state.sessionBriefCollapsed;
    renderCollapseToggle(toggle, state.sessionBriefCollapsed, 'session brief');
    syncFloatingPanelCollapse(panel, state.sessionBriefCollapsed);

    const brief = state.sessionBrief?.session_id === trace.session_id ? state.sessionBrief : null;
    const agent = runtime.agent || {};
    const running = Boolean(state.agentRun && !terminalAgentStatus(state.agentRun.status));
    autoField.hidden = !runtime.live?.enabled;
    autoToggle.checked = state.sessionBriefAutoUpdate;
    autoToggle.disabled = state.sessionBriefBusy || !runtime.qa?.configured;
    button.textContent = brief?.available ? 'Refresh summary' : 'Summarize session';
    button.disabled = Boolean(
      !agent.available ||
        !agent.workflows?.checkpoints ||
        !agent.harness_available ||
        !runtime.qa?.configured ||
        running ||
        state.agentBusy ||
        state.qaBusy ||
        state.sessionBriefBusy,
    );
    button.title = 'Send a session summary refresh request in Studio conversation';

    const eventCount = Number(brief?.event_count ?? trace.events_total ?? trace.events.length) || 0;
    const through = Number(brief?.through_event_sequence) || 0;
    const latest = Number(brief?.latest_event_sequence) || 0;
    const newEvents = Number(brief?.new_event_count) || 0;
    const sampling = brief?.result?.event_details_sampled ? ' · representative event detail' : '';
    statusNode.classList.toggle('session-brief-stale', Boolean(brief?.stale));
    if (state.sessionBriefError) {
      statusNode.textContent = state.sessionBriefError;
    } else if (brief?.status === 'updating' || state.sessionBriefBusy) {
      statusNode.textContent = `Updating ${formatInteger(eventCount)} session events`;
    } else if (brief?.available && brief.stale) {
      statusNode.textContent = `${formatInteger(newEvents)} new event${newEvents === 1 ? '' : 's'} · summarized through #${formatInteger(through)} of #${formatInteger(latest)}`;
    } else if (brief?.available) {
      const revision = brief.revision ? ` · revision ${formatInteger(brief.revision)}` : '';
      const updated = brief.updated_at ? ` · ${formatDate(brief.updated_at)}` : '';
      statusNode.textContent = `Session brief covers ${formatInteger(eventCount)} events through #${formatInteger(through)}${revision}${sampling}${updated}`;
    } else {
      statusNode.textContent = `${formatInteger(eventCount)} session events · no summary yet`;
    }

    const openCheckpoints = new Set(
      Array.from(content.querySelectorAll('.checkpoint-item[open]')).map((node) => node.dataset.checkpointKey),
    );
    clear(content);
    if (!brief?.available || !brief.result) {
      content.appendChild(element('p', 'session-brief-empty', 'No session brief has been generated.'));
      return;
    }
    renderCheckpointResult(
      content,
      brief.result,
      { run_id: brief.run_id, session_id: brief.session_id, kind: 'checkpoints' },
      openCheckpoints,
    );
  }

  function toolRowsForSession(sessionId) {
    const counts = new Map();
    data.turns.filter((turn) => turn.session_id === sessionId).forEach((turn) => {
      Object.entries(turn.tool_breakdown || {}).forEach(([name, count]) => {
        counts.set(name, (counts.get(name) || 0) + Number(count || 0));
      });
    });
    return [...counts.entries()]
      .map(([name, value]) => ({ name, value }))
      .sort((left, right) => right.value - left.value || left.name.localeCompare(right.name));
  }

  function traceEventMatches(event, query, category = state.traceCategory, toolName = state.traceTool) {
    if (category !== 'all' && event.category !== category) return false;
    if (toolName && event.tool_name !== toolName) return false;
    if (!query) return true;
    return [
      event.line_number,
      event.output_line_number,
      event.title,
      event.kind,
      event.role,
      event.phase,
      event.text,
      event.tool_name,
      event.call_id,
      event.status,
      event.input_text,
      event.output_text,
    ].some((value) => normalizedSearch(value).includes(query));
  }

  function traceEventSummary(event) {
    if (event.category === 'tool') {
      return compactText(event.input_text || event.output_text || event.call_id || event.kind);
    }
    return compactText(event.text || event.status || event.kind);
  }

  function traceTurnSummary(turn, events) {
    const userMessage = events.find((event) => event.role === 'user' && event.text);
    if (userMessage) return compactText(userMessage.text, 86);
    const assistantMessage = events.find((event) => event.role === 'assistant' && event.text);
    if (assistantMessage) return compactText(assistantMessage.text, 86);
    if (turn.turn_id === 'session') return 'Trace metadata and execution lifecycle';
    return `${formatInteger(events.length)} normalized events`;
  }

  function ensureTraceSelection(turns, grouped) {
    if (!turns.some((turn) => turn.turn_id === state.traceTurnId)) {
      resetAgentFocus();
      state.traceTurnId = turns.length ? turns[0].turn_id : null;
      state.traceEventSequence = null;
    }
    const events = grouped.get(state.traceTurnId) || [];
    if (!events.some((event) => event.sequence === state.traceEventSequence)) {
      resetAgentFocus();
      state.traceEventSequence = events.length ? events[0].sequence : null;
    }
  }

  function renderTraceCategoryFilters(events, query) {
    const container = document.getElementById('trace-category-filters');
    clear(container);
    traceCategories.forEach(([key, label]) => {
      const count = events.filter((event) => traceEventMatches(event, query, key, key === 'tool' ? state.traceTool : '')).length;
      const active = state.traceCategory === key;
      const button = element('button', `metric-button trace-category-button${active ? ' active' : ''}`);
      button.type = 'button';
      button.dataset.traceCategory = key;
      button.append(document.createTextNode(label), element('span', 'trace-filter-count', formatInteger(count)));
      button.addEventListener('click', () => {
        pauseLiveFollow();
        resetAgentFocus();
        state.traceCategory = key;
        if (key !== 'tool') state.traceTool = '';
        state.traceEventSequence = null;
        renderDashboard();
      });
      container.appendChild(button);
    });
  }

  function renderToolSummary() {
    const rows = toolRowsForSession(state.traceSessionId);
    const total = rows.reduce((sum, row) => sum + row.value, 0);
    document.getElementById('trace-tool-total').textContent = formatInteger(total);
    document.getElementById('trace-tool-unique').textContent = formatInteger(rows.length);
    const chips = document.getElementById('trace-tool-chips');
    clear(chips);
    rows.slice(0, 10).forEach((row) => {
      const active = state.traceTool === row.name;
      const button = element('button', `tool-chip${active ? ' active' : ''}`);
      button.type = 'button';
      button.title = `Filter trace to ${row.name}`;
      button.append(element('span', '', row.name), element('strong', '', formatInteger(row.value)));
      button.addEventListener('click', () => {
        pauseLiveFollow();
        resetAgentFocus();
        state.traceCategory = 'tool';
        state.traceTool = active ? '' : row.name;
        state.traceEventSequence = null;
        renderDashboard();
      });
      chips.appendChild(button);
    });
    if (!rows.length) chips.appendChild(element('span', 'muted', 'No tool calls'));
  }

  function renderTraceTurns(turns, grouped, query) {
    const container = document.getElementById('trace-turn-list');
    clear(container);
    const visible = turns.filter((turn) => {
      const events = grouped.get(turn.turn_id) || [];
      return events.some((event) => traceEventMatches(event, query));
    });
    if (visible.length && !visible.some((turn) => turn.turn_id === state.traceTurnId)) {
      resetAgentFocus();
      state.traceTurnId = visible[0].turn_id;
      state.traceEventSequence = null;
    }
    document.getElementById('trace-turn-count').textContent = `${formatInteger(visible.length)} of ${formatInteger(turns.length)}`;
    visible.forEach((turn) => {
      const events = grouped.get(turn.turn_id) || [];
      const matching = events.filter((event) => traceEventMatches(event, query));
      const active = turn.turn_id === state.traceTurnId;
      const button = element('button', `trace-row trace-turn-row${active ? ' active' : ''}`);
      button.type = 'button';
      button.dataset.turnId = turn.turn_id;
      button.setAttribute('aria-haspopup', 'menu');
      const heading = element('span', 'trace-row-heading');
      heading.append(
        element('strong', '', turn.trace_label),
        element('span', `trace-status ${turn.status}`, turn.status),
      );
      button.append(
        heading,
        element('span', 'trace-row-summary', traceTurnSummary(turn, events)),
        element('span', 'trace-row-meta', `${formatInteger(matching.length)} events · ${formatDate(turn.started_at)}`),
      );
      button.addEventListener('click', () => {
        pauseLiveFollow();
        const scrollTop = container.scrollTop;
        resetAgentFocus();
        state.traceTurnId = turn.turn_id;
        state.traceEventSequence = null;
        renderDashboard();
        document.getElementById('trace-turn-list').scrollTop = scrollTop;
      });
      container.appendChild(button);
    });
    if (!visible.length) container.appendChild(element('div', 'empty-state', 'No matching turns.'));
    return visible;
  }

  function renderTraceEvents(events) {
    const container = document.getElementById('trace-event-list');
    clear(container);
    document.getElementById('trace-event-count').textContent = `${formatInteger(events.length)} shown`;
    if (!events.some((event) => event.sequence === state.traceEventSequence)) {
      resetAgentFocus();
      state.traceEventSequence = events.length ? events[0].sequence : null;
    }
    events.forEach((event) => {
      const active = event.sequence === state.traceEventSequence;
      const button = element('button', `trace-row trace-event-row ${event.category}${active ? ' active' : ''}`);
      button.type = 'button';
      button.dataset.turnId = event.turn_id;
      button.dataset.eventSequence = String(event.sequence);
      button.setAttribute('aria-haspopup', 'menu');
      const heading = element('span', 'trace-row-heading');
      heading.append(
        element('strong', '', event.title),
        element('span', `trace-kind ${event.category}`, event.category),
      );
      const outputLine = event.output_line_number ? ` → ${event.output_line_number}` : '';
      button.append(
        heading,
        element('span', 'trace-row-summary', traceEventSummary(event)),
        element('span', 'trace-row-meta', `line ${event.line_number}${outputLine} · ${formatDate(event.timestamp)}`),
      );
      button.addEventListener('click', () => {
        pauseLiveFollow();
        const scrollTop = container.scrollTop;
        resetAgentFocus();
        state.traceEventSequence = event.sequence;
        renderTraceEvents(events);
        renderTraceDetail(events.find((candidate) => candidate.sequence === state.traceEventSequence));
        renderAgentContext();
        renderHighlightedContext();
        document.getElementById('trace-event-list').scrollTop = scrollTop;
      });
      container.appendChild(button);
    });
    if (!events.length) container.appendChild(element('div', 'empty-state', 'No matching events.'));
  }

  function definitionItem(label, value) {
    const item = element('div', 'definition-item');
    item.append(element('div', 'definition-label', label), element('div', 'definition-value', value));
    return item;
  }

  function appendTraceText(container, label, value, fieldName, event) {
    if (!value) return;
    const section = element('section', 'trace-text-section');
    const heading = element('div', 'trace-text-heading');
    heading.appendChild(element('h3', '', label));
    if ((event.truncated_fields || []).includes(fieldName)) {
      heading.appendChild(element('span', 'trace-truncated', 'Truncated'));
    }
    section.append(heading, element('pre', 'trace-pre', value));
    container.appendChild(section);
  }

  function renderTraceDetail(event) {
    const container = document.getElementById('trace-detail');
    clear(container);
    if (!event) {
      container.appendChild(element('div', 'empty-state', 'No event selected.'));
      return;
    }
    const heading = element('div', 'trace-detail-heading');
    heading.append(element('h3', '', event.title), element('span', `trace-kind ${event.category}`, event.category));
    container.appendChild(heading);
    const definitions = element('div', 'definition-grid trace-definition-grid');
    const metadata = [
      ['Line', event.output_line_number ? `${event.line_number} / ${event.output_line_number}` : event.line_number],
      ['Timestamp', formatDate(event.timestamp)],
      ['Type', event.kind],
      ['Turn', shortId(event.turn_id)],
      ['Role', event.role],
      ['Phase', event.phase],
      ['Tool', event.tool_name],
      ['Status', event.status],
      ['Duration', event.duration_secs === null ? '' : formatDuration(event.duration_secs)],
      ['Call ID', event.call_id],
    ].filter(([, value]) => value !== '' && value !== null && value !== undefined);
    metadata.forEach(([label, value]) => definitions.appendChild(definitionItem(label, value)));
    container.appendChild(definitions);
    appendTraceText(container, 'Readable text', event.text, 'text', event);
    appendTraceText(container, 'Tool input', event.input_text, 'input_text', event);
    appendTraceText(container, 'Tool output', event.output_text, 'output_text', event);
    const details = document.createElement('details');
    details.className = 'trace-json-details';
    details.append(element('summary', '', 'Normalized event'), element('pre', 'trace-pre', JSON.stringify(event, null, 2)));
    container.appendChild(details);
  }

  function renderTrace() {
    const trace = selectedTrace();
    if (!trace) {
      document.getElementById('trace-summary').textContent = 'No trace data.';
      renderTraceEvents([]);
      renderTraceDetail(null);
      renderAgentContext();
      return;
    }
    document.getElementById('trace-summary').textContent =
      `${formatInteger(trace.events_total)} events · ${formatInteger(trace.source_rows)} source rows · ` +
      `${formatInteger(trace.collapsed_rows)} collapsed or omitted · ${formatInteger(trace.truncated_events)} truncated`;
    const grouped = eventsByTurn(trace);
    const turns = traceTurnRows(trace);
    ensureTraceSelection(turns, grouped);
    const query = normalizedSearch(document.getElementById('trace-search').value);
    const visibleTurns = renderTraceTurns(turns, grouped, query);
    const selectedEvents = grouped.get(state.traceTurnId) || [];
    renderTraceCategoryFilters(selectedEvents, query);
    if (!visibleTurns.length) {
      renderTraceEvents([]);
      renderTraceDetail(null);
      renderAgentContext();
      return;
    }
    const events = selectedEvents.filter((event) => traceEventMatches(event, query));
    renderTraceEvents(events);
    renderTraceDetail(events.find((event) => event.sequence === state.traceEventSequence));
    renderAgentContext();
  }

  function terminalAgentStatus(status) {
    return qaConversationState.isTerminal(status);
  }

  function terminalActivityStatus(status) {
    return terminalAgentStatus(status) || ['completed', 'cancelled', 'failed'].includes(status);
  }

  function agentStatusLabel(status) {
    const labels = {
      queued: 'Queued',
      investigating: 'Investigating',
      extracting: 'Extracting',
      summarizing: 'Summarizing',
      auditing: 'Auditing',
      repairing: 'Repairing',
      retrying: 'Retrying',
      checking: 'Checking',
      verifying: 'Verifying',
      applying: 'Applying',
      investigated: 'Investigated',
      extracted: 'Extracted',
      summarized: 'Summarized',
      audited: 'Audited',
      passed: 'Passed',
      paused: 'Paused',
      blocked: 'Blocked',
      failed: 'Failed',
      cancelled: 'Cancelled',
      interrupted: 'Interrupted',
      discarded: 'Discarded',
    };
    return labels[status] || 'Unknown';
  }

  function renderAgentCapabilities(agent) {
    const container = document.getElementById('agent-capability-list');
    clear(container);
    const workflows = agent.workflows || {};
    const capabilities = [
      ['Trace Q&A', Boolean(runtime.qa?.configured)],
      ['Investigate', Boolean(workflows.investigate)],
      ['Session brief', Boolean(workflows.checkpoints)],
      ['Extract memories', Boolean(workflows.memories)],
      ['Audit behavior', Boolean(runtime.audit_rules?.available)],
      ['Parser audit', Boolean(workflows.audit)],
      ['Repair', Boolean(workflows.repair)],
      ['Customize', Boolean(workflows.customize)],
    ];
    capabilities.forEach(([label, available]) => {
      const chip = element(
        'span',
        `agent-capability-chip ${available ? 'is-available' : 'is-unavailable'}`,
        label,
      );
      chip.title = `${label}: ${available ? 'available' : 'unavailable in this runtime'}`;
      chip.setAttribute('aria-label', chip.title);
      container.appendChild(chip);
    });
  }

  function renderAgentActions() {
    const agent = runtime.agent || {};
    const apiConfigured = Boolean(runtime.qa?.api_configured);
    const controllerReady = Boolean(runtime.qa?.configured);
    const running = Boolean(state.agentRun && !terminalAgentStatus(state.agentRun.status));
    const harnessSelect = document.getElementById('agent-harness-select');
    const harnesses = Array.isArray(agent.harnesses) ? agent.harnesses : [];
    const harnessSignature = JSON.stringify(
      [agent.selection_notice, harnesses.map((item) => [item.id, item.label, item.available, item.detail, item.model])],
    );
    if (harnessSelect.dataset.signature !== harnessSignature) {
      clear(harnessSelect);
      if (agent.selection_notice) {
        const option = element('option', '', 'Choose an agent type');
        option.value = '';
        option.disabled = true;
        harnessSelect.appendChild(option);
      }
      harnesses.forEach((item) => {
        const suffix = item.available ? '' : ' (unavailable)';
        const option = element('option', '', `${item.label || item.id}${suffix}`);
        option.value = item.id;
        option.disabled = !item.available && item.id !== agent.harness_id;
        option.title = item.detail || '';
        harnessSelect.appendChild(option);
      });
      harnessSelect.dataset.signature = harnessSignature;
    }
    harnessSelect.value = agent.selection_notice ? '' : agent.harness_id || 'opencode';
    harnessSelect.disabled =
      !runtime.interactive || !agent.available || running || state.agentBusy || state.qaBusy || state.agentHarnessBusy;
    harnessSelect.title = agent.harness_detail || 'Choose the agent type used for every Studio workflow';
    const status = document.getElementById('agent-actions-status');
    if (!runtime.interactive) status.textContent = 'Available in local server mode';
    else if (!agent.available) status.textContent = agent.reason || 'Local source workspace unavailable';
    else if (!controllerReady) status.textContent = runtime.qa?.agent_detail || 'Selected agent is unavailable';
    else if (running || state.agentRun?.recovery) status.textContent = state.agentRun.message || 'Agent workflow stopped';
    else if (!agent.harness_available) {
      status.textContent = `${agent.harness_label || 'Selected agent'} unavailable: ${agent.harness_detail || 'not ready'}`;
    } else if (agent.requires_api_settings && !apiConfigured) {
      status.textContent = `${agent.harness_label || 'Agent'} · configure a model API to run workflows`;
    } else {
      status.textContent = '';
    }
    if (agent.selection_notice && !running) status.textContent = agent.selection_notice;
    if (state.agentError) {
      status.textContent = state.agentError;
      status.classList.add('error-text');
    } else {
      status.classList.remove('error-text');
    }
    status.hidden = !status.textContent;
    renderAgentCapabilities(agent);
    renderAgentRun();
    renderAgentActivity(latestQAActivity() || state.agentRun || { activity: [] });
    renderConversationRunDetails();
  }

  function renderConversationRunDetails() {
    const details = document.getElementById('qa-run-details');
    if (!details) return;
    const run = state.agentRun;
    details.hidden = !run;
    if (!run) return;
    const needsAction = !document.getElementById('agent-run-recovery').hidden;
    document.getElementById('qa-run-summary').textContent = `${agentStatusLabel(run.status)}${needsAction ? ' · Action needed' : ''}`;
    const attentionKey = needsAction ? `${run.run_id}:${run.status}` : '';
    if (attentionKey && details.dataset.attentionKey !== attentionKey) details.open = true;
    details.dataset.attentionKey = attentionKey;
  }

  function activityPhaseLabel(phase) {
    const label = String(phase || 'Agent');
    return label.trim().toLowerCase() === 'harness' ? 'Agent' : label;
  }

  function renderAgentActivity(run) {
    const panel = document.getElementById('agent-activity');
    const list = document.getElementById('agent-activity-list');
    const status = document.getElementById('agent-activity-status');
    const activity = Array.isArray(run.activity) ? run.activity.filter((item) => item && item.message) : [];
    panel.hidden = activity.length === 0;
    if (!activity.length) {
      clear(list);
      list.dataset.runId = '';
      list.dataset.lastSequence = '';
      status.textContent = '';
      return;
    }

    const activityId = String(run.activity_id || run.request_id || run.run_id || '');
    const runChanged = list.dataset.runId !== activityId;
    const nearBottom = runChanged || list.scrollHeight - list.scrollTop - list.clientHeight < 28;
    if (runChanged) clear(list);
    const rows = new Map(Array.from(list.children).map((row) => [row.dataset.sequence, row]));
    const retained = new Set(activity.map((item, index) => String(item.sequence ?? index + 1)));
    rows.forEach((row, sequence) => {
      if (!retained.has(sequence)) row.remove();
    });

    activity.forEach((item, index) => {
      const sequence = String(item.sequence ?? index + 1);
      let row = rows.get(sequence);
      if (!row || !row.isConnected) {
        row = element('li', 'agent-activity-row');
        row.dataset.sequence = sequence;
        row.append(
          element('span', 'agent-activity-marker'),
          element('time', 'agent-activity-time'),
          element('span', 'agent-activity-phase'),
          element('span', 'agent-activity-message'),
        );
        list.appendChild(row);
      }
      const time = row.querySelector('.agent-activity-time');
      const formattedTime = formatActivityTime(item.at);
      time.textContent = formattedTime;
      if (item.at) time.dateTime = item.at;
      row.querySelector('.agent-activity-phase').textContent = activityPhaseLabel(item.phase);
      const message = row.querySelector('.agent-activity-message');
      message.textContent = item.message;
      const repeats = Number(item.repeat_count) || 1;
      if (repeats > 1) message.appendChild(element('span', 'agent-activity-repeat', ` x${formatInteger(repeats)}`));
      const current = index === activity.length - 1 && !terminalActivityStatus(run.status);
      row.classList.toggle('current', current);
      row.classList.toggle(
        'failed',
        index === activity.length - 1 && ['paused', 'blocked', 'failed', 'cancelled', 'interrupted'].includes(run.status),
      );
      if (current) row.setAttribute('aria-current', 'step');
      else row.removeAttribute('aria-current');
    });

    const latest = activity[activity.length - 1];
    const latestSequence = String(latest.sequence ?? activity.length);
    const hasNewActivity = list.dataset.lastSequence !== latestSequence;
    list.dataset.runId = activityId;
    list.dataset.lastSequence = latestSequence;
    const latestTime = formatActivityTime(latest.at);
    status.textContent = terminalActivityStatus(run.status)
      ? latestTime
        ? `Finished ${latestTime}`
        : 'Finished'
      : latestTime
        ? `Live at ${latestTime}`
        : 'Live';
    if (hasNewActivity && nearBottom) list.scrollTop = list.scrollHeight;
  }

  function appendAgentResultList(container, title, items) {
    if (!Array.isArray(items) || !items.length) return;
    const group = element('section', 'agent-result-group');
    group.appendChild(element('h4', '', title));
    const list = element('ul', 'agent-result-list');
    items.forEach((item) => list.appendChild(traceAnchoredElement('li', '', item)));
    group.appendChild(list);
    container.appendChild(group);
  }

  function renderInvestigationResult(container, result) {
    const heading = element('div', 'agent-result-heading');
    const outcome = element('span', 'run-state', result.outcome || 'unclear');
    outcome.dataset.status =
      result.outcome === 'failed' ? 'failed' : result.outcome === 'completed' ? 'passed' : 'audited';
    heading.append(
      element('h3', '', result.title || 'Session investigation'),
      outcome,
    );
    container.append(heading, traceAnchoredElement('p', '', result.summary || ''));
    const objective = element('p', 'agent-result-objective');
    objective.append(element('strong', '', 'Objective'), document.createTextNode(` ${result.objective || 'Unclear'}`));
    container.appendChild(objective);
    appendAgentResultList(container, 'Key actions', result.key_actions);
    appendAgentResultList(container, 'Findings', result.findings);
    appendAgentResultList(container, 'Issues', result.issues);
    appendAgentResultList(container, 'Lessons', result.lessons);
    const claims = Array.isArray(result.evidence) ? result.evidence : [];
    if (claims.length) {
      const group = element('section', 'agent-result-group');
      group.appendChild(element('h4', '', 'Evidence'));
      const list = element('ul', 'agent-evidence-list');
      claims.forEach((claim) => {
        const item = element('li');
        item.appendChild(traceAnchoredElement('span', '', claim.claim || ''));
        const anchors = Array.isArray(claim.evidence_anchors) ? claim.evidence_anchors : [];
        if (anchors.length) {
          const evidence = element('small', 'muted agent-result-anchors');
          appendTraceEvidenceAnchors(evidence, anchors);
          item.appendChild(evidence);
        }
        list.appendChild(item);
      });
      group.appendChild(list);
      container.appendChild(group);
    }
  }

  function renderMemoryResult(container, result) {
    container.append(
      element('h3', '', 'Memory candidates'),
      traceAnchoredElement('p', '', result.summary || ''),
    );
    const candidates = Array.isArray(result.candidates) ? result.candidates : [];
    if (!candidates.length) {
      container.appendChild(element('p', 'muted', 'No durable memory candidates were supported by the evidence.'));
      return;
    }
    const list = element('div', 'agent-memory-list');
    candidates.forEach((candidate) => {
      const item = element('article', 'agent-memory-item');
      const heading = element('div', 'agent-memory-heading');
      heading.append(
        element('strong', '', candidate.title || 'Memory candidate'),
        element('span', 'memory-category', String(candidate.category || 'context').replaceAll('_', ' ')),
        element('span', `finding-severity ${candidate.confidence || 'low'}`, candidate.confidence || 'low'),
      );
      item.append(
        heading,
        traceAnchoredElement('p', '', candidate.memory || ''),
        traceAnchoredElement('p', 'muted', candidate.why_reusable || ''),
      );
      const anchors = Array.isArray(candidate.evidence_anchors) ? candidate.evidence_anchors : [];
      if (anchors.length) {
        const evidence = element('small', 'muted agent-result-anchors');
        appendTraceEvidenceAnchors(evidence, anchors);
        item.appendChild(evidence);
      }
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  function checkpointSelectionKey(runId, index) {
    return `${runId || 'checkpoint-run'}:${index}`;
  }

  function checkpointSelection(run, checkpoint, index) {
    return {
      run_id: String(run.run_id || ''),
      index,
      title: String(checkpoint.title || `Checkpoint ${index}`),
      status: String(checkpoint.status || 'unclear'),
      summary: String(checkpoint.summary || ''),
      turn_ids: Array.isArray(checkpoint.turn_ids) ? checkpoint.turn_ids.map(String).slice(0, 12) : [],
      start_event_sequence: Number.isInteger(checkpoint.start_event_sequence)
        ? checkpoint.start_event_sequence
        : null,
      end_event_sequence: Number.isInteger(checkpoint.end_event_sequence) ? checkpoint.end_event_sequence : null,
      actions: Array.isArray(checkpoint.actions) ? checkpoint.actions.map(String).slice(0, 10) : [],
      achievements: Array.isArray(checkpoint.achievements) ? checkpoint.achievements.map(String).slice(0, 10) : [],
      blockers: Array.isArray(checkpoint.blockers) ? checkpoint.blockers.map(String).slice(0, 8) : [],
      artifacts: Array.isArray(checkpoint.artifacts) ? checkpoint.artifacts.map(String).slice(0, 10) : [],
      next_steps: Array.isArray(checkpoint.next_steps) ? checkpoint.next_steps.map(String).slice(0, 8) : [],
      evidence_anchors: Array.isArray(checkpoint.evidence_anchors)
        ? checkpoint.evidence_anchors.map((anchor) => String(anchor)).slice(0, 8)
        : [],
    };
  }

  function reconcileSelectedCheckpoint(brief) {
    const selected = state.traceCheckpoint;
    const checkpoints = Array.isArray(brief?.result?.checkpoints) ? brief.result.checkpoints : [];
    if (!selected || !brief?.available || !checkpoints.length) return false;
    const runId = String(brief.run_id || '');
    if (selected.run_id === runId) return false;
    const normalizedTitle = compactText(selected.title, 200).toLowerCase();
    const exactRange = (checkpoint) =>
      Number.isInteger(selected.start_event_sequence) &&
      Number.isInteger(selected.end_event_sequence) &&
      checkpoint.start_event_sequence === selected.start_event_sequence &&
      checkpoint.end_event_sequence === selected.end_event_sequence;
    let index = checkpoints.findIndex(
      (checkpoint) => exactRange(checkpoint) && compactText(checkpoint.title, 200).toLowerCase() === normalizedTitle,
    );
    if (index < 0) index = checkpoints.findIndex(exactRange);
    if (index < 0 && normalizedTitle) {
      index = checkpoints.findIndex(
        (checkpoint) => compactText(checkpoint.title, 200).toLowerCase() === normalizedTitle,
      );
    }
    state.traceCheckpoint = index < 0 ? null : checkpointSelection({ run_id: runId }, checkpoints[index], index + 1);
    return true;
  }

  function selectCheckpoint(run, checkpoint, index) {
    resetAgentFocus();
    renderHighlightedContext();
    state.traceCheckpoint = checkpointSelection(run, checkpoint, index);
    const selectedKey = checkpointSelectionKey(state.traceCheckpoint.run_id, index);
    document.querySelectorAll('.checkpoint-item').forEach((item) => {
      const active = item.dataset.checkpointKey === selectedKey;
      item.classList.toggle('active', active);
      const summary = item.querySelector('.checkpoint-summary');
      if (active) summary?.setAttribute('aria-current', 'true');
      else summary?.removeAttribute('aria-current');
    });
    renderAgentContext();
  }

  function selectionIsInside(node) {
    const selection = window.getSelection();
    return Boolean(selection && !selection.isCollapsed && node.contains(selection.anchorNode));
  }

  function appendCheckpointDetailList(container, title, items, emptyText, { evidence = false } = {}) {
    const group = element('section', 'checkpoint-detail-group');
    group.appendChild(element('h4', '', title));
    const values = Array.isArray(items) ? items.map(String).filter(Boolean) : [];
    if (!values.length) {
      group.appendChild(element('p', 'muted', emptyText));
    } else {
      const list = element('ul', 'checkpoint-detail-list');
      values.forEach((value) => {
        const item = element('li');
        if (evidence) appendTraceEvidenceAnchors(item, [value]);
        else appendTraceAnchoredText(item, value);
        list.appendChild(item);
      });
      group.appendChild(list);
    }
    container.appendChild(group);
  }

  function renderCheckpointResult(container, result, run, openCheckpoints = new Set()) {
    const heading = element('div', 'agent-result-heading');
    const outcome = element('span', 'run-state', result.outcome || 'unclear');
    outcome.dataset.status =
      result.outcome === 'completed'
        ? 'summarized'
        : result.outcome === 'failed'
          ? 'failed'
          : result.outcome === 'in_progress'
            ? 'summarizing'
            : 'audited';
    heading.append(element('h3', '', result.title || 'Session brief'), outcome);
    container.append(heading, traceAnchoredElement('p', '', result.summary || ''));
    const objective = element('p', 'agent-result-objective');
    objective.append(element('strong', '', 'Objective'), document.createTextNode(` ${result.objective || 'Unclear'}`));
    container.appendChild(objective);
    const checkpoints = Array.isArray(result.checkpoints) ? result.checkpoints : [];
    if (!checkpoints.length) {
      container.appendChild(element('p', 'muted', 'No distinct checkpoints were supported by this session.'));
    } else {
      const list = element('div', 'checkpoint-list');
      checkpoints.forEach((checkpoint, index) => {
        const item = element('details', 'checkpoint-item');
        const checkpointIndex = index + 1;
        const selectionKey = checkpointSelectionKey(run.run_id, checkpointIndex);
        const active =
          state.traceCheckpoint?.run_id === String(run.run_id || '') && state.traceCheckpoint?.index === checkpointIndex;
        item.dataset.checkpointKey = selectionKey;
        item.open = openCheckpoints.has(selectionKey);
        item.classList.toggle('active', active);
        const summary = element('summary', 'checkpoint-summary');
        summary.title = 'Expand and use as agent context';
        if (active) summary.setAttribute('aria-current', 'true');
        const content = element('span', 'checkpoint-summary-content');
        const checkpointHeading = element('div', 'checkpoint-heading');
        const status = element('span', 'run-state', checkpoint.status || 'unclear');
        status.dataset.status =
          checkpoint.status === 'completed'
            ? 'summarized'
            : checkpoint.status === 'in_progress'
              ? 'summarizing'
              : checkpoint.status || 'audited';
        checkpointHeading.append(element('strong', '', checkpoint.title || `Checkpoint ${index + 1}`), status);
        content.append(
          checkpointHeading,
          traceAnchoredElement('span', 'checkpoint-summary-text', checkpoint.summary || ''),
        );
        summary.append(element('span', 'checkpoint-index', formatInteger(checkpointIndex)), content);
        item.appendChild(summary);

        const details = element('div', 'checkpoint-details');
        appendCheckpointDetailList(details, 'Turns', checkpoint.turn_ids, 'No turn boundary was returned.');
        appendCheckpointDetailList(details, 'What the agent did', checkpoint.actions, 'No actions were returned.');
        appendCheckpointDetailList(details, 'Achieved', checkpoint.achievements, 'No achievement was returned.');
        appendCheckpointDetailList(details, 'Main blockers', checkpoint.blockers, 'No blocker was returned.');
        appendCheckpointDetailList(details, 'Artifacts', checkpoint.artifacts, 'No artifact was returned.');
        appendCheckpointDetailList(details, 'Next steps', checkpoint.next_steps, 'No next step was returned.');
        appendCheckpointDetailList(
          details,
          'Evidence',
          checkpoint.evidence_anchors,
          'No evidence anchor was returned.',
          { evidence: true },
        );
        item.appendChild(details);
        item.addEventListener('click', () => {
          if (!selectionIsInside(item)) selectCheckpoint(run, checkpoint, checkpointIndex);
        });
        item.addEventListener('contextmenu', () => selectCheckpoint(run, checkpoint, checkpointIndex));
        list.appendChild(item);
      });
      container.appendChild(list);
    }
    appendAgentResultList(container, 'Artifacts', result.artifacts);
    appendAgentResultList(container, 'Blockers', result.blockers);
    appendAgentResultList(container, 'Next steps', result.next_steps);
  }

  function fixedItemsForAttempt(attempt) {
    const verifier = attempt?.verifier && typeof attempt.verifier === 'object' ? attempt.verifier : {};
    const fixedItems = Array.isArray(verifier.fixed_items)
      ? verifier.fixed_items.map((item) => String(item).trim()).filter(Boolean)
      : [];
    if (fixedItems.length || verifier.status !== 'pass') return fixedItems;
    const issues = Array.isArray(attempt?.audit?.issues) ? attempt.audit.issues : [];
    return issues.map((issue) => String(issue?.title || '').trim()).filter(Boolean);
  }

  function repairFixedItems(run, summary) {
    const items = [];
    const seen = new Set();
    const add = (item, attempt) => {
      const text = String(item || '').trim();
      const attemptNumber = Number(attempt) || 0;
      const key = `${attemptNumber}\u0000${text}`;
      if (!text || seen.has(key)) return;
      seen.add(key);
      items.push({ attempt: attemptNumber, item: text });
    };
    const summarized = Array.isArray(summary.fixed_items) ? summary.fixed_items : [];
    summarized.forEach((entry) => {
      if (entry && typeof entry === 'object') add(entry.item, entry.attempt);
      else add(entry, 0);
    });
    (Array.isArray(run.attempts) ? run.attempts : []).forEach((attempt, index) => {
      fixedItemsForAttempt(attempt).forEach((item) => add(item, attempt.attempt || index + 1));
    });
    if (!items.length) {
      const attempts = Array.isArray(run.attempts) ? run.attempts : [];
      const passingAttempt = [...attempts].reverse().find((attempt) => attempt?.verifier?.status === 'pass');
      if (passingAttempt) {
        const fixing = Array.isArray(summary.fixing) ? summary.fixing : [];
        fixing.forEach((item) => add(item, passingAttempt.attempt));
      }
    }
    return items;
  }

  function renderRepairSummary(run) {
    const container = document.getElementById('agent-repair-summary');
    clear(container);
    const sourceChange = ['repair', 'customize'].includes(run.kind);
    const summary = sourceChange && run.repair_summary ? run.repair_summary : null;
    container.hidden = !summary;
    if (!summary) return;
    container.appendChild(element('h3', '', run.kind === 'customize' ? 'Customization summary' : 'Repair summary'));
    const grid = element('div', 'agent-repair-summary-grid');
    const fixing = Array.isArray(summary.fixing) ? summary.fixing : [];
    if (fixing.length) {
      const section = element('section');
      section.appendChild(element('h4', '', run.kind === 'customize' ? 'Requested change' : 'Fixing'));
      const list = element('ul', 'agent-result-list');
      fixing.forEach((item) => list.appendChild(element('li', '', item)));
      section.appendChild(list);
      grid.appendChild(section);
    }
    const fixedItems = repairFixedItems(run, summary);
    const fixedSection = element('section');
    fixedSection.appendChild(element('h4', '', run.kind === 'customize' ? 'Completed items' : 'Fixed items'));
    if (fixedItems.length) {
      const list = element('ul', 'agent-result-list');
      fixedItems.forEach((entry) => {
        const prefix = entry.attempt ? `Attempt ${formatInteger(entry.attempt)}: ` : '';
        list.appendChild(element('li', '', `${prefix}${entry.item}`));
      });
      fixedSection.appendChild(list);
    } else {
      fixedSection.appendChild(element('p', 'muted', 'No items have been independently verified as fixed yet.'));
    }
    grid.appendChild(fixedSection);
    const changedFiles = Array.isArray(summary.changed_files) ? summary.changed_files : [];
    if (changedFiles.length) {
      const section = element('section');
      section.appendChild(element('h4', '', `Candidate files (${formatInteger(changedFiles.length)})`));
      const list = element('ul', 'agent-attempt-file-list');
      changedFiles.forEach((file) => {
        const item = element('li');
        item.appendChild(element('code', '', file));
        list.appendChild(item);
      });
      section.appendChild(list);
      grid.appendChild(section);
    }
    container.appendChild(grid);
  }

  function notifyAgentStopped(run) {
    if (!run?.recovery) return;
    const key = `${run.run_id || ''}:${run.status || ''}:${run.updated_at || ''}`;
    if (state.agentNotificationKey === key) return;
    state.agentNotificationKey = key;
    if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
      new Notification('Agent Trace Studio', { body: run.message || 'Agent workflow needs your attention.' });
    }
  }

  function renderAgentRecovery(run) {
    const panel = document.getElementById('agent-run-recovery');
    const recovery = run.recovery && typeof run.recovery === 'object' ? run.recovery : null;
    panel.hidden = !recovery;
    if (!recovery) return;
    const actions = new Set(Array.isArray(recovery.actions) ? recovery.actions : []);
    document.getElementById('agent-recovery-title').textContent =
      run.status === 'paused' ? 'Model request paused' : `${agentStatusLabel(run.status)} run`;
    document.getElementById('agent-recovery-reason').textContent = recovery.reason || run.message || 'Agent stopped.';
    const activateButton = document.getElementById('agent-run-activate');
    const continueButton = document.getElementById('agent-run-continue');
    const restartButton = document.getElementById('agent-run-restart');
    const discardButton = document.getElementById('agent-run-discard');
    activateButton.hidden = !actions.has('activate');
    continueButton.hidden = !actions.has('continue');
    restartButton.hidden = !actions.has('restart');
    discardButton.hidden = !actions.has('discard');
    activateButton.disabled = state.agentBusy;
    continueButton.disabled = state.agentBusy;
    restartButton.disabled = state.agentBusy;
    discardButton.disabled = state.agentBusy;
    document.getElementById('agent-recovery-instruction-field').hidden = !actions.has('continue');
    notifyAgentStopped(run);
  }

  function renderDeploymentLifecycle(deployment) {
    const health = deployment.health && typeof deployment.health === 'object' ? deployment.health : {};
    const rollback = deployment.rollback && typeof deployment.rollback === 'object' ? deployment.rollback : {};
    const deploymentStatus = String(deployment.status || 'pending');
    const runtimeState = {
      promoted: ['Promoted', 'success'],
      retrying: ['Activating', 'pending'],
      failed: ['Rejected', 'failure'],
      rolled_back: ['Rolled back', 'failure'],
    }[deploymentStatus] || [deploymentStatus.replaceAll('_', ' '), 'neutral'];
    const healthState = health.passed === true
      ? ['Passed', 'success']
      : health.passed === false
        ? ['Failed', 'failure']
        : ['Not reported', 'neutral'];
    const rollbackState = {
      available: ['Ready', 'success'],
      restored: ['Restored', 'success'],
      blocked: ['Blocked', 'failure'],
      unavailable: ['Unavailable', 'neutral'],
    }[String(rollback.status || '')] || ['Not reported', 'neutral'];
    const stages = [
      ['Candidate', 'Verified', 'success', 'Deterministic checks and independent verification passed.'],
      ['Health check', healthState[0], healthState[1], health.message || 'No health-check result was reported.'],
      ['Runtime', runtimeState[0], runtimeState[1], deployment.message || 'No runtime result was reported.'],
      ['Rollback', rollbackState[0], rollbackState[1], rollback.message || 'No rollback result was reported.'],
    ];
    const lifecycle = element('div', 'deployment-lifecycle');
    lifecycle.setAttribute('aria-label', 'Verified activation lifecycle');
    stages.forEach(([label, value, stageState, detail]) => {
      const stage = element('div', 'deployment-stage');
      stage.dataset.state = stageState;
      stage.title = detail;
      stage.append(element('span', 'deployment-stage-label', label), element('strong', '', value));
      lifecycle.appendChild(stage);
    });
    return lifecycle;
  }

  function renderAgentRun() {
    const run = state.agentRun;
    const panel = document.getElementById('agent-run-panel');
    panel.hidden = !run;
    if (!run) return;
    const isSourceChange = ['repair', 'customize'].includes(run.kind);
    const usesSelectedHarness = ['investigate', 'memories', 'checkpoints', 'audit', 'repair', 'customize'].includes(run.kind);
    document.getElementById('agent-run-label').textContent = run.historical
      ? 'Previous run'
      : terminalAgentStatus(run.status)
        ? 'Latest run'
        : 'Current run';
    const runTitles = {
      investigate: 'Investigate session',
      memories: 'Extract memories',
      checkpoints: 'Update session brief',
      audit: 'Parser audit',
      repair: 'Fix and activate local source',
      customize: 'Customize and activate dashboard',
    };
    const harnessLabel = usesSelectedHarness ? run.harness_label || runtime.agent?.harness_label : '';
    document.getElementById('agent-run-title').textContent = harnessLabel
      ? `${runTitles[run.kind] || 'Agent workflow'} · ${harnessLabel}`
      : runTitles[run.kind] || 'Agent workflow';
    const stateNode = document.getElementById('agent-run-state');
    stateNode.textContent = agentStatusLabel(run.status);
    stateNode.dataset.status = run.status;
    document.getElementById('agent-run-message').textContent = run.message || '';
    const attempt = Number(run.attempt) || 0;
    const maximum = Number(run.max_attempts) || Number(runtime.agent?.max_attempts) || 1;
    const recordedAttempts = Array.isArray(run.attempts) ? run.attempts.length : 0;
    const runMeta = document.getElementById('agent-run-session-meta');
    const runIdentity = run.run_id ? shortId(run.run_id) : '';
    if (isSourceChange && runIdentity) {
      const attemptsLabel = `${formatInteger(Math.max(recordedAttempts, attempt))} attempt${
        Math.max(recordedAttempts, attempt) === 1 ? '' : 's'
      } recorded`;
      const approvalLabel = run.source_authorization ? ' · approval consumed' : '';
      runMeta.textContent = `Fixer session ${runIdentity} · ${attemptsLabel}${approvalLabel}`;
    } else {
      runMeta.textContent = runIdentity ? `Workflow ${runIdentity}` : '';
    }
    const stageProgress = {
      queued: 0.05,
      investigating: 0.55,
      extracting: 0.55,
      summarizing: 0.55,
      auditing: 0.55,
      repairing: Math.max(attempt / maximum, 0.15),
      retrying: Math.max(attempt / maximum, 0.15),
      checking: Math.max((attempt - 0.4) / maximum, 0.2),
      verifying: Math.max((attempt - 0.15) / maximum, 0.3),
      applying: 0.95,
    };
    const progress = terminalAgentStatus(run.status) ? 1 : Math.min(stageProgress[run.status] || 0.05, 1);
    const runScopes = {
      investigate: 'Selected session context',
      memories: 'Reusable learning',
      checkpoints: 'All processed session events',
      audit: 'Read-only source review',
    };
    document.getElementById('agent-run-attempt').textContent = isSourceChange
      ? `Attempt ${formatInteger(attempt)} of ${formatInteger(maximum)}`
      : runScopes[run.kind] || 'Agent workflow';
    document.getElementById('agent-run-progress-fill').style.width = `${Math.max(progress * 100, 4)}%`;
    const cancel = document.getElementById('agent-run-cancel');
    cancel.hidden = terminalAgentStatus(run.status);
    cancel.disabled = state.agentBusy;
    renderRepairSummary(run);
    renderAgentRecovery(run);
    const auditNode = document.getElementById('agent-run-audit');
    clear(auditNode);
    if (run.audit) {
      auditNode.appendChild(element('h3', '', run.kind === 'customize' ? 'Customization request' : 'Audit'));
      auditNode.appendChild(traceAnchoredElement('p', '', run.audit.summary));
      const issues = run.audit.issues || [];
      if (issues.length) {
        const list = element('ul', 'agent-finding-list');
        issues.forEach((issue) => {
          const item = element('li');
          item.append(
            element('span', `finding-severity ${issue.severity || 'low'}`, issue.severity || 'low'),
            element('strong', '', issue.title || 'Parser finding'),
            traceAnchoredElement('p', '', issue.evidence || ''),
          );
          list.appendChild(item);
        });
        auditNode.appendChild(list);
      }
    }
    const attemptsNode = document.getElementById('agent-run-attempts');
    const openAttempts = new Set(
      Array.from(attemptsNode.querySelectorAll('.agent-attempt[open]')).map((node) => node.dataset.attempt),
    );
    clear(attemptsNode);
    const attempts = run.attempts || [];
    if (attempts.length) {
      attemptsNode.appendChild(element('h3', '', 'Verification attempts'));
      const list = element('div', 'agent-attempt-list');
      attempts.forEach((item, index) => {
        const attemptKey = String(item.attempt || index + 1);
        const details = element('details', 'agent-attempt');
        details.dataset.attempt = attemptKey;
        details.open = openAttempts.has(attemptKey);
        const summary = element('summary', 'agent-attempt-summary');
        const row = element('span', 'agent-attempt-row');
        const checksPassed = Boolean(item.deterministic_gates_passed);
        const inheritedFailures = Array.isArray(item.inherited_failed_checks) ? item.inherited_failed_checks : [];
        const verifier = item.verifier || {};
        const verdict = verifier.status || 'fail';
        const fixedItems = fixedItemsForAttempt(item);
        const candidateFiles = (item.changed_files || []).length;
        const outcomeSummary = fixedItems.length
          ? `${formatInteger(fixedItems.length)} fixed · ${formatInteger(candidateFiles)} candidate files`
          : `${formatInteger(candidateFiles)} candidate files`;
        row.append(
          element('strong', '', `Attempt ${formatInteger(item.attempt)}`),
          element(
            'span',
            checksPassed ? 'check-pass' : 'check-fail',
            checksPassed
              ? inheritedFailures.length
                ? `No new mechanical failures · ${formatInteger(inheritedFailures.length)} inherited warning${
                    inheritedFailures.length === 1 ? '' : 's'
                  }`
                : 'Mechanical checks passed'
              : 'Mechanical checks failed',
          ),
          element(
            'span',
            verdict === 'pass' ? 'check-pass' : 'check-fail',
            `Independent review ${verdict}`,
          ),
          element('span', 'muted', outcomeSummary),
        );
        summary.appendChild(row);
        details.appendChild(summary);

        const body = element('div', 'agent-attempt-details');
        if (item.fixer_summary) {
          const section = element('section', 'agent-attempt-section');
          section.append(
            element('h4', '', 'What this attempt changed'),
            element('p', '', compactText(item.fixer_summary, 1200)),
          );
          body.appendChild(section);
        }

        if (fixedItems.length) {
          const section = element('section', 'agent-attempt-section');
          section.appendChild(element('h4', '', 'Fixed items'));
          const list = element('ul', 'agent-result-list');
          fixedItems.forEach((fixedItem) => list.appendChild(element('li', '', fixedItem)));
          section.appendChild(list);
          body.appendChild(section);
        }

        const files = Array.isArray(item.changed_files) ? item.changed_files : [];
        if (files.length) {
          const previousFiles = new Set(index ? attempts[index - 1].changed_files || [] : []);
          const section = element('section', 'agent-attempt-section');
          section.appendChild(element('h4', '', `Candidate files (${formatInteger(files.length)}, cumulative)`));
          const fileList = element('ul', 'agent-attempt-file-list');
          files.forEach((file) => {
            const fileItem = element('li');
            fileItem.appendChild(element('code', '', file));
            if (index && !previousFiles.has(file)) {
              fileItem.appendChild(element('span', 'attempt-new-file', 'New this attempt'));
            }
            fileList.appendChild(fileItem);
          });
          section.appendChild(fileList);
          body.appendChild(section);
        }

        const checks = Array.isArray(item.checks)
          ? item.checks.filter(
              (check) =>
                !(
                  check.status === 'skipped' &&
                  String(check.output || '').startsWith('Unchanged baseline failure;')
                ),
            )
          : [];
        if (checks.length) {
          const section = element('section', 'agent-attempt-section');
          section.appendChild(element('h4', '', 'Deterministic checks'));
          const checkList = element('div', 'agent-check-list');
          checks.forEach((check) => {
            const checkRow = element('div', 'agent-check-row');
            const statusLabel = check.status === 'passed' ? 'Passed' : check.status === 'skipped' ? 'Skipped' : 'Failed';
            let detail = compactText(check.output, 220);
            if (check.name === 'Journal replay' && item.parser_after && !item.parser_after.error) {
              detail = `${formatInteger(item.parser_after.events_total)} events · ${formatInteger(
                (item.parser_after.issues || []).length,
              )} parser issues`;
            } else if (check.name === 'Dashboard syntax' && !detail) {
              detail = 'JavaScript parsed successfully';
            }
            checkRow.append(
              element('strong', '', check.name || 'Check'),
              element('span', check.status === 'failed' ? 'check-fail' : 'check-pass', statusLabel),
              element('span', 'muted', detail || 'Completed without output'),
            );
            checkList.appendChild(checkRow);
          });
          section.appendChild(checkList);
          body.appendChild(section);
        }

        if (inheritedFailures.length) {
          const section = element('section', 'agent-attempt-section');
          section.append(
            element('h4', '', 'Inherited baseline failures'),
            element(
              'p',
              'muted',
              'These eligible static-check failures matched the normalized baseline fingerprint and did not implicate a changed file or check configuration. They were not assigned to this attempt. Test, replay, missing, and candidate-related failures remain blocking.',
            ),
          );
          const inheritedList = element('div', 'agent-check-list');
          inheritedFailures.forEach((check) => {
            const checkRow = element('div', 'agent-check-row');
            checkRow.append(
              element('strong', '', check.name || 'Check'),
              element('span', 'check-pass', 'Inherited'),
              element('span', 'muted', compactText(check.output, 220) || 'Unchanged baseline failure'),
            );
            inheritedList.appendChild(checkRow);
          });
          section.appendChild(inheritedList);
          body.appendChild(section);
        }

        const verifierSection = element('section', 'agent-attempt-section');
        verifierSection.append(
          element('h4', '', verifier.status === 'fail' ? 'Why verification failed' : 'Verification result'),
          element('p', '', verifier.summary || 'No verifier summary was returned.'),
        );
        appendAgentResultList(verifierSection, 'Unresolved issues', verifier.unresolved_issues);
        appendAgentResultList(verifierSection, 'Regressions', verifier.regressions);
        appendAgentResultList(verifierSection, 'Required changes', verifier.required_changes);
        body.appendChild(verifierSection);
        details.appendChild(body);
        list.appendChild(details);
      });
      attemptsNode.appendChild(list);
    }
    const resultNode = document.getElementById('agent-run-result');
    clear(resultNode);
    if (run.result) {
      if (run.result.workflow === 'investigation') {
        renderInvestigationResult(resultNode, run.result);
        return;
      }
      if (run.result.workflow === 'memories') {
        renderMemoryResult(resultNode, run.result);
        return;
      }
      if (run.result.workflow === 'checkpoints') {
        resultNode.append(
          element('h3', '', 'Session brief updated'),
          element(
            'p',
            '',
            `Revision ${formatInteger(run.result.revision || 1)} covers ${formatInteger(run.result.event_count || 0)} events through #${formatInteger(run.result.through_event_sequence || 0)}.`,
          ),
        );
        return;
      }
      resultNode.appendChild(element('h3', '', 'Result'));
      const files = run.result.changed_files || [];
      resultNode.appendChild(
        element(
          'p',
          '',
          files.length ? `${formatInteger(files.length)} verified source files applied.` : 'No source change was required.',
        ),
      );
      if (run.result.restart_required) {
        const refresh = run.dashboard_refresh || {};
        const message = refresh.message || 'Restart Agent Trace Studio to load the updated source.';
        resultNode.appendChild(element('p', 'restart-notice', message));
      } else if (run.result.dashboard_refreshed) {
        resultNode.appendChild(element('p', 'refresh-notice', 'Updated dashboard loaded automatically.'));
      }
      const deployment = run.deployment || run.result.deployment || {};
      if (deployment.status) {
        const deploymentNotice = element('section', `deployment-notice ${deployment.status}`);
        deploymentNotice.append(
          element('h4', '', 'Runtime deployment'),
          element('strong', '', String(deployment.status).replaceAll('_', ' ')),
          renderDeploymentLifecycle(deployment),
          element('p', '', deployment.message || 'No deployment message was provided.'),
        );
        if (deployment.generation) {
          deploymentNotice.appendChild(element('code', '', `Generation ${deployment.generation}`));
        }
        resultNode.appendChild(deploymentNotice);
      } else if (run.result.runtime_restarted) {
        resultNode.appendChild(element('p', 'refresh-notice', 'Verified runtime activated successfully.'));
      }
      if (files.length) {
        const list = element('ul', 'changed-file-list');
        files.forEach((file) => list.appendChild(element('li', '', file)));
        resultNode.appendChild(list);
      }
    }
  }

  function setupStudioSurfaces() {
    // A supervised server can regenerate an older in-memory HTML template after
    // an asset-only update. Move its existing controls before binding events.
    let agentSettings = document.getElementById('agent-selection-settings');
    if (!agentSettings) {
      agentSettings = element('div', 'agent-selection-settings');
      agentSettings.id = 'agent-selection-settings';
      const status = document.getElementById('agent-actions-status');
      status.before(agentSettings);
      agentSettings.appendChild(status);
    }
    const settings = document.getElementById('qa-configure-button');
    settings.className = 'command-button compact secondary';
    settings.textContent = 'API settings';
    settings.setAttribute('aria-label', 'API settings');
    settings.setAttribute('aria-haspopup', 'dialog');
    settings.setAttribute('aria-controls', 'qa-config-dialog');
    settings.title = 'API settings';
    agentSettings.prepend(settings);
    const legacyPanel = document.getElementById('studio-panel');
    if (legacyPanel) {
      const conversation = document.getElementById('qa-conversation-popover');
      const messages = document.getElementById('qa-messages');
      const context = element('details', 'qa-context-details');
      context.id = 'qa-context-details';
      const contextHeading = element('summary', '', 'Context ');
      const contextLabel = element('span');
      contextLabel.id = 'qa-context-summary';
      contextHeading.appendChild(contextLabel);
      context.append(contextHeading, legacyPanel.querySelector('.agent-context'), legacyPanel.querySelector('.agent-capability-row'));
      const body = element('div', 'qa-conversation-body');
      body.id = 'qa-conversation-body';
      messages.before(context, body);
      body.appendChild(messages);
      const runDetails = element('details', 'qa-run-details');
      runDetails.id = 'qa-run-details';
      runDetails.hidden = true;
      const runHeading = element('summary', '', 'Run details ');
      const runLabel = element('span');
      runLabel.id = 'qa-run-summary';
      runHeading.appendChild(runLabel);
      runDetails.append(runHeading, document.getElementById('agent-activity'), document.getElementById('agent-run-panel'));
      body.appendChild(runDetails);
      legacyPanel.remove();
    }
    document.getElementById('qa-conversation-toggle').setAttribute(
      'aria-controls', 'qa-context-details qa-conversation-body qa-highlight-context qa-form',
    );
    let sourceDialog = document.getElementById('source-dialog');
    if (!sourceDialog) {
      sourceDialog = element('dialog', 'source-dialog');
      sourceDialog.id = 'source-dialog';
      sourceDialog.setAttribute('aria-labelledby', 'source-dialog-title');
      const loader = document.querySelector('.source-loader');
      const heading = loader.querySelector('.source-panel-heading');
      clear(heading);
      const title = element('h2', '', 'Add a source');
      title.id = 'source-dialog-title';
      const close = element('button', 'icon-button', '×');
      close.id = 'source-dialog-close';
      close.type = 'button';
      close.setAttribute('aria-label', 'Close Add a source');
      close.title = 'Close';
      heading.append(title, close);
      const feedback = element('p', 'source-dialog-status');
      feedback.id = 'source-dialog-status';
      feedback.hidden = true;
      feedback.setAttribute('role', 'status');
      document.getElementById('source-loader-status').after(feedback);
      sourceDialog.appendChild(loader);
      document.body.appendChild(sourceDialog);
      const trigger = element('button', 'command-button secondary', 'Add a source');
      trigger.id = 'add-source-button';
      trigger.type = 'button';
      trigger.setAttribute('aria-haspopup', 'dialog');
      trigger.setAttribute('aria-controls', 'source-dialog');
      document.querySelector('#source-workspace .workspace-heading').appendChild(trigger);
    }
    if (sourceDialog.dataset.ready === 'true') return;
    sourceDialog.dataset.ready = 'true';
    document.getElementById('add-source-button').addEventListener('click', openSourceDialog);
    document.getElementById('source-dialog-close').addEventListener('click', closeSourceDialog);
    sourceDialog.addEventListener('close', () => {
      document.getElementById('add-source-button').focus({ preventScroll: true });
    });
    sourceDialog.addEventListener('click', (event) => {
      if (event.target !== sourceDialog) return;
      const bounds = sourceDialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) {
        closeSourceDialog();
      }
    });
  }

  function openSourceDialog() {
    const dialog = document.getElementById('source-dialog');
    if (dialog.open) return;
    qaPromptMenuController?.hide();
    dialog.showModal();
    const input = document.getElementById('session-id');
    (input.disabled ? document.getElementById('source-dialog-close') : input).focus({ preventScroll: true });
  }

  function closeSourceDialog() {
    const dialog = document.getElementById('source-dialog');
    if (dialog?.open) dialog.close();
  }

  function scrollQAConversationToBottom() {
    const body = document.getElementById('qa-conversation-body') || document.getElementById('qa-messages');
    body.scrollTop = body.scrollHeight;
  }

  let qaPromptMenuController = null;

  function qaStatusText(configured) {
    const model = runtime.qa?.agent_model || runtime.qa?.model || 'Default model';
    const provider = runtime.qa?.provider || 'Q&A provider';
    const harness = runtime.qa?.agent_harness || 'Studio agent';
    const usesAPI = Boolean(runtime.qa?.agent_uses_api_settings);
    if (!runtime.interactive) return 'Read-only file mode';
    if (!configured) return `${harness} · ${runtime.qa?.agent_detail || 'not ready'}`;
    if (!usesAPI) return `${harness} · ${model}`;
    if (runtime.qa?.credential_locked) return `${harness} · ${provider} · ${model} · saved key locked`;
    const keyState = runtime.qa?.remembered
      ? `saved in ${runtime.qa?.credential_store || 'system credential store'}`
      : 'active for this server';
    return `${harness} · ${provider} · ${model} · ${keyState}`;
  }

  function renderQAConversationChrome() {
    const popover = document.getElementById('qa-conversation-popover');
    const button = document.getElementById('qa-floating-button');
    const collapseToggle = document.getElementById('qa-conversation-toggle');
    const unread = document.getElementById('qa-unread-count');
    popover.hidden = !state.qaConversationOpen;
    popover.classList.toggle('collapsed', state.qaConversationCollapsed);
    renderCollapseToggle(collapseToggle, state.qaConversationCollapsed, 'Studio conversation');
    button.setAttribute('aria-expanded', String(state.qaConversationOpen));
    button.setAttribute(
      'aria-label',
      state.qaConversationOpen ? 'Close Studio conversation' : 'Open Studio conversation',
    );
    button.title = state.qaConversationOpen
      ? 'Close Studio conversation; drag to move'
      : 'Open Studio conversation; drag to move';
    unread.hidden = state.qaUnreadCount < 1 || (state.qaConversationOpen && !state.qaConversationCollapsed);
    unread.textContent = state.qaUnreadCount > 99 ? '99+' : String(state.qaUnreadCount);
    qaPromptMenuController?.sync();
  }

  function qaPromptIcon(name) {
    const paths = {
      latest: 'M12 8v4l3 2 M21 12a9 9 0 1 1-9-9 9 9 0 0 1 9 9',
      session: 'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
      brief: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z M14 2v6h6 M8 12h8 M8 16h6',
      memory: 'M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1Z',
      sparkle: 'm12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z',
      arrow: 'M5 12h14 M13 6l6 6-6 6',
    };
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('class', 'qa-prompt-icon');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('aria-hidden', 'true');
    icon.setAttribute('focusable', 'false');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', paths[name] || paths.sparkle);
    icon.appendChild(path);
    return icon;
  }

  function qaFloatingSuggestedPrompts() {
    return [
      { label: 'Latest update', prompt: 'What is the latest item in this session?', icon: 'latest' },
      ...qaSuggestedPrompts().slice(0, 3),
    ];
  }

  function prefillQAPrompt(suggestion) {
    if (!runtime.interactive || !runtime.qa?.configured || !selectedTrace() || state.qaBusy) return;
    openQAConversation({ focus: false });
    const input = document.getElementById('qa-question');
    input.value = suggestion.prefill || suggestion.prompt;
    input.focus({ preventScroll: true });
    input.setSelectionRange(input.value.length, input.value.length);
  }

  function setupQAPromptMenu(button) {
    const menu = element('section', 'qa-prompt-menu');
    menu.id = 'qa-prompt-menu';
    menu.hidden = true;
    menu.tabIndex = -1;
    menu.setAttribute('aria-labelledby', 'qa-prompt-menu-title');
    menu.setAttribute('aria-describedby', 'qa-prompt-menu-hint');
    const title = element('div', 'qa-prompt-menu-title');
    title.id = 'qa-prompt-menu-title';
    title.append(qaPromptIcon('sparkle'), element('span', '', 'Ask Studio'));
    const list = element('div', 'qa-prompt-list');
    const hint = element('p', 'qa-prompt-menu-hint');
    hint.id = 'qa-prompt-menu-hint';
    menu.append(title, list, hint);
    button.after(menu);
    const help = element(
      'span', 'qa-prompt-help',
      'Hover or press Arrow Up or Arrow Down for suggested prompts. Activate to open Studio conversation. Drag to move.',
    );
    help.id = 'qa-prompt-help';
    menu.after(help);
    button.setAttribute('aria-describedby', help.id);
    button.setAttribute('aria-keyshortcuts', 'ArrowUp ArrowDown');
    let closeTimer = null;
    let pointerInside = false;
    let suppressFocus = false;

    const cancelClose = () => {
      if (closeTimer !== null) window.clearTimeout(closeTimer);
      closeTimer = null;
    };
    const hide = ({ restoreFocus = false } = {}) => {
      cancelClose();
      menu.hidden = true;
      button.classList.remove('has-prompt-menu');
      if (restoreFocus) {
        suppressFocus = true;
        button.focus({ preventScroll: true });
        suppressFocus = false;
      }
    };
    const updateAvailability = () => {
      const reason = !runtime.interactive ? 'Use the local server to chat.'
        : !selectedTrace() ? 'Load a session to use prompts.'
          : !runtime.qa?.configured ? 'Configure the agent to use prompts.'
            : state.qaBusy ? 'Available when the current reply finishes.' : '';
      list.querySelectorAll('button').forEach((item) => { item.disabled = Boolean(reason); });
      hint.textContent = reason || 'Choose a prompt, then review and send.';
    };
    const position = () => {
      if (menu.hidden) return;
      const bounds = button.getBoundingClientRect();
      const gap = 8;
      const above = Math.max(0, bounds.top - qaFloatingViewportMargin - gap);
      const below = Math.max(0, window.innerHeight - bounds.bottom - qaFloatingViewportMargin - gap);
      const direction = above >= below ? 'above' : 'below';
      menu.style.maxHeight = `${Math.max(above, below)}px`;
      const menuBounds = menu.getBoundingClientRect();
      menu.style.left = `${bounded(
        bounds.right - menuBounds.width, qaFloatingViewportMargin,
        Math.max(qaFloatingViewportMargin, window.innerWidth - menuBounds.width - qaFloatingViewportMargin),
      )}px`;
      menu.style.top = `${direction === 'above' ? bounds.top - gap - menuBounds.height : bounds.bottom + gap}px`;
      menu.dataset.anchor = direction;
    };
    const show = () => {
      cancelClose();
      if (state.qaConversationOpen || button.classList.contains('is-dragging')) return;
      if (menu.hidden) {
        clear(list);
        qaFloatingSuggestedPrompts().forEach((suggestion) => {
          const item = element('button', 'qa-prompt-button');
          item.type = 'button';
          item.title = suggestion.prefill || suggestion.prompt;
          item.append(qaPromptIcon(suggestion.icon), element('span', '', suggestion.label), qaPromptIcon('arrow'));
          item.addEventListener('click', () => prefillQAPrompt(suggestion));
          list.appendChild(item);
        });
      }
      updateAvailability();
      menu.hidden = false;
      button.classList.add('has-prompt-menu');
      position();
    };
    const scheduleClose = () => {
      cancelClose();
      closeTimer = window.setTimeout(() => {
        closeTimer = null;
        const focused = menu.contains(document.activeElement)
          || (document.activeElement === button && button.matches(':focus-visible'));
        if (!pointerInside && !focused) hide();
      }, 220);
    };
    [button, menu].forEach((target) => {
      target.addEventListener('pointerenter', (event) => {
        if (event.pointerType === 'touch') return;
        pointerInside = true;
        if (target === button) show();
        else cancelClose();
      });
      target.addEventListener('pointerleave', () => {
        pointerInside = false;
        scheduleClose();
      });
      target.addEventListener('focusout', scheduleClose);
    });
    button.addEventListener('focus', () => {
      if (!suppressFocus && button.matches(':focus-visible')) show();
    });
    menu.addEventListener('focusin', cancelClose);
    button.addEventListener('keydown', (event) => {
      if (!['ArrowUp', 'ArrowDown'].includes(event.key) || state.qaConversationOpen) return;
      event.preventDefault();
      show();
      const items = [...list.querySelectorAll('button:not(:disabled)')];
      const target = event.key === 'ArrowUp' ? items.at(-1) : items[0];
      (target || menu).focus({ preventScroll: true });
    });
    menu.addEventListener('keydown', (event) => {
      if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const items = [...list.querySelectorAll('button:not(:disabled)')];
      if (!items.length) return;
      const current = items.indexOf(document.activeElement);
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[index].focus({ preventScroll: true });
    });
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || menu.hidden) return;
      event.preventDefault();
      hide({ restoreFocus: menu.contains(document.activeElement) });
    });
    document.addEventListener('pointerdown', (event) => {
      if (!button.contains(event.target) && !menu.contains(event.target)) hide();
    });
    return {
      hide, position,
      sync: () => {
        if (state.qaConversationOpen) hide();
        else if (!menu.hidden) { updateAvailability(); position(); }
      },
    };
  }

  function constrainedQAFloatingButtonPosition(left, top) {
    const button = document.getElementById('qa-floating-button');
    const bounds = button.getBoundingClientRect();
    const maximumLeft = Math.max(
      qaFloatingViewportMargin,
      window.innerWidth - bounds.width - qaFloatingViewportMargin,
    );
    const maximumTop = Math.max(
      qaFloatingViewportMargin,
      window.innerHeight - bounds.height - qaFloatingViewportMargin,
    );
    return {
      left: bounded(left, qaFloatingViewportMargin, maximumLeft),
      top: bounded(top, qaFloatingViewportMargin, maximumTop),
    };
  }

  function qaConversationAttachmentDirection(buttonBounds, panelBounds) {
    const vertical = buttonBounds.top + buttonBounds.height / 2 >= window.innerHeight / 2
      ? ['above', 'below']
      : ['below', 'above'];
    const horizontal = buttonBounds.left + buttonBounds.width / 2 >= window.innerWidth / 2
      ? ['left', 'right']
      : ['right', 'left'];
    const order = [vertical[0], horizontal[0], horizontal[1], vertical[1]];
    const available = {
      above: buttonBounds.top - qaFloatingViewportMargin - qaConversationGap,
      below: window.innerHeight - buttonBounds.bottom - qaFloatingViewportMargin - qaConversationGap,
      left: buttonBounds.left - qaFloatingViewportMargin - qaConversationGap,
      right: window.innerWidth - buttonBounds.right - qaFloatingViewportMargin - qaConversationGap,
    };
    const required = {
      above: panelBounds.height,
      below: panelBounds.height,
      left: panelBounds.width,
      right: panelBounds.width,
    };
    const fitting = order.find((direction) => available[direction] >= required[direction]);
    if (fitting) return fitting;
    return order.reduce((best, direction) => (
      available[direction] / required[direction] > available[best] / required[best]
        ? direction
        : best
    ));
  }

  function positionQAConversationNearButton() {
    const panel = document.getElementById('qa-conversation-popover');
    if (panel.hidden) return;
    const buttonBounds = document.getElementById('qa-floating-button').getBoundingClientRect();
    const panelBounds = panel.getBoundingClientRect();
    const direction = qaConversationAttachmentDirection(buttonBounds, panelBounds);
    const maximumLeft = Math.max(
      qaFloatingViewportMargin,
      window.innerWidth - panelBounds.width - qaFloatingViewportMargin,
    );
    const maximumTop = Math.max(
      qaFloatingViewportMargin,
      window.innerHeight - panelBounds.height - qaFloatingViewportMargin,
    );
    let left = buttonBounds.left + (buttonBounds.width - panelBounds.width) / 2;
    let top = buttonBounds.top + (buttonBounds.height - panelBounds.height) / 2;
    if (direction === 'above') top = buttonBounds.top - qaConversationGap - panelBounds.height;
    if (direction === 'below') top = buttonBounds.bottom + qaConversationGap;
    if (direction === 'left') left = buttonBounds.left - qaConversationGap - panelBounds.width;
    if (direction === 'right') left = buttonBounds.right + qaConversationGap;
    left = bounded(left, qaFloatingViewportMargin, maximumLeft);
    top = bounded(top, qaFloatingViewportMargin, maximumTop);
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
    panel.dataset.anchor = direction;
    panel.style.setProperty(
      '--qa-anchor-x',
      `${bounded(buttonBounds.left + buttonBounds.width / 2 - left, 16, panelBounds.width - 16)}px`,
    );
    panel.style.setProperty(
      '--qa-anchor-y',
      `${bounded(buttonBounds.top + buttonBounds.height / 2 - top, 16, panelBounds.height - 16)}px`,
    );
  }

  function applyQAFloatingButtonPosition({ persist = false } = {}) {
    const button = document.getElementById('qa-floating-button');
    if (state.qaFloatingButtonPosition) {
      state.qaFloatingButtonPosition = constrainedQAFloatingButtonPosition(
        state.qaFloatingButtonPosition.left,
        state.qaFloatingButtonPosition.top,
      );
      button.style.left = `${state.qaFloatingButtonPosition.left}px`;
      button.style.top = `${state.qaFloatingButtonPosition.top}px`;
      button.style.right = 'auto';
      button.style.bottom = 'auto';
      if (persist) saveQAFloatingButtonPosition();
    }
    if (state.qaConversationOpen) positionQAConversationNearButton();
    qaPromptMenuController?.position();
  }

  function setupQAFloatingDock() {
    const button = document.getElementById('qa-floating-button');
    const panel = document.getElementById('qa-conversation-popover');
    let drag = null;
    let suppressClick = false;
    qaPromptMenuController = setupQAPromptMenu(button);

    button.addEventListener('click', (event) => {
      if (suppressClick) {
        suppressClick = false;
        event.preventDefault();
        return;
      }
      toggleQAConversation();
    });

    button.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      qaPromptMenuController.hide();
      const bounds = button.getBoundingClientRect();
      drag = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        left: bounds.left,
        top: bounds.top,
        moved: false,
      };
      button.setPointerCapture(event.pointerId);
    });

    button.addEventListener('pointermove', (event) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const deltaX = event.clientX - drag.startX;
      const deltaY = event.clientY - drag.startY;
      if (!drag.moved && Math.hypot(deltaX, deltaY) < 4) return;
      event.preventDefault();
      if (!drag.moved) {
        drag.moved = true;
        button.classList.add('is-dragging');
      }
      state.qaFloatingButtonPosition = constrainedQAFloatingButtonPosition(
        drag.left + deltaX,
        drag.top + deltaY,
      );
      applyQAFloatingButtonPosition();
    });

    const finishDrag = (event) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const moved = drag.moved;
      drag = null;
      button.classList.remove('is-dragging');
      if (moved) {
        saveQAFloatingButtonPosition();
        if (event.type === 'pointerup') {
          suppressClick = true;
          window.setTimeout(() => { suppressClick = false; }, 100);
        }
      }
    };
    button.addEventListener('pointerup', finishDrag);
    button.addEventListener('pointercancel', finishDrag);

    let resizeFrame = null;
    window.addEventListener('resize', () => {
      if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = null;
        applyQAFloatingButtonPosition({ persist: true });
      });
    });
    if ('ResizeObserver' in window) {
      const observer = new ResizeObserver(() => {
        if (state.qaConversationOpen) positionQAConversationNearButton();
      });
      observer.observe(panel);
    }
    applyQAFloatingButtonPosition();
  }

  function openQAConversation({ focus = true, expand = true } = {}) {
    state.qaConversationOpen = true;
    if (expand && state.qaConversationCollapsed) {
      state.qaConversationCollapsed = false;
      persistQAConversationCollapsed();
    }
    state.qaUnreadCount = 0;
    renderQA();
    applyQAFloatingButtonPosition();
    window.requestAnimationFrame(() => {
      positionQAConversationNearButton();
      scrollQAConversationToBottom();
      if (focus && !state.qaConversationCollapsed) {
        document.getElementById('qa-question').focus({ preventScroll: true });
      }
    });
  }

  function closeQAConversation({ restoreFocus = true } = {}) {
    state.qaConversationOpen = false;
    renderQAConversationChrome();
    if (restoreFocus) qaPromptMenuController.hide({ restoreFocus: true });
  }

  function toggleQAConversation() {
    if (state.qaConversationOpen) closeQAConversation();
    else openQAConversation({ expand: false });
  }

  function markQAUnread() {
    if (!state.qaConversationOpen || state.qaConversationCollapsed) state.qaUnreadCount += 1;
    renderQAConversationChrome();
  }

  function renderSourceAuthorization(message) {
    const authorization = message.authorization || {};
    const content = element('div', 'source-authorization');
    content.appendChild(element('p', 'source-authorization-message', message.text));
    const details = element('dl', 'source-authorization-details');
    const rows = [
      ['Action', String(authorization.action || 'source change').replaceAll('_', ' ')],
      ['Instruction', authorization.message || ''],
      ['Workspace', compactPath(authorization.source_workspace || '')],
      [
        'After verification',
        authorization.activation_effect || 'Activate the verified local dashboard and keep the current runtime on failure.',
      ],
      ['Request', shortId(authorization.request_hash || '')],
      ['Selection', shortId(authorization.selection_hash || '')],
      ['Expires', authorization.expires_at ? formatDate(authorization.expires_at) : 'soon'],
    ];
    rows.forEach(([label, value]) => {
      details.append(element('dt', '', label), element('dd', '', value));
    });
    const actions = element('div', 'source-authorization-actions');
    const approve = element(
      'button',
      'primary-button',
      authorization.action === 'activate_run' ? 'Approve activation' : 'Approve change and activate',
    );
    approve.type = 'button';
    approve.disabled = state.qaBusy || message.resolved;
    approve.addEventListener('click', () => approveSourceAction(message));
    const cancel = element('button', 'secondary-button', 'Cancel');
    cancel.type = 'button';
    cancel.disabled = state.qaBusy || message.resolved;
    cancel.addEventListener('click', () => cancelSourceAction(message));
    actions.append(approve, cancel);
    content.append(details, actions);
    return content;
  }

  function qaSuggestedPrompts() {
    const suggestions = [];
    if (state.assuranceContract) {
      suggestions.push({
        label: 'Review selected audit finding',
        prompt: 'Review the selected audit finding, its expected behavior, current result, and supporting evidence.',
      });
    } else if (state.traceCheckpoint) {
      suggestions.push({
        label: 'Review selected checkpoint',
        prompt: 'Review the selected checkpoint, including achievements, blockers, and supporting evidence.',
      });
    } else if (selectedTraceEvent(selectedTrace())) {
      suggestions.push({
        label: 'Explain selected event',
        prompt: 'Explain the currently selected event and why it matters in this session.',
      });
    }
    suggestions.push(
      {
        label: 'Investigate this session',
        prompt: 'Investigate this session and summarize its objective, outcome, key actions, and issues.',
        icon: 'session',
      },
      {
        label: 'Update session brief',
        prompt: 'Update the session-wide checkpoint brief from the current trace.',
        icon: 'brief',
      },
      {
        label: 'Extract reusable memories',
        prompt: 'Extract reusable memories from this session with supporting evidence.',
        icon: 'memory',
      },
    );
    if (runtime.audit_rules?.available) {
      suggestions.push({
        label: 'Create an audit rule...',
        prefill: 'Create an audit rule: ',
      });
    }
    if (runtime.agent?.workflows?.audit) {
      suggestions.push({
        label: 'Audit the parser',
        prompt: 'Audit the parser for issues handling the currently loaded session.',
      });
    }
    if (runtime.agent?.workflows?.customize) {
      suggestions.push({
        label: 'Customize the dashboard...',
        prefill: 'Customize the dashboard: ',
      });
    }
    return suggestions;
  }

  function useQASuggestion(suggestion) {
    if (state.qaBusy || !runtime.qa?.configured || !selectedTrace()) return;
    const input = document.getElementById('qa-question');
    if (suggestion.prefill) {
      input.value = suggestion.prefill;
      input.focus({ preventScroll: true });
      input.setSelectionRange(input.value.length, input.value.length);
      return;
    }
    sendAgentMessage(suggestion.prompt);
  }

  function renderQASuggestions(messages, configured) {
    const suggestions = element('section', 'qa-suggestions');
    suggestions.id = 'qa-suggestions';
    suggestions.setAttribute('aria-label', 'Suggested prompts');
    suggestions.appendChild(element('h3', '', 'Suggested prompts'));
    const list = element('div', 'qa-suggestion-list');
    qaSuggestedPrompts().forEach((suggestion) => {
      const button = element('button', 'qa-suggestion-button', suggestion.label);
      button.type = 'button';
      button.disabled = !configured || !selectedTrace() || state.qaBusy;
      button.addEventListener('click', () => useQASuggestion(suggestion));
      list.appendChild(button);
    });
    suggestions.appendChild(list);
    messages.appendChild(suggestions);
  }

  function renderQA() {
    const body = document.getElementById('qa-conversation-body') || document.getElementById('qa-messages');
    const followOutput = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    const configured = Boolean(runtime.interactive && runtime.qa?.configured);
    const statusText = qaStatusText(configured);
    document.getElementById('qa-conversation-status').textContent = statusText;
    document.getElementById('qa-configure-button').disabled = !runtime.interactive || state.qaConfigBusy;
    const submit = document.getElementById('qa-submit');
    submit.disabled = !configured || state.qaBusy;
    submit.textContent = state.qaBusy ? 'Agent working…' : 'Send';
    const stop = document.getElementById('qa-stop');
    const workflowRunning = Boolean(state.agentRun && !terminalAgentStatus(state.agentRun.status));
    stop.hidden = !state.qaRequestId && !workflowRunning;
    stop.disabled = state.qaStopBusy || state.agentBusy;
    document.getElementById('qa-new-conversation').disabled = state.qaBusy || state.qaStopBusy;
    renderHighlightedContext();
    const messages = document.getElementById('qa-messages');
    clear(messages);
    state.qaMessages.forEach((message) => {
      const item = element('article', `qa-message ${message.role}`);
      const content = element('div', 'qa-message-text');
      if (message.role === 'assistant') content.appendChild(renderMarkdown(message.text));
      else if (message.role === 'authorization') content.appendChild(renderSourceAuthorization(message));
      else if (message.role === 'audit_rule_proposal') renderAuditRuleProposal(content, message.proposal, message);
      else if (message.role === 'activity') renderQAActivityTimeline(content, message);
      else content.textContent = message.text;
      if (message.role === 'activity') {
        item.appendChild(content);
      } else {
        item.append(
          element(
            'div',
            'qa-message-role',
            message.role === 'user'
              ? 'You'
              : message.role === 'error'
                ? 'Error'
                : message.role === 'authorization'
                  ? 'Approval'
                  : message.role === 'audit_rule_proposal'
                    ? 'Rule proposal'
                    : 'Agent',
          ),
          content,
        );
      }
      messages.appendChild(item);
    });
    if (!state.qaMessages.length) {
      renderQASuggestions(messages, configured);
    }
    if (followOutput) scrollQAConversationToBottom();
    renderQAConversationChrome();
  }

  function latestQAActivity() {
    const activity = [...state.qaMessages].reverse().filter((message) => message.role === 'activity');
    return activity.find((message) => !terminalActivityStatus(message.status)) || activity[0] || null;
  }

  function renderQAActivityTimeline(content, record) {
    const rawActivity = Array.isArray(record.activity) ? record.activity : [];
    const hasContextCalls = rawActivity.some((entry) => entry.details?.kind === 'context_action');
    const toolCallCount = rawActivity.filter((entry) => entry.details?.kind === 'context_action').length;
    const activity = hasContextCalls
      ? rawActivity.filter((entry) => entry.phase !== 'Context'
        || !/^(The agent requested |Context actions completed;)/.test(entry.message || ''))
      : rawActivity;
    const terminal = terminalActivityStatus(record.status);
    const details = element('details', 'qa-activity-details');
    details.dataset.status = record.status || 'running';
    details.dataset.terminal = String(terminal);
    details.open = typeof record.expanded === 'boolean' ? record.expanded : !terminal;
    const duration = activityDurationLabel(record);
    const turnCount = activity.filter(
      (entry) => entry.phase === 'Turn' && /\bstarted\b/i.test(entry.message || ''),
    ).length;
    const statusTitle = terminal
      ? record.status === 'completed'
        ? `Completed${duration ? ` in ${duration}` : ''}`
        : `${activityStatusLabel(record.status)}${duration ? ` after ${duration}` : ''}`
      : 'Working';
    const eventCount = `${formatInteger(activity.length)} event${activity.length === 1 ? '' : 's'}`;
    const countText = turnCount
      ? `${formatInteger(turnCount)} turn${turnCount === 1 ? '' : 's'} · ${eventCount}`
      : eventCount;
    const summary = element('summary', 'qa-activity-summary');
    summary.append(
      element('span', 'qa-activity-summary-indicator'),
      element('strong', '', terminal && record.status === 'completed' && duration ? 'Worked for ' + duration : statusTitle),
      element('span', 'qa-activity-summary-count', hasContextCalls
        ? formatInteger(toolCallCount) + (toolCallCount === 1 ? ' tool call' : ' tool calls')
        : countText),
    );
    details.appendChild(summary);
    const body = element('div', 'qa-activity-body');
    const timeline = element('ol', 'qa-activity-list');
    activity.forEach((entry, index) => {
      if (entry.details?.kind === 'context_action') {
        timeline.appendChild(renderQAContextCall(entry, record));
        return;
      }
      const row = element('li', 'qa-activity-row');
      const phase = activityPhaseLabel(entry.phase);
      row.dataset.phase = phase.toLowerCase();
      if (index === activity.length - 1 && !terminalActivityStatus(record.status)) row.classList.add('current');
      const header = element('div', 'qa-live-activity-line');
      header.appendChild(element('span', 'qa-live-activity-indicator'));
      header.appendChild(element('span', 'qa-live-activity-phase', phase));
      const formattedTime = formatActivityTime(entry.at);
      if (formattedTime) {
        const time = element('time', 'qa-live-activity-time', formattedTime);
        time.dateTime = entry.at;
        header.appendChild(time);
      }
      const message = element('div', 'qa-live-activity-message', entry.message || '');
      if (entry.repeat_count > 1) {
        message.appendChild(
          element('span', 'qa-live-activity-repeat', ` x${formatInteger(entry.repeat_count)}`),
        );
      }
      row.append(header, message);
      timeline.appendChild(row);
    });
    body.appendChild(timeline);
    details.appendChild(body);
    details.addEventListener('toggle', () => {
      record.expanded = details.open;
      persistQAConversation();
    });
    content.appendChild(details);
  }

  function renderQAContextCall(entry, record) {
    const call = entry.details;
    const row = element('li', 'qa-activity-row qa-context-call');
    row.dataset.status = call.status;
    if (call.status === 'running') row.classList.add('current');
    const details = element('details', 'qa-tool-details');
    details.open = (record.expanded_steps || []).includes(call.id);
    const summary = element('summary', 'qa-tool-summary');
    summary.setAttribute('aria-label', 'Show arguments and result for ' + call.tool);
    const verb = call.status === 'running' ? 'Calling' : call.status === 'cancelled' ? 'Stopped' : 'Called';
    summary.append(
      element('span', 'qa-live-activity-indicator'),
      element('strong', 'qa-tool-verb', verb),
      element('code', 'qa-tool-invocation', call.tool + '(' + JSON.stringify(call.arguments) + ')'),
    );
    if (call.status === 'failed') summary.appendChild(element('span', 'qa-tool-status', 'Failed'));
    if (Number.isFinite(call.duration_ms)) {
      const elapsed = call.duration_ms < 1000
        ? Math.round(call.duration_ms) + ' ms'
        : (call.duration_ms / 1000).toFixed(1) + ' s';
      summary.appendChild(element('span', 'qa-tool-duration', elapsed));
    }
    details.appendChild(summary);
    const body = element('div', 'qa-tool-body');
    body.append(
      element('div', 'qa-tool-section-label', 'Arguments'),
      element('pre', 'qa-tool-content', JSON.stringify(call.arguments, null, 2)),
    );
    if (call.arguments_truncated) {
      body.appendChild(element('p', 'qa-tool-note', 'Long argument values are truncated in this view.'));
    }
    const output = (call.output || '')
      .replace(/^CONTEXT ACTION:[^\n]*\n/, '')
      .replace(/^DASHBOARD RESOURCE:[^\n]*\nclassification:[^\n]*\n/, '');
    body.append(
      element('div', 'qa-tool-section-label', 'Result'),
      element('pre', 'qa-tool-content', output || (call.status === 'running' ? 'Waiting for result…' : 'No output.')),
    );
    const notes = [];
    if (call.status !== 'running') notes.push(formatInteger(call.output_chars) + ' characters returned');
    if (call.preview_truncated) notes.push('Preview truncated to 4,000 characters');
    if (call.context_truncated) notes.push('Context was truncated before delivery to the agent');
    if (call.redacted) notes.push('Sensitive values and local paths omitted');
    if (notes.length) body.appendChild(element('p', 'qa-tool-note', notes.join(' · ')));
    details.appendChild(body);
    row.appendChild(details);
    if (output) {
      const lines = output.split('\n');
      const preview = element('div', 'qa-tool-preview');
      preview.appendChild(element('pre', '', lines.slice(0, 4).map((line) => line.length > 160
        ? line.slice(0, 160) + '…' : line).join('\n')));
      if (lines.length > 4 || call.preview_truncated || lines.some((line) => line.length > 160)) {
        const more = element('button', 'qa-tool-more', 'Show details');
        more.type = 'button';
        more.addEventListener('click', () => {
          details.open = true;
          summary.focus();
        });
        preview.appendChild(more);
      }
      row.appendChild(preview);
    }
    details.addEventListener('toggle', () => {
      const expanded = new Set(record.expanded_steps || []);
      if (details.open) expanded.add(call.id);
      else expanded.delete(call.id);
      record.expanded_steps = Array.from(expanded);
      persistQAConversation();
    });
    return row;
  }

  function activityDurationLabel(record) {
    const normalize = (value) => String(value || '').replace(
      /(\.\d{3})\d+(?=(?:Z|[+-]\d{2}:\d{2})$)/,
      '$1',
    );
    const started = Date.parse(normalize(record.started_at));
    const completed = Date.parse(normalize(record.completed_at || record.updated_at));
    if (!Number.isFinite(started) || !Number.isFinite(completed) || completed < started) return '';
    const seconds = (completed - started) / 1000;
    if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
    const minutes = Math.floor(seconds / 60);
    const remainder = Math.round(seconds % 60);
    return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  }

  function activityStatusLabel(status) {
    const labels = {
      running: 'Working',
      cancelling: 'Stopping',
      completed: 'Completed',
    };
    return labels[status] || agentStatusLabel(status);
  }

  function renderQAActivity() {
    const existing = document.getElementById('qa-live-activity');
    existing?.remove();
  }

  function renderHighlightedContext() {
    const panel = document.getElementById('qa-highlight-context');
    const askTarget = state.askTarget;
    panel.hidden = !state.highlightedText && !askTarget;
    const label = document.getElementById('qa-highlight-label');
    const text = document.getElementById('qa-highlight-text');
    if (state.highlightedText) {
      label.textContent = state.highlightTruncated
        ? 'Highlighted text · first 2,000 characters'
        : 'Highlighted text';
      text.textContent = state.highlightedText;
      text.title = state.highlightedText;
      return;
    }
    label.textContent =
      askTarget?.kind === 'turn'
        ? 'Turn context'
        : askTarget?.kind === 'message'
          ? 'Message context'
          : 'Event context';
    text.textContent = askTarget
      ? `${askTarget.label}: ${askTarget.summary || askTarget.text || 'Selected trace item'}`
      : '';
    text.title = askTarget ? askTarget.text || askTarget.summary || askTarget.label : '';
  }

  function clearAskContext() {
    state.highlightedText = '';
    state.highlightOrigin = '';
    state.highlightTruncated = false;
    state.askTarget = null;
    document.getElementById('qa-question').placeholder = '';
    renderHighlightedContext();
  }

  function resetAgentFocus({ checkpoint = false, contract = true } = {}) {
    state.highlightedText = '';
    state.highlightOrigin = '';
    state.highlightTruncated = false;
    state.askTarget = null;
    const input = document.getElementById('qa-question');
    if (input) input.placeholder = '';
    if (checkpoint) state.traceCheckpoint = null;
    if (contract) state.assuranceContract = null;
  }

  function setQAConfigFeedback(message, error = false) {
    const feedback = document.getElementById('qa-config-feedback');
    feedback.textContent = message;
    feedback.classList.toggle('error-text', error);
  }

  function providerDefinition(providerId) {
    return (runtime.qa?.providers || []).find((provider) => provider.id === providerId) || null;
  }

  function populateModelOptions(providerId, currentModel) {
    const definition = providerDefinition(providerId);
    const preset = document.getElementById('qa-api-model-preset');
    const custom = document.getElementById('qa-api-model');
    clear(preset);
    (definition?.models || []).forEach((model) => {
      const option = element('option', '', model.label);
      option.value = model.id;
      preset.appendChild(option);
    });
    const customOption = element('option', '', 'Custom model ID');
    customOption.value = '__custom__';
    preset.appendChild(customOption);
    const knownModel = (definition?.models || []).some((model) => model.id === currentModel);
    preset.value = knownModel ? currentModel : '__custom__';
    custom.hidden = knownModel;
    custom.required = !knownModel;
    custom.value = knownModel ? '' : currentModel || '';
  }

  function configuredModel() {
    const preset = document.getElementById('qa-api-model-preset');
    return preset.value === '__custom__' ? document.getElementById('qa-api-model').value.trim() : preset.value;
  }

  function refreshAPIKeyRequirement() {
    const providerId = document.getElementById('qa-api-provider').value;
    const configured = Boolean(runtime.qa?.configured);
    const sameProvider = providerId === runtime.qa?.provider_id;
    const locked = Boolean(runtime.qa?.credential_locked);
    const encryptedCredential = runtime.qa?.credential_mode === 'encrypted_vault';
    const usesVaultForSave = runtime.qa?.save_credential_mode === 'encrypted_vault';
    const canKeepCurrentKey = configured && sameProvider;
    const canUnlock = locked && sameProvider && encryptedCredential;
    const definition = providerDefinition(providerId);
    const key = document.getElementById('qa-api-key');
    const remember = document.getElementById('qa-remember-key');
    const credentialStore = runtime.qa?.credential_store || 'system credential store';
    const persistenceAvailable = Boolean(runtime.qa?.credential_store_available);
    const unlocking = canUnlock && !key.value;
    key.required = !canKeepCurrentKey && !canUnlock;
    key.placeholder = canUnlock
      ? 'Leave blank to unlock the saved key'
      : canKeepCurrentKey
        ? 'Leave blank to keep the current key'
        : 'Required';
    if (canUnlock) {
      document.getElementById('qa-api-key-hint').textContent =
        'Leave blank to unlock the saved key, or enter a new API key to replace it.';
    } else if (canKeepCurrentKey) {
      document.getElementById('qa-api-key-hint').textContent =
        `A key is ${runtime.qa?.remembered ? `saved in ${credentialStore}` : 'active'}. Enter a new value only to replace it.`;
    } else {
      document.getElementById('qa-api-key-hint').textContent =
        `Enter an API key for ${definition?.label || 'this provider'}.`;
    }
    if (!persistenceAvailable) remember.checked = false;
    remember.disabled = state.qaConfigBusy || !persistenceAvailable;
    if (encryptedCredential || usesVaultForSave) {
      document.getElementById('qa-remember-key-hint').textContent = locked
        ? 'The encrypted local vault is locked.'
        : runtime.qa?.vault_password_from_environment
          ? 'Encrypted locally and automatically unlocked from the configured environment password.'
          : 'Encrypted locally; the vault password is required again after a server restart.';
    } else {
      document.getElementById('qa-remember-key-hint').textContent = persistenceAvailable
        ? `Stored securely in ${credentialStore}; provider settings are restored after restart.`
        : 'The key will last for this server process only.';
    }
    const needsVaultPassword = Boolean(
      remember.checked &&
      (unlocking ||
        (usesVaultForSave &&
          !runtime.qa?.vault_password_from_environment &&
          (runtime.qa?.vault_password_required || locked))),
    );
    const vaultFields = document.getElementById('qa-vault-fields');
    const vaultPassword = document.getElementById('qa-vault-password');
    const confirmField = document.getElementById('qa-vault-confirm-field');
    const confirmPassword = document.getElementById('qa-vault-password-confirm');
    const needsConfirmation = needsVaultPassword && !unlocking;
    vaultFields.hidden = !needsVaultPassword;
    vaultPassword.required = needsVaultPassword;
    vaultPassword.disabled = state.qaConfigBusy || !needsVaultPassword;
    confirmField.hidden = !needsConfirmation;
    confirmPassword.required = needsConfirmation;
    confirmPassword.disabled = state.qaConfigBusy || !needsConfirmation;
    document.getElementById('qa-vault-password-hint').textContent = unlocking
      ? 'Enter the password used when this API key was saved.'
      : 'Use at least 12 characters. This password is never stored.';
    document.getElementById('qa-config-save').textContent = unlocking ? 'Unlock API' : 'Save API';
    document.getElementById('qa-clear-key').disabled =
      state.qaConfigBusy || (!canKeepCurrentKey && !runtime.qa?.remembered);
  }

  function setQAConfigBusy(busy) {
    state.qaConfigBusy = busy;
    document.getElementById('qa-config-save').disabled = busy;
    document.getElementById('qa-config-cancel').disabled = busy;
    document.querySelectorAll('#qa-config-form input, #qa-config-form select').forEach((control) => {
      control.disabled = busy;
    });
    refreshAPIKeyRequirement();
    renderQA();
  }

  function openQAConfig() {
    if (!runtime.interactive) return;
    const providerSelect = document.getElementById('qa-api-provider');
    clear(providerSelect);
    (runtime.qa?.providers || []).forEach((provider) => {
      const option = element('option', '', provider.label);
      option.value = provider.id;
      providerSelect.appendChild(option);
    });
    providerSelect.value = runtime.qa?.provider_id || 'openai';
    const definition = providerDefinition(providerSelect.value);
    const key = document.getElementById('qa-api-key');
    key.value = '';
    document.getElementById('qa-vault-password').value = '';
    document.getElementById('qa-vault-password-confirm').value = '';
    const remember = document.getElementById('qa-remember-key');
    remember.checked = runtime.qa?.configured || runtime.qa?.remembered
      ? Boolean(runtime.qa?.remembered)
      : Boolean(runtime.qa?.credential_store_available);
    populateModelOptions(providerSelect.value, runtime.qa?.model || definition?.default_model || '');
    document.getElementById('qa-api-base-url').value =
      runtime.qa?.base_url || definition?.default_base_url || '';
    setQAConfigFeedback(runtime.qa?.persistence_error || '', Boolean(runtime.qa?.persistence_error));
    setQAConfigBusy(false);
    document.getElementById('qa-config-dialog').showModal();
    (runtime.qa?.credential_locked ? document.getElementById('qa-vault-password') : key).focus();
  }

  async function saveQAConfig() {
    const key = document.getElementById('qa-api-key');
    const vaultPassword = document.getElementById('qa-vault-password');
    const vaultPasswordConfirm = document.getElementById('qa-vault-password-confirm');
    if (!document.getElementById('qa-vault-confirm-field').hidden && vaultPassword.value !== vaultPasswordConfirm.value) {
      setQAConfigFeedback('Vault passwords do not match.', true);
      vaultPasswordConfirm.focus();
      return;
    }
    const unlocking = Boolean(
      runtime.qa?.credential_locked &&
      runtime.qa?.credential_mode === 'encrypted_vault' &&
      document.getElementById('qa-api-provider').value === runtime.qa?.provider_id &&
      !key.value,
    );
    const request = unlocking
      ? { vault_password: vaultPassword.value }
      : {
          api_key: key.value,
          provider: document.getElementById('qa-api-provider').value,
          model: configuredModel(),
          base_url: document.getElementById('qa-api-base-url').value.trim(),
          remember: document.getElementById('qa-remember-key').checked,
          vault_password: vaultPassword.value,
        };
    key.value = '';
    vaultPassword.value = '';
    vaultPasswordConfirm.value = '';
    setQAConfigBusy(true);
    setQAConfigFeedback(unlocking ? 'Unlocking…' : 'Saving…');
    try {
      const response = await apiJson(unlocking ? '/api/qa/unlock' : '/api/qa/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      });
      runtime.qa = response.qa || runtime.qa;
      document.getElementById('qa-config-dialog').close();
    } catch (error) {
      setQAConfigFeedback(error.message, true);
    } finally {
      if ('api_key' in request) request.api_key = '';
      request.vault_password = '';
      setQAConfigBusy(false);
      renderAgentActions();
      renderQA();
    }
  }

  async function clearQAKey() {
    setQAConfigBusy(true);
    setQAConfigFeedback('Clearing…');
    try {
      const response = await apiJson('/api/qa/config', { method: 'DELETE' });
      runtime.qa = response.qa || runtime.qa;
      document.getElementById('qa-api-key').value = '';
      document.getElementById('qa-config-dialog').close();
    } catch (error) {
      setQAConfigFeedback(error.message, true);
    } finally {
      setQAConfigBusy(false);
      renderAgentActions();
      renderQA();
    }
  }

  function renderDashboard() {
    renderHeader();
    renderSourceContext();
    renderSessionBrief();
    renderAssurance();
    renderToolSummary();
    renderTrace();
    renderAgentActions();
    renderQA();
  }

  function setSourceStatus(message, error = false) {
    const node = document.getElementById('source-status');
    node.textContent = message;
    node.classList.toggle('error-text', error);
    const feedback = document.getElementById('source-dialog-status');
    if (feedback) {
      feedback.textContent = message;
      feedback.hidden = !message;
      feedback.classList.toggle('error-text', error);
    }
  }

  function setSourceBusy(busy) {
    state.sourceBusy = busy;
    const unavailable = busy || !runtime.interactive;
    document.getElementById('session-id').disabled = unavailable;
    document.getElementById('session-path').disabled = unavailable;
    document.getElementById('load-session-id-button').disabled = unavailable;
    document.getElementById('load-path-button').disabled = unavailable;
    document.getElementById('upload-button').disabled = unavailable;
    const status = document.getElementById('source-loader-status');
    status.hidden = runtime.interactive;
    status.textContent = 'Read-only report. Open Agent Trace Studio through its local server to add sources.';
  }

  async function apiJson(path, options = {}) {
    const response = await fetch(path, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function auditRuleValues() {
    const rules = state.auditRules?.rule_set?.rules;
    return Array.isArray(rules) ? rules : [];
  }

  function auditRuleUsesRelation(ruleType) {
    return ['require_before', 'require_after'].includes(ruleType);
  }

  function auditRuleTypeLabel(ruleType) {
    return {
      forbid_event: 'Event must not occur',
      require_event: 'Event must occur',
      require_before: 'Required event before trigger',
      require_after: 'Required event after trigger',
    }[ruleType] || ruleType || 'Unknown rule type';
  }

  function emptyAuditMatcher() {
    return { category: '', kind: '', role: '', phase: '', tool_name: '', status: '', contains: '' };
  }

  function emptyAuditRule() {
    return {
      id: '',
      title: '',
      expectation: '',
      severity: 'medium',
      enabled: true,
      rule_type: 'forbid_event',
      event: emptyAuditMatcher(),
      required_event: null,
      same_turn: false,
      automatic_action: null,
      version: null,
    };
  }

  function auditMatcherSummary(matcher) {
    if (!matcher || typeof matcher !== 'object') return 'no matcher';
    const values = [
      ['tool', matcher.tool_name],
      ['kind', matcher.kind],
      ['category', matcher.category],
      ['role', matcher.role],
      ['phase', matcher.phase],
      ['status', matcher.status],
      ['contains', matcher.contains],
    ]
      .filter(([, value]) => value)
      .map(([label, value]) => `${label}=${value}`);
    return values.join(', ') || 'no matcher';
  }

  function setAuditMatcherFields(prefix, matcher = {}) {
    const fields = {
      tool: 'tool_name',
      kind: 'kind',
      category: 'category',
      role: 'role',
      phase: 'phase',
      status: 'status',
      contains: 'contains',
    };
    Object.entries(fields).forEach(([field, key]) => {
      document.getElementById(`audit-rule-${prefix}-${field}`).value = matcher?.[key] || '';
    });
  }

  function auditMatcherFromFields(prefix) {
    const matcher = {
      tool_name: document.getElementById(`audit-rule-${prefix}-tool`).value.trim(),
      kind: document.getElementById(`audit-rule-${prefix}-kind`).value.trim(),
      category: document.getElementById(`audit-rule-${prefix}-category`).value.trim(),
      role: document.getElementById(`audit-rule-${prefix}-role`).value.trim(),
      phase: document.getElementById(`audit-rule-${prefix}-phase`).value.trim(),
      status: document.getElementById(`audit-rule-${prefix}-status`).value.trim(),
      contains: document.getElementById(`audit-rule-${prefix}-contains`).value.trim(),
    };
    if (!Object.values(matcher).some(Boolean)) throw new Error('Each event matcher needs at least one value.');
    return matcher;
  }

  function updateAuditRuleTypeFields() {
    const ruleType = document.getElementById('audit-rule-type').value;
    const relation = auditRuleUsesRelation(ruleType);
    document.getElementById('audit-rule-required-fields').hidden = !relation;
    document.getElementById('audit-rule-same-turn-field').hidden = !relation;
    document.getElementById('audit-rule-event-legend').textContent = relation
      ? 'Trigger event'
      : ruleType === 'forbid_event'
        ? 'Forbidden event'
        : 'Required event';
  }

  function updateAuditRuleActionFields() {
    const enabled = document.getElementById('audit-rule-action-enabled').checked;
    const field = document.getElementById('audit-rule-action-message-field');
    const message = document.getElementById('audit-rule-action-message');
    field.hidden = !enabled;
    message.required = enabled;
  }

  function populateAuditRuleForm(rule = emptyAuditRule()) {
    document.getElementById('audit-rule-version').value = rule.version || '';
    const idInput = document.getElementById('audit-rule-id');
    idInput.value = rule.id || '';
    idInput.disabled = Boolean(rule.version);
    document.getElementById('audit-rule-title').value = rule.title || '';
    document.getElementById('audit-rule-expectation').value = rule.expectation || '';
    document.getElementById('audit-rule-severity').value = rule.severity || 'medium';
    document.getElementById('audit-rule-enabled').checked = rule.enabled !== false;
    document.getElementById('audit-rule-type').value = rule.rule_type || 'forbid_event';
    document.getElementById('audit-rule-same-turn').checked = Boolean(rule.same_turn);
    document.getElementById('audit-rule-action-enabled').checked = Boolean(rule.automatic_action);
    document.getElementById('audit-rule-action-message').value = rule.automatic_action?.message || '';
    setAuditMatcherFields('event', rule.event || emptyAuditMatcher());
    setAuditMatcherFields('required', rule.required_event || emptyAuditMatcher());
    const archive = document.getElementById('audit-rule-archive');
    archive.hidden = !rule.version || rule.enabled === false;
    updateAuditRuleTypeFields();
    updateAuditRuleActionFields();
  }

  function auditRuleFromForm() {
    const ruleType = document.getElementById('audit-rule-type').value;
    const automaticAction = document.getElementById('audit-rule-action-enabled').checked;
    const actionMessage = document.getElementById('audit-rule-action-message').value.trim();
    if (automaticAction && !actionMessage) throw new Error('Automatic actions need an exact session message.');
    return {
      id: document.getElementById('audit-rule-id').value.trim(),
      title: document.getElementById('audit-rule-title').value.trim(),
      expectation: document.getElementById('audit-rule-expectation').value.trim(),
      severity: document.getElementById('audit-rule-severity').value,
      enabled: document.getElementById('audit-rule-enabled').checked,
      rule_type: ruleType,
      event: auditMatcherFromFields('event'),
      required_event: auditRuleUsesRelation(ruleType) ? auditMatcherFromFields('required') : null,
      same_turn: auditRuleUsesRelation(ruleType) && document.getElementById('audit-rule-same-turn').checked,
      automatic_action: automaticAction ? { type: 'send_session_message', message: actionMessage } : null,
    };
  }

  function selectAuditRule(ruleId) {
    state.auditRuleSelectedId = ruleId || null;
    state.auditRuleEditing = false;
    const rule = auditRuleValues().find((candidate) => candidate.id === state.auditRuleSelectedId);
    populateAuditRuleForm(rule || emptyAuditRule());
    renderAuditRuleDialog();
  }

  function beginAuditRuleEdit() {
    const rule = auditRuleValues().find((candidate) => candidate.id === state.auditRuleSelectedId);
    if (!rule || state.auditRulesBusy) return;
    state.auditRuleEditing = true;
    populateAuditRuleForm(rule);
    renderAuditRuleDialog();
    document.getElementById('audit-rule-title').focus();
  }

  function cancelAuditRuleEdit() {
    const rule = auditRuleValues().find((candidate) => candidate.id === state.auditRuleSelectedId);
    state.auditRuleEditing = false;
    populateAuditRuleForm(rule || emptyAuditRule());
    renderAuditRuleDialog();
  }

  function appendAuditRuleReviewItem(container, label, value, className = '') {
    const item = element('div', 'audit-rule-review-item');
    item.append(element('dt', '', label), element('dd', className, value || 'Not set'));
    container.appendChild(item);
  }

  function startAuditRuleDraftInStudio() {
    const dialog = document.getElementById('audit-rules-dialog');
    if (dialog.open) dialog.close();
    const input = document.getElementById('qa-question');
    input.value = 'Create an audit rule: ';
    input.setSelectionRange(input.value.length, input.value.length);
    openQAConversation();
  }

  function renderAuditRuleReview(rule) {
    const review = document.getElementById('audit-rule-review');
    const content = document.getElementById('audit-rule-review-content');
    const editButton = document.getElementById('audit-rule-edit');
    review.hidden = state.auditRuleEditing;
    clear(content);
    editButton.hidden = !rule;
    editButton.disabled = state.auditRulesBusy || !rule;
    if (!rule) {
      const empty = element('div', 'empty-state audit-rule-empty-state');
      empty.appendChild(element('p', '', 'No saved audit rules.'));
      const create = element('button', 'command-button', 'Create new rule');
      create.type = 'button';
      create.addEventListener('click', startAuditRuleDraftInStudio);
      empty.appendChild(create);
      content.appendChild(empty);
      return;
    }

    const contracts = state.auditRules?.assurance?.contracts;
    const contract = Array.isArray(contracts) ? contracts.find((candidate) => candidate.id === rule.id) || null : null;
    const status = contract?.status || (rule.enabled ? 'watching' : 'disabled');
    const heading = element('div', 'audit-rule-review-heading');
    const title = element('div');
    title.append(
      element('span', 'control-label', `Rule ${rule.id} · version ${formatInteger(rule.version)}`),
      element('h3', '', rule.title),
      element('p', 'muted', rule.enabled ? 'Active in the local audit rule set' : 'Disabled and retained in history'),
    );
    const badges = element('div', 'audit-rule-review-badges');
    badges.append(
      element('span', `assurance-severity ${rule.severity || 'medium'}`, rule.severity || 'medium'),
      element(
        'span',
        `assurance-status ${rule.enabled ? 'satisfied' : 'watching'}`,
        rule.enabled ? 'Active' : 'Disabled',
      ),
    );
    heading.append(title, badges);
    content.appendChild(heading);

    const expectation = element('section', 'audit-rule-review-section');
    expectation.append(element('h4', '', 'Expected behavior'), element('p', '', rule.expectation));
    content.appendChild(expectation);

    const definition = element('section', 'audit-rule-review-section');
    definition.appendChild(element('h4', '', 'Rule definition'));
    const definitionGrid = element('dl', 'audit-rule-review-grid');
    appendAuditRuleReviewItem(definitionGrid, 'Rule type', auditRuleTypeLabel(rule.rule_type));
    appendAuditRuleReviewItem(
      definitionGrid,
      rule.rule_type === 'forbid_event'
        ? 'Forbidden event'
        : auditRuleUsesRelation(rule.rule_type)
          ? 'Trigger event'
          : 'Required event',
      auditMatcherSummary(rule.event),
      'audit-rule-review-matcher',
    );
    if (rule.required_event) {
      appendAuditRuleReviewItem(
        definitionGrid,
        'Required event',
        auditMatcherSummary(rule.required_event),
        'audit-rule-review-matcher',
      );
    }
    appendAuditRuleReviewItem(
      definitionGrid,
      'Relationship scope',
      auditRuleUsesRelation(rule.rule_type) ? (rule.same_turn ? 'Same turn' : 'Entire session') : 'Not applicable',
    );
    appendAuditRuleReviewItem(
      definitionGrid,
      'Violation action',
      rule.automatic_action
        ? `Automatic read-only session message: ${rule.automatic_action.message}`
        : 'Manual host-authored notification after an explicit click',
    );
    appendAuditRuleReviewItem(
      definitionGrid,
      'Last updated by',
      rule.updated_by === 'agent' ? 'Approved Studio agent proposal' : 'Manual edit',
    );
    appendAuditRuleReviewItem(definitionGrid, 'Last updated', formatDate(rule.updated_at));
    definition.appendChild(definitionGrid);
    content.appendChild(definition);

    const evaluation = element('section', 'audit-rule-review-section');
    const evaluationHeading = element('div', 'audit-rule-review-section-heading');
    evaluationHeading.appendChild(element('h4', '', 'Current session result'));
    if (contract) {
      evaluationHeading.appendChild(element('span', `assurance-status ${status}`, assuranceStatusLabel(status)));
    }
    evaluation.appendChild(evaluationHeading);
    if (!rule.enabled) {
      evaluation.appendChild(element('p', 'muted', 'Disabled rules are not evaluated.'));
    } else if (!contract) {
      evaluation.appendChild(element('p', 'muted', 'No evaluation is available for the selected session.'));
    } else {
      evaluation.appendChild(element('p', '', contract.observation || 'No observation was returned.'));
      if (contract.action?.receipt?.message) {
        evaluation.appendChild(element('p', 'audit-action-status muted', contract.action.receipt.message));
      } else if (contract.status === 'violated' && contract.action?.reason) {
        evaluation.appendChild(element('p', 'audit-action-status muted', contract.action.reason));
      }
      const evidence = Array.isArray(contract.evidence) ? contract.evidence : [];
      if (evidence.length) {
        const evidenceList = element('div', 'assurance-evidence-list audit-rule-review-evidence');
        evidence.forEach((item) => {
          const button = element('button', 'assurance-evidence-button');
          button.type = 'button';
          button.append(
            element('strong', '', item.label || item.event_title || 'Trace event'),
            element('span', '', `${item.event_title || 'Event'} · line ${formatInteger(item.line_number)}`),
          );
          button.addEventListener('click', () => {
            document.getElementById('audit-rules-dialog').close();
            selectAssuranceEvidence(contract, status, item);
          });
          evidenceList.appendChild(button);
        });
        evaluation.appendChild(evidenceList);
      }
    }
    content.appendChild(evaluation);
  }

  function renderAuditRuleProposal(container, proposal, message = null) {
    clear(container);
    if (!proposal?.id) {
      container.hidden = true;
      container.classList.remove('audit-rule-proposal');
      return;
    }
    container.hidden = false;
    container.classList.add('audit-rule-proposal');
    const rule = proposal.rule || {};
    const copy = element('div', 'audit-rule-proposal-copy');
    copy.append(
      element(
        'strong',
        '',
        `${proposal.operation === 'update' ? 'Update' : 'Create'} · ${rule.title || rule.id || 'Audit rule'}`,
      ),
      element('p', '', proposal.summary || ''),
      element('p', '', `Expected: ${rule.expectation || 'Not supplied'}`),
      element(
        'p',
        'muted',
        `${rule.id || 'new-rule'} · ${rule.rule_type || 'rule'} · ${rule.severity || 'medium'} · ${
          rule.enabled === false ? 'disabled' : 'enabled'
        }`,
      ),
      element('p', 'muted', `Trigger: ${auditMatcherSummary(rule.event)}`),
    );
    if (rule.required_event) {
      copy.appendChild(
        element(
          'p',
          'muted',
          `Required: ${auditMatcherSummary(rule.required_event)}${rule.same_turn ? ' · same turn' : ''}`,
        ),
      );
    }
    if (rule.automatic_action) {
      copy.appendChild(element('p', 'muted', `Automatic action: send “${rule.automatic_action.message}”`));
    }
    copy.appendChild(element('p', 'muted', `Requested: ${proposal.instruction || 'Not supplied'}`));
    if (proposal.expires_at) copy.appendChild(element('p', 'muted', `Expires: ${formatDate(proposal.expires_at)}`));
    const actions = element('div', 'audit-rule-proposal-actions');
    const approve = element('button', 'command-button', 'Approve and save');
    approve.type = 'button';
    approve.disabled = state.auditRulesBusy || message?.resolved;
    approve.addEventListener('click', () => approveAuditRuleProposal(proposal, message));
    const discard = element('button', 'command-button secondary', 'Discard');
    discard.type = 'button';
    discard.disabled = state.auditRulesBusy || message?.resolved;
    discard.addEventListener('click', () => cancelAuditRuleProposal(proposal, message));
    actions.append(approve, discard);
    container.append(copy, actions);
  }

  function renderAuditRuleDialog() {
    const ruleSet = state.auditRules?.rule_set;
    const rules = auditRuleValues();
    const selectedExists = rules.some((rule) => rule.id === state.auditRuleSelectedId);
    if (!state.auditRuleEditing && !selectedExists) state.auditRuleSelectedId = rules[0]?.id || null;
    const selectedRule = rules.find((rule) => rule.id === state.auditRuleSelectedId) || null;
    document.getElementById('audit-rules-revision').textContent = ruleSet
      ? `Revision ${formatInteger(ruleSet.revision || 0)} · ${formatInteger(rules.length)} saved rules`
      : 'Rule set unavailable';
    const feedback = document.getElementById('audit-rules-feedback');
    feedback.textContent = state.auditRulesError || '';
    feedback.classList.toggle('error-text', Boolean(state.auditRulesError));
    const list = document.getElementById('audit-rule-list');
    clear(list);
    if (!rules.length) list.appendChild(element('p', 'audit-rule-list-empty muted', 'No saved rules.'));
    rules.forEach((rule) => {
      const button = element('button', 'audit-rule-list-button');
      button.type = 'button';
      button.classList.toggle('active', rule.id === state.auditRuleSelectedId);
      if (rule.id === state.auditRuleSelectedId) button.setAttribute('aria-current', 'true');
      button.disabled = state.auditRulesBusy || state.auditRuleEditing;
      button.append(
        element('strong', '', rule.title),
        element('span', `audit-rule-list-state${rule.enabled ? '' : ' disabled'}`, rule.enabled ? 'Active' : 'Disabled'),
        element('small', '', `${rule.id} · v${formatInteger(rule.version)} · ${rule.severity}`),
      );
      button.addEventListener('click', () => selectAuditRule(rule.id));
      list.appendChild(button);
    });
    document.getElementById('audit-rule-form').hidden = !state.auditRuleEditing;
    renderAuditRuleReview(selectedRule);
    document.getElementById('audit-rule-save').disabled = state.auditRulesBusy;
    document.getElementById('audit-rule-archive').disabled = state.auditRulesBusy;
    document.getElementById('audit-rule-cancel-edit').disabled = state.auditRulesBusy;
  }

  function openAuditRulesDialog() {
    const dialog = document.getElementById('audit-rules-dialog');
    const rules = auditRuleValues();
    if (!state.auditRuleSelectedId && rules.length) state.auditRuleSelectedId = rules[0].id;
    state.auditRuleEditing = false;
    const selected = rules.find((rule) => rule.id === state.auditRuleSelectedId);
    populateAuditRuleForm(selected || emptyAuditRule());
    renderAuditRuleDialog();
    dialog.showModal();
  }

  async function refreshAuditRules() {
    const sessionId = state.traceSessionId;
    if (!runtime.interactive || !runtime.audit_rules?.available || !sessionId) return;
    if (state.auditRulesBusy) {
      state.auditRulesRefreshPending = true;
      return;
    }
    state.auditRulesBusy = true;
    state.auditRulesRefreshPending = false;
    renderAssurance();
    try {
      const response = await apiJson(`/api/audit-rules?session_id=${encodeURIComponent(sessionId)}`);
      if (state.traceSessionId !== sessionId) return;
      state.auditRules = response;
      state.auditRulesError = response.rule_set?.history_error || '';
      const selectedExists = auditRuleValues().some((rule) => rule.id === state.auditRuleSelectedId);
      if (!selectedExists) state.auditRuleSelectedId = null;
    } catch (error) {
      if (state.traceSessionId === sessionId) state.auditRulesError = error.message;
    } finally {
      state.auditRulesBusy = false;
      renderAssurance();
      if (document.getElementById('audit-rules-dialog').open) renderAuditRuleDialog();
      if (state.auditRulesRefreshPending) refreshAuditRules();
    }
  }

  async function saveAuditRule() {
    if (state.auditRulesBusy) return;
    let rule;
    try {
      rule = auditRuleFromForm();
    } catch (error) {
      state.auditRulesError = error.message;
      renderAuditRuleDialog();
      return;
    }
    const versionValue = document.getElementById('audit-rule-version').value;
    state.auditRulesBusy = true;
    state.auditRulesError = '';
    renderAuditRuleDialog();
    try {
      const response = await apiJson('/api/audit-rules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rule, expected_version: versionValue ? Number(versionValue) : null }),
      });
      state.auditRuleSelectedId = response.rule?.id || rule.id;
      state.auditRuleEditing = false;
      state.auditRules = { ...(state.auditRules || {}), rule_set: response.rule_set };
      populateAuditRuleForm(response.rule || rule);
    } catch (error) {
      state.auditRulesError = error.message;
    } finally {
      state.auditRulesBusy = false;
      renderAuditRuleDialog();
      await refreshAuditRules();
    }
  }

  async function archiveAuditRule() {
    const ruleId = document.getElementById('audit-rule-id').value.trim();
    const version = Number(document.getElementById('audit-rule-version').value);
    if (!ruleId || !version || state.auditRulesBusy) return;
    state.auditRulesBusy = true;
    state.auditRulesError = '';
    renderAuditRuleDialog();
    try {
      const response = await apiJson(
        `/api/audit-rules/${encodeURIComponent(ruleId)}?expected_version=${encodeURIComponent(version)}`,
        { method: 'DELETE' },
      );
      state.auditRuleEditing = false;
      state.auditRules = { ...(state.auditRules || {}), rule_set: response.rule_set };
      populateAuditRuleForm(response.rule);
    } catch (error) {
      state.auditRulesError = error.message;
    } finally {
      state.auditRulesBusy = false;
      renderAuditRuleDialog();
      await refreshAuditRules();
    }
  }

  function resolveAuditRuleProposalMessages(proposalId, text) {
    state.qaMessages.forEach((message) => {
      if (message.role !== 'audit_rule_proposal' || message.proposal?.id !== proposalId) return;
      updateWorkflowHistory(message.workflowContext, text);
      message.resolved = true;
      message.role = 'assistant';
      message.text = text;
      delete message.proposal;
      delete message.workflowContext;
    });
  }

  async function approveAuditRuleProposal(proposal, message = null) {
    if (!proposal?.id || !proposal.token || state.auditRulesBusy || message?.resolved) return;
    state.auditRulesBusy = true;
    state.auditRulesError = '';
    renderQA();
    renderAuditRuleDialog();
    try {
      const response = await apiJson(`/api/audit-rules/proposals/${encodeURIComponent(proposal.id)}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: proposal.token, client_session_nonce: state.clientSessionNonce }),
      });
      const text = `Audit rule **${response.rule?.title || proposal.rule?.title || proposal.rule?.id}** is active at version ${formatInteger(response.rule?.version || 1)}.`;
      resolveAuditRuleProposalMessages(proposal.id, text);
      state.auditRuleSelectedId = response.rule?.id || proposal.rule?.id || null;
      state.auditRuleEditing = false;
      state.auditRules = { ...(state.auditRules || {}), rule_set: response.rule_set };
    } catch (error) {
      state.auditRulesError = error.message;
      if (message) state.qaMessages.push({ role: 'error', text: error.message });
    } finally {
      state.auditRulesBusy = false;
      persistQAConversation();
      renderQA();
      renderAuditRuleDialog();
      await refreshAuditRules();
    }
  }

  async function cancelAuditRuleProposal(proposal, message = null) {
    if (!proposal?.id || !proposal.token || state.auditRulesBusy || message?.resolved) return;
    state.auditRulesBusy = true;
    state.auditRulesError = '';
    renderQA();
    renderAuditRuleDialog();
    try {
      await apiJson(`/api/audit-rules/proposals/${encodeURIComponent(proposal.id)}/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: proposal.token, client_session_nonce: state.clientSessionNonce }),
      });
      resolveAuditRuleProposalMessages(proposal.id, 'Audit rule proposal discarded. The active rule set was not changed.');
    } catch (error) {
      state.auditRulesError = error.message;
      if (message) state.qaMessages.push({ role: 'error', text: error.message });
    } finally {
      state.auditRulesBusy = false;
      persistQAConversation();
      renderQA();
      renderAuditRuleDialog();
    }
  }

  function stopSessionBriefAutoRefresh() {
    if (state.sessionBriefRefreshTimer !== null) window.clearTimeout(state.sessionBriefRefreshTimer);
    state.sessionBriefRefreshTimer = null;
  }

  function scheduleSessionBriefAutoRefresh(delay = 5000) {
    stopSessionBriefAutoRefresh();
    if (
      !state.sessionBriefAutoUpdate ||
      !runtime.live?.enabled ||
      !state.sessionBrief?.stale ||
      !runtime.qa?.configured
    ) {
      return;
    }
    state.sessionBriefRefreshTimer = window.setTimeout(async () => {
      state.sessionBriefRefreshTimer = null;
      const running = Boolean(state.agentRun && !terminalAgentStatus(state.agentRun.status));
      if (running || state.agentBusy || state.qaBusy || state.sessionBriefBusy) {
        scheduleSessionBriefAutoRefresh(3000);
        return;
      }
      await summarizeSessionCheckpoints({ automatic: true });
    }, delay);
  }

  async function refreshSessionBrief() {
    const sessionId = state.traceSessionId;
    if (!runtime.interactive || !sessionId) return;
    if (state.sessionBriefFetchBusy) {
      state.sessionBriefFetchPending = true;
      return;
    }
    state.sessionBriefFetchBusy = true;
    state.sessionBriefFetchPending = false;
    try {
      const brief = await apiJson(`/api/agent/session-brief?session_id=${encodeURIComponent(sessionId)}`);
      if (state.traceSessionId !== sessionId) return;
      state.sessionBrief = brief;
      state.sessionBriefError = '';
      const checkpointChanged = reconcileSelectedCheckpoint(brief);
      renderSessionBrief();
      if (checkpointChanged) renderAgentContext();
      scheduleSessionBriefAutoRefresh();
    } catch (error) {
      if (state.traceSessionId !== sessionId) return;
      state.sessionBriefError = error.message;
      renderSessionBrief();
    } finally {
      state.sessionBriefFetchBusy = false;
      if (state.sessionBriefFetchPending) refreshSessionBrief();
    }
  }

  function closeLiveStream() {
    if (state.liveEventSource) state.liveEventSource.close();
    state.liveEventSource = null;
    state.liveConnected = false;
  }

  async function refreshLivePayload() {
    if (state.liveRefreshBusy) {
      state.liveRefreshPending = true;
      return;
    }
    state.liveRefreshBusy = true;
    try {
      do {
        state.liveRefreshPending = false;
        const payload = await apiJson('/api/payload');
        applyLivePayload(payload);
        state.liveRevision = Math.max(state.liveRevision, Number(runtime.live?.revision) || 0);
      } while (state.liveRefreshPending);
    } catch (error) {
      runtime.live = { ...(runtime.live || {}), state: 'error', error: error.message };
      renderLiveMonitor();
    } finally {
      state.liveRefreshBusy = false;
    }
  }

  function setupLiveStream() {
    closeLiveStream();
    if (!runtime.interactive || !runtime.live?.enabled || typeof EventSource === 'undefined') return;
    const source = new EventSource(`/api/live/stream?after=${encodeURIComponent(state.liveRevision)}`);
    state.liveEventSource = source;
    source.addEventListener('open', () => {
      state.liveConnected = true;
      renderLiveMonitor();
    });
    source.addEventListener('trace-update', (event) => {
      let update = {};
      try {
        update = JSON.parse(event.data);
      } catch (_error) {
        return;
      }
      runtime.live = update;
      state.liveRevision = Math.max(state.liveRevision, Number(update.revision) || 0);
      state.liveConnected = true;
      renderLiveMonitor();
      refreshLivePayload();
    });
    source.addEventListener('error', () => {
      state.liveConnected = false;
      renderLiveMonitor();
    });
  }

  function stopAgentPolling() {
    if (state.agentPollTimer) window.clearTimeout(state.agentPollTimer);
    state.agentPollTimer = null;
  }

  function scheduleAgentPoll(runId, delay = 900) {
    stopAgentPolling();
    state.agentPollTimer = window.setTimeout(() => pollAgentRun(runId), delay);
  }

  function queueDashboardReload(run) {
    const refresh = run?.dashboard_refresh || {};
    const deployment = run?.deployment || run?.result?.deployment || {};
    const assetReload = refresh.status === 'ready' && refresh.auto_reload && refresh.revision;
    const runtimeReload = deployment.status === 'promoted' && deployment.generation;
    if (!assetReload && !runtimeReload) return false;
    const revision = assetReload ? refresh.revision : deployment.generation;
    const marker = `agent-trace-studio.dashboard-refresh.${run.run_id}.${revision}`;
    if (dashboardReloadMarkers.has(marker)) return false;
    try {
      if (window.sessionStorage.getItem(marker)) return false;
      window.sessionStorage.setItem(marker, '1');
    } catch (_error) {
      // Privacy modes may disable storage; the durable run is finalized before this response.
    }
    dashboardReloadMarkers.add(marker);
    persistQAConversation();
    window.setTimeout(() => window.location.reload(), 350);
    return true;
  }

  function scheduleDeploymentPoll(delay = 3000) {
    if (state.deploymentPollTimer !== null) window.clearTimeout(state.deploymentPollTimer);
    if (!runtime.deployment?.supervised) return;
    state.deploymentPollTimer = window.setTimeout(pollDeploymentGeneration, delay);
  }

  async function pollDeploymentGeneration() {
    state.deploymentPollTimer = null;
    try {
      const status = await apiJson('/api/status');
      const deployment = status.deployment || {};
      const generation = String(deployment.generation || '');
      if (state.deploymentGeneration && generation && generation !== state.deploymentGeneration) {
        persistQAConversation();
        window.location.reload();
        return;
      }
      state.deploymentGeneration = generation || state.deploymentGeneration;
      runtime.deployment = deployment;
    } catch (_error) {
      // The stable supervisor may briefly be switching child processes.
    }
    scheduleDeploymentPoll();
  }

  async function pollAgentRun(runId) {
    try {
      const run = await apiJson(`/api/agent/runs/${encodeURIComponent(runId)}`);
      state.agentRun = run;
      state.agentError = '';
      runtime.agent.active_run_id = terminalAgentStatus(run.status) ? null : runId;
      recoverPendingSourceWorkflow(run);
      const activityChanged = updateQAActivity(run);
      const completionAdded = appendAgentRunCompletion(run);
      renderAgentActions();
      if (completionAdded) renderQA();
      else if (activityChanged) renderQA();
      if (queueDashboardReload(run)) return;
      if (!terminalAgentStatus(run.status)) scheduleAgentPoll(runId);
      else if (run.kind === 'checkpoints') await refreshSessionBrief();
    } catch (error) {
      state.agentError = error.message;
      renderAgentActions();
      if (!error.status || error.status >= 500) scheduleAgentPoll(runId, 2000);
    }
  }

  async function selectAgentHarness(harness) {
    if (!harness || state.agentHarnessBusy || state.qaBusy) return;
    state.agentHarnessBusy = true;
    state.agentError = '';
    renderAgentActions();
    try {
      const response = await apiJson('/api/agent/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ harness }),
      });
      runtime.agent = response.agent || runtime.agent;
      runtime.qa = response.qa || runtime.qa;
      state.qaConversationBootstrapped = false;
    } catch (error) {
      state.agentError = error.message;
    } finally {
      state.agentHarnessBusy = false;
      renderAgentActions();
      renderQA();
    }
  }

  async function cancelAgentWorkflow() {
    const runId = state.agentRun?.run_id;
    if (!runId || state.agentBusy || terminalAgentStatus(state.agentRun.status)) return;
    state.agentBusy = true;
    renderAgentActions();
    renderQA();
    try {
      state.agentRun = await apiJson(`/api/agent/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' });
      state.agentError = '';
      scheduleAgentPoll(runId);
    } catch (error) {
      state.agentError = error.message;
    } finally {
      state.agentBusy = false;
      renderAgentActions();
      renderQA();
    }
  }

  async function stopStudioAgent() {
    const requestId = state.qaRequestId;
    if (!requestId) {
      await cancelAgentWorkflow();
      return;
    }
    if (state.qaStopBusy) return;
    state.qaStopBusy = true;
    state.qaStoppedRequestId = requestId;
    renderQA();
    try {
      const response = await apiJson(`/api/agent/messages/${encodeURIComponent(requestId)}`, { method: 'DELETE' });
      if (!response.cancelled) throw new Error('The Studio request already finished.');
      state.qaRequestController?.abort();
    } catch (error) {
      state.qaStoppedRequestId = null;
      state.qaMessages.push({ role: 'error', text: error.message });
      markQAUnread();
    } finally {
      state.qaStopBusy = false;
      persistQAConversation();
      renderQA();
    }
  }

  async function startNewStudioConversation() {
    if (state.qaBusy || state.qaStopBusy) return;
    const conversationId = state.qaConversationId;
    const sessionId = state.traceSessionId;
    rotateQAConversationId();
    state.qaHistory = [];
    state.qaMessages = [];
    state.qaWorkflowRuns = {};
    state.qaPendingSourceActions = {};
    state.qaActivity = null;
    state.qaUnreadCount = 0;
    state.qaConversationBootstrapped = false;
    clearStoredQAConversation();
    persistQAConversation();
    renderQA();
    if (!runtime.interactive || !conversationId || !sessionId) return;
    try {
      await apiJson(
        `/api/agent/conversations/${encodeURIComponent(conversationId)}?session_id=${encodeURIComponent(sessionId)}`,
        { method: 'DELETE' },
      );
    } catch (error) {
      state.qaMessages.push({
        role: 'error',
        text: `The new conversation started, but old context cleanup failed: ${error.message}`,
      });
      persistQAConversation();
      renderQA();
    }
  }

  function recordRunControlHistory(run, action, instruction, answer) {
    const actionLabel =
      action === 'continue' ? 'Continue' : action === 'restart' ? 'Restart' : action === 'activate' ? 'Activate' : 'Discard';
    const question = instruction ? `${actionLabel} workflow: ${instruction}` : `${actionLabel} agent workflow.`;
    const historyIndex = state.qaHistory.length;
    state.qaHistory.push({
      question,
      answer: storedQAText(answer) || `${actionLabel} requested.`,
      session_id: run?.session_id || state.traceSessionId,
      turn_id: run?.turn_id || state.traceTurnId || null,
    });
    return {
      session_id: run?.session_id || state.traceSessionId,
      turn_id: run?.turn_id || state.traceTurnId || null,
      history_index: historyIndex,
    };
  }

  async function actOnAgentRun(action) {
    const runId = state.agentRun?.run_id;
    if (!runId || state.agentBusy) return;
    const feedback = document.getElementById('agent-recovery-feedback');
    state.agentBusy = true;
    state.agentError = '';
    feedback.textContent = `${
      action === 'continue'
        ? 'Continuing'
        : action === 'restart'
          ? 'Starting over'
          : action === 'activate'
            ? 'Preparing activation approval'
            : 'Discarding'
    }…`;
    feedback.classList.remove('error-text');
    renderAgentActions();
    try {
      const instruction = document.getElementById('agent-recovery-instruction').value.trim();
      const response = await apiJson(`/api/agent/runs/${encodeURIComponent(runId)}/actions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action,
          instruction: instruction || null,
          client_session_nonce: state.clientSessionNonce,
        }),
      });
      document.getElementById('agent-recovery-instruction').value = '';
      const workflowContext = recordRunControlHistory(
        state.agentRun,
        action,
        instruction,
        response.answer || response.message,
      );
      if (response.kind === 'authorization') {
        appendSourceAuthorization(response, workflowContext);
        feedback.textContent = 'Approval required in the agent conversation.';
      } else {
        feedback.textContent = '';
        trackStartedRun(response, workflowContext);
      }
    } catch (error) {
      state.agentError = error.message;
      feedback.textContent = error.message;
      feedback.classList.add('error-text');
    } finally {
      state.agentBusy = false;
      renderAgentActions();
    }
  }

  function updateRuntimeFromPayload(payload) {
    if (!payload.runtime || typeof payload.runtime !== 'object') return;
    runtime = {
      interactive: true,
      qa: payload.runtime.qa || runtime.qa,
      agent: payload.runtime.agent || runtime.agent,
      live: payload.runtime.live || runtime.live,
      audit_rules: payload.runtime.audit_rules || runtime.audit_rules,
      deployment: payload.runtime.deployment || runtime.deployment,
    };
  }

  function traceScrollSnapshot() {
    return {
      turns: document.getElementById('trace-turn-list')?.scrollTop || 0,
      events: document.getElementById('trace-event-list')?.scrollTop || 0,
      detail: document.getElementById('trace-detail')?.scrollTop || 0,
    };
  }

  function restoreTraceScroll(snapshot, follow) {
    const turns = document.getElementById('trace-turn-list');
    const events = document.getElementById('trace-event-list');
    const detail = document.getElementById('trace-detail');
    if (follow) {
      if (turns) turns.scrollTop = 0;
      if (events) events.scrollTop = events.scrollHeight;
      if (detail) detail.scrollTop = 0;
      return;
    }
    if (turns) turns.scrollTop = snapshot.turns;
    if (events) events.scrollTop = snapshot.events;
    if (detail) detail.scrollTop = snapshot.detail;
  }

  function selectLatestLiveEvent() {
    const trace = selectedTrace();
    if (!trace) return;
    const turns = traceTurnRows(trace);
    const latestTurn = turns.find((turn) => turn.turn_id !== 'session') || turns[0];
    resetAgentFocus();
    state.traceTurnId = latestTurn?.turn_id || null;
    const events = eventsByTurn(trace).get(state.traceTurnId) || [];
    state.traceEventSequence = events.at(-1)?.sequence || null;
  }

  function pauseLiveFollow() {
    if (!runtime.live?.enabled || !state.followLive) return;
    state.followLive = false;
    renderLiveMonitor();
  }

  function applyLivePayload(payload) {
    const selectedSessionId = state.traceSessionId;
    const previousTrace = selectedTrace();
    const previousCount = previousTrace?.events_total || 0;
    const scroll = traceScrollSnapshot();
    updateRuntimeFromPayload(payload);
    data = payload;
    const selectedStillLoaded = data.traces.some((trace) => trace.session_id === selectedSessionId);
    state.traceSessionId = (selectedStillLoaded ? selectedSessionId : data.traces[0]?.session_id) || null;
    if (state.traceSessionId !== selectedSessionId) resetAgentFocus({ checkpoint: true });
    if (state.followLive) {
      state.liveNewEvents = 0;
      selectLatestLiveEvent();
    } else {
      const current = selectedTrace();
      if (current?.session_id === selectedSessionId) {
        state.liveNewEvents += Math.max((current.events_total || 0) - previousCount, 0);
      }
    }
    renderDashboard();
    restoreTraceScroll(scroll, state.followLive);
    refreshSessionBrief();
    refreshAuditRules();
  }

  function applyPayload(payload, previousTraceKeys = new Set()) {
    const selectedSessionId = state.traceSessionId;
    updateRuntimeFromPayload(payload);
    data = payload;
    const addedTrace = data.traces.find((trace) => !previousTraceKeys.has(traceKey(trace)));
    const selectedStillLoaded = data.traces.some((trace) => trace.session_id === selectedSessionId);
    state.traceSessionId =
      addedTrace?.session_id || (selectedStillLoaded ? selectedSessionId : data.traces[0]?.session_id) || null;
    state.traceTurnId = null;
    state.traceEventSequence = null;
    resetAgentFocus({ checkpoint: true });
    state.traceCategory = 'all';
    state.traceTool = '';
    state.qaHistory = [];
    state.qaMessages = [];
    state.qaWorkflowRuns = {};
    state.qaPendingSourceActions = {};
    state.qaActivity = null;
    state.qaUnreadCount = 0;
    clearStoredQAConversation();
    document.getElementById('trace-search').value = '';
    renderDashboard();
    refreshSessionBrief();
    refreshAuditRules();
  }

  async function loadPath(path) {
    setSourceBusy(true);
    setSourceStatus('Adding path…');
    const previousTraceKeys = loadedTraceKeys();
    try {
      const payload = await apiJson('/api/session/path', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      applyPayload(payload, previousTraceKeys);
      document.getElementById('session-path').value = '';
      setSourceStatus(`${formatInteger(data.traces.length)} traces loaded`);
      closeSourceDialog();
    } catch (error) {
      setSourceStatus(error.message, true);
    } finally {
      setSourceBusy(false);
    }
  }

  async function loadSessionId(sessionId) {
    setSourceBusy(true);
    setSourceStatus('Finding Codex session…');
    const previousTraceKeys = loadedTraceKeys();
    try {
      const payload = await apiJson('/api/session/id', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId }),
      });
      applyPayload(payload, previousTraceKeys);
      document.getElementById('session-id').value = '';
      setSourceStatus(`${formatInteger(data.traces.length)} traces loaded`);
      closeSourceDialog();
    } catch (error) {
      setSourceStatus(error.message, true);
    } finally {
      setSourceBusy(false);
    }
  }

  async function uploadFiles(files) {
    const selectedFiles = [...files];
    if (!selectedFiles.length) return;
    setSourceBusy(true);
    const previousTraceKeys = loadedTraceKeys();
    let latestPayload = null;
    const errors = [];
    for (const [index, file] of selectedFiles.entries()) {
      setSourceStatus(`Adding file ${index + 1} of ${selectedFiles.length}…`);
      try {
        latestPayload = await apiJson('/api/session/upload', {
          method: 'POST',
          headers: { 'X-Session-Filename': encodeURIComponent(file.name) },
          body: file,
        });
      } catch (error) {
        errors.push(`${file.name}: ${error.message}`);
      }
    }
    if (latestPayload) applyPayload(latestPayload, previousTraceKeys);
    if (errors.length) {
      setSourceStatus(errors[0], true);
    } else {
      setSourceStatus(
        `${formatInteger(selectedFiles.length)} file${selectedFiles.length === 1 ? '' : 's'} added · ` +
          `${formatInteger(data.traces.length)} traces loaded`,
      );
      closeSourceDialog();
    }
    setSourceBusy(false);
  }

  function nodeElement(node) {
    if (!node) return null;
    return node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  }

  function highlightedSelection(target) {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
    const selectedText = selection.toString().replace(/\s+/g, ' ').trim();
    if (!selectedText) return null;
    const range = selection.getRangeAt(0);
    const common = nodeElement(range.commonAncestorContainer);
    const targetElement = nodeElement(target);
    if (!common || !targetElement || !document.body.contains(common) || !document.body.contains(targetElement)) {
      return null;
    }
    if (targetElement.closest('input, textarea, select, option, [contenteditable="true"], #studio-context-menu')) {
      return null;
    }
    try {
      if (!range.intersectsNode(targetElement)) return null;
    } catch (_error) {
      return null;
    }
    let origin = 'dashboard';
    if (targetElement.closest('.checkpoint-item')) origin = 'checkpoint';
    else if (targetElement.closest('#trace-detail')) origin = 'trace event';
    else if (targetElement.closest('.trace-event-list')) origin = 'event list';
    else if (targetElement.closest('.trace-turn-list')) origin = 'turn list';
    else if (targetElement.closest('#agent-run-panel')) origin = 'agent result';
    else if (targetElement.closest('#qa-messages')) origin = 'Studio conversation';
    else if (targetElement.closest('#session-brief-panel')) origin = 'session brief';
    else if (targetElement.closest('#assurance-panel, #audit-rules-dialog')) origin = 'audit rules';
    const truncated = selectedText.length > 2000;
    return { text: selectedText.slice(0, 2000), origin, truncated };
  }

  function traceAskTarget(node) {
    const target = nodeElement(node);
    const trace = selectedTrace();
    if (!target || !trace) return null;
    const turnRow = target.closest('.trace-turn-row');
    if (turnRow) {
      const turn = traceTurnRows(trace).find((candidate) => candidate.turn_id === turnRow.dataset.turnId);
      if (!turn) return null;
      const events = eventsByTurn(trace).get(turn.turn_id) || [];
      return {
        kind: 'turn',
        turn_id: turn.turn_id,
        label: turn.trace_label,
        summary: traceTurnSummary(turn, events),
        status: String(turn.status || ''),
      };
    }
    const eventRow = target.closest('.trace-event-row');
    const detail = target.closest('#trace-detail');
    if (!eventRow && !detail) return null;
    const turnId = eventRow?.dataset.turnId || state.traceTurnId;
    const sequence = eventRow ? Number(eventRow.dataset.eventSequence) : state.traceEventSequence;
    const event = trace.events.find((candidate) => candidate.turn_id === turnId && candidate.sequence === sequence);
    if (!event) return null;
    return {
      kind: event.category === 'message' ? 'message' : 'event',
      turn_id: event.turn_id,
      event_sequence: event.sequence,
      line_number: event.line_number,
      label: event.title,
      summary: traceEventSummary(event),
      text: String(event.text || event.output_text || event.input_text || '').slice(0, 2000),
      role: String(event.role || ''),
      category: String(event.category || ''),
      status: String(event.status || ''),
    };
  }

  function activateTraceAskTarget(target) {
    pauseLiveFollow();
    const scroll = traceScrollSnapshot();
    resetAgentFocus();
    state.traceTurnId = target.turn_id;
    state.traceEventSequence = target.event_sequence || null;
    renderDashboard();
    restoreTraceScroll(scroll, false);
    state.askTarget = target;
    renderHighlightedContext();
    renderAgentContext();
    const targetName = target.kind === 'turn' ? 'this turn' : target.kind === 'message' ? 'this message' : 'this event';
    document.getElementById('qa-question').placeholder = `Ask about ${targetName}`;
    openQAConversation();
  }

  function closeStudioContextMenu() {
    const menu = document.getElementById('studio-context-menu');
    menu.hidden = true;
    if (menu.parentElement !== document.body) document.body.appendChild(menu);
    state.studioAskContext = null;
  }

  function openStudioContextMenu(event, context) {
    const menu = document.getElementById('studio-context-menu');
    const dialog = nodeElement(event.target)?.closest('dialog[open]') || null;
    state.studioAskContext = { ...context, dialog };
    (dialog || document.body).appendChild(menu);
    menu.hidden = false;
    menu.style.left = '0px';
    menu.style.top = '0px';
    const bounds = menu.getBoundingClientRect();
    const inset = 8;
    const left = Math.max(inset, Math.min(event.clientX, window.innerWidth - bounds.width - inset));
    const top = Math.max(inset, Math.min(event.clientY, window.innerHeight - bounds.height - inset));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    document.getElementById('studio-context-menu-ask').focus({ preventScroll: true });
  }

  function askAgentFromContextMenu() {
    const context = state.studioAskContext;
    closeStudioContextMenu();
    if (!context) return;
    if (context.dialog?.open) context.dialog.close();
    if (context.kind === 'trace') {
      activateTraceAskTarget(context.target);
      return;
    }
    state.askTarget = null;
    state.highlightedText = context.text;
    state.highlightOrigin = context.origin;
    state.highlightTruncated = Boolean(context.truncated);
    renderHighlightedContext();
    renderAgentContext();
    document.getElementById('qa-question').placeholder = 'Ask about the highlighted text';
    openQAConversation();
  }

  function handleSelectionContextMenu(event) {
    const context = highlightedSelection(event.target);
    if (context) {
      event.preventDefault();
      openStudioContextMenu(event, { kind: 'highlight', ...context });
      return;
    }
    const target = traceAskTarget(event.target);
    if (!target) {
      closeStudioContextMenu();
      return;
    }
    event.preventDefault();
    openStudioContextMenu(event, { kind: 'trace', target });
  }

  function trackStartedRun(run, workflowContext = null) {
    if (!run?.run_id) return;
    const runId = String(run.run_id);
    recoverPendingSourceWorkflow(run);
    const existing = state.qaWorkflowRuns[runId];
    if (workflowContext || existing) {
      const workflow = qaConversationState.beginWorkflow({
        runId,
        existing,
        context: workflowContext,
        terminal: terminalAgentStatus(run.status),
        fallbackSessionId: state.traceSessionId,
      });
      if (workflow) state.qaWorkflowRuns[runId] = workflow;
    }
    const activityChanged = updateQAActivity(run);
    state.agentRun = run;
    runtime.agent.active_run_id = terminalAgentStatus(run.status) ? null : runId;
    if (terminalAgentStatus(run.status)) {
      stopAgentPolling();
      if (appendAgentRunCompletion(run)) renderQA();
      else if (activityChanged) renderQA();
      if (run.kind === 'checkpoints') refreshSessionBrief();
    } else {
      if (activityChanged) renderQA();
      scheduleAgentPoll(runId);
    }
    persistQAConversation();
  }

  function appendAgentRunCompletion(run) {
    if (!run?.run_id || !terminalAgentStatus(run.status)) return false;
    const runId = String(run.run_id);
    const workflow = state.qaWorkflowRuns[runId];
    if (!workflow) return false;
    const completed = qaConversationState.completeWorkflow({ workflow, run, history: state.qaHistory });
    if (!completed.appended) return false;
    state.qaWorkflowRuns[runId] = completed.workflow;
    state.qaHistory = completed.history;
    state.qaMessages.push({ role: 'assistant', text: completed.answer });
    markQAUnread();
    persistQAConversation();
    return true;
  }

  function updateQAActivity(run) {
    if (!run?.run_id) return false;
    const runId = String(run.run_id);
    const next = qaConversationState.workflowActivity({
      workflow: state.qaWorkflowRuns[runId] || null,
      run,
    });
    if (!next) return false;
    const previous = state.qaMessages.find(
      (message) => message.role === 'activity' && message.activity_id === next.activity_id,
    );
    const previousLatest = previous?.activity?.at(-1) || {};
    const nextLatest = next.activity.at(-1) || {};
    const previousKey = previous
      ? `${previous.status}:${previous.activity.length}:${previousLatest.sequence}:${previousLatest.at}:${previousLatest.repeat_count}:${previousLatest.message}`
      : '';
    const nextKey = `${next.status}:${next.activity.length}:${nextLatest.sequence}:${nextLatest.at}:${nextLatest.repeat_count}:${nextLatest.message}`;
    if (previousKey === nextKey) return false;
    upsertQAActivity(next);
    return true;
  }

  function durableWorkflowContext(context = null) {
    const indexes = [];
    if (Number.isInteger(context?.history_index)) indexes.push(context.history_index);
    (Array.isArray(context?.history_indexes) ? context.history_indexes : []).forEach((index) => {
      if (Number.isInteger(index)) indexes.push(index);
    });
    return {
      session_id: context?.session_id || state.traceSessionId,
      turn_id: context?.turn_id || state.traceTurnId || null,
      history_indexes: Array.from(new Set(indexes)).slice(-8),
      completion_key: '',
    };
  }

  function updateWorkflowHistory(context, answer) {
    const text = storedQAText(answer, 12000);
    if (!text) return;
    durableWorkflowContext(context).history_indexes.forEach((index) => {
      if (state.qaHistory[index]) state.qaHistory[index].answer = text;
    });
  }

  function recoverPendingSourceWorkflow(run) {
    if (!run?.run_id) return false;
    const recovered = qaConversationState.pendingContextForRun(run, state.qaPendingSourceActions);
    if (!recovered) return false;
    delete state.qaPendingSourceActions[recovered.authorizationId];
    const runId = String(run.run_id);
    const workflow = qaConversationState.beginWorkflow({
      runId,
      existing: state.qaWorkflowRuns[runId] || null,
      context: recovered.context,
      terminal: terminalAgentStatus(run.status),
      fallbackSessionId: state.traceSessionId,
    });
    if (workflow) state.qaWorkflowRuns[runId] = workflow;
    state.qaMessages.forEach((message) => {
      if (message.role !== 'authorization' || message.authorization?.id !== recovered.authorizationId) return;
      message.resolved = true;
      message.role = 'assistant';
      message.text = 'Source change approved; the workflow was recovered after reconnecting.';
      delete message.authorization;
      delete message.workflowContext;
    });
    persistQAConversation();
    return true;
  }

  function appendSourceAuthorization(response, workflowContext = null) {
    const authorization = response.authorization;
    if (!authorization?.id || !authorization?.token) throw new Error('Source approval response is incomplete');
    state.qaPendingSourceActions[authorization.id] = {
      authorization_id: authorization.id,
      ...durableWorkflowContext(workflowContext),
    };
    state.qaMessages.push({
      role: 'authorization',
      text: response.answer || 'Approve this source change before the agent starts.',
      authorization,
      workflowContext,
      resolved: false,
    });
    persistQAConversation();
    markQAUnread();
    openQAConversation({ focus: false });
  }

  function appendAuditRuleProposal(response, workflowContext = null) {
    const proposal = response.proposal;
    if (!proposal?.id || !proposal?.token) throw new Error('Audit rule proposal response is incomplete');
    state.qaMessages.push({
      role: 'audit_rule_proposal',
      text: response.answer || 'Review this audit rule proposal.',
      proposal,
      workflowContext,
      resolved: false,
    });
    persistQAConversation();
    markQAUnread();
    openQAConversation({ focus: false });
  }

  function upsertQAActivity(record) {
    const next = qaConversationState.normalizeActivityRecord(record);
    if (!next) return null;
    const index = state.qaMessages.findIndex(
      (message) => message.role === 'activity' && message.activity_id === next.activity_id,
    );
    const previous = index >= 0 ? state.qaMessages[index] : null;
    const becameTerminal = previous
      && !terminalActivityStatus(previous.status)
      && terminalActivityStatus(next.status);
    if (becameTerminal) next.expanded = false;
    else if (previous && next.expanded === null) next.expanded = previous.expanded;
    if (previous) next.expanded_steps = previous.expanded_steps || [];
    if (index >= 0) state.qaMessages[index] = next;
    else state.qaMessages.push(next);
    return next;
  }

  function stopStudioActivityPolling() {
    if (state.qaRequestPollTimer !== null) window.clearTimeout(state.qaRequestPollTimer);
    state.qaRequestPollTimer = null;
  }

  function scheduleStudioActivityPoll(requestId, delay = 350) {
    stopStudioActivityPolling();
    state.qaRequestPollTimer = window.setTimeout(() => pollStudioRequestActivity(requestId), delay);
  }

  async function pollStudioRequestActivity(requestId) {
    state.qaRequestPollTimer = null;
    try {
      const record = await apiJson(`/api/agent/messages/${encodeURIComponent(requestId)}`);
      const previous = state.qaMessages.find(
        (message) => message.role === 'activity' && message.activity_id === requestId,
      );
      const previousLatest = previous?.activity?.at(-1) || {};
      const nextLatest = record?.activity?.at(-1) || {};
      const changed = !previous
        || previous.status !== record.status
        || previous.activity_revision !== record.activity_revision
        || previous.activity.length !== record.activity?.length
        || previousLatest.at !== nextLatest.at
        || previousLatest.repeat_count !== nextLatest.repeat_count;
      if (changed && upsertQAActivity(record)) {
        persistQAConversation();
        renderQA();
        renderAgentActions();
      }
      if (!terminalActivityStatus(record.status)) scheduleStudioActivityPoll(requestId, 500);
    } catch (error) {
      if (state.qaRequestId === requestId && (!error.status || error.status >= 500 || error.status === 404)) {
        scheduleStudioActivityPoll(requestId, 500);
      }
    }
  }

  async function approveSourceAction(message) {
    const authorization = message.authorization;
    if (!authorization?.id || state.qaBusy || message.resolved) return;
    state.qaBusy = true;
    renderQA();
    renderAgentActions();
    const workflowContext = message.workflowContext || state.qaPendingSourceActions[authorization.id] || null;
    try {
      const response = await apiJson(
        `/api/agent/source-actions/${encodeURIComponent(authorization.id)}/approve`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            token: authorization.token,
            client_session_nonce: state.clientSessionNonce,
          }),
        },
      );
      message.resolved = true;
      message.role = 'assistant';
      message.text = response.answer || 'Source change approved.';
      delete message.authorization;
      delete message.workflowContext;
      delete state.qaPendingSourceActions[authorization.id];
      trackStartedRun(response.run, workflowContext);
    } catch (error) {
      state.qaMessages.push({ role: 'error', text: error.message });
      markQAUnread();
      window.setTimeout(setupRuntime, 500);
    } finally {
      state.qaBusy = false;
      persistQAConversation();
      renderQA();
      renderAgentActions();
    }
  }

  async function cancelSourceAction(message) {
    const authorization = message.authorization;
    if (!authorization?.id || state.qaBusy || message.resolved) return;
    state.qaBusy = true;
    renderQA();
    const workflowContext = message.workflowContext || state.qaPendingSourceActions[authorization.id] || null;
    try {
      await apiJson(`/api/agent/source-actions/${encodeURIComponent(authorization.id)}/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token: authorization.token,
          client_session_nonce: state.clientSessionNonce,
        }),
      });
      message.resolved = true;
      message.role = 'assistant';
      message.text = 'Source change cancelled. No workflow was started.';
      updateWorkflowHistory(workflowContext, message.text);
      delete message.authorization;
      delete message.workflowContext;
      delete state.qaPendingSourceActions[authorization.id];
    } catch (error) {
      state.qaMessages.push({ role: 'error', text: error.message });
      markQAUnread();
    } finally {
      state.qaBusy = false;
      persistQAConversation();
      renderQA();
      renderAgentActions();
    }
  }

  async function sendAgentMessage(question) {
    const dashboardState = dashboardStateSnapshot();
    const relevantHistory = state.qaHistory.filter(
      (item) => item.session_id === dashboardState.source.session_id,
    );
    const requestId = newStudioRequestId();
    const requestController = new AbortController();
    state.qaBusy = true;
    state.qaRequestId = requestId;
    state.qaRequestController = requestController;
    state.qaStoppedRequestId = null;
    state.qaMessages.push({ role: 'user', text: question });
    upsertQAActivity({
      activity_id: requestId,
      request_id: requestId,
      status: 'running',
      started_at: new Date().toISOString(),
      activity: [],
    });
    persistQAConversation();
    renderQA();
    renderAgentActions();
    scrollQAConversationToBottom();
    try {
      const responsePromise = apiJson('/api/agent/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: question,
          session_id: dashboardState.source.session_id,
          turn_id: dashboardState.selection.turn_id,
          dashboard_state: dashboardState,
          history: state.qaConversationBootstrapped ? [] : relevantHistory,
          conversation_id: state.qaConversationId,
          client_session_nonce: state.clientSessionNonce,
          request_id: requestId,
        }),
        signal: requestController.signal,
      });
      scheduleStudioActivityPoll(requestId);
      const response = await responsePromise;
      if (response.request_activity) upsertQAActivity(response.request_activity);
      if (state.qaStoppedRequestId === requestId) {
        const stopped = new Error('Dashboard agent stopped.');
        stopped.name = 'AbortError';
        throw stopped;
      }
      state.qaConversationBootstrapped = true;
      const answer = response.answer || 'Agent workflow started.';
      const historyIndex = state.qaHistory.length;
      state.qaHistory.push({
        question,
        answer,
        session_id: dashboardState.source.session_id,
        turn_id: dashboardState.selection.turn_id,
      });
      const workflowContext = {
        session_id: dashboardState.source.session_id,
        turn_id: dashboardState.selection.turn_id,
        history_index: historyIndex,
      };
      if (response.kind === 'authorization') appendSourceAuthorization(response, workflowContext);
      else if (response.kind === 'audit_rule_proposal') appendAuditRuleProposal(response, workflowContext);
      else {
        state.qaMessages.push({ role: 'assistant', text: answer });
        markQAUnread();
      }
      trackStartedRun(response.run, workflowContext);
    } catch (error) {
      try {
        upsertQAActivity(await apiJson(`/api/agent/messages/${encodeURIComponent(requestId)}`));
      } catch (_activityError) {
        // The request may have failed validation before the activity record was created.
      }
      const stopped = state.qaStoppedRequestId === requestId || error.name === 'AbortError';
      state.qaMessages.push({
        role: stopped ? 'assistant' : 'error',
        text: stopped ? 'Dashboard agent stopped.' : error.message,
      });
      markQAUnread();
    } finally {
      if (state.qaRequestId === requestId) {
        state.qaRequestId = null;
        state.qaRequestController = null;
      }
      if (state.qaStoppedRequestId === requestId) state.qaStoppedRequestId = null;
      state.qaBusy = false;
      const activity = state.qaMessages.find(
        (message) => message.role === 'activity' && message.activity_id === requestId,
      );
      if (terminalActivityStatus(activity?.status)) stopStudioActivityPolling();
      else scheduleStudioActivityPoll(requestId, 500);
      persistQAConversation();
      renderQA();
      renderAgentActions();
    }
  }

  async function summarizeSessionCheckpoints({ automatic = false } = {}) {
    if (
      !runtime.interactive || !runtime.qa?.configured ||
      state.agentBusy || state.qaBusy || state.sessionBriefBusy ||
      (state.agentRun && !terminalAgentStatus(state.agentRun.status))
    ) return;
    const dashboardState = dashboardStateSnapshot();
    if (!dashboardState.source.session_id) return;
    stopSessionBriefAutoRefresh();
    state.sessionBriefBusy = true;
    // Studio owns manual requests and their Stop control; only the background
    // direct-workflow start needs the separate agent-operation busy flag.
    if (automatic) state.agentBusy = true;
    state.agentError = '';
    state.sessionBriefError = '';
    if (state.sessionBrief) state.sessionBrief = { ...state.sessionBrief, status: 'updating' };
    renderSessionBrief();
    renderAgentActions();
    try {
      if (!automatic) {
        openQAConversation();
        await sendAgentMessage('Update the session-wide checkpoint brief from the current trace.');
        // Restore the persisted summary state even when the conversation request
        // fails or the controller answers without starting a checkpoint workflow.
        await refreshSessionBrief();
        return;
      }
      const run = await apiJson('/api/agent/checkpoints', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dashboard_state: dashboardState }),
      });
      trackStartedRun(run);
    } catch (error) {
      state.agentError = error.message;
      state.sessionBriefError = automatic ? `Live summary update paused: ${error.message}` : error.message;
      if (state.sessionBrief) {
        state.sessionBrief = { ...state.sessionBrief, status: state.sessionBrief.stale ? 'out_of_date' : 'current' };
      }
    } finally {
      if (automatic) state.agentBusy = false;
      state.sessionBriefBusy = false;
      renderSessionBrief();
      renderAgentActions();
    }
  }

  function setupControls() {
    document.getElementById('reset-layout').addEventListener('click', () => resetFloatingPanelLayout());
    document.getElementById('live-audit-start').addEventListener('click', startLiveMonitorAndAudit);
    document.getElementById('assurance-replay').addEventListener('click', replayAssurance);
    document.getElementById('assurance-manage').addEventListener('click', openAuditRulesDialog);
    document.getElementById('assurance-toggle').addEventListener('click', () => {
      state.assuranceCollapsed = !state.assuranceCollapsed;
      persistAssuranceCollapsed();
      renderAssurance();
    });
    document.getElementById('audit-rules-close').addEventListener('click', () => {
      document.getElementById('audit-rules-dialog').close();
    });
    document.getElementById('audit-rule-edit').addEventListener('click', beginAuditRuleEdit);
    document.getElementById('audit-rule-cancel-edit').addEventListener('click', cancelAuditRuleEdit);
    document.getElementById('audit-rule-type').addEventListener('change', updateAuditRuleTypeFields);
    document.getElementById('audit-rule-action-enabled').addEventListener('change', updateAuditRuleActionFields);
    document.getElementById('audit-rule-form').addEventListener('submit', (event) => {
      event.preventDefault();
      saveAuditRule();
    });
    document.getElementById('audit-rule-archive').addEventListener('click', archiveAuditRule);
    document.getElementById('trace-session-select').addEventListener('change', (event) => {
      pauseLiveFollow();
      state.traceSessionId = event.target.value;
      resetAgentFocus({ checkpoint: true });
      state.traceTurnId = null;
      state.traceEventSequence = null;
      state.traceCategory = 'all';
      state.traceTool = '';
      state.sessionBrief = null;
      state.sessionBriefError = '';
      stopSessionBriefAutoRefresh();
      state.qaHistory = [];
      state.qaMessages = [];
      state.qaWorkflowRuns = {};
      state.qaPendingSourceActions = {};
      state.qaActivity = null;
      state.qaUnreadCount = 0;
      state.qaConversationBootstrapped = false;
      clearStoredQAConversation();
      document.getElementById('trace-search').value = '';
      renderDashboard();
      refreshSessionBrief();
      refreshAuditRules();
    });
    let searchTimer = null;
    document.getElementById('trace-search').addEventListener('input', () => {
      pauseLiveFollow();
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        resetAgentFocus();
        state.traceEventSequence = null;
        renderTrace();
        renderHighlightedContext();
      }, 100);
    });
    document.getElementById('session-id-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const sessionId = document.getElementById('session-id').value.trim();
      if (sessionId) loadSessionId(sessionId);
    });
    document.getElementById('path-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const path = document.getElementById('session-path').value.trim();
      if (path) loadPath(path);
    });
    const upload = document.getElementById('session-upload');
    document.getElementById('upload-button').addEventListener('click', () => upload.click());
    upload.addEventListener('change', () => {
      if (upload.files?.length) uploadFiles(upload.files);
      upload.value = '';
    });
    document.getElementById('follow-live').addEventListener('change', (event) => {
      state.followLive = event.target.checked;
      state.liveNewEvents = 0;
      const scroll = traceScrollSnapshot();
      if (state.followLive) selectLatestLiveEvent();
      renderDashboard();
      restoreTraceScroll(scroll, state.followLive);
    });
    document.getElementById('qa-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const input = document.getElementById('qa-question');
      const question = input.value.trim();
      if (!question || state.qaBusy || !runtime.qa?.configured) return;
      input.value = '';
      sendAgentMessage(question);
    });
    document.getElementById('qa-question').addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        document.getElementById('qa-form').requestSubmit();
      }
    });
    document.getElementById('qa-highlight-clear').addEventListener('click', clearAskContext);
    document.getElementById('qa-conversation-toggle').addEventListener('click', () => {
      state.qaConversationCollapsed = !state.qaConversationCollapsed;
      if (!state.qaConversationCollapsed) state.qaUnreadCount = 0;
      persistQAConversationCollapsed();
      renderQAConversationChrome();
      window.requestAnimationFrame(positionQAConversationNearButton);
    });
    document.getElementById('qa-conversation-close').addEventListener('click', () => closeQAConversation());
    document.getElementById('qa-new-conversation').addEventListener('click', startNewStudioConversation);
    document.getElementById('qa-stop').addEventListener('click', stopStudioAgent);
    document.getElementById('studio-context-menu-ask').addEventListener('click', askAgentFromContextMenu);
    document.addEventListener('contextmenu', handleSelectionContextMenu);
    document.addEventListener('pointerdown', (event) => {
      const menu = document.getElementById('studio-context-menu');
      if (!menu.hidden && !menu.contains(event.target)) closeStudioContextMenu();
    });
    window.addEventListener('blur', closeStudioContextMenu);
    window.addEventListener('resize', closeStudioContextMenu);
    window.addEventListener('scroll', closeStudioContextMenu, true);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !document.getElementById('studio-context-menu').hidden) {
        event.preventDefault();
        closeStudioContextMenu();
        return;
      }
      if (
        event.key === 'Escape' &&
        state.qaConversationOpen &&
        !document.getElementById('qa-config-dialog').open &&
        !document.getElementById('source-dialog').open
      ) {
        closeQAConversation();
      }
    });
    document.getElementById('agent-harness-select').addEventListener('change', (event) => {
      selectAgentHarness(event.target.value);
    });
    document.getElementById('session-brief-button').addEventListener('click', () => summarizeSessionCheckpoints());
    document.getElementById('session-brief-toggle').addEventListener('click', () => {
      state.sessionBriefCollapsed = !state.sessionBriefCollapsed;
      persistSessionBriefCollapsed();
      renderSessionBrief();
    });
    document.getElementById('session-brief-auto').addEventListener('change', (event) => {
      state.sessionBriefAutoUpdate = event.target.checked;
      persistSessionBriefAutoUpdate();
      if (state.sessionBriefAutoUpdate) {
        refreshSessionBrief();
      } else {
        stopSessionBriefAutoRefresh();
      }
      renderSessionBrief();
    });
    document.getElementById('agent-run-cancel').addEventListener('click', cancelAgentWorkflow);
    document.getElementById('agent-run-activate').addEventListener('click', () => actOnAgentRun('activate'));
    document.getElementById('agent-run-continue').addEventListener('click', () => actOnAgentRun('continue'));
    document.getElementById('agent-run-restart').addEventListener('click', () => actOnAgentRun('restart'));
    document.getElementById('agent-run-discard').addEventListener('click', () => actOnAgentRun('discard'));
    const configDialog = document.getElementById('qa-config-dialog');
    document.getElementById('qa-configure-button').addEventListener('click', openQAConfig);
    document.getElementById('agent-run-configure-api').addEventListener('click', openQAConfig);
    document.getElementById('qa-config-cancel').addEventListener('click', () => configDialog.close());
    document.getElementById('qa-api-provider').addEventListener('change', (event) => {
      const definition = providerDefinition(event.target.value);
      document.getElementById('qa-api-key').value = '';
      document.getElementById('qa-vault-password').value = '';
      document.getElementById('qa-vault-password-confirm').value = '';
      populateModelOptions(event.target.value, definition?.default_model || '');
      document.getElementById('qa-api-base-url').value = definition?.default_base_url || '';
      refreshAPIKeyRequirement();
    });
    document.getElementById('qa-api-key').addEventListener('input', refreshAPIKeyRequirement);
    document.getElementById('qa-remember-key').addEventListener('change', refreshAPIKeyRequirement);
    document.getElementById('qa-api-model-preset').addEventListener('change', (event) => {
      const custom = document.getElementById('qa-api-model');
      const usesCustomModel = event.target.value === '__custom__';
      custom.hidden = !usesCustomModel;
      custom.required = usesCustomModel;
      if (usesCustomModel) custom.focus();
    });
    document.getElementById('qa-config-form').addEventListener('submit', (event) => {
      event.preventDefault();
      if (!state.qaConfigBusy) saveQAConfig();
    });
    document.getElementById('qa-clear-key').addEventListener('click', () => {
      if (!state.qaConfigBusy && (runtime.qa?.configured || runtime.qa?.remembered)) clearQAKey();
    });
    configDialog.addEventListener('close', () => {
      document.getElementById('qa-api-key').value = '';
      document.getElementById('qa-vault-password').value = '';
      document.getElementById('qa-vault-password-confirm').value = '';
      setQAConfigFeedback('');
    });
  }

  async function setupRuntime() {
    if (state.runtimeRetryTimer) window.clearTimeout(state.runtimeRetryTimer);
    state.runtimeRetryTimer = null;
    if (!['http:', 'https:'].includes(window.location.protocol)) {
      setSourceBusy(false);
      renderDashboard();
      return;
    }
    try {
      const status = await apiJson('/api/status');
      runtime = {
        interactive: true,
        qa: status.qa || {},
        agent: status.agent || { available: false },
        live: status.live || { enabled: false, state: 'disabled', revision: 0 },
        deployment: status.deployment || { supervised: false, generation: '' },
        audit_rules: status.audit_rules || { available: false, revision: 0, active_count: 0 },
      };
      state.agentError = '';
      const embeddedSources = new Set(data.traces.map((trace) => trace.session_file));
      const serverSources = status.source_paths || [];
      const sourcesDiffer =
        serverSources.length !== embeddedSources.size || serverSources.some((source) => !embeddedSources.has(source));
      const serverRevision = Number(runtime.live?.revision) || 0;
      if (sourcesDiffer || serverRevision > state.liveRevision) {
        const payload = await apiJson('/api/payload');
        if (runtime.live?.enabled) applyLivePayload(payload);
        else applyPayload(payload);
      }
      state.liveRevision = Math.max(state.liveRevision, serverRevision);
      state.deploymentGeneration = String(runtime.deployment?.generation || '');
      scheduleDeploymentPoll();
      setupLiveStream();
      await refreshSessionBrief();
      await refreshAuditRules();
      const runId = runtime.agent?.active_run_id || runtime.agent?.latest_run?.run_id;
      if (runId) await pollAgentRun(runId);
    } catch (error) {
      runtime = {
        interactive: false,
        qa: {
          configured: false,
          remembered: false,
          credential_store_available: false,
          native_credential_store_available: false,
          credential_store: 'system credential store',
          credential_mode: 'memory',
          save_credential_mode: 'memory',
          credential_locked: false,
          vault_password_required: false,
          vault_password_from_environment: false,
          model: '',
          base_url: '',
          provider: '',
          provider_id: 'openai',
          providers: [],
        },
        agent: {
          available: false,
          active_run_id: null,
          latest_run: null,
          harness_id: 'opencode',
          harnesses: [],
        },
        live: {
          enabled: false,
          state: 'disabled',
          revision: 0,
          monitored_files: 0,
          sources: [],
        },
        deployment: {
          supervised: false,
          generation: '',
        },
        audit_rules: {
          available: false,
          revision: 0,
          active_count: 0,
        },
      };
      closeLiveStream();
      setSourceStatus(error.message, true);
      state.runtimeRetryTimer = window.setTimeout(setupRuntime, 2000);
    }
    setSourceBusy(false);
    renderDashboard();
  }

  setupStudioSurfaces();
  setupTraceResizing();
  setupQAFloatingDock();
  setupControls();
  setSourceBusy(false);
  renderDashboard();
  setupRuntime().finally(setupFloatingPanels);
  window.addEventListener('beforeunload', () => {
    persistQAConversation();
    closeLiveStream();
    stopSessionBriefAutoRefresh();
    if (state.assuranceReplayTimer !== null) window.clearTimeout(state.assuranceReplayTimer);
    if (state.deploymentPollTimer !== null) window.clearTimeout(state.deploymentPollTimer);
  });
})();
