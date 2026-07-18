"""Content guardrails for agent-authored skills (create_skill is a default-off live-write path;
when enabled, it has no human gate; GEPA promotions go through human approval instead).

Deliberately narrow: we hard-reject only *low-false-positive* signals. We do NOT denylist shell
commands, `.env`/credential mentions, or `curl | sh`, because legitimate skills routinely contain
code, install steps, and secret-handling guidance — scanning for those would break real skills.
The residual risk (a skill is followed instructions) is covered by the README threat model: run the
agent without secrets or sensitive filesystem access in its container."""
import os
import re

# Anthropic Agent Skills frontmatter limits (docs: skill-authoring best practices).
MAX_DESCRIPTION = int(os.environ.get("SKILL_MAX_DESCRIPTION", "1024"))  # description hard cap
MAX_BODY = int(os.environ.get("SKILL_MAX_BODY", "40000"))              # ~500 lines, the recommended body ceiling

# The classic prompt-injection / instruction-override phrasing. A genuine skill (which *is*
# instructions) has no reason to tell the agent to ignore its earlier instructions — low FP.
# Match "<override-verb> … <cue> … <noun>" within a short window, so reorderings and filler words
# ("ignore all previous instructions", "forget your prior system prompt", "override the above rules")
# are all caught, while the three-part shape keeps false positives on real skills low.
_INJECTION = re.compile(
    r"(ignore|disregard|forget|override|bypass|drop)\b[\s\S]{0,40}?"
    r"(previous|prior|earlier|above|preceding|system|initial|original)\b[\s\S]{0,25}?"
    r"(instruction|prompt|message|rule|direction|guardrail|guideline|restriction)",
    re.IGNORECASE,
)
# The description is injected verbatim into the system prompt — no XML/HTML tags allowed there
# (both an Anthropic frontmatter rule and a prompt-structure-injection guard). Bodies may hold code.
_XML_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")


def scan(description: str, body: str) -> list[str]:
    """Return a list of reasons the content should be rejected (empty = looks OK)."""
    reasons = []
    if not description.strip():
        reasons.append("empty description")
    if len(description) > MAX_DESCRIPTION:
        reasons.append(f"description too long ({len(description)} > {MAX_DESCRIPTION} chars)")
    if _XML_TAG.search(description):
        reasons.append("description contains an XML/HTML tag")
    if len(body) > MAX_BODY:
        reasons.append(f"body too long ({len(body)} > {MAX_BODY} chars)")
    if not body.strip():
        reasons.append("empty body")
    if _INJECTION.search(f"{description}\n{body}"):
        reasons.append("contains an instruction-override phrase (possible prompt injection)")
    return reasons
