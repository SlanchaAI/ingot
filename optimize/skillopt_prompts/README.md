# Vendored SkillOpt prompts

These `.md` files are copied **verbatim** from [microsoft/SkillOpt](https://github.com/microsoft/SkillOpt)
(`skillopt/prompts/`), which is MIT-licensed (© 2026 Microsoft Corporation). They are the prompt IP
behind the body-pass optimizer's four borrowed mechanics (reflection with a step buffer, edit-budget
clipping, autonomous learning-rate, epoch slow-update).

We vendor them because the published `skillopt==0.2.0` **wheel ships the code but not the prompt
files** (`skillopt/prompts/` is empty in the wheel), so `skillopt.prompts.load_prompt` cannot find
them at runtime. The *code* we use (gate metric, patch application, update-mode helpers, JSON
extraction) is imported from the pinned package and upgrades with a version bump; only these prompts
are copied.

| file | drives | upstream |
|------|--------|----------|
| `analyst_error.md`  | reflection → skill edits (append/insert_after/replace/delete) | `skillopt/prompts/analyst_error.md` |
| `ranking.md`        | edit-budget clipping (rank a pool, keep top-L)               | `skillopt/prompts/ranking.md` |
| `lr_autonomous.md`  | autonomous learning-rate (how many edits to apply this step) | `skillopt/prompts/lr_autonomous.md` |
| `slow_update.md`    | epoch-end slow/meta consolidation                            | `skillopt/prompts/slow_update.md` |

## Upgrading

Copied from SkillOpt commit `b860a5cf88ce` (tag v0.2.0). To sync with a newer release:

1. Bump `skillopt==<new>` in `requirements.txt` and rebuild the image.
2. Re-copy any changed prompt from `skillopt/prompts/*.md` at that tag into this directory.
3. Run `pytest tests/test_skillopt_bridge.py tests/test_skillopt_loop.py` — they assert the prompt
   files load and the JSON contracts still parse, so a breaking upstream prompt change fails loudly.

All SkillOpt integration is funnelled through `optimize/skillopt_bridge.py`; that module and this
directory are the only things to touch on an upgrade.
