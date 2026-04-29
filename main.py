"""
Jira → Jenkins Proxy
────────────────────
Polls Jira using a JQL query from a JSON store, deduplicates issues in that
same file, and POSTs to a configurable Jenkins generic webhook. A built-in
web dashboard edits JQL, webhook URL, and poll interval, and shows processed
issues.

Environment variables:
  JIRA_AUTH_MODE    "bearer" (default) or "basic" (site REST + email API token).

  --- bearer (default): api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/3/... ---
  JIRA_URL          Site URL for /browse/{KEY} links in webhooks, e.g. https://your-org.atlassian.net
  JIRA_CLOUD_ID     Cloud UUID (or ATLASSIAN_CLOUD_ID / CLOUD_ID).
  JIRA_BEARER_TOKEN OAuth or scoped token (or ATLASSIAN_API_TOKEN).

  --- basic: only if JIRA_AUTH_MODE=basic ---
  JIRA_URL          Site REST base, e.g. https://your-org.atlassian.net
  JIRA_USER         Jira Cloud email or Server/DC REST username.
  JIRA_TOKEN        API token or password/PAT per your server.

  JENKINS_URL       Initial Jenkins webhook URL when the JSON file is first created
  POLL_INTERVAL     Initial poll seconds when the JSON file is first created (default: 30)
  JQL               Initial JQL when the JSON file is first created
  DATA_PATH         JSON store path (default: /data/jira_proxy_store.json)
  WEB_HOST          Bind address (default: 0.0.0.0)
  WEB_PORT          Port (default: 8000)
  API_TOKEN         If set, required as Bearer token for config/processed mutations
  JIRA_ISSUE_SEARCH_STYLE  basic auth only: "classic" for legacy GET .../search on some
                     Server/DC. Omit for .../search/jql (required on atlassian.net / Cloud).
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import requests
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from requests.auth import HTTPBasicAuth

from json_store import AppConfig, JsonStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

JIRA_AUTH_MODE = os.getenv("JIRA_AUTH_MODE", "bearer").strip().lower()
JIRA_REST_VER = os.getenv("JIRA_REST_API_VERSION", "3").strip() or "3"

DEFAULT_JENKINS_URL = os.environ.get("JENKINS_URL", "")
DEFAULT_POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
DEFAULT_JQL = os.getenv(
    "JQL", 'assignee = "Jira_AI" AND updated >= "-10m"'
)
DATA_PATH = os.getenv("DATA_PATH", "/data/jira_proxy_store.json")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))
API_TOKEN = os.getenv("API_TOKEN", "").strip()

JIRA_URL = ""
JIRA_SEARCH_URL = ""
_JIRA_REQUEST_KW: dict = {}


def _issue_search_api_path() -> str:
    """
    Jira Cloud deprecated GET .../rest/api/3/search (returns 410 Gone).
    Use .../search/jql. On some Data Center/Server sites only /search exists — set
    JIRA_ISSUE_SEARCH_STYLE=classic to use the legacy path.
    """
    if os.getenv("JIRA_ISSUE_SEARCH_STYLE", "").strip().lower() == "classic":
        return f"/rest/api/{JIRA_REST_VER}/search"
    return f"/rest/api/{JIRA_REST_VER}/search/jql"


def _init_jira_client() -> None:
    """Set JIRA_URL (browse base), JIRA_SEARCH_URL, and _JIRA_REQUEST_KW for requests."""
    global JIRA_URL, JIRA_SEARCH_URL, _JIRA_REQUEST_KW

    jira_site = os.environ.get("JIRA_URL", "").strip().rstrip("/")
    if not jira_site:
        raise SystemExit("JIRA_URL is required (site URL, e.g. https://your-org.atlassian.net)")

    if JIRA_AUTH_MODE == "basic":
        user = os.environ.get("JIRA_USER", "").strip()
        token = os.environ.get("JIRA_TOKEN", "").strip()
        if not user or not token:
            raise SystemExit(
                "JIRA_AUTH_MODE=basic requires JIRA_USER and JIRA_TOKEN "
                "(or use default bearer auth with JIRA_CLOUD_ID and JIRA_BEARER_TOKEN)."
            )
        JIRA_URL = jira_site
        JIRA_SEARCH_URL = f"{JIRA_URL}{_issue_search_api_path()}"
        _JIRA_REQUEST_KW = {
            "auth": HTTPBasicAuth(user, token),
            "headers": {"Accept": "application/json"},
        }
        log.info("Jira client: basic auth → %s", JIRA_SEARCH_URL)
        return

    cloud = (
        os.getenv("JIRA_CLOUD_ID", "").strip()
        or os.getenv("ATLASSIAN_CLOUD_ID", "").strip()
        or os.getenv("CLOUD_ID", "").strip()
    )
    bearer = (
        os.getenv("JIRA_BEARER_TOKEN", "").strip()
        or os.getenv("ATLASSIAN_API_TOKEN", "").strip()
    )
    if not cloud or not bearer:
        raise SystemExit(
            "Bearer auth (default) requires JIRA_CLOUD_ID (or ATLASSIAN_CLOUD_ID / CLOUD_ID) "
            "and JIRA_BEARER_TOKEN (or ATLASSIAN_API_TOKEN). "
            "Use JIRA_AUTH_MODE=basic with JIRA_USER and JIRA_TOKEN for site-only REST."
        )
    JIRA_URL = jira_site
    # Cloud gateway always requires /search/jql (classic /search returns 410).
    JIRA_SEARCH_URL = (
        f"https://api.atlassian.com/ex/jira/{cloud}/rest/api/{JIRA_REST_VER}/search/jql"
    )
    _JIRA_REQUEST_KW = {
        "headers": {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
        },
    }
    log.info("Jira client: bearer auth → %s", JIRA_SEARCH_URL.split("?")[0][:70] + "…")


_init_jira_client()

STATIC_DIR = Path(__file__).resolve().parent / "static"

store = JsonStore(DATA_PATH)
_poller_stop = threading.Event()
_poller_thread: Optional[threading.Thread] = None

store.ensure_exists(
    default_jql=DEFAULT_JQL,
    default_jenkins_url=DEFAULT_JENKINS_URL,
    default_poll_interval=DEFAULT_POLL_INTERVAL,
)


def fetch_issues(jql: str) -> list[dict]:
    params = {
        "jql": jql,
        "maxResults": 50,
        "fields": (
            "summary,assignee,status,issuetype,priority,reporter,"
            "created,updated,project"
        ),
    }
    try:
        resp = requests.get(
            JIRA_SEARCH_URL,
            params=params,
            timeout=15,
            **_JIRA_REQUEST_KW,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Jira request failed: %s", exc)
        return []

    raw_issues = resp.json().get("issues", [])
    issues = []
    for item in raw_issues:
        fields = item["fields"]
        assignee_field = fields.get("assignee") or {}
        reporter_field = fields.get("reporter") or {}
        priority_field = fields.get("priority") or {}
        project_field = fields.get("project") or {}
        issue_type_field = fields.get("issuetype") or {}
        key = item["key"]
        issues.append(
            {
                "key": key,
                "summary": fields.get("summary", ""),
                "assignee_name": assignee_field.get("displayName", "Unknown"),
                "assignee_email": assignee_field.get("emailAddress") or "",
                "assignee_account_id": assignee_field.get("accountId") or "",
                "status": fields.get("status", {}).get("name", ""),
                "issue_type": issue_type_field.get("name", ""),
                "priority": priority_field.get("name", ""),
                "project_key": project_field.get("key", ""),
                "project_name": project_field.get("name", ""),
                "reporter_name": reporter_field.get("displayName", ""),
                "reporter_email": reporter_field.get("emailAddress") or "",
                "created": fields.get("created") or "",
                "updated": fields.get("updated") or "",
                "jira_issue_url": f"{JIRA_URL}/browse/{key}",
            }
        )
    log.info("Jira returned %d issue(s) for JQL: %s", len(issues), jql)
    return issues


def trigger_jenkins(webhook_url: str, issue: dict) -> None:
    if not webhook_url:
        raise ValueError("Jenkins webhook URL is empty")
    # JSON body for Jenkins "Generic Webhook Trigger": map fields via Post content parameters
    # e.g. Variable JIRA_KEY, Expression $.jira_key (or $.issue_key)
    payload = {
        "jira_key": issue["key"],
        "issue_key": issue["key"],
        "jira_issue_url": issue.get("jira_issue_url", ""),
        "summary": issue["summary"],
        "status": issue["status"],
        "assignee": issue["assignee_name"],
        "assignee_name": issue["assignee_name"],
        "assignee_email": issue.get("assignee_email", ""),
        "assignee_account_id": issue.get("assignee_account_id", ""),
        "issue_type": issue.get("issue_type", ""),
        "priority": issue.get("priority", ""),
        "project_key": issue.get("project_key", ""),
        "project_name": issue.get("project_name", ""),
        "reporter_name": issue.get("reporter_name", ""),
        "reporter_email": issue.get("reporter_email", ""),
        "created": issue.get("created", ""),
        "updated": issue.get("updated", ""),
    }
    resp = requests.post(webhook_url, json=payload, timeout=10)
    resp.raise_for_status()
    log.info(
        "Jenkins triggered for %s (assignee=%s) → HTTP %s",
        issue["key"],
        issue["assignee_name"],
        resp.status_code,
    )


def poll_once() -> None:
    cfg = store.get_config()
    if not cfg.jql.strip():
        log.warning("JQL is empty; skipping poll.")
        return

    issues = fetch_issues(cfg.jql)
    if not issues:
        return

    for issue in issues:
        key = issue["key"]
        if store.is_processed(key):
            log.debug("Skipping already-processed issue: %s", key)
            continue

        log.info("New issue found: %s — triggering Jenkins …", key)
        try:
            trigger_jenkins(cfg.jenkins_webhook_url, issue)
            store.mark_processed(key)
            log.info("Marked %s as processed.", key)
        except Exception as exc:
            log.warning("Will retry %s next poll. Reason: %s", key, exc)


def poller_loop() -> None:
    log.info("Background poller started")
    while not _poller_stop.is_set():
        try:
            poll_once()
        except Exception:
            log.exception("Unexpected error during poll")
        interval = store.get_config().poll_interval_seconds
        if _poller_stop.wait(timeout=interval):
            break
    log.info("Background poller stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poller_thread
    _poller_stop.clear()
    _poller_thread = threading.Thread(
        target=poller_loop, name="jira-poller", daemon=True
    )
    _poller_thread.start()
    log.info("Web UI listening on http://%s:%s", WEB_HOST, WEB_PORT)
    yield
    _poller_stop.set()
    if _poller_thread is not None:
        _poller_thread.join(timeout=15)


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="Jira to Jenkins Proxy", version="2.0", lifespan=lifespan)


class ConfigBody(BaseModel):
    jql: Optional[str] = Field(None, description="Jira JQL query")
    jenkins_webhook_url: Optional[str] = Field(
        None, description="Jenkins generic webhook URL"
    )
    poll_interval_seconds: Optional[int] = Field(
        None, ge=5, description="Seconds between polls"
    )


def require_write_auth(request: Request) -> None:
    if not API_TOKEN:
        return
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def api_get_config() -> dict:
    c = store.get_config()
    return {
        "jql": c.jql,
        "jenkins_webhook_url": c.jenkins_webhook_url,
        "poll_interval_seconds": c.poll_interval_seconds,
        "jira_auth_mode": JIRA_AUTH_MODE,
        "jira_url_masked": JIRA_URL.split("//")[-1][:48] + ("…" if len(JIRA_URL) > 48 else ""),
        "data_path": str(store.path),
        "write_auth_required": bool(API_TOKEN),
    }


@app.put("/api/config")
def api_put_config(
    body: ConfigBody,
    _: None = Depends(require_write_auth),
) -> dict:
    if (
        body.jql is None
        and body.jenkins_webhook_url is None
        and body.poll_interval_seconds is None
    ):
        raise HTTPException(status_code=400, detail="No fields to update")
    c = store.update_config(
        jql=body.jql,
        jenkins_webhook_url=body.jenkins_webhook_url,
        poll_interval_seconds=body.poll_interval_seconds,
    )
    return {
        "jql": c.jql,
        "jenkins_webhook_url": c.jenkins_webhook_url,
        "poll_interval_seconds": c.poll_interval_seconds,
    }


@app.get("/api/processed")
def api_list_processed() -> dict:
    return {"items": store.list_processed()}


@app.delete("/api/processed/{issue_key:path}")
def api_delete_processed(
    issue_key: str,
    _: None = Depends(require_write_auth),
) -> JSONResponse:
    if not store.delete_processed(issue_key):
        raise HTTPException(status_code=404, detail="Issue not in store")
    return JSONResponse({"removed": issue_key})


@app.get("/")
def dashboard() -> FileResponse:
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=500, detail="Dashboard UI missing")
    return FileResponse(index)


if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


def main() -> None:
    log.info("Starting Jira → Jenkins proxy + web UI")
    log.info("  JIRA_AUTH  = %s", JIRA_AUTH_MODE)
    log.info("  JIRA_URL   = %s", JIRA_URL)
    log.info("  DATA_PATH  = %s", DATA_PATH)
    log.info("  WEB        = http://%s:%s", WEB_HOST, WEB_PORT)

    uvicorn.run(
        app,
        host=WEB_HOST,
        port=WEB_PORT,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
