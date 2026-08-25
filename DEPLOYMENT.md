# Deploying this server for Claude

This is the runbook for hosting the server as a shared web service that
people add to Claude as a connector, each signing in with their own Google
account. It is written for someone who has not seen this project before and
covers everything from the Google-side prerequisites to day-two operations.
It describes Google Cloud Run, which is where this setup was built and
validated; the server itself is a plain container with one persistent
directory, so other container hosts work too, subject to the notes in
"Operations".

The upstream README covers running the server locally over `stdio`. This
file covers the hosted, multi-user case.

Time budget: about an hour, most of it waiting on the Google Ads developer
token application if you do not already have one.

## 0. What you need before starting

| Role | Needed for |
|---|---|
| Google Ads **manager account (MCC) admin** | developer token; granting users read-only access to accounts |
| Google **Workspace admin** | confirming the OAuth app is allowed; optionally restricting it to a group |
| Google Cloud **project owner** (with billing) | OAuth client, Cloud Run, storage bucket |

## 1. Google Ads

### 1.1 Developer token

Go to the manager account's **API Center** (https://ads.google.com/aw/apicenter)
and apply for a developer token. Choose:

- **Access level: Basic.** Access levels set daily quotas (Basic: 15,000
  operations/day; one query = one operation) and which accounts you can
  reach; they do not restrict what the API can do. A team of a few analysts
  uses on the order of a few hundred operations per day.
- **Permissible use: Reporting.** Google defines it as "only make
  `GoogleAdsService.Search` or `SearchStream` requests, or read-only calls",
  and permissible use determines which API features the token can access.
  This is the only place the developer token can be made read-only; the
  server only ever issues search calls, so Reporting is exactly what it
  needs.

The token is a string; it becomes `GOOGLE_ADS_DEVELOPER_TOKEN`.

### 1.2 User access

Every person who will use the connector needs access in Google Ads to the
accounts they should see. Grant it on a manager account to cover everything
beneath it, or on individual accounts. **Read-only ("Email only" or "Read
only") is enough** and is the strongest read-only guarantee in the whole
setup: it holds even if the software changes. The server has no user list
of its own — a person with no Google Ads access signs in fine and sees zero
accounts; removing someone from Google Ads removes their access within 10
minutes.

## 2. Google Cloud project

Enable these APIs on the project: **Cloud Run**, **Cloud Build**,
**Artifact Registry**, **Google Ads API**.

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com googleads.googleapis.com
```

### 2.1 OAuth consent screen and client

*APIs & Services → OAuth consent screen*:

- **User type: Internal.** Only accounts in your Google Workspace can sign
  in, and no Google verification is required. (External would require
  Google to verify the app for the `https://www.googleapis.com/auth/adwords`
  scope; External in "testing" mode is not an option — its tokens expire
  after 7 days, forcing weekly re-logins.)
- Scopes: `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`,
  `.../auth/adwords`. Google publishes only one Google Ads scope and it is
  read/write; there is no read-only scope to request.

*APIs & Services → Credentials → Create credentials → OAuth client ID*:

- Type **Web application**.
- Authorized redirect URI: you do not know the service URL yet. Add a
  placeholder now (e.g. `https://example.com/auth/callback`) and replace it
  in step 3.3.
- Save the **client ID** and **client secret**; they become
  `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID` / `_SECRET`. They stay on the server —
  users and Claude never see them (Claude registers itself with the server
  dynamically).

### 2.2 Workspace admin check

Many Workspaces restrict which OAuth apps may request Google Ads access.
In the Admin console, *Security → Access and data control → API controls →
App access control*, make sure this OAuth client is **trusted** (or at
least not blocked) for the adwords scope. This is also where access can be
narrowed to a group: restricting the app to an organisational unit or
group is the switch for adding/removing people at the sign-in layer, and
needs no redeploy.

If this is wrong, users get an error from Google at the consent screen,
before anything reaches the server.

### 2.3 Storage bucket for OAuth state

