"""Bounded, public details for host-executed Studio context calls."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

_OUTPUT_LIMIT = 4_000
_ARGUMENTS = {
    'inspect_current_context': (),
    'list_dashboard_resources': (),
    'read_dashboard_resource': ('resource', 'resource_id', 'limit'),
    'search_trace': ('query', 'turn_id', 'category', 'tool_name', 'limit'),
    'read_trace_turn': ('turn_id', 'around_sequence', 'limit'),
}
_STATUSES = frozenset({'running', 'completed', 'failed', 'cancelled'})
_PRIVATE_KEY = re.compile(
    r'(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|'
    r'credential|encrypted[_-]?(?:content|reasoning)|raw[_-]?(?:journal|record|rows?))'
)
_PRIVATE_VALUE = re.compile(
    r'(?i)(?<![\w-])(["\']?(?:[\w-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|'
    r'secret|credential|encrypted[_-]?(?:content|reasoning))[\w-]*)["\']?\s*[:=]\s*)'
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s,;}\]]+)'
)
_PATH = re.compile(
    r'(?:file://|(?<![:/\w])/(?!/)|~/|[A-Za-z]:\\)[^\s"\'<>]*'
    r'|(?<![\w.])(?:rollout[-\w.]*|[-\w.]+)\.jsonl\b'
)
_TOKEN = re.compile(r'\b(?:sk|sess)-[A-Za-z0-9_-]{10,}|\beyJ[\w-]+\.[\w-]+(?:\.[\w-]+)?')
_AUTH = re.compile(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/_.=-]+')
_PEM = re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)', re.DOTALL)
_JOURNAL_TYPES = frozenset({'session_meta', 'turn_context', 'event_msg', 'response_item'})


def _public_text(text: str, private_values: tuple[str, ...] = ()) -> str:
    if re.search(r'"type"\s*:\s*"(?:session_meta|turn_context|event_msg|response_item)"', text) and re.search(
        r'"payload"\s*:', text
    ):
        return '[raw journal record omitted]'
    if re.search(r'["\']encrypted[_-]?(?:content|reasoning)["\']\s*:', text, re.IGNORECASE):
        return '[encrypted content omitted]'
    if re.search(r'["\']raw[_-]?(?:journal|record|rows?)["\']\s*:', text, re.IGNORECASE):
        return '[raw journal record omitted]'
    for value in private_values:
        if value:
            text = text.replace(value, '[redacted]')
    text = _PEM.sub('[private key omitted]', text)
    text = _AUTH.sub('"[redacted]"', text)
    text = _PRIVATE_VALUE.sub(lambda match: f'{match[1]}"[redacted]"', text)
    text = _TOKEN.sub('[redacted]', text)
    return _PATH.sub('[local path omitted]', text)


def _public_json(value: object, private_values: tuple[str, ...], depth: int = 0) -> object:
    if depth > 12:
        return '[nested content omitted]'
    if isinstance(value, dict):
        if isinstance(value.get('type'), str) and value['type'] in _JOURNAL_TYPES and 'payload' in value:
            return '[raw journal record omitted]'
        return {
            _public_text(str(key), private_values): (
                '[omitted]' if _PRIVATE_KEY.search(str(key)) else _public_json(item, private_values, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_public_json(item, private_values, depth + 1) for item in value]
    return _public_text(value, private_values) if isinstance(value, str) else value


def _public_output(text: str, private_values: tuple[str, ...]) -> str:
    # Resource results have a short host heading followed by JSON. Treat nested
    # raw records and private fields structurally before formatting the preview.
    for start in (0, *(match.end() for match in re.finditer(r'\n(?=[{\[])', text))):
        try:
            value = json.loads(text[start:])
        except (ValueError, RecursionError):
            continue
        return _public_text(text[:start], private_values) + json.dumps(
            _public_json(value, private_values), ensure_ascii=False, indent=2
        )
    # An incomplete JSON block or PEM can span many lines, including headings
    # between trace anchors. Check the whole block before considering each line.
    text = _public_text(text, private_values)
    lines = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            lines.append(_public_text(line, private_values))
        else:
            lines.append(json.dumps(_public_json(value, private_values), ensure_ascii=False))
    return '\n'.join(lines)


def normalize_activity_details(value: object, *, private_values: tuple[str, ...] = ()) -> dict[str, object] | None:
    """Allow only context-call metadata and a redacted preview into Activity."""

    if not isinstance(value, Mapping) or value.get('kind') != 'context_action':
        return None
    tool = value.get('tool')
    identifier = value.get('id')
    status = value.get('status')
    if (
        not isinstance(tool, str)
        or tool not in _ARGUMENTS
        or not isinstance(identifier, str)
        or re.fullmatch(r'[A-Za-z0-9_-]{1,96}', identifier) is None
        or not isinstance(status, str)
        or status not in _STATUSES
    ):
        return None
    raw_arguments = value.get('arguments')
    raw_arguments = raw_arguments if isinstance(raw_arguments, Mapping) else {}
    arguments: dict[str, str | int] = {}
    arguments_truncated = bool(value.get('arguments_truncated'))
    redacted = bool(value.get('redacted'))
    for key in _ARGUMENTS[tool]:
        item = raw_arguments.get(key)
        if isinstance(item, str):
            clean = _public_text(item, private_values)
            redacted = redacted or clean != item
            arguments_truncated = arguments_truncated or len(clean) > 500
            arguments[key] = clean[:500]
        elif isinstance(item, int) and not isinstance(item, bool) and 0 < item <= 1_000_000:
            arguments[key] = item
    raw_output = value.get('output')
    raw_output = raw_output if isinstance(raw_output, str) else ''
    output = _public_output(raw_output, private_values)
    redacted = redacted or any(
        marker in output
        for marker in (
            '[redacted]',
            '[omitted]',
            '[local path omitted]',
            '[raw journal record omitted]',
            '[encrypted',
            '[private key omitted]',
            '[nested content omitted]',
        )
    )
    result: dict[str, object] = {
        'kind': 'context_action',
        'id': identifier,
        'tool': tool,
        'status': value['status'],
        'arguments': arguments,
        'arguments_truncated': arguments_truncated,
        'output': output[:_OUTPUT_LIMIT],
        'output_chars': _nonnegative_int(value.get('output_chars'), default=len(raw_output)),
        'preview_truncated': bool(value.get('preview_truncated')) or len(output) > _OUTPUT_LIMIT,
        'context_truncated': bool(value.get('context_truncated')),
        'redacted': redacted,
    }
    duration = value.get('duration_ms')
    if isinstance(duration, int | float) and not isinstance(duration, bool) and 0 <= duration <= 86_400_000:
        result['duration_ms'] = round(duration)
    return result


def _nonnegative_int(value: object, *, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default
