# Ingot — product context

## Register

**Product.** Design serves the tool. The console is an operating surface for a decision,
not a page that sells one. Marketing surfaces for Ingot live elsewhere and use the warm
Slancha marketing register; nothing from that register belongs here.

## What it is

Ingot is evidence-gated change control for agent instructions. A skill's instructions are
content-addressed, so every edit produces a new revision. An optimizer may propose a
revision but may never activate one: proposals land quarantined, with the evidence that
was measured at generation time. A human approves, and approval snapshots the revision it
displaces so the change stays reversible.

The console at `ui/static/index.html` is where that approval happens. It is the only
surface in the system that can promote or roll back.

## Who operates it

One technical operator, at a desk, checking in when something is quarantined — not a team,
not a demo. That sets the defaults:

- **Density over onboarding.** No tours, no scaffolding, no seeded examples. The operator
  wrote the skills.
- **Theme follows the OS.** Both light and dark are first-class; neither is the "real" one.
- **Keyboard-first.** Dialogs take focus, return it, and trap Tab.

## The job

Route → measure → decide → keep the ability to go back. The console owns the last two.
Its entire reason to exist is that a promotion is irreversible in effect (the router
starts serving the new instructions immediately) and must therefore be reversible in
record.

## Strategic design principles

1. **The decision surface carries every reason to hesitate.** Any caution shown on the
   review card must survive into the confirmation dialog. A dialog that shows only the
   winning numbers is how noise gets promoted.
2. **State effect size in the judge's own units.** The judge scores on a discrete ladder.
   A mean that moves less than one rung is not a result, and the interface must say so
   rather than render it as a green delta. Re-running an unchanged arm has moved a mean
   by exactly that much.
3. **Never report a blocked state without the step that unblocks it.** The library
   arrives with no eval task sets, so every action is disabled. The command that fixes
   that belongs where the state is reported, not in a tooltip on a disabled button.
4. **Zero is a state, not an error.** Empty, no-result, and not-yet-possible are three
   different things and read differently.
5. **Reversibility is the feature.** History and the audit trail are not an appendix.

## Anti-references

- **Dashboards that report without exposing their inputs.** A number no one can trace is
  worse than no number.
- **Confirmation dialogs that only restate the happy path.** The reason this product
  exists is the case where the happy path is wrong.
- **The hero-metric template.** Big number, small label, gradient accent. The KPI strip
  reports counts because counts are the state; it must never grow into a marketing block.
- **Marketing warmth.** Cream, paper, display serif. Those belong to the other register.

## Accessibility

WCAG AA throughout. Body text ≥4.5:1, large text ≥3:1, visible focus on every interactive
element, dialogs operable by keyboard alone. The mono metadata style is where this is
easiest to lose — small, uppercase, low-contrast — and it is held to the same bar.