The server keeps two things it must not lose: the dynamic client
registrations Claude creates, and each user's Google refresh token
(encrypted at rest with a key derived from the client secret). They live
under `$FASTMCP_HOME/oauth-proxy/`. **If that directory is ephemeral,
every restart signs everyone out.** On Cloud Run the directory is a Cloud
Storage bucket mounted as a volume.

```bash
gcloud storage buckets create gs://YOUR_BUCKET --location=REGION \
  --uniform-bucket-level-access
# The Cloud Run runtime service account must be able to read and write it.
# By default that is the Compute Engine default service account:
PROJECT_NUMBER=$(gcloud projects describe YOUR_PROJECT_ID --format='value(projectNumber)')
gcloud storage buckets add-iam-policy-binding gs://YOUR_BUCKET \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role=roles/storage.objectUser
```

Keep the bucket private (no public access). It holds credentials.

## 3. First deploy

### 3.1 Deploy from source

From the repository root:

```bash
gcloud run deploy google-ads-mcp \
  --source . \
  --region REGION \
  --allow-unauthenticated \
  --cpu 1 --memory 512Mi \
  --min-instances 1 --max-instances 2 \
  --add-volume name=state,type=cloud-storage,bucket=YOUR_BUCKET \
  --add-volume-mount volume=state,mount-path=/data/fastmcp-home \
  --set-env-vars "FASTMCP_HOST=0.0.0.0,FASTMCP_HOME=/data/fastmcp-home,GOOGLE_ADS_DEVELOPER_TOKEN=YOUR_TOKEN,GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=YOUR_CLIENT_ID,GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=YOUR_CLIENT_SECRET,GOOGLE_ADS_MCP_BASE_URL=https://placeholder"
```

What the flags mean and why they are not optional:

- `--allow-unauthenticated`: authentication happens *inside* the server
  (the OAuth proxy). Cloud Run's own IAM check must be off or Claude cannot
  reach the sign-in endpoints. `/mcp` still rejects any request without a
  valid token (`401`).
- `--min-instances 1`: **required.** See "Operations" — with scale-to-zero,
  Claude conversations start without the connector's tools.
- `--max-instances 2`: the state store has no locking; two instances
  refreshing the same user's token concurrently can clobber each other and
  force a re-login. One instance handles a team comfortably (measured peak
  ~30 requests/minute against a capacity of 80 concurrent requests).
- The first build takes 3–5 minutes (Cloud Build creates an Artifact
  Registry repository for you and builds the Dockerfile).

If you prefer not to put secrets in environment variables, store them in
Secret Manager and use `--set-secrets` instead of `--set-env-vars` for the
token and client secret; the server reads plain environment variables
either way.

### 3.2 Environment variables reference

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | yes | Google Ads API developer token. |
| `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID` | yes | OAuth client id. Setting both OAuth variables switches the server from `stdio` to HTTP on port 8080. **Without them the container starts in stdio mode and Cloud Run reports "failed to listen on PORT".** |
| `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET` | yes | OAuth client secret. Also seeds the signing key for the tokens the server issues — **rotating it signs every user out.** |
| `GOOGLE_ADS_MCP_BASE_URL` | yes | Public URL of the service, no trailing slash. Must match the redirect URI on the OAuth client. |
| `FASTMCP_HOME` | yes | Directory for the OAuth proxy's persistent state — the mounted bucket. |
| `FASTMCP_HOST` | yes on Cloud Run | `0.0.0.0` so the server binds all interfaces in the container. |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | no | **Leave unset.** Forces one server-wide manager account for every user. When unset, the manager is resolved per user from their own Google Ads access, which is what makes nested manager structures work. |
| `GOOGLE_ADS_MCP_TOOLS_CONFIG` | no | Path to a `tools_config.yaml` to disable tools or tool categories. The bundled default enables all four tools. |

### 3.3 Set the real URL

Cloud Run prints the service URL (`https://google-ads-mcp-….run.app`).

1. Replace the placeholder redirect URI on the OAuth client with
   `<URL>/auth/callback`.
2. Update the environment variable. This does not rebuild, only creates a
   new revision:

```bash
gcloud run services update google-ads-mcp --region REGION \
  --update-env-vars GOOGLE_ADS_MCP_BASE_URL=<URL>
```

