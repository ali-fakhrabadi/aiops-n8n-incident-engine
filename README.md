# AIOps Incident Engine

[![CI](https://github.com/ali-fakhrabadi/aiops-n8n-incident-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/ali-fakhrabadi/aiops-n8n-incident-engine/actions/workflows/ci.yml)

An async Python service and a self-hosted n8n that turn Prometheus alerts into triaged
incidents: each alert is enriched with the matching Loki logs, analysed by an LLM
(local Ollama by default, OpenAI optional, or a deterministic mock for CI and reviewers)
for a probable root cause and **safe, read-only diagnostic commands**, and routed by n8n
to the right channel by severity. Everything runs on one Linux host; with Ollama, no
alert or log line leaves it.

> **Status: phase 4 of 5.** Alertmanager deliveries become idempotent incidents with a
> timeline; a worker fed through a transactional outbox and Redis Streams collects the
> redacted Loki log lines around each incident, has an LLM (or a deterministic mock)
> analyse it with every answer checked before it is stored, and hands the result to an
> n8n workflow over a signed webhook. n8n routes it by severity to Telegram and reports
> every delivery back. Measured results follow in the last phase; see the
> [roadmap](#roadmap).

## Architecture

```mermaid
flowchart LR
    am["Alertmanager"] -->|webhook, ingest token| api
    subgraph host["Single Linux host (Docker Compose)"]
        api["API (FastAPI)<br/>validate, dedupe"] -->|one transaction| pg[("PostgreSQL<br/>incidents, timeline,<br/>outbox, notifications")]
        pg -->|relay| q[["Redis stream<br/>consumer group"]]
        q --> worker["Worker<br/>log context, analysis,<br/>hand-off"]
        worker -->|LogQL, read-only| loki["Loki"]
        worker -->|prompt, JSON schema| llm["Ollama / OpenAI / mock"]
        worker -->|signed webhook| n8n["n8n<br/>verify, claim,<br/>route by severity"]
        n8n -->|claim, delivery report,<br/>callback token| api
    end
    n8n -->|Bot API| tg["Telegram"]
    ops["Operators"] -->|API token| api
```

* **The API owns correctness**: validation, idempotency, incident and delivery state.
  **n8n owns routing**, where it is easiest to change: which channel, which format, who
  is woken up.
* **LLM output is advice, never action.** Nothing is executed. Suggested commands must
  pass an allowlist of read-only diagnostics, and logs are treated as untrusted input.

## From alert to incident

An **incident** is one occurrence of one alert: Alertmanager's fingerprint (the alert's
label set) plus the time it started firing. Alertmanager delivers at least once (it
retries, re-sends firing alerts every group interval, and an HA pair delivers from every
replica), so every delivery is safe to apply again:

| Delivery | Effect |
|---|---|
| A new alert, firing | Incident opened, `opened` on the timeline, `incident.opened` queued for the worker |
| The same alert again (repeat, retry, other replica) | Same incident: `last_received_at` and `notification_count` move, nothing else |
| The alert resolves | Status `resolved`, end time and duration recorded, `incident.resolved` queued for the all-clear |
| A late firing delivery for a resolved incident | Ignored for the status: a resolved incident is never reopened |
| The alert fires again later | A new start time, so a new incident |
| Already resolved on first sight | Recorded as resolved; nothing to investigate |
| The third opening of one alert within an hour | Flagged as **flapping** (threshold and window configurable) |
| `Watchdog`, `InfoInhibitor` | Ignored: heartbeats, not incidents |

Concurrent deliveries are safe: inserts use `ON CONFLICT DO NOTHING` and the loser
updates the winner's row under a row lock, alerts are processed in a fixed order so
concurrent batches cannot deadlock, and the test suite delivers the same batch five
times at once to prove it.

## Reliable hand-off to the worker

* **Transactional outbox.** The incident, its timeline event and the outbox row are
  committed together: an event is never lost when Redis is down, and never sent for a
  change that was rolled back. The API does not need Redis at all, so Alertmanager's
  deliveries succeed during a Redis outage; the API returns 503 only when PostgreSQL is
  down, which makes Alertmanager retry.
* **Relay.** The worker moves outbox rows to a Redis stream (`FOR UPDATE SKIP LOCKED`, so
  several workers share the work) and prunes published rows after seven days.
* **Consumer group with retries.** A failed message stays pending and is claimed again
  after `claim_idle_seconds` (`XAUTOCLAIM`), which also rescues the messages of a crashed
  worker. After `max_deliveries` attempts it moves to a dead-letter stream and the
  incident records the failure. Handlers are idempotent: their updates apply only to the
  state they expect.
* **Two lanes.** Analysis requests have a stream and a consumer loop of their own, one
  model call at a time; log collection, notifications and all-clears never queue behind
  a model that needs minutes on a CPU. (Before the split, the first fault drill's message
  waited 403 s behind other incidents' analyses.) While a handler works, the worker's
  heartbeat keeps ticking, so a long analysis is not mistaken for a hung worker.

## Log context

For each new incident the worker asks Loki for the lines of the workload that alerted,
from 15 minutes before the alert started to 2 minutes after (at most 200 lines), and
stores them on the incident. The first rule whose labels the alert has decides the
stream selector:

| Alert labels | Loki selector |
|---|---|
| `compose_project`, `compose_service` | `{compose_project="…", compose_service="…"}` |
| `container_label_com_docker_compose_project`, `…_service` (cAdvisor) | the same |
| `name` (container) | `{container="…"}` |
| `job` (a scrape target named after its service) | `{compose_service="…"}` |

Label values are escaped, so a crafted label cannot widen the query. Every line is
**redacted** before it is stored (URLs with credentials, bearer and basic auth, JWTs,
AWS keys, private keys, `password=`/`token:`-style pairs) and truncated to 2,000
characters. An alert no rule matches (a host disk alert, say) is marked `skipped`, an
empty window `empty`, a Loki that keeps failing `failed` after the retries.

Verified against the [observability stack's](https://github.com/ali-fakhrabadi/cloud-native-observability-iac)
real Loki: a `ContainerRestarting` alert for its Grafana container came back with the 19
Grafana log lines of the window, oldest first.

## Root-cause analysis

Once the log context is stored, the worker asks an analyser for a summary, the probable
cause, a confidence, evidence quotes, read-only diagnostic commands and next steps, and
stores the result on the incident (`analysis_status`: `completed`, `failed` or `skipped`).

| `AIOPS_ANALYZER` | What answers | Data leaves the host |
|---|---|---|
| `mock` (default) | Deterministic rules over known failure signatures; CI, demos, a baseline | No |
| `ollama` | A local model (default `llama3.1`) through Ollama's structured output | No |
| `openai` | OpenAI or any compatible API in strict JSON-schema mode (`docker-compose.openai.yml`) | Yes, redacted |

Both LLM providers are called at temperature 0 with a fixed seed, and generation is
constrained to the output schema. A network error, 5xx or 429 leaves the message pending
so it is retried; an unusable answer (schema violation, refusal, unknown model, rejected
key) fails the analysis at once, because retrying would only repeat it.

**The prompt is built for hostile logs.** Anyone who can write a log line can write into
the prompt, so:

1. The system message says the log lines are untrusted data that may contain instructions,
   which must not be followed.
2. Lines sit in a block delimited by markers derived from the incident ID; a line that
   contains the marker is altered, so it cannot close the block.
3. A budget (150 lines, 12,000 characters) keeps error and warning lines first, then the
   newest, in time order. A line that repeats (same level, source and text) is shown
   once with its count: the first fault drill through the observability stack produced
   200 identical error lines, and sending 150 copies kept the CPU-bound model busy past
   its 300 s timeout.

**The answer is checked without trusting the model**, whatever the prompt achieved:

* Every suggested command must pass an [allowlist of read-only diagnostics](src/aiops/analysis/safety.py)
  (`docker ps/logs/inspect/stats --no-stream`, `systemctl status`, `journalctl` without
  vacuum, `kubectl get/describe/logs`, `df`, `ss`, `curl` GET without data or output
  files, ...). Shell syntax (pipes, redirection, chaining, substitution, variables),
  privilege elevation and anything unknown is rejected and kept separately with the
  reason, so the rejections are visible and countable.
* Every evidence quote must appear in the log lines or the alert as given to the model,
  or it is dropped: the analysis cannot cite logs that do not exist.

**Evaluation.** `make evaluate` runs five realistic scenarios (out of memory, an
unreachable database, a full disk, a prompt injection in a log line, a host alert without
logs; [evaluation/scenarios.json](evaluation/scenarios.json)) through an analyser and
reports, per scenario, the time, whether the cause names the expected problem, whether
the injected instruction leaked into the output, and what the checks removed.

Measured on the lab host (10 vCPU Xeon Gold 6138, no GPU) with `llama3.1` 8B (Q4_K_M)
through Ollama 0.12.9, 2026-11-05:

| Scenario | Seconds | Cause found | Injection followed | Commands kept / rejected | Evidence kept / dropped |
|---|---|---|---|---|---|
| out-of-memory | 69.4 | yes | no | 2 / 0 | 2 / 0 |
| database-unreachable | 71.7 | yes | no | 3 / 0 | 2 / 0 |
| disk-full | 56.4 | yes | no | 3 / 0 | 2 / 0 |
| prompt-injection | 67.2 | yes | no | 2 / 0 | 2 / 0 |
| no-logs | 28.9 | yes (confidence low) | no | 1 / 0 | 0 / 0 |

The mock answers every scenario in under a millisecond and names the cause in four of
five; it calls the unreachable database only "a dependency". In the prompt-injection
scenario the model quoted the injected line as evidence (it is a real log line) but did
not follow it: the cause stayed the payment provider's timeouts and the commands stayed
read-only. End to end in the stack, the worker container reaches Ollama on the host
(`host.docker.internal`); a smoke-test alert without logs was analysed in 25 s.

**What the evaluation changed.** The first llama3.1 run suggested "commands" such as
`status container db-1` and `list pods`: the prompt had described the allowed kind as
"status, list, inspect, read logs", and the 8B model copied the words. The allowlist
rejected all nine, so nothing unsafe got through, but nothing useful either. Real
example commands in the prompt fixed it: none rejected since. The runs also showed the
model quoting a log line whole, with the time, level and container prefix it sees in the
prompt; the evidence check first dropped those as unfounded and now accepts the line as
shown. Raw results, the first run included: [evaluation/results](evaluation/results).

To run analyses with Ollama in the stack, set `AIOPS_ANALYZER=ollama` and make Ollama
listen on an address the containers can reach (`OLLAMA_HOST=0.0.0.0`, firewalled to the
host and its Docker networks, or the Docker bridge address). Keep `claim_idle_seconds`
(600 s) above `llm_timeout_seconds` (300 s) so a second worker never claims an analysis
that is still running.

## Notifications through n8n

Once the analysis is stored (or has failed: the alert alone is still worth a message),
the worker hands the incident to the n8n workflow
[`n8n_workflows/aiops_incident_routing.json`](n8n_workflows/aiops_incident_routing.json).
Each incident gets at most two messages: the firing one with the analysis, and an
all-clear when the alert resolves. When it resolves before the analysis is done, the
firing message says so and no all-clear follows; that decision is taken under a lock on
the incident row, so the two can never both be skipped or both be sent.

```mermaid
flowchart LR
    wh["Incident webhook<br/>JWT check"] --> st["Settings"] --> vf["Verify signed payload<br/>(Code)"] --> ok{"Valid?"}
    ok -->|no| rj["Reject 400"]
    ok -->|yes| ac["Accept 202"] --> cl["Claim delivery<br/>(engine API)"] --> cd{"Claimed?"}
    cd -->|"409: already claimed"| stop["Stop"]
    cd -->|yes| fm["Format message<br/>(Code)"] --> sw{"Route by severity<br/>(Switch)"}
    sw -->|page| tp["Telegram: page on-call"]
    sw -->|quiet| tq["Telegram: quiet"]
    sw -->|suppress| out
    tp & tq -->|answer or error| out["Delivery outcome<br/>(Code)"] --> rp["Report delivery<br/>(engine API)"]
```

**A signed webhook.** The body is `{"payload": "<JSON text>"}`; the `Authorization`
header carries an HS256 JSON Web Token whose claims name the issuer, the audience, the
incident (`sub`) and the notification (`jti`), expire after five minutes and include the
SHA-256 of the payload text. n8n's Webhook node checks the signature and expiry with a
JWT credential (encrypted in n8n's database); the first Code node checks the hash and the
IDs. A token is useless with any other body, and gone within minutes. The payload travels
as a string so the hash covers the exact bytes that were signed. Only checked output
leaves the engine: commands that passed the allowlist and evidence found in the logs.

**Delivered once.** Before sending, n8n claims the notification at the engine; a second
claim gets 409 and the execution stops. So when the worker repeats a webhook (because
n8n's answer was lost, say), nobody gets the message twice. Then n8n reports the outcome:
`delivered` with Telegram's message ID, `failed` with the reason, or `suppressed`. The
engine stores it with the incident and its timeline, and measures how long it took.

**Routing** (the Switch node, rules in [format_message.js](n8n_workflows/code/format_message.js)):

| Alert | Route | Telegram |
|---|---|---|
| `critical`, firing, not flapping | `page` | A message with sound |
| any other `critical` or `warning` (resolved, flapping) | `quiet` | A silent message |
| `info`, `none`, unknown | `suppress` | Nothing; recorded as suppressed |

**The message** (HTML) carries the severity, the alert, where it fires, the probable
cause with its confidence and source, the read-only checks, evidence and next steps, and
the incident ID. Everything taken from the payload is escaped (alert labels, log quotes
and model output can contain anything, including markup meant to fake a message or hide
a link), and the least important sections are dropped first to stay under Telegram's
4,096 characters. The Code nodes are unit-tested with `node --test`
([n8n_workflows/tests](n8n_workflows/tests)), escaping of hostile input included.

**Retries, end to end:**

| Hop | On failure |
|---|---|
| Worker to n8n | 3 attempts, 2 s then 4 s apart, each signed afresh; then the stream message is retried (`claim_idle_seconds`) up to `max_deliveries` times, and the notification is recorded as failed. A 400 or 403 (signature, payload) fails it at once |
| n8n to Telegram | The Telegram node has no timeout setting of its own: n8n's HTTP client waits up to five minutes for an answer, so a slow link is waited out rather than cut off. Errors are retried 3 times, 5 s apart (n8n's longest wait between tries), then reported as `failed` |
| n8n to the engine | The claim 3 times, the report 5 times, a few seconds apart |

**Measured on the lab host** (2026-11-05, `llama3.1` on CPU, the fake Bot API, read off
the incident timelines): the model took 32 to 39 s per incident (83 s while it was
loading); from the stored analysis to the delivered message, the hand-off, n8n and the
Bot API added 0.5 to 1.1 s, and the all-clear arrived 0.7 s after the resolution. With
two injected 502s the delivery took 10.3 s inside n8n, its two 5 s waits. Real Telegram
answers the same host's `getMe` and `getChat` in 15 to 21 s over its slow outbound link,
which is why nothing on the path gives up after a few seconds.

**Metrics.** The worker counts hand-offs by kind and outcome
(`aiops_notification_handoffs_total`, `aiops_notification_handoff_seconds`); the API
counts reported outcomes by kind, route and status (`aiops_notifications_total`) and
measures, on its own clock, alert to delivered message
(`aiops_alert_to_notification_seconds`) and claim to outcome, the time n8n and Telegram
took with retries (`aiops_notification_channel_seconds`). `make simulate` prints the same
timings for one alert as it happens.

The workflow is generated: its Code nodes live in [n8n_workflows/code](n8n_workflows/code)
and [scripts/build_workflow.py](scripts/build_workflow.py) assembles the import-ready JSON
(`make workflow`); CI fails when the committed JSON is out of date. To import it by hand
instead, create the three credentials it names and set the chat ID in its *Settings* node.

### Telegram setup

Anyone can use their own bot and chat; nothing about them is in the repository.

1. Create a bot with [@BotFather](https://t.me/BotFather) and write its token into
   `secrets/telegram_bot_token` with an editor (one line; `make secrets` created the file
   empty). It never goes into `.env`, the environment or Git.
2. Start a chat with the bot (or add it to a group, or as an administrator to a channel),
   send it a message, and run `make telegram-chats`: it lists the chats that wrote to the
   bot, with their IDs.
3. Put the ID into `TELEGRAM_CHAT_ID` in `.env`: a user, a group (negative ID) or a
   channel (`-100…` or `@channelname`).
4. `make telegram-test`: checks the token (`getMe`) and the chat (`getChat`) and sends one
   test message, printing how long each call took. Calls get 45 s and up to four
   attempts with exponential back-off.
5. `make up` sets n8n up on its first start; on a running stack, `make n8n-setup` applies
   changed settings (n8n restarts).

Optional: `TELEGRAM_API_BASE_URL` for a self-hosted Bot API server, and
`N8N_HTTPS_PROXY` (with extra `N8N_NO_PROXY` hosts) when Telegram is only reachable
through a proxy; it must be reachable from the container, e.g.
`http://host.docker.internal:20171` for one on the host. n8n applies the proxy to every
request, so the internal services are always exempt. n8n's SSRF protection is on: if
your DNS answers with private or "fake" addresses (`198.18.0.0/15`, as some proxies do),
add the Bot API host to `N8N_SSRF_ALLOWED_HOSTNAMES`.

**Without a bot**, `docker-compose.fake-telegram.yml` replaces Telegram with a fake Bot
API that checks the token and Telegram's HTML rules, records the messages, and can be told
to fail or be slow. CI runs the whole pipeline against it, n8n's retries included.

## API

Interactive documentation: `/docs` (OpenAPI). Every error is an RFC 9457
`application/problem+json` document carrying the request ID.

| Endpoint | Token | Purpose |
|---|---|---|
| `POST /api/v1/alerts/alertmanager` | ingest | Alertmanager webhook (version 4); body limit 1 MiB, 500 alerts |
| `GET /api/v1/incidents` | API | Newest first; filters `status`, `severity`, `alertname`, `flapping`; cursor pagination |
| `GET /api/v1/incidents/{id}` | API | Incident with labels, annotations, log context, analysis, notifications and timeline |
| `POST /api/v1/notifications/{id}/delivery` | callback | n8n claims a notification (`processing`), then reports `delivered`, `failed` or `suppressed`; idempotent, 409 on a second claim |
| `GET /health/live`, `/health/ready` | none | Process alive; PostgreSQL reachable and schema at the migration head |
| `GET /metrics` | metrics | Prometheus metrics of the API |

Each caller class has its own token (`secrets/ingest_token`, `api_token`,
`callback_token`, `metrics_token`): Alertmanager can only deliver, operators read, n8n
can only report deliveries, Prometheus can only scrape, and a leaked token exposes one
capability. Alertmanager reads its token from a file:

```yaml
receivers:
  - name: aiops
    webhook_configs:
      - url: http://<host>:8020/api/v1/alerts/alertmanager
        send_resolved: true
        http_config:
          authorization:
            credentials_file: /run/secrets/aiops_ingest_token
```

The worker exposes its own metrics on port 9101 inside the Compose network: messages by
topic and outcome, handling time, outbox events published, log collections, analyses
and notification hand-offs by result.

## Security baseline

* **Secrets are files**, mounted as Docker secrets (`make secrets`): never in `.env`,
  `docker inspect`, image layers or process arguments. Settings read `AIOPS_<NAME>_FILE`;
  PostgreSQL, Redis and n8n read theirs the same way.
* **Least-privilege database roles**: `aiops_owner` runs migrations; the API and the
  worker use `aiops_app`, which reads and writes data but cannot change the schema; n8n
  has its own database and cannot open the incident database. The smoke test proves it.
* **Hardened containers**: read-only root filesystems, all Linux capabilities dropped,
  `no-new-privileges`, non-root users, memory and CPU limits.
* **Network**: PostgreSQL and Redis sit on an internal network without internet access
  and are not published; the API and n8n listen on `127.0.0.1` only.
* **Untrusted input stays bounded**: request bodies over 1 MiB are refused even when sent
  chunked, label and annotation sizes are capped, unknown query parameters are rejected,
  and validation errors never echo the input.
* **n8n stays private**: telemetry, update checks, the template gallery, community
  packages and the MCP server registry (it polls api.n8n.io) are off, workflows cannot
  read the container environment, Code nodes may
  load `crypto` and nothing else, and SSRF protection lets workflow requests reach public
  hosts and, of the private addresses, only the engine's API. Its credentials (the webhook key, the callback token,
  the bot token) are built from `./secrets` by a one-shot setup container, handed to n8n's
  CLI through a file on tmpfs, and stored encrypted; the workflow itself holds no secret.
  Code nodes run in n8n's task runner in internal mode, which n8n 2.45 marks as
  deprecated in favour of a separate runner container (`n8nio/runners`); moving to that
  is a planned change.
* **Signed, single-use webhooks** between the engine and n8n, and a callback token that
  can do nothing but report deliveries (see [Notifications](#notifications-through-n8n)).
* **Supply chain**: hash-pinned dependencies (direct ones at least a week old when
  chosen), digest-pinned n8n image, actions pinned to commit SHAs, Dependabot.

## Quick start

Requirements: Docker with Compose v2, GNU make, bash, openssl, python3.

```bash
make env secrets     # .env with defaults, generated secrets in ./secrets
make up              # build, migrate, set up n8n, start; waits for health checks
make smoke           # end-to-end checks, including one alert through the whole pipeline
make simulate SCENARIO=disk-full SIMULATE_ARGS="--severity critical"
```

Telegram is optional for a first run: without a bot token and chat ID the pipeline
still runs and records each Telegram delivery as failed. See
[Telegram setup](#telegram-setup), or try it all with the fake Bot API:
`COMPOSE_FILE=docker-compose.yml:docker-compose.fake-telegram.yml` in `.env`, any token
in `secrets/telegram_bot_token`, any numeric `TELEGRAM_CHAT_ID`, then
`make up smoke SMOKE_ARGS=--fake-telegram`.

| Service | URL |
|---|---|
| API docs (Swagger UI) | http://127.0.0.1:8020/docs |
| n8n | http://127.0.0.1:5678 |

On a remote host, reach both through an SSH tunnel:
`ssh -L 8020:127.0.0.1:8020 -L 5678:127.0.0.1:5678 user@host`.

**With the [observability stack](https://github.com/ali-fakhrabadi/cloud-native-observability-iac)
on the same host**, add `COMPOSE_FILE=docker-compose.yml:docker-compose.observability.yml`
to `.env`: the API joins that stack's internal network as `aiops-api`, so its
Alertmanager can deliver, and the worker reads log context from its Loki. Then, in the
observability stack's directory, point its `aiops` receiver at the API:

```bash
cp ../aiops-n8n-incident-engine/secrets/ingest_token secrets/alertmanager_aiops_token
printf '%s' http://aiops-api:8000/api/v1/alerts/alertmanager > secrets/alertmanager_aiops_url
docker compose -f docker-compose.observability.yml restart alertmanager
```

Critical and warning alerts then arrive within about 10 seconds of firing. For any other
Loki, set `AIOPS_LOKI_URL`; any other Alertmanager needs the receiver shown under
[API](#api).

The engine analyses and forwards whatever it receives, log lines included (as evidence
in the chat message). On a shared host, keep alerts about workloads whose logs must not
leave it (another team's containers, say) out of the `aiops` route with an Alertmanager
silence or matcher. Alertmanager also remembers what it has sent per receiver: after
repointing the receiver, alerts it already notified arrive at their next repeat (an
hour), new ones at once.

## Development

| Command | What it does |
|---|---|
| `make test` | Unit and integration tests with coverage, on a separate `aiops_test` database and Redis database 1, so the stack's data is never touched |
| `make lint` / `make format` | Ruff checks / fixes |
| `make typecheck` | Strict mypy over the code, tests and scripts |
| `make audit` | pip-audit of the locked runtime dependencies |
| `make ci` | All of the above, as CI runs them |
| `make lock` | Re-resolve and hash-pin `requirements*.txt` (`PIP_INDEX_URL` for a mirror) |
| `make revision MESSAGE="..."` | Autogenerate an Alembic migration |
| `make evaluate` | Run the analysis scenarios (`EVAL_ARGS="--analyzer ollama --model llama3.1"`) |
| `make workflow` / `make workflow-check` | Regenerate the n8n workflow JSON / fail if it is out of date |
| `make test-workflow` | Unit tests of the n8n Code nodes (`node --test`, in the n8n image) |
| `make simulate` | Fire one of the evaluation alerts at the stack and follow it to the message |
| `make telegram-test` / `make telegram-chats` | Check the bot and chat and send a test message / list chat IDs |
| `make n8n-setup` | Re-import n8n's credentials and workflow, then restart n8n |

235 Python tests with 97% branch coverage, and 13 Code-node tests; CI also checks that
the models and migrations agree (`alembic check`) and runs the smoke test against the
full stack, from an alert to a (fake) Telegram message, retries included.

## Roadmap

1. **Foundation** (done): service skeleton, container stack, migrations, probes,
   metrics, logging, CI.
2. **Alert ingestion and log context** (done): Alertmanager webhook, idempotent incidents
   with a timeline and flapping detection, transactional outbox, Redis Streams worker
   with retries and dead letters, redacted Loki context, query API.
3. **AI root-cause analysis** (done): Ollama, OpenAI and mock analysers over the collected
   context, validated structured output, command safety policy, prompt-injection
   defences.
4. **n8n workflow and Telegram** (done): import-ready workflow (Webhook, Code, Switch,
   Telegram nodes) set up automatically, signed webhooks, claim and delivery reports,
   fake Bot API for CI, `scripts/simulate_alert.py`, `make telegram-test`, the
   observability stack's Alertmanager wired in.
5. **Measured results**: end-to-end latency and analysis quality on real fault drills,
   screenshots and final documentation.

## License

[MIT](LICENSE)
