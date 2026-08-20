"""A SKILL.md parser that reports what is wrong instead of repairing it.

`ingot.mcp_server.registry.parse_skill` is deliberately tolerant: absent frontmatter, unparseable YAML,
and a frontmatter that is not a mapping all normalize to empty metadata so the server keeps
serving. That is correct for serving and useless for diagnosis -- silent normalization is not a
diagnostic API. This parser answers the same question in the opposite direction: it keeps the
malformed input and returns findings.

The two must agree on the shape of a well-formed document, so the frontmatter pattern is imported
from the serving parser rather than restated here."""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from ingot.mcp_server.registry import _FRONTMATTER

ERROR = "error"
WARNING = "warning"
INFO = "info"


@dataclass(frozen=True)
class Finding:
    code: str
    level: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict:
        found = {"code": self.code, "level": self.level, "message": self.message}
        if self.path is not None:
            found["path"] = self.path
        return found


@dataclass
class RawSkill:
    """`frontmatter` is None when the document has none that can be read as a mapping. That is the
    distinction the serving parser erases, and every caller here depends on it."""
    frontmatter: dict | None
    body: str
    findings: list[Finding] = field(default_factory=list)


def parse_raw(text: str) -> RawSkill:
    match = _FRONTMATTER.match(text)
    if not match:
        return RawSkill(None, text.strip(), [Finding(
            "frontmatter-missing", ERROR,
            "no YAML frontmatter: a SKILL.md must open with a '---' delimited block")])

    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as error:
        detail = str(error).replace("\n", " ").strip()
        return RawSkill(None, match.group(2).strip(), [Finding(
            "frontmatter-invalid", ERROR, f"frontmatter is not valid YAML: {detail}")])

    if not isinstance(loaded, dict):
        kind = type(loaded).__name__ if loaded is not None else "nothing"
        return RawSkill(None, match.group(2).strip(), [Finding(
            "frontmatter-not-a-mapping", ERROR,
            f"frontmatter must be a mapping of fields, found {kind}")])

    return RawSkill(loaded, match.group(2).strip(), [])
