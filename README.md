# snapstream-mcp

MCP server for the SnapStream tenant API (the cloud API served through AWS API Gateway). It lets an MCP client such as Claude Code search recordings, read transcripts and smart chapters, browse the program guide and scheduled recordings, stage clips, and call any read method in the API catalog. Writes go through a stage, approve and commit gate.

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
| `scopes` | no | space-separated; omitted from the token request when empty (the spec's scope is `api/read api/write`) |
| `instance` | no | label shown by `ss_status` |

The server refuses any `REPLACE_ME` placeholder and any non-https URL. Error messages name the key, never the value. To keep the file elsewhere, set `SNAPSTREAM_MCP_SECRETS` to its path.

Each call gets an access token from the Cognito token endpoint (client_credentials), caches it, and refreshes it 60 seconds before it expires; a 401 triggers one refresh and retry. Requests are `POST {base_url}/{url_path}/json/{Method}` with `Authorization: Bearer <token>`, `x-api-key`, and a JSON body keyed by parameter name. `url_path` is the gateway path segment from the catalog: `Config`, `LocalConfig` and `Notifier` for the Configuration, LocalConfiguration and NotifierManager services, the service name for the rest. Tools accept either name.

The gateway resolves the session from the client_id, so every call runs as the one SnapStream user mapped to the API client, and a token with only `api/read` scope does not stop writes. A caller-supplied `authTicket` is always removed. The 19 methods whose permission check depends on their input (ClipFile, CreateScreenshot, the ClipAnd*, Tweet*, Slack* and upload methods) are sent with `"authTicket": {}`, because the gateway's permission inspector throws a NullReferenceException without it on builds before the fix. `SNAPSTREAM_MCP_EMPTY_AUTH_TICKET=off` turns the stub off once tenants are upgraded.

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

Optional environment variables:

| Variable | Default | Notes |
|---|---|---|
| `SNAPSTREAM_MCP_SECRETS` | `secrets.yaml` in the repo root | |
| `SNAPSTREAM_MCP_TIMEOUT` | 28 | seconds; under the gateway's 29 s limit |
| `SNAPSTREAM_MCP_MAX_CONCURRENCY` | 4 | concurrent requests |
| `SNAPSTREAM_MCP_RATE_PER_SEC` | 8 | token bucket refill for every API request (the gateway allows 10/s) |
| `SNAPSTREAM_MCP_BURST` | 16 | token bucket size |
| `SNAPSTREAM_MCP_DAILY_BUDGET` | 20000 | soft cap on API calls per UTC day (the key allows 100,000/day, shared by everyone using it) |
| `SNAPSTREAM_MCP_WRITE_MIN_INTERVAL` | 5 | seconds between write calls |
| `SNAPSTREAM_MCP_MAX_RESPONSE_CHARS` | 40000 | |
| `SNAPSTREAM_MCP_EMPTY_AUTH_TICKET` | on | send `"authTicket": {}` to the methods that need it |
| `SNAPSTREAM_MCP_AUDIT_FILE` | `snapstream_writes.audit.jsonl` in the repo root | the daily call counter, `snapstream_calls.daily.json`, is kept beside it |

## Tools

### Status, catalog and generic access

| Tool | What it does |
|---|---|
| `ss_status` | Instance label, API host, config validity, token check, catalog counts, pending writes, calls today against the daily budget, and the request rate. Never returns secrets. |
| `ss_api_catalog` | Offline search of all 479 methods by keyword (including web app names such as "Create for Social"), service and access class. |
| `ss_api_describe` | One method's parameters, permissions, return type, access class and an example params object, plus the field list (types, descriptions, enum names) of complex parameter types such as ClipProperties. |
| `ss_call_read` | Calls any method whose access is `read`. Refuses write and blocked methods. |
| `ss_stage_write` | Validates and freezes a write (including the keys inside complex parameters), returns a preview and a single-use token (10 minute TTL). |
| `ss_stage_clip` | Builds and stages a clip from a recording: reads the media item, converts seconds, `hh:mm:ss` or `ms:<n>` to 100 ns ticks, bounds-checks against the duration, and stages TaskManager/ClipFile (`kind="clip"`) or AiClipDesigner (`kind="social"`, Create for Social). Social clips need an explicit `orientation`: the spec says 0 = vertical, a live test rendered 1 as vertical, so the value is unresolved. |
| `ss_commit_write` | Sends exactly the staged payload once. The only tool that changes SnapStream. |
| `ss_cancel_staged` | Discards a staged write. |
| `ss_pending_writes` | Lists staged writes that have not been committed. |

### Curated read tools

| Tool | API method |
|---|---|
| `ss_search_recordings` | Search/SearchRecordings (type defaults to `default`, 254) |
| `ss_count_recording_matches` | Search/SearchRecordingsCount |
| `ss_search_in_recording` | Search/SearchRecordingWithRefinement |
| `ss_search_guide` | Search/SearchAirings (type defaults to `all`, 7) |
| `ss_list_media_items` | Library/GetMediaItems |
| `ss_get_media_item` | Library/GetMediaItem |
| `ss_get_transcript` | Library/GetTranscript (windowed by caption index, seconds offset and size; optional text filter), after a Library/GetMediaItem permission check |
| `ss_get_smart_chapters` | Library/GetSmartChapters (commercial-break segment boundaries, not AI topic chapters), after a Library/GetMediaItem permission check |
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

Search types are bit flags sent as integers. Recordings (CCSearchTypes): `description` 1, `title` 2, `captions` 4, `advanced_description` 8, `tags` 16, `as_run` 32, `speaker` 64, `ai_chapters` 128, `program_info` 58, `default` 254, `all` 255. Guide (EpisodeSearchTypes): `description` 1, `title` 2, `people` 4, `all` 7 (the spec's All is 3 and leaves out people). Names are case-insensitive; a list of names or `"a|b"` is OR-ed; an integer passes through. Caption searches return caption-level hits with an offset into the recording; title and program_info searches return one hit per recording.

### Response format

* The `<Method>Result` wrapper is removed.
* `/Date(ms[+-hhmm])/` becomes ISO 8601 UTC. The min date (`/Date(-62135596800000)/`, or the same instant shifted by a UTC offset of up to 14 hours) and the empty GUID become `null`.
* ISO 8601 durations are kept and gain a `<field>_seconds` number.
* Tick fields (100 ns units: `StartPosition`, `EndPosition`, `*Ticks`, and smart chapter `Start`/`End`) keep their integer and gain a `<field>_seconds` number.
* Null and empty fields are dropped by default (`compact_output=false` keeps them).
* Output is capped (`max_chars`); when capped, `truncated` is true and `hint` says how to page.
* Credential-looking fields (password, secret, token, api key and similar) are masked in every response. Export destination, task action and tuner property responses use a stricter mask that also covers user and account fields.
* Date-time inputs are ISO 8601 (for example `2026-11-24T20:15:00Z`, or `now`) and are sent as `/Date(ms)/`.

## Safety model

* **Read-only by default.** Every method has an access class in the catalog: `read` (249), `write` (162) or `blocked` (68). `ss_call_read` and the curated tools only run `read` methods.
* **Blocked methods are never callable**: login and session methods, credential generators (GenerateApiLoginCredentials, GenerateTVBoxLoginCredentials, TestLiveFeedConnection), social integration account getters that may return OAuth tokens, generic String/Object/Guid settings getters, the RTMP stream key getter, the FileAccess methods (not exposed by the gateway per the OpenAPI spec), and internal pipeline methods. A method missing from the classification is built as `write` (fail closed).
* **Permission bypass guard.** Library/GetTranscript and GetSmartChapters skip folder permissions server-side, so the curated tools and `ss_call_read` first call Library/GetMediaItem for the same id and refuse with `not_found_or_no_access` when it returns nothing or fails.
* **Faults are failures.** The API reports many errors as HTTP 200 with a base64 `CustomError` header, or as an XML, HTML or WCF fault body. These raise `api_fault` with the first line of the message (stack frames stripped, redacted) and the gateway's `x-amzn-RequestId`; a write that gets one is never reported as committed.
* **Write staging.** `ss_stage_write` validates parameter names against the catalog, checks the keys inside complex parameters against the API schemas (an unknown key or a case-only mismatch such as `subtitles` for `Subtitles` is refused with a "did you mean", never auto-corrected; `__type` selects a subtype), checks ClipProperties tick positions (integers, start before end, at least 1 s, shown as `hh:mm:ss` in the preview), freezes the exact payload with a SHA-256 hash, and returns a preview plus a single-use token that expires in 10 minutes. `ss_commit_write` re-checks the hash and sends exactly that payload once. When the client declares the elicitation capability it asks the user to confirm again, and anything other than an explicit confirm (decline, cancel, a malformed reply, a timeout or any other error) aborts the write. Only a client that does not declare elicitation proceeds on the token alone. The commit tool is annotated destructive and requires user interaction.
* **Unknown outcomes.** A commit that times out, loses its connection after sending, or gets HTTP 502, 503 or 504 returns `outcome_unknown` (not committed, not failed) with a `verify_hint` naming the read that confirms it (TaskManager/GetTaskLists or `ss_get_job` for clips; Scheduler/GetRecordingJobs or GetCurrentAndUpcomingRecordings for recordings; NotifierManager/GetMyAlerts for alerts). A canceled or interrupted commit is audited as `commit_unknown` the same way. A fault from the method itself or an HTTP 5xx is `error` with a `verify_hint`, since part of the write may have run; only failures known to come before the method ran (the gateway permission inspector, HTTP 4xx, connect failures) say "Not committed". A RecordAiring "already scheduled" fault is `already_done`. A write whose return type is Guid must answer with a non-empty GUID, or it is reported as `error` (`unexpected_result`). Payloads are sent with `__type` first in every object, as WCF requires. Writes are never retried automatically.
* **Throttle.** Writes are serialized and spaced at least `SNAPSTREAM_MCP_WRITE_MIN_INTERVAL` seconds apart; all calls share a concurrency cap and a token bucket. Reads that get 429 or 503 are retried (Retry-After or jittered backoff, 3 attempts in all). A soft daily budget refuses calls with `kind="budget"` once spent. `count` is capped at 1000 and paged reads default to small pages. Gateway rate limits do not cap the load operations put on the recorder, so keep writes deliberate.
* **Audit.** Every stage, commit (`commit_done`, `commit_error`, `commit_unknown`), decline and cancel, and every attempt to stage a blocked method, is appended to `snapstream_writes.audit.jsonl` (gitignored). The audit holds method names, payload hashes, the gateway request id on commit lines, a `stage_id` (a short hash of the staging token) on every line about a staged write so each line joins to its `staged` line, the confirmation mode on commit lines, and redacted parameters, never credentials or tokens. Export destination, task action, live streaming and federation writes use the stricter mask in the preview and the audit. A commit is refused if the audit file cannot be written.
* **Redaction.** Client ID, client secret, API key and the current access token are masked from every error and log line and from every string in successful tool output, as are JWTs and credential query parameters in URLs (token, sig, signature, key, X-Amz-*); HTTP errors return the status and a short redacted body excerpt.
* **Demo only during development.** Point `secrets.yaml` at the demo instance until the server has been validated. The `tests/` suite is fully offline; the opt-in live suite is described below.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest
```

All tests run offline with `httpx.MockTransport` and fake credentials; they never read `secrets.yaml`. `pytest` with no arguments runs only `tests/` (`testpaths`), never the live suite.

`smoke_test.py` is a manual, read-only live check (token, `ss_status`, GetMediaItems with count 2, SearchRecordings with count 2, and a truncated transcript of the first item). It prints only pass or fail, HTTP status and response shapes. Run it by hand against the demo instance only.

## Live test suite

`tests_live/` runs the real server code (the `Runtime` and the `ss_*` tool functions, plus one session over stdio) against the instance in `secrets.yaml`, to find what mocks cannot. It is built to be safe on the shared demo instance, where the API client is mapped to one user, so anything it creates shows as that user's.

* Nothing runs without `--live`: every test is skipped at collection, no config is read and no call is made.
* It refuses to start unless `secrets.yaml` points at `demo.cloud.snapstream.com` (override with `SNAPSTREAM_LIVE_EXPECTED_HOST`).
* A write firewall wraps the client. In the read tier every write is refused before it leaves the machine. In the write tier only TaskManager/ClipFile and AiClipDesigner (hard cap 2 per run) and Library/DeleteMediaItem (only for ids this run created and verified, one per create) can pass. The firewall refuses every write unless the run was started with `--live-writes`, and the write tier's fixture skips without it, so the gate does not rest on the collection-time skip marker alone (for example under `-p no:skipping`).
* Output: pass, fail and skip lines plus the report path. Assertion messages name methods, key paths, error kinds and HTTP statuses, never response values, tokens or secrets. pytest's default traceback (`--tb=auto` or `long`) prints each frame's arguments, and `-l` prints its locals, which here would be fixture items and response dicts. So with `--live` the suite forces `--tb=short` when the style is `auto` or `long`, turns off `-l`/`--showlocals` and `--full-trace` (extra arguments passed through `live_test.py` cannot turn them back on), and drops the arguments and locals sections from every failure report. A failure shows source lines and the message the test wrote. Failure reports are also passed through the server's redactor.

```powershell
# offline checks: collects, and skips everything without --live
.venv\Scripts\python.exe -m pytest tests_live --collect-only -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests_live -p no:cacheprovider

# read tier (roughly 400 API calls; no writes)
.venv\Scripts\python.exe live_test.py

# read + write tier: W1 standard clip, then delete it
.venv\Scripts\python.exe live_test.py --writes

# also W2 Create for Social with an explicit AiClipOrientation value (unresolved: 0 or 1 = vertical)
.venv\Scripts\python.exe live_test.py --writes --orientation 1

# extra arguments go to pytest, for example one file or one test
.venv\Scripts\python.exe live_test.py -k sweep

# the same without the runner
.venv\Scripts\python.exe -m pytest tests_live --live [--live-writes] [--orientation N] [--report PATH] -p no:cacheprovider -rfEs
```

Read tier (`--live`):

| File | Covers |
|---|---|
| `test_01_environment.py` | `ss_status` config and token, `ss_whoami`, Config/GetApiLevel under both service names (gateway path mapping), the daily call counter, and one stdio session (`python -m snapstream_mcp`: initialize, list_tools, `ss_status`, one search) |
| `test_02_curated_reads.py` | every curated read tool with real ids: shape (ISO dates, no `/Date(`, `_seconds` siblings, no error key, no secrets), paging and the 1000 count cap, transcript windows by caption, offset and text filter, every named search type, caption vs title result shapes, list OR-ing, guide by channel and by time, what's on, recordings, jobs, storage and scheduler status |
| `test_03_catalog_sweep.py` | every catalog method with access read and spec read-only true, once, with params built from its schema and the discovered ids. Fails on HTTP 404, timeouts or network errors, auth/config/budget errors, a local crash or a leaked secret; records API faults from synthetic params without failing. Reads whose spec read-only flag is false are skipped and listed |
| `test_04_guards.py` | local refusals with no API call (unknown param, write via `ss_call_read`, blocked method for read and stage, bad search type, unknown nested key, case-only mismatch, millisecond positions), `not_found_or_no_access` for a random GUID transcript, stage then cancel, reused and expired tokens |
| `test_05_rate.py` | a burst of 30 reads: no 429 reaches the caller and the time matches the token bucket |

Discovery runs once per run: a completed, non-clip media item of at least 2 minutes with captions, a channel, an airing, a recording job, a task list, a drive and, if one exists, a clip or bookmark. The media item comes from caption searches (`-IS:CLIP`, hits on clips or on items still recording dropped before they are checked, two pages per word), then from the Library when every candidate is rejected; the reason lists rejection counts. A test whose fixture is genuinely empty is skipped with the reason. A fixture that is missing because a tool returned an error (or discovery raised) fails the dependent tests instead, so a broken tool does not show up as a skip.

Write tier (`--live --live-writes`, `test_10_writes.py`): W1 stages a 10 s standard clip from 60 s into the fixture item titled `MCP live test <UTC timestamp>`, checks the preview (ticks, hh:mm:ss, mapped-user line), commits, polls the task list until it finishes (8 minutes at most for a clip, 10 for a social clip; renders take 2 to 7 minutes), finds the clip under its parent (Library/GetMyClipsForItem first, then the parent's clips and bookmarks, then the newest Library pages, each read page by page past output capping) and checks its title and length. W2 (only with `--orientation`) does the same through Create for Social with Subtitles on. Cleanup runs in a finalizer even when a test fails. It first waits up to 5 more minutes for a render the test stopped waiting for; one still running after that is reported with its task list id, to cancel by hand with TaskManager/DeleteTaskList (not StopTask), and nothing is deleted. It deletes only a media item whose id this run recorded, whose title is exactly this run's title, which is marked as a clip of the fixture item (Clip true, or ClipParentId equal to it) and whose file (FolderPathId and Filename) differs from the fixture item's, all re-checked with GetMediaItem right before a staged and committed Library/DeleteMediaItem. What it deleted, or could not delete and why, goes in the report.

Reports go to `live_reports/<UTC timestamp>.md` and `.json` (gitignored; `--report PATH` to choose). They hold test outcomes, discovery results, the sweep table (method, outcome, HTTP status, short message, latency), the burst timing, the clips created and the cleanup results, never response bodies. Stages and commits are also written to the normal audit log, and the calls count against the shared daily budget.

## Regenerating the catalog

`snapstream_mcp/data/api_catalog.json` is generated from the tenant API OpenAPI spec, with the HTML reference as an optional supplement for display names, short summaries and notes:

```powershell
.venv\Scripts\python.exe tools\build_catalog.py C:\path\to\snapstream-tenant-api.json --html C:\path\to\SnapStreamAPI.html
```

Passing only the HTML reference still works as a fallback (no schemas or spec flags). The builder:

* records each service's `url_path` and, per method, the spec's permissions, features, read-only flag, `valid_ticket_only` (the SCRUM-7181 gateway limitation) and `auth_ticket` (`empty_object` when the permission check depends on the input, else `omit`);
* applies the access classes in `tools/access_overrides.json` (authoritative). Any method not listed there is built as `write` and reported; overrides that no longer match a method are reported too. A `read` method the spec flags as not read-only fails the build unless its row has a `read_only_comment`. Rows marked `not_in_spec` (the FileAccess methods) are kept and must be `blocked`;
* writes a trimmed `schemas` section from `components.schemas` (fields with type and description, enum names, the four bit-flag enums parsed from their descriptions, `__type` hints) with `tools/type_overrides.json` merged over it. The overrides correct ClipProperties from the team's DX feedback (required fields, case-sensitive `Subtitles`, fields the spec leaves out). The source-item fields copied from GetMediaItem (ItemId, FolderPathId, Filename and the rest) sit on BaseClipProperties, so every clip properties type, social ones included, accepts them.

Review the reported lists, update the override files, and rebuild before committing.
