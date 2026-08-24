# Hosting this server for Claude

This documents how to run the server as a shared, per-user-authenticated
web service that people add to Claude as a connector. It is the setup this
fork was built and validated on (Google Cloud Run); everything is plain
Docker + environment variables, so any host that can run a container and
mount a persistent directory will do.

The upstream README covers local `stdio` use. This file covers the parts it
does not: the OAuth proxy state that must survive restarts, the Google-side
prerequisites, and the knobs that turned out to matter in production.

## How authentication works

The server does **not** hold any user's Google Ads credentials. It runs
FastMCP's Google OAuth *proxy*: when someone adds the connector in Claude,
they sign in with their own Google account, and every API call the server
makes on their behalf uses *their* token and therefore reaches only the
accounts *they* have been granted in Google Ads. Access control stays
entirely in Google Ads; the server has no allow-list of its own.

Because of that, the service must be reachable **without** platform-level
authentication (on Cloud Run: "Allow unauthenticated invocations"). The
`/mcp` endpoint itself rejects requests without a valid token (`401`).

## Prerequisites (one-time, on the Google side)

1. **A Google Ads developer token** (`GOOGLE_ADS_DEVELOPER_TOKEN`). Issued
   per manager account (MCC) under *Tools → API Center*; the server is
   read-only, but the token still needs at least *Basic* access to reach
   production accounts. It is tied to your MCC and cannot be reused from
   another organization.
2. **A GCP project** with the *Google Ads API* enabled.
3. **An OAuth 2.0 client** (type *Web application*) in that project:
   - Consent screen: *Internal* if all users are in one Google Workspace
     (simplest — no verification). *External* requires Google's verification
     for the `https://www.googleapis.com/auth/adwords` scope.
   - Authorized redirect URI: `<BASE_URL>/auth/callback`.
   - Keep the client id and secret server-side only; Claude never sees them
     (the proxy registers Claude dynamically).
4. **Workspace admins**: check *Security → API controls*. Organizations
   that restrict third-party OAuth apps must trust this client for the
   adwords scope, or users' sign-ins will fail at the consent screen.
5. **Google Ads access for each user**, granted on the accounts or manager
   accounts they should work in. Access granted on a manager cascades to
   everything beneath it; the server resolves that hierarchy per user, live
   (`ads_mcp/customer_resolver.py`), so nothing is configured here.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | yes | Google Ads API developer token. |
| `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID` | yes | OAuth client id. Setting both OAuth vars switches the server from `stdio` to HTTP on port 8080. **Without them the container starts in stdio mode and the platform reports "failed to listen on PORT".** |
| `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET` | yes | OAuth client secret. Also seeds the signing key for the tokens the server issues — **rotating it signs every user out.** |
| `GOOGLE_ADS_MCP_BASE_URL` | yes | Public URL of the service, no trailing slash. Must match the redirect URI registered on the OAuth client. Unknown until the first deploy; deploy once, then set it and redeploy. |
| `FASTMCP_HOME` | yes (see below) | Directory for the OAuth proxy's persistent state. Point it at a mounted volume. |
| `FASTMCP_HOST` | yes on Cloud Run | `0.0.0.0` so the server binds all interfaces inside the container. |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | no | **Leave unset.** Forces one server-wide manager account for every user; only useful if everyone works through the same single MCC. When unset the manager is resolved per user. |
| `GOOGLE_ADS_MCP_TOOLS_CONFIG` | no | Path to a `tools_config.yaml` to enable/disable tools or categories. |

## Persistent state — the part that bites

The OAuth proxy stores two things under `$FASTMCP_HOME/oauth-proxy/`: the
dynamic client registrations Claude creates, and each user's upstream
Google refresh token, encrypted. If that directory is ephemeral, **every
restart or scale-to-zero signs everyone out** and Claude shows the
connector as broken until they re-authenticate. This was the single
largest source of "I had to log in again" reports before it was fixed.

Mount a persistent directory there. On Cloud Run:

```bash
# one-time
gcloud storage buckets create gs://YOUR_BUCKET --location=REGION
# the service's runtime service account needs objectUser on the bucket
gcloud storage buckets add-iam-policy-binding gs://YOUR_BUCKET \
  --member=serviceAccount:RUNTIME_SA --role=roles/storage.objectUser
```

then deploy with `--add-volume name=state,type=cloud-storage,bucket=YOUR_BUCKET`
and `--add-volume-mount volume=state,mount-path=/data/fastmcp-home` and
`FASTMCP_HOME=/data/fastmcp-home`. After the first sign-in you should see
`oauth-proxy/` appear in the bucket.

