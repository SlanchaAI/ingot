# SSO + RBAC for the shared change-control UI — design

Status: proposed (with the decision-independent RBAC core landed) · Scope: UI authentication +
authorization for a shared/enterprise deployment.

> **Already on this branch:** the provider-agnostic authorization core — `ui/rbac.py` (roles,
> claim→role mapping, `authorize`, and OIDC-claims→identity parsing) with unit tests
> (`tests/test_rbac.py`). It needs no IdP and no decision below, so it is built and tested now. The
> OIDC browser flow, provider integration, and endpoint wiring remain deferred pending the open
> decisions.

Follow-up to the minimal LAN password auth (PR #24). That work made approvals *attributable* — the
authenticated user is written as the audit `actor` on promote/reject/rollback. This spec takes the
next step for a shared, enterprise deployment: real SSO (OIDC) and role-based authorization, while
keeping the local/LAN paths unchanged.

## Goals

- Let people sign in with their **corporate identity** (no per-user passwords to manage).
- Gate the state-changing actions by **role** (who may propose vs approve vs promote vs administer).
- Keep the existing **attributable audit trail** — the SSO identity becomes the `actor`.
- **Do not** regress the zero-config local default or the LAN password mode; SSO is an opt-in
  deployment profile layered on top.

## Non-goals (explicitly deferred)

- **Machine / agent auth to the MCP server.** Agents can't do an interactive OIDC browser flow;
  they need service credentials (client-credentials or API tokens). Separate spec.
- **SCIM user/group provisioning.** Nice for large orgs; not needed for a first cut.
- **Fine-grained per-skill / per-namespace ACLs.** Roles are global here; team namespaces are a
  later item.

## Provider decision

We authenticate against **protocols, not vendors**: Entra, Okta, Google Workspace, Ping, OneLogin,
and Cognito all speak **OIDC**. One generic OIDC implementation supports all of them, so "which
providers" is really **which we test and document against**.

- **Certify Microsoft Entra ID + Okta.** Entra is the dominant enterprise workforce IdP; Okta is the
  standard independent one. Together they cover the large majority of enterprise buyers.
- **Generic OIDC underneath** so Google Workspace / Ping / OneLogin / etc. work without new code.
- **Amazon Cognito is intentionally not a certified target.** Cognito is AWS's CIAM / user-pool
  service (customer identity, or an IdP broker) — enterprises don't use it for *workforce* SSO. It
  only becomes relevant if the product pivots from self-hosted to a **hosted multi-tenant SaaS**,
  in which case the right move is a single broker (Cognito, Auth0, or **WorkOS**) that federates to
  each customer's IdP — see "Deployment model" below.
- **SAML** is on the roadmap (some large/old enterprises are SAML-only and it appears in RFPs) but
  out of scope for phase 1; OIDC-first.

### Deployment model changes the answer

- **Self-hosted** (customer runs Ingot on their LAN/cloud — the OSS/enterprise-self-hostable path):
  the customer brings *their* IdP. Support the customer's IdP directly → Entra + Okta + generic
  OIDC. This is the assumed model here.
- **Hosted SaaS** (we run it multi-tenant): integrate *one* broker (WorkOS / Auth0 / Cognito
  federation) once, and it federates to each customer's IdP. If the product goes this way, replace
  "certify two IdPs" with "integrate one broker."

## Design

### Authentication (OIDC Authorization Code + PKCE)

Extend `ui/auth.py` with an OIDC mode selected by config (below). When enabled:

1. Unauthenticated request → redirect to the provider's authorize endpoint (auth-code + PKCE).
2. `/auth/callback` validates the returned ID token against the provider JWKS (issuer, audience,
   expiry, nonce), then establishes a **signed session cookie** (Starlette `SessionMiddleware` /
   `itsdangerous`). Store `{sub, email, name, roles}` in the session.
3. `/auth/logout` clears the session.
4. The existing `current_actor` dependency returns the session's `email`/`sub` instead of the Basic
   username, so the audit `actor` path is unchanged.

Library: **authlib** (OIDC discovery + JWKS handled for us). The three modes coexist and are chosen
by config precedence: **OIDC → LAN password (`AUTH_USER`/users file) → open**.

### Authorization (RBAC)

Roles, least- to most-privileged:

| role | may |
|------|-----|
| `viewer` | read the board, pending evidence, history |
| `proposer` | trigger candidate generation (`/api/optimize`) |
| `approver` | promote, reject, rollback |
| `admin` | the above + change config / manage mappings |

- The user's roles come from provider claims mapped to app roles:
  - **Entra: use *app roles*, not groups.** App roles are always present in the token; groups hit
    the ">200 groups overage" problem where Entra omits them and forces a Microsoft Graph call.
  - **Okta / others: groups claim** → app role via a configured map.
- Enforcement: a small `require_role(role)` dependency on the state-changing endpoints
  (`/api/promote`, `/api/reject`, `/api/rollback` → `approver`; `/api/optimize` → `proposer`).
  Read endpoints require only a valid session.
- Default-deny: a session with no mapped role is `viewer`.

### Config (per provider, env-driven, compose-wired)

```
AUTH_MODE=oidc                         # oidc | password | open  (default: password behavior from #24)
OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0   # or the Okta issuer URL
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...                 # (or PKCE-only public client)
OIDC_REDIRECT_URL=https://ingot.corp.example/auth/callback
OIDC_ROLE_CLAIM=roles                  # Entra app roles; "groups" for Okta
OIDC_ROLE_MAP=ingot-approver:approver,ingot-admin:admin   # provider value -> app role
SESSION_SECRET=...                     # signs the session cookie
```

## Gotchas that cost time (flagged early)

1. **Entra groups overage** → use **app roles** (above). Saves a Graph integration.
2. **Redirect URI + TLS.** OIDC needs a stable HTTPS callback; this couples to the enterprise
   network profile (real host + TLS), not just the app.
3. **Machine agents ≠ SSO** (deferred, but must be said so it doesn't silently balloon scope).
4. **Clock skew / token expiry** on validation; **session lifetime** (re-login vs refresh — phase 1
   can just re-login on expiry).

## Phasing & estimate

Roughly **~1 week** of focused work for tested phase 1+2; the real tax is integration against two
live tenants, which can't be mocked.

- **Phase 1 — OIDC login (~2–3 days).** Auth-code+PKCE, session cookie, callback validation, one
  coarse role (`admin` vs everyone). Certified against Entra + Okta.
- **Phase 2 — RBAC (~1–2 days).** The four roles, claim→role mapping, `require_role` on endpoints,
  actor = SSO identity in the audit.
- **Phase 3 — SAML (later).** For SAML-only enterprises.
- Tests throughout: mock the IdP (stub discovery/JWKS/callback) for CI; manual integration against
  real Entra + Okta tenants before release.

## Open decisions (need a call before building)

1. **Self-hosted vs hosted SaaS** — determines "certify Entra + Okta" vs "integrate one broker."
2. **Entra app roles vs groups** — recommend app roles; needs the customer to define app roles in
   their tenant (a small onboarding ask).
3. **How many roles for v1** — ship all four, or start `admin`-vs-everyone and add the middle roles
   in phase 2?
4. **Where the audit trail lives at scale** — the attributed `actor` currently lands in
   `runs/approval-audit.jsonl`; a shared enterprise deployment likely wants it in a real store
   (ties to the trace-backend / observability profile decision).
