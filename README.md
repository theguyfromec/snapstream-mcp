# snapstream-mcp

MCP server for the SnapStream tenant API (the cloud API served through AWS API Gateway). It lets an MCP client such as Claude Code search recordings, read transcripts and smart chapters, browse the program guide and scheduled recordings, and call any read method in the API catalog. Writes go through a stage, approve and commit gate.

## Setup

Requires Python 3.11 or newer.

```powershell
cd C:\path\to\snapstream-mcp
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # or requirements.txt without pytest
```

### Credentials

Copy `secrets.yaml.example` to `secrets.yaml` and fill it in from Cloud Admin (instance > Modify > API, shown after Provision API). `secrets.yaml` is gitignored. Never commit it, and never send these values through chat, Slack or email.

| Key | Required | Notes |
|---|---|---|
| `base_url` | yes | `https://` only |
| `token_endpoint` | yes | Cognito token endpoint, `https://` only |
| `client_id`, `client_secret` | yes | Cognito app client |
| `api_key` | yes | sent as `x-api-key` |
| `scopes` | no | space-separated; omitted from the token request when empty |
| `instance` | no | label shown by `ss_status` |

The server refuses any `REPLACE_ME` placeholder and any non-https URL. Error messages name the key, never the value. To keep the file elsewhere, set `SNAPSTREAM_MCP_SECRETS` to its path.

Each call gets an access token from the Cognito token endpoint (client_credentials), caches it, and refreshes it 60 seconds before it expires; a 401 triggers one refresh and retry. Requests are `POST {base_url}/{Service}/json/{Method}` with `Authorization: Bearer <token>`, `x-api-key`, and a JSON body keyed by parameter name. The `authTicket` parameter is always omitted; the gateway resolves it from the client_id.

## Register with Claude Code

Add to the project `.mcp.json` (or user settings). `cwd` must be the repo root so `-m snapstream_mcp` can import the package and find `secrets.yaml`.

```json
{
  "mcpServers": {
    "snapstream": {
      "type": "stdio",
      "command": "C:\\path\\to\\snapstream-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "snapstream_mcp"],
      "cwd": "C:\\path\\to\\snapstream-mcp"
    }
  }
}
```

If your client cannot set `cwd`, install the package into the venv once (`.venv\Scripts\python.exe -m pip install -e .`). The module then imports from any working directory, and `secrets.yaml` is still read from the repo root.

Optional environment variables: `SNAPSTREAM_MCP_SECRETS`, `SNAPSTREAM_MCP_TIMEOUT` (seconds, default 30), `SNAPSTREAM_MCP_MAX_CONCURRENCY` (default 4), `SNAPSTREAM_MCP_WRITE_MIN_INTERVAL` (seconds between write calls, default 5), `SNAPSTREAM_MCP_MAX_RESPONSE_CHARS` (default 40000), `SNAPSTREAM_MCP_AUDIT_FILE` (default `snapstream_writes.audit.jsonl` in the repo root).

## Tools

### Status, catalog and generic access

| Tool | What it does |
|---|---|
| `ss_status` | Instance label, API host, config validity, token check, catalog counts, pending writes. Never returns secrets. |
| `ss_api_catalog` | Offline search of all 461 methods by keyword, service and access class. |
| `ss_api_describe` | One method's parameters, permissions, return type, access class and an example params object. |
| `ss_call_read` | Calls any method whose access is `read`. Refuses write and blocked methods. |
| `ss_stage_write` | Validates and freezes a write, returns a preview and a single-use token (10 minute TTL). |
| `ss_commit_write` | Sends exactly the staged payload once. The only tool that changes SnapStream. |
| `ss_cancel_staged` | Discards a staged write. |
| `ss_pending_writes` | Lists staged writes that have not been committed. |

### Curated read tools

| Tool | API method |
|---|---|
| `ss_search_recordings` | Search/SearchRecordings |
| `ss_count_recording_matches` | Search/SearchRecordingsCount |
| `ss_search_in_recording` | Search/SearchRecordingWithRefinement |
| `ss_search_guide` | Search/SearchAirings |
| `ss_list_media_items` | Library/GetMediaItems |
| `ss_get_media_item` | Library/GetMediaItem |
| `ss_get_transcript` | Library/GetTranscript (windowed by caption index, seconds offset and size; optional text filter) |
| `ss_get_smart_chapters` | Library/GetSmartChapters |
| `ss_list_clips_and_bookmarks` | Library/GetClipsAndBookmarks |
| `ss_get_bookmark` | Library/GetBookmarkById |
| `ss_list_library_channels` | Library/GetAllPastAndCurrentChannels |
| `ss_storage_status` | Library/GetDrives |
| `ss_list_channels` | Scheduler/GetChannels |
| `ss_whats_on` | Scheduler/GetAiringsAtTime |
| `ss_guide_for_channel` | Scheduler/GetAiringsByChannelAndTimeRange |
| `ss_guide_by_time` | Scheduler/GetAiringsByStartTimeRange |
| `ss_get_airing` | Scheduler/GetAiring |
| `ss_list_scheduled_recordings` | Scheduler/GetCurrentAndUpcomingRecordings |
| `ss_list_current_recordings` | Scheduler/GetCurrentRecordings |
| `ss_list_recent_recordings` | Scheduler/GetRecentRecordings |
| `ss_list_recording_jobs` | Scheduler/GetRecordingJobs |
| `ss_scheduler_status` | Scheduler/GetStats, GetUpdateStatus, GetLastRebuild |
| `ss_get_job` | TaskManager/GetTaskList |
| `ss_whoami` | AuthenticationManager/GetUserEmail |

