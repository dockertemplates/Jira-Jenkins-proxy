# Jira → Jenkins proxy
Polls **Jira** with a **JQL** query, tracks processed issues in a **JSON** file, and **POSTs** ticket metadata to a **Jenkins Generic Webhook Trigger** URL when new issues appear. Includes a small **web UI** to edit JQL, webhook URL, and poll interval.
| Path | Purpose |
|------|--------|
| `main.py` | App: poller, FastAPI, Jira + Jenkins integration |
| `json_store.py` | Thread-safe JSON store (config + processed keys) |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Container image |
| `static/index.html` | Required for the dashboard at `/` |
---
## Jira authentication (pick one)
### Option A — Bearer / service-style (default)
Use this when you call Atlassian’s gateway with a **cloud ID** and **Bearer** token (OAuth or scoped API token), for example:
`https://api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/3/search/jql`
| Variable | Required | Description |
|----------|----------|-------------|
| `JIRA_URL` | Yes | Site URL for issue links in webhooks, e.g. `https://your-org.atlassian.net` |
| `JIRA_CLOUD_ID` | Yes | Cloud UUID (aliases: `ATLASSIAN_CLOUD_ID`, `CLOUD_ID`) |
| `JIRA_BEARER_TOKEN` | Yes | Bearer token (alias: `ATLASSIAN_API_TOKEN`) |
| `JIRA_AUTH_MODE` | No | Omit or `bearer` (default) |
### Option B — Basic / “regular” account (email + API token on the site)
Use this for **Jira Cloud** with a normal user **email** + **API token**, or **Server/Data Center** with username/token as your admin documents.
| Variable | Required | Description |
|----------|----------|-------------|
| `JIRA_AUTH_MODE` | Yes | Set to `basic` |
| `JIRA_URL` | Yes | Site base, e.g. `https://your-org.atlassian.net` |
| `JIRA_USER` | Yes | Cloud: Atlassian login email; Server/DC: REST username |
| `JIRA_TOKEN` | Yes | API token (Cloud) or PAT/password per your server |
**On-prem only:** if `.../search/jql` is not supported, set `JIRA_ISSUE_SEARCH_STYLE=classic` to use legacy `GET .../search`. Do **not** use `classic` with bearer + `api.atlassian.com` (Cloud requires `/search/jql`).
---
## Other useful variables
| Variable | Default | Description |
|----------|---------|-------------|
| `JENKINS_URL` | empty | Initial webhook URL (also editable in UI) |
| `POLL_INTERVAL` | `30` | Seconds between polls |
| `JQL` | built-in | Initial JQL when JSON store is first created |
| `DATA_PATH` | `/data/jira_proxy_store.json` | JSON store path (mount a volume on `/data` in Docker) |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8000` | Dashboard bind |
| `API_TOKEN` | empty | If set, dashboard **Save** / **Remove** need `Authorization: Bearer <value>` — **not** the Jira token |
---
## Jenkins: JSON → job variables
The proxy **POSTs JSON** to your Generic Webhook Trigger URL. In the job configuration, under **Generic Webhook Trigger** → **Post content parameters**, add rows such as:
| Variable | Expression |
|----------|------------|
| `JIRA_KEY` | `$.jira_key` |
| `JIRA_ISSUE_URL` | `$.jira_issue_url` |
| `SUMMARY` | `$.summary` |
Use those names in a Pipeline as `env.JIRA_KEY`, `env.SUMMARY`, etc. (exact exposure can depend on Jenkins and plugin version).
---