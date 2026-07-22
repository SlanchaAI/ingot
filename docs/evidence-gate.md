# The evidence gate (anti reward-hacking)

A generated change is only as trustworthy as the evidence attached to it, and optimizing against an
LLM judge invites the classic failure: the challenger learns to please the judge, not to get better.
The gate is what a reviewer is relying on when they read those numbers, so it closes the obvious
paths:

1. **Separate judge and author models.** Configure the authoring LM (`SKILLOPT_MODEL`) and judge
   (`JUDGE_MODEL`) as different models. The process warns when they match, but this configuration
   mistake is not currently a hard promotion block. Set `JUDGE_MODELS=a,b` for an ensemble judge.
2. **Held-out gate, not a lucky mean.** Promotion requires a margin (`PROMOTE_MIN_MARGIN`, default
   +0.15), enough samples (`PROMOTE_MIN_SAMPLES`), and no catastrophic per-task regression.
3. **No routing hacks.** A rewritten `description` needs an explicit routing suite. It is checked
   against every other skill, and a rewrite that shadows another skill (cosine ≥
   `COLLISION_SCORE`) is blocked. The challenger must keep recall@3 and no-route precision at or
   above 0.95, avoid regressing top-1 accuracy, recall@3, or no-route precision, and preserve full
   cross-harness parity for exercised cases.
4. **Execution-grounded judging, sandboxed by default.** For code tasks, `execcheck.py` parses the
   code and hands the judge a verdict it must treat as ground truth. By default
   (`EXEC_SANDBOX=docker`) the code also runs in a throwaway locked-down container: no network, no
   mounts, read-only rootfs, `nobody` user, all capabilities dropped, memory/pid/cpu limits. If
   docker is unreachable the check fails closed to inconclusive; there is never a silent fallback
   to unsandboxed execution. `SANDBOX_RUNTIME=runsc` swaps in [gVisor](https://gvisor.dev);
   `EXEC_SANDBOX=1` is the legacy bare-subprocess mode; `EXEC_SANDBOX=off` disables execution. A
   task can also ship a `check:` spec (fixture + assert) in its task YAML for artifact-verified
   execution; a broken fixture counts as inconclusive, never against the answer.
5. **Acceptance criteria.** A skill's task YAML can declare `acceptance:` `forbid` regexes,
   deterministic invariants checked against the challenger's held-out answers (e.g. a Tailwind v4
   skill must never emit the v3 `@tailwind base/components/utilities` directives), grounding the
   judge with a check
   it cannot be talked out of. They're also fed into the SkillOpt inner loop as a training signal so
   it *removes* forbidden content rather than appending around it. The gate is graded: a forbidden
   pattern in more than `PROMOTE_ACCEPT_BLOCK_RATE` of the answers (default 0.5) blocks; a minority
   is a ⚠ warning a human weighs, so a large win isn't auto-killed by one residual model slip.
6. **Length penalty.** The objective subtracts a penalty for a bloated body.
7. **Deletions need evidence.** A challenger that drops most of the champion body (retention below
   `RETENTION_WARN`) gets a warning with the retention number and sample count. The approval panel
   also shows added lines, removed lines, percent of body changed, body-size change, and a collapsed
   side-by-side comparison before the reviewer can approve it.
8. **Blocked means blocked.** A challenger that wins the mean but fails the gate is still recorded
   for diagnosis, but the UI refuses approval and shows the exact reasons, and `approve_pending`
   refuses it again server-side.
9. **Evidence must still describe the skill on disk.** Promotion recomputes the champion and
   challenger revisions; if the champion changed since the run, approval is refused rather than
   applied to a skill the evidence never measured.
10. **Human decisions stay attributable.** Password and OIDC deployments surface the signed-in
    user. Open mode has no signed-in identity. Rejection requires confirmation and can store an
    optional reason in the metadata-only audit.

```
[ab] champion 0.55 vs challenger 0.60 -> CHALLENGER WINS
[ab] ⛔ challenger won the mean but the promotion gate BLOCKED it:
     margin +0.10 < required +0.15; catastrophic regression on 1 task(s) the champion passed
```
