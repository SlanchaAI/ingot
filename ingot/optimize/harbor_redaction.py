"""Shared redaction for persisted Harbor diagnostics and exported text."""
from __future__ import annotations

import re
from collections.abc import Mapping


_HARBOR_RECEIPT_EXCERPT_LIMIT = 2000


def _receipt_secret_values(parent: Mapping[str, str]) -> tuple[str, ...]:
    """Known ambient secrets, longest first so a shorter value cannot leave a suffix behind."""
    return tuple(sorted({value for key, value in parent.items() if value and
                         (key.endswith(("API_KEY", "TOKEN", "SECRET", "PASSWORD")) or key == "API_KEY")},
                        key=len, reverse=True))


def _redact_harbor_receipt_output(value: object, parent: Mapping[str, str]) -> str:
    """Return a bounded Harbor diagnostic excerpt without endpoint or credential material."""
    text = str(value or "")
    for secret in _receipt_secret_values(parent):
        text = text.replace(secret, "<redacted-secret>")
    text = re.sub(r"https?://[^\s\"']+", "<redacted-url>", text, flags=re.IGNORECASE)
    text = re.sub(r"(?im)\b(authorization\s*:\s*)[^\r\n]+", r"\1<redacted-secret>", text)
    text = re.sub(r"(?i)\b(x-api-key\s*:\s*)[^\s,;\"']+", r"\1<redacted-secret>", text)
    text = re.sub(r"\b(?:sk|pk)[_-][A-Za-z0-9_-]+\b", "<redacted-secret>", text)
    text = re.sub(r"(?i)([\"']?[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'}]+",
                  r"\1<redacted-secret>", text)
    text = re.sub(r"\b(?:localhost|[A-Za-z0-9.-]+):[0-9]{2,5}\b", "<redacted-host>", text)
    return text[-_HARBOR_RECEIPT_EXCERPT_LIMIT:]


def _redact_persisted(value: object, parent: Mapping[str, str]) -> object:
    """Copy diagnostics for disk; runtime callers keep their original exception objects."""
    if isinstance(value, str):
        return _redact_harbor_receipt_output(value, parent)
    if isinstance(value, Mapping):
        return {key: _redact_persisted(item, parent) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_persisted(item, parent) for item in value]
    return value