Curated tools take the exact parameter names from the API catalog (for example `itemId`, `channelId`, `highlightStart`).

### Response format

* The `<Method>Result` wrapper is removed.
* `/Date(ms[+-hhmm])/` becomes ISO 8601 UTC. The min date (`/Date(-62135596800000)/`, or the same instant shifted by a UTC offset of up to 14 hours) and the empty GUID become `null`.
* ISO 8601 durations are kept and gain a `<field>_seconds` number.
* Null and empty fields are dropped by default (`compact_output=false` keeps them).
* Output is capped (`max_chars`); when capped, `truncated` is true and `hint` says how to page.
* Credential-looking fields (password, secret, token, api key and similar) are masked in every response. Export destination, task action and tuner property responses use a stricter mask that also covers user and account fields.
* Date-time inputs are ISO 8601 (for example `2026-11-24T20:15:00Z`, or `now`) and are sent as `/Date(ms)/`.

## Safety model

* **Read-only by default.** Every method has an access class in the catalog: `read` (246), `write` (159) or `blocked` (56). `ss_call_read` and the curated tools only run `read` methods.
* **Blocked methods are never callable**: login and session methods, social integration account getters that may return OAuth tokens, generic String/Object/Guid settings getters, the RTMP stream key getter, raw file stream plumbing, and internal pipeline methods. A method missing from the classification is built as `write` (fail closed).
* **Write staging.** `ss_stage_write` validates parameter names against the catalog, freezes the exact payload with a SHA-256 hash, and returns a preview plus a single-use token that expires in 10 minutes. `ss_commit_write` re-checks the hash and sends exactly that payload once. When the client declares the elicitation capability it asks the user to confirm again, and anything other than an explicit confirm (decline, cancel, a malformed reply, a timeout or any other error) aborts the write. Only a client that does not declare elicitation proceeds on the token alone. The commit tool is annotated destructive and requires user interaction.
* **Throttle.** Writes are serialized and spaced at least `SNAPSTREAM_MCP_WRITE_MIN_INTERVAL` seconds apart; all calls share a concurrency cap. `count` is capped at 1000 and paged reads default to small pages. Gateway rate limits do not cap the load operations put on the recorder, so keep writes deliberate.
* **Audit.** Every stage, commit, decline and cancel, and every attempt to stage a blocked method, is appended to `snapstream_writes.audit.jsonl` (gitignored). The audit holds method names, payload hashes, a `stage_id` (a short hash of the staging token) on every line about a staged write so each line joins to its `staged` line, the confirmation mode on commit lines, and redacted parameters, never credentials or tokens. Export destination, task action, live streaming and federation writes use the stricter mask in the preview and the audit. A commit is refused if the audit file cannot be written.
* **Redaction.** Client ID, client secret, API key and the current access token are masked from every error and log line and from every string in successful tool output, as are JWTs and credential query parameters in URLs (token, sig, signature, key, X-Amz-*); HTTP errors return the status and a short redacted body excerpt.
* **Demo only during development.** Point `secrets.yaml` at the demo instance until the server has been validated. The test suite is fully offline.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest
```

All tests run offline with `httpx.MockTransport` and fake credentials; they never read `secrets.yaml`.

`smoke_test.py` is a manual, read-only live check (token, `ss_status`, GetMediaItems with count 2, SearchRecordings with count 2, and a truncated transcript of the first item). It prints only pass or fail, HTTP status and response shapes. Run it by hand against the demo instance only.

## Regenerating the catalog

`snapstream_mcp/data/api_catalog.json` is generated from the SnapStream API HTML reference:

```powershell
.venv\Scripts\python.exe tools\build_catalog.py C:\path\to\SnapStreamAPI.html
```

The builder removes `authTicket` from every parameter list (recording `"auth_ticket": "auto_resolved"`) and applies the access classes in `tools/access_overrides.json`. Any method not listed there is built as `write` and reported; overrides that no longer match a method are reported too. Review both lists, update `tools/access_overrides.json`, and rebuild before committing.
