"""Silent fail-open lifecycle hook entrypoint for Codex and Claude Code."""

from __future__ import annotations

import json
import sys

from agent_trace_studio.live import send_live_hook_event


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict) and payload.get('hook_event_name') in {'SessionStart', 'SessionEnd'}:
            if len(sys.argv) > 1 and sys.argv[1].strip():
                payload['adapter'] = sys.argv[1].strip()
            send_live_hook_event(payload)
    except (OSError, ValueError):
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