### 3.4 Verify

```bash
curl -s <URL>/.well-known/oauth-authorization-server | head -c 200   # 200 and JSON
curl -s -o /dev/null -w '%{http_code}\n' -X POST <URL>/mcp             # 401
```

After the first user signs in, `oauth-proxy/` appears in the bucket. If it
does not, the volume mount or the bucket IAM is wrong and users will be
signed out on the next restart.

## 4. Connecting from Claude

Give users one thing: the URL `<URL>/mcp`. In Claude: *Settings →
Connectors → Add custom connector*, paste the URL. No client id or secret.

The first time, Claude opens a browser window with two pages: the server's
own "allow Claude to access this server?" page (approve), then Google's
sign-in and consent for the Google Ads scope. After that, sign-ins are
silent: the server issues its own 24-hour tokens and refreshes them against
Google's refresh token (valid about a year, or until revoked).

Two client-side things worth telling users:

- Google's sign-in sometimes refuses browsers with aggressive
  tracking-protection or ad-blocking extensions ("this browser may not be
  secure"). Completing the sign-in in a browser that is logged into
  claude.ai *and* acceptable to Google (e.g. a private window, or Chrome)
  fixes it. The whole flow has to finish in the same browser session, or
  the final step back to claude.ai never completes.
- If a connector ever shows **⚠ Reconnect** in Settings → Connectors, click
  it. Claude flags a connector after a few failed requests and stops using
  it until told to reconnect; nothing needs to change on the server.

## 5. Operations

### Automatic deploys from GitHub

In the Cloud Run console, the service can be connected to a GitHub
repository ("Set up continuous deployment"): a push to the chosen branch
rebuilds the image and rolls out a new revision in 2–3 minutes. Any
`--min-instances`, volume and env settings carry over between revisions.

### Rules learned in production

- **`min-instances` must stay at 1.** Claude builds a chat's tool list when
  the conversation starts and gives up after a few failed requests. A cold
  start of this container takes 14–17 s (importing the Google Ads client
  library), so with scale-to-zero, the first conversation after ~15 minutes
  of idle comes up with **no Google Ads tools at all**, and Claude then
  flags the connector as needing reconnection. Cost of the always-on
  instance at 1 vCPU / 512 MiB in an EU region: ~€8/month.
- There are two different "min" settings in Cloud Run: `--min-instances`
  (per revision, what you want) and `--min` (service level). A revision's
  own value acts as a floor that the service-level setting cannot lower,
  so always use `--min-instances`.
- **Every new revision terminates all live MCP sessions.** People in the
  middle of a conversation see one connector error and need to start a new
  chat. Deploy outside working hours.
- **Do not downgrade `fastmcp` to the 3.x line.** `pyproject.toml` pins
  `fastmcp==4.0.0b3` deliberately. The MCP transport and the OAuth proxy
  live in that package; 3.4.x depends on `mcp` 1.x, which answered Claude's
  requests with `400 Bad Request: Missing session ID` and left chats
  without tools within half an hour of going live. 4.0.0b3 (`mcp` 2.x)
  served the same client for weeks without one. To move to a newer 4.x:
  change the pin, deploy off-hours, then watch the logs for `POST /mcp 400`
  and for unexpected `SIGNED IN` lines over the following day.

### Google Ads API version upgrades

Google releases a new API version roughly three times a year and retires
old ones about a year after release. The version is hardcoded as `v24` in
`ads_mcp/utils.py` and `ads_mcp/resources/discovery.py` (`grep -rn v24
ads_mcp` finds every occurrence); bumping it means:

1. Update those places and the `google-ads` dependency if needed.
2. Regenerate the list of queryable resources:
   `google-ads-mcp-update-gaql` (needs a developer token and credentials).
3. Run the tests (`nox -s tests`), regenerate the tool-list snapshots
   (`nox -s update_smoke_golden`), commit, deploy.

### Cost

- Google Ads API: free; capped by the developer token's daily quota.
- Cloud Run: the request-driven part of this workload sits inside the free
  tier; the always-on instance is the bill, ~€8/month. Bucket and logs are
  negligible. A Cloud Billing budget alert (e.g. $20/month) on the project
  is the right place for a safety net; nothing in the code needs it.

## 6. Reading the logs

Cloud Logging, filtered to the service. Every tool call is attributed to a
user. Useful lines:

- `ads_mcp auth: <email> SIGNED IN (completed the consent screen)` vs
  `... refreshed silently (no sign-in needed)` — someone who reports
  repeated logins should show `SIGNED IN` lines; if they only show
  `refreshed`, the problem is on the client side.
- `ads_mcp: resolved N account(s) under M accessible root(s) for <email>` —
  what the user can reach; `could not expand accessible root X` means a
  granted account is cancelled or the grant was removed.
- `ads_mcp.search query|done|failed [<email> cid=<id>] ...` — each query,
  its row count and latency, or Google's error class. Query text is logged;
  result rows are not.
- Access tokens are never logged (the HTTP client libraries that would log
  them are silenced in `ads_mcp/utils.py`).

Default log retention is 30 days.

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Container fails: "failed to listen on PORT 8080" | OAuth env vars missing → server started in stdio mode. |
| Everyone signed out after a deploy or restart | `FASTMCP_HOME` is not on the mounted bucket, the bucket IAM is wrong, or the client secret changed. |
| Google shows an error at the consent screen, before returning to the server | Workspace app-access restriction on the adwords scope, or redirect URI mismatch with `GOOGLE_ADS_MCP_BASE_URL`. |
| Google says "this browser may not be secure" | Client-side; see section 4. |
| Connector shows **⚠ Reconnect** / Claude says it has no Google Ads tool | Claude gave up on the server after failed requests (cold start, a rollout mid-conversation, or a bad server version). Fix the server side if it is a server problem, then the user clicks Reconnect. Check the logs for `POST /mcp 400` (bad `fastmcp` version) or cold-start latency (`min-instances` not 1). |
| `PERMISSION_DENIED` on an account the user can open in the Ads UI | The request went through a manager account the user is not a member of. Make sure `GOOGLE_ADS_LOGIN_CUSTOMER_ID` is unset so the per-user resolver picks the manager the user was actually granted. |
| `CUSTOMER_NOT_ENABLED` in logs | A granted account is cancelled/suspended. Harmless; remove the grant to silence it. |
| A query fails with a `query_error` | Normal: Google rejected the GAQL. The error message includes the field and a hint; Claude retries with a corrected query. Only a pattern of the *same* failure is worth looking at. |

## 8. What the server does and does not do (for security reviews)

- Read-only: four tools (`list_accessible_customers`, `get_resource_metadata`,
  `search`, `search_batch`), all declared read-only; the only Google Ads API
  methods called are `GoogleAdsService.Search`, `CustomerService.ListAccessibleCustomers`
  and a `customer_client` query to expand manager accounts. No mutate
  service is wired to any tool.
- Data reachable: any read-only GAQL reporting resource, for accounts the
  signed-in user holds access to (the full list is `ads_mcp/gaql_resources.txt`;
  Google's reference: https://developers.google.com/google-ads/api/fields/v24/overview).
  Note this includes `lead_form_submission_data` / `local_services_lead*`
  (consumer contact details, where an account runs those ad formats),
  `customer_user_access*` (who has access to an account) and `click_view`
  (per-click records, no user identity). It does not include audience-list
  members, uploaded customer-match data, or anything outside Google Ads.
- Stored on the server: encrypted OAuth tokens in the bucket; logs with
  user email, account ids and query text (not results), 30 days.
- Where results go: back to the MCP client that asked (Claude), and from
  there under that tool's data terms. The server adds no third party.
- Components: Google's open-source `google-ads-mcp` and `google-ads` client
  (Apache-2.0); FastMCP (Prefect Technologies, Apache-2.0) for the MCP
  protocol layer and the OAuth proxy — Google sign-in, token issuing and
  the encrypted token store. This fork adds per-user manager-account
  resolution, batch queries, richer error messages and per-user audit
  logging on top.