The store is one small object per key with no locking, so **keep
`max-instances` low** (1–2). Two instances rotating the same user's refresh
token concurrently would clobber each other and force a re-login. Traffic
from a team of ~10 analysts peaks around 30 requests/minute, far below what
one instance handles.

## Deploying to Cloud Run

```bash
gcloud run deploy google-ads-mcp \
  --source . \
  --region REGION \
  --allow-unauthenticated \
  --min-instances 1 --max-instances 2 \
  --add-volume name=state,type=cloud-storage,bucket=YOUR_BUCKET \
  --add-volume-mount volume=state,mount-path=/data/fastmcp-home \
  --set-env-vars "FASTMCP_HOST=0.0.0.0,FASTMCP_HOME=/data/fastmcp-home,GOOGLE_ADS_DEVELOPER_TOKEN=...,GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=...,GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=...,GOOGLE_ADS_MCP_BASE_URL=https://PLACEHOLDER"
```

Then take the URL Cloud Run prints, set `GOOGLE_ADS_MCP_BASE_URL` to it,
register `<URL>/auth/callback` on the OAuth client, and redeploy. Cloud Run
can also build automatically from a connected GitHub repository ("deploy
from source"), which is how this fork is run: a push to the deployment
branch rebuilds and rolls out a new revision in about 2–3 minutes.

Notes from running it:

- **`min-instances 1`** is optional. Cold starts take ~8 s (the Google Ads
  client library is heavy to import), which Claude tolerates, but a warm
  instance keeps the in-memory per-user account cache alive between
  conversations. Costs a few euros a month at 1 vCPU / 512 MiB.
- **Rolling out a new revision terminates every live MCP session.** Users
  mid-conversation will see the connector error once and need to start a
  new chat. Deploy outside working hours.
- The exact `fastmcp==3.4.7` pin in `pyproject.toml` is deliberate: the
  OAuth proxy's storage layout and the hooks `ads_mcp/auth_logging.py`
  overrides live in `fastmcp`, and an unbounded build once picked up a 4.0
  pre-release on a routine rebuild. Bump it on purpose: check that the
  three overridden hooks and the `mcp-*` storage collections are unchanged,
  redeploy, and confirm sign-in and silent refresh still work.

## Connecting from Claude

Give users only the MCP URL: `<BASE_URL>/mcp`. In Claude, *Settings →
Connectors → Add custom connector*, paste the URL, no client id or secret.
They will be sent through Google's consent screen once; afterwards the
server refreshes tokens silently (its own access tokens last 24 h, the
Google refresh token about a year).

Verifying without a client:

```bash
curl -s <BASE_URL>/.well-known/oauth-authorization-server | head -c 200   # 200, JSON
curl -s -o /dev/null -w '%{http_code}\n' -X POST <BASE_URL>/mcp             # 401
```

## Reading the logs

Every tool call is attributed. Useful lines:

- `ads_mcp auth: <email> SIGNED IN (completed the consent screen)` vs
  `... refreshed silently` — a user who reports repeated logins should show
  `SIGNED IN` lines; if they only show `refreshed`, the problem is on the
  client side.
- `ads_mcp: resolved N account(s) under M accessible root(s) for <email>` —
  what the user can reach; `could not expand accessible root X` means a
  granted account is cancelled or the grant was removed.
- `ads_mcp.search query|done|failed [<email> cid=<id>] ...` — each query,
  its row count and latency, or its Google error class.
- Bursts of `POST /mcp 401` with no `POST /token` afterwards mean a Claude
  client is replaying a dead token. Server side is fine; the user should
  remove and re-add the connector.

Authentication URLs (`/authorize`, `/token`) never carry user identity in
the logs; the `ads_mcp auth:` lines are what to search for.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Container fails: "failed to listen on PORT 8080" | OAuth env vars missing → server started in stdio mode. |
| Everyone signed out after a deploy/restart | `FASTMCP_HOME` not on persistent storage, or the client secret changed. |
| Consent screen fails inside Google, before returning to the server | Workspace admin restriction on the adwords scope, or redirect URI mismatch. |
| `PERMISSION_DENIED` on an account the user can open in the Ads UI | The user was granted a *nested* manager and the request went through a manager they are not a member of. Make sure `GOOGLE_ADS_LOGIN_CUSTOMER_ID` is unset so the per-user resolver picks the granted root. |
| `CUSTOMER_NOT_ENABLED` in logs | A granted account is cancelled/suspended. Harmless; remove the grant to silence it. |
| Connector present in Claude but with no tools | Dead token replay (see logs section). Remove and re-add the connector. |
