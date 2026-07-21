# Sign in with Google (SSO + roles)

For a shared or company-wide deployment, Ingot can put the change-control UI behind **Sign in with
Google**, restrict access to your **Google Workspace domain**, and assign **roles** so that who may
propose, approve, and roll back is enforced (and recorded in the audit trail as the person's email).

This is an opt-in deployment profile. The zero-config local default and the LAN password mode
([Configuration](configuration.md), [Security](security.md)) are unchanged; you turn this on with
`AUTH_MODE=oidc`.

## What you get

- **Google login** for the UI (OIDC Authorization Code + PKCE). No passwords for Ingot to store.
- **Domain-restricted access.** Only accounts in the Workspace domain(s) you list may sign in; a
  personal `@gmail.com` account has no hosted-domain and is refused.
- **Four roles**, assigned by email, gating the state-changing actions:

  | role | may |
  |------|-----|
  | `viewer` | read the board, pending evidence, history |
  | `proposer` | trigger candidate generation (`/api/optimize/{skill}`) |
  | `approver` | promote, reject, roll back |
  | `admin` | the above, plus configuration |

  Roles are hierarchical (an `admin` satisfies every lower requirement). A signed-in domain member
  who is not in the role map is a `viewer`: they can look, not change.
- **Attributable audit trail.** The signed-in email becomes the `actor` on every promote / reject /
  rollback, exactly as the LAN password mode does.

## Setup

### 1. Create a Google OAuth client

In the [Google Cloud console](https://console.cloud.google.com/) for your Workspace org: **APIs &
Services -> Credentials -> Create credentials -> OAuth client ID -> Web application**. Add your
callback as an **Authorized redirect URI**:

- production: `https://ingot.your-company.example/auth/callback`
- local testing: `http://localhost:8080/auth/callback`

Copy the **Client ID** and **Client secret**.

### 2. Give the box a DNS name (LAN / on-prem)

Google only accepts redirect URIs that are `https://` on a hostname (never a raw IP), and the
hostname's domain must end in a real public TLD, so `.local` / `.lan` / `.internal` names are
rejected. That is the whole constraint: the name does **not** need to be publicly resolvable and
the certificate does **not** need to be publicly trusted, because Google never connects to it —
only the signed-in user's browser follows the redirect. (The LAN password mode has none of these
constraints.)

So, for a box on a LAN:

1. **Pick a name under a domain you own**, e.g. `ingot.corp.example`.
2. **Make it resolve to the box's LAN IP for your teammates.** Either an A record on your internal
   DNS (router, Pi-hole, corporate DNS): `ingot.corp.example -> 192.168.x.x`, or, for a quick
   trial, a hosts-file line on each machine (`/etc/hosts`, or on Windows
   `C:\Windows\System32\drivers\etc\hosts`):

   ```
   192.168.x.x  ingot.corp.example
   ```

3. **TLS is already handled.** The `lan` compose profile's front door
   ([Security: Network exposure](security.md#network-exposure)) mints a certificate for any
   hostname on demand from its local CA — nothing to configure. Browsers warn once about the
   unknown CA; proceed past it or trust the CA root to remove the warning.

Use `https://ingot.corp.example/auth/callback` both as the Google client's authorized redirect URI
(step 1) and as `OIDC_REDIRECT_URL` (step 3).

### 3. Configure Ingot

Set these in `.env` (they are passed through by `docker-compose.yml`):

```bash
AUTH_MODE=oidc
OIDC_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
OIDC_CLIENT_SECRET=...
OIDC_REDIRECT_URL=https://ingot.your-company.example/auth/callback   # must match the client exactly
OIDC_ALLOWED_DOMAINS=your-company.example         # one or more Workspace domains, comma-separated
OIDC_ROLE_MAP=alice@your-company.example:admin,bob@your-company.example:approver   # email -> role
SESSION_SECRET=<32+ random chars>                 # signs the session cookie
```

Generate a session secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

`OIDC_ISSUER` defaults to `https://accounts.google.com` and only needs setting for a non-Google
provider (see below).

### 4. Start it

```bash
docker compose up                          # local (http://localhost:8080)
docker compose --profile lan up -d         # LAN, behind the TLS front door (https://<hostname>)
```

Visiting the UI now bounces an unauthenticated visitor straight to Google, and returns them signed
in. The top bar shows the signed-in email and role, with a **Log out** link.

### 5. Test it

Before involving Google at all, smoke-test the plumbing — DNS, TLS, and the login bounce — from any
teammate machine (or with `curl --resolve` before the DNS record exists):

```bash
curl -sk -o /dev/null -w '%{redirect_url}\n' https://ingot.corp.example/auth/login
```

No DNS record or hosts entry yet? Fake it from a container (`--add-host` writes the container's
`/etc/hosts`, so nothing on the host changes):

```bash
docker run --rm --add-host ingot.corp.example:<box-lan-ip> curlimages/curl \
  -sk -o /dev/null -w '%{redirect_url}\n' https://ingot.corp.example/auth/login
```

This must print an `https://accounts.google.com/...` URL whose `redirect_uri` parameter is exactly
your `OIDC_REDIRECT_URL`; if Google later shows `redirect_uri_mismatch`, the URI in the Google
client and this value differ.

Then the real flow, which needs a human (Google logins cannot be scripted):

- Sign in with a Workspace account: the top bar shows your email and role, and
  `https://ingot.corp.example/auth/me` returns them as JSON.
- Sign in with a personal `@gmail.com` account (or a non-allowlisted domain): refused with 403.
- An account not in `OIDC_ROLE_MAP` should land as `viewer`: the read views work and the
  promote/reject/rollback actions are refused.

The equivalent flow is exercised headlessly in CI against a real Keycloak
(`tests/test_keycloak_integration.py`), so the code path itself is covered without a Google login.

## How access is decided

On each login Ingot validates the Google ID token (signature via Google's JWKS, issuer, audience,
expiry, and the login `nonce`), then:

1. **Domain gate.** The email must be verified (`email_verified`), always — even when no domain
   allowlist is configured — because roles are mapped from the email claim. With
   `OIDC_ALLOWED_DOMAINS` set, the token's hosted-domain (`hd`) claim must also be on the
   allowlist. Otherwise the login is refused (403).
2. **Role.** The email is looked up in `OIDC_ROLE_MAP`; an unmapped domain member defaults to
   `viewer`. Google ID tokens carry no roles or groups, which is why roles come from this map rather
   than from the provider.

## Security notes

- **Use HTTPS in production.** The session cookie is marked `Secure` when `OIDC_REDIRECT_URL` is
  `https://...`, so the browser only sends it over TLS. Over plain `http://` (local testing) the flag
  is off. The cookie is also `HttpOnly` and `SameSite=Lax`.
- **The session cookie is signed, not encrypted.** It carries only `{sub, email, name, role}`; treat
  its contents as readable by the client, and never put a secret in it.
- **Rotating `SESSION_SECRET` logs everyone out.** It is the operator kill switch. Sessions also
  expire after `SESSION_MAX_AGE` seconds (default 8 hours); phase 1 re-logs in rather than refreshing.
- **Fail closed.** With `AUTH_MODE=oidc`, Ingot refuses to start if `OIDC_CLIENT_ID`,
  `OIDC_REDIRECT_URL`, or a strong `SESSION_SECRET` is missing. It never silently downgrades to LAN
  password or open access.
- **CSRF protection is preserved.** The role check is added alongside the existing same-origin guard
  on the state-changing routes, not in place of it.

## Other OIDC providers

The engine is a generic OIDC client; Google is the supported and documented target. To point it at
another provider (Entra, Okta, Keycloak, ...), set `OIDC_ISSUER` to the provider's issuer URL and
`OIDC_ROLE_CLAIM` to the claim carrying role/group values (`roles` for Entra app roles, `groups` for
Okta), then map those values in `OIDC_ROLE_MAP`. Leave `OIDC_ALLOWED_DOMAINS` unset when the provider
does not emit an `hd` claim. This path is exercised in CI against a real Keycloak
(`tests/test_keycloak_integration.py`, `docker compose --profile sso up -d keycloak`) because a real
Google login cannot be scripted; the browser flow it drives is the same one Google uses.

## Not covered (by design)

- **Machine / agent auth to the MCP serving endpoint.** Agents cannot do an interactive browser
  login; they need service credentials. Put the MCP endpoint behind your network boundary or a proxy.
- **SCIM provisioning** and **per-skill / per-namespace ACLs.** Roles are global here.

See [docs/superpowers/specs/2026-07-19-sso-rbac-design.md](superpowers/specs/2026-07-19-sso-rbac-design.md)
for the design and rationale.
