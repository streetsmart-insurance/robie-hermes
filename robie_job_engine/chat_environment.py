"""Fail-closed Chat ingress routing; mirrors the Pub/Sub environment contract."""
from __future__ import annotations

import re
from typing import Any

_MARKER = re.compile(r'^(?:\[\[robie-test\]\]|robie-test:)(?=\s|$)\s*', re.I)
_MENTION = re.compile(r'^(?:<users/[^>]+>|@robie)\s*', re.I)


def strip_test_marker(text: str) -> tuple[str, bool]:
    text = str(text or '').lstrip()
    # Chat argumentText removes mentions; raw Workspace events may retain them.
    prefix = _MENTION.match(text)
    body = text[prefix.end():] if prefix else text
    match = _MARKER.match(body)
    if not match:
        return text, False
    return body[match.end():].lstrip(), True


def route_message(message: dict[str, Any], attributes: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Return prod/test/invalid and a copy with routing tokens removed.

    Explicit attributes and markers must agree. Message parameters cannot
    select an environment. Never mutate the original transport payload.
    """
    attrs = attributes or {}
    stamp = attrs.get('robie_env')
    if stamp is not None and stamp not in {'prod', 'test'}:
        return 'invalid', message
    clean = dict(message)
    marked = False
    for key in ('text', 'argumentText'):
        if key in message:
            clean[key], found = strip_test_marker(message[key])
            marked = marked or found
    if marked and stamp == 'prod':
        return 'invalid', message
    return ('test' if marked else stamp or 'prod'), clean
