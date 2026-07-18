# Human-Gated Skill Lifecycle Design

## Objective

Keep agent-authored skill creation as a default product capability while ensuring no
application-controlled path can activate a new or rewritten skill without an explicit human review
action.

## Invariants

1. `create_skill` authors a candidate; it never writes under an active skill root or reloads the
   router.
2. New skills and rewrites use the same `runs/pending/<skill>.json` review queue.
3. Only the approval UI calls the activation function in production code.
4. Optimizer CLI and canary paths may create or update pending recommendations; they never activate
   a skill.
5. Rewrites retain their existing Behavioral CI evidence gate before the human can approve them.
6. Brand-new skills require current content, naming, collision, and injection checks plus human
   content review; they do not require a behavioral evaluation suite before first activation.
7. Direct operator edits under `skills/` remain possible. The human-gate guarantee covers Ingot's
   application-controlled write paths, not filesystem administrators.

## Pending creation record

Agent creation writes the existing pending JSON format with these fields:

```json
{
  "kind": "creation",
  "skill": "low-fodmap-meal-planning",
  "champion_components": {"description": "", "body": ""},
  "challenger_components": {
    "description": "Use this skill when...",
    "body": "..."
  },
  "changed_components": ["description", "body"],
  "gate": {"promotable": true, "blocked": []},
  "source": "agent"
}
```

Empty champion components let the existing unified-diff UI render a complete file addition. A
pending creation has no Behavioral CI evidence because no champion exists yet.

## Authoring flow

`create_skill(name, description, body)` keeps its current MCP contract and all current defenses:

- normalize and validate the slug;
- reject an active skill or pending candidate with the same name;
- run content and optional model guards;
- reject routing collisions against active skills;
- save the creation record atomically under `runs/pending/`;
- return a message pointing to the approval UI.

It does not create `skills/<slug>`, reload state, or make the candidate routable.

## Review UI

`GET /api/skills` returns the union of active skills and pending-only creations. Pending creations
have `pending: true`, `creation: true`, and no optimize action. The existing pending-detail endpoint
renders their description and body as additions and labels the review as a new skill.

Reject deletes the pending record. Approve invokes the single activation boundary.

## Activation boundary

Replace the broadly callable `promote()` entry point with `approve_pending(skill)`. Production code
has one caller: `ui.app.approve`.

For `kind: creation`, approval:

1. revalidates the slug and confirms no active skill or destination directory now exists;
2. re-runs content and collision checks to catch drift since authoring;
3. builds a temporary sibling directory;
4. writes `SKILL.md` with `source: agent`;
5. atomically renames the temporary directory into the active local skill root;
6. deletes the pending record only after activation succeeds.

For existing rewrites, approval preserves current evidence validation, revision checks, snapshot,
staged swap, rollback, and pending cleanup.

Any failure leaves active skills untouched and preserves the pending record for diagnosis or retry.
The MCP server's existing filesystem refresh makes an approved creation routable on the next
request.

## Remove automated activation

- Delete the optimizer's `--promote` behavior. A successful A/B run always writes a pending record.
- Change canary success from calling promotion to recording a promote recommendation in pending
  state.
- Remove every non-UI production caller of the activation function.
- Keep direct calls in promotion unit tests only.

## Public behavior

- Remove `ENABLE_AGENT_SKILL_WRITES`; creation is available by default.
- Documentation says agent-authored skills await human approval and are not routable before it.
- Security documentation distinguishes content checks, Behavioral CI, and explicit human approval.
- The approval UI remains localhost-only and unauthenticated. “Human-gated” means an explicit UI
  approval action in normal operation, not cryptographic proof of personhood.

## Verification

Test-driven implementation must prove:

- agent creation produces pending JSON and no active directory;
- pending creations are absent from routing and visible in the UI;
- duplicate active and duplicate pending names are rejected;
- rejection never activates the skill;
- approval atomically activates a new skill and clears pending state;
- failed activation preserves pending state and leaves active roots unchanged;
- existing rewrite approval still rejects missing, blocked, stale, or mismatched evidence;
- canary and optimizer cannot activate skills;
- only the UI is a production caller of `approve_pending`;
- full Docker tests, Compose validation, GitNexus change detection, CodeScene pre-commit safeguard,
  and CodeScene branch analysis pass.
