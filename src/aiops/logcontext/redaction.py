"""Remove credentials from log lines before they are stored or shown to an LLM.

Applications log secrets by accident (connection strings, Authorization headers, debug
dumps of configuration). Everything collected passes through here first; a false
positive costs a word of context, a false negative leaks a credential.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # -----BEGIN ... PRIVATE KEY----- blocks, or what is left of them on one line.
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)"),
        "[REDACTED PRIVATE KEY]",
    ),
    # Credentials embedded in URLs: scheme://user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), rf"\1{REDACTED}@"),
    # Authorization headers and bearer tokens.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]+=*"), rf"\1 {REDACTED}"),
    # JSON Web Tokens.
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), REDACTED),
    # AWS access key IDs.
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    # key=value and "key": "value" pairs whose key names a secret.
    (
        re.compile(
            r"(?i)\b([\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
            r"|private[_-]?key|credentials?|session[_-]?id|cookie)[\w.-]*)"
            r"(\"?\s*[:=]\s*\"?)([^\s\"',;&}]+)"
        ),
        rf"\1\2{REDACTED}",
    ),
)


def redact(line: str) -> str:
    for pattern, replacement in _PATTERNS:
        line = pattern.sub(replacement, line)
    return line
