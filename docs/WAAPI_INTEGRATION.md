# WaAPI integration

This server-side connector is opt-in and uses the WaAPI contract provided for
`GET /instances`, `POST .../client/action/send-message`, and the `message`
webhook event. It never exposes the WaAPI bearer token to the browser.

## Configuration

Copy `.env.example` to a local `.env` (the real `.env` is gitignored), or set
the values in the server's secret/environment settings:

```dotenv
WAAPI_BEARER_TOKEN=
WAAPI_INSTANCE_ID=
WAAPI_NOTIFY_CHAT_ID=
WAAPI_WEBHOOK_PATH_SECRET=
WAAPI_WEBHOOK_HEADER_NAME=
WAAPI_WEBHOOK_HEADER_VALUE=
CELA_WAAPI_ENABLE=0
```

- `WAAPI_BEARER_TOKEN` is the API credential. Store the token alone; the server
  adds the `Bearer` prefix. Do not commit it, return it from an endpoint, or
  send it to frontend code.
- `WAAPI_INSTANCE_ID` selects the configured instance.
- `WAAPI_NOTIFY_CHAT_ID` is the fixed recipient for authenticated admin
  notifications; incoming customer replies use the webhook's `from` value.
- `WAAPI_WEBHOOK_PATH_SECRET` must be an independent URL-safe random value of at
  least 32 characters. Generate one with
  `python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
- `WAAPI_WEBHOOK_HEADER_NAME` and `WAAPI_WEBHOOK_HEADER_VALUE` are optional
  defense-in-depth. If used, configure the same custom header in WaAPI. The
  value must be a separate URL-safe random secret of at least 32 characters.
- Keep `CELA_WAAPI_ENABLE=0` until the credentials and callback settings are
  verified. With the flag set to `1`, the server starts its durable queue worker
  only when a bearer token and instance ID are configured.

The inbox and outbox share `.cela/waapi-outbox.sqlite3` by default; override the
path with `CELA_WAAPI_OUTBOX_PATH`. The file is gitignored, uses SQLite WAL and
full synchronous writes, and stores message text/chat IDs so queued work survives
restarts. Restrict filesystem access because these records and the normal run
store may contain customer data. No WaAPI secret is stored in SQLite.

## Server endpoints

Admin endpoints require the existing `CELA_API_TOKEN` / `OS_API_TOKEN`:

- `GET /api/waapi/status` returns readiness booleans only.
- `GET /api/waapi/instances?filter=...` calls WaAPI and returns only `id`,
  `name`, and `is_trial`; owner and webhook details are omitted.
- `POST /api/waapi/notifications` accepts `{"idempotency_key":"…","text":"…"}`
  and queues a notification to `WAAPI_NOTIFY_CHAT_ID`.
- `GET /api/waapi/notifications/{message_id}` returns status metadata only,
  never the message body.

The worker sends text using the documented minimal body:

```json
{"chatId":"201126212452@c.us","message":"Hello from CELIA"}
```

It checks nested `data.status` as authoritative. Explicit `data.status: error`
becomes `rejected`; a transport timeout, malformed response, or process crash
becomes `unknown` and is **not automatically retried**, to avoid duplicate sends.
Idempotency keys are unique per instance; reusing a key with different content
is rejected.

## Incoming message webhook

Configure the WaAPI callback URL as:

```text
https://<public-host>/api/webhooks/waapi/<WAAPI_WEBHOOK_PATH_SECRET>
```

Subscribe to the `message` event. The receiver:

1. Compares the path secret in constant time (and checks the optional custom
   header, if configured).
2. Requires `instanceId` to match `WAAPI_INSTANCE_ID`.
3. Accepts only incoming text messages (`event=message`, `type=chat`,
   `fromMe=false`) with an original message ID. Events without an ID are rejected
   because they cannot be safely deduplicated.
4. Commits the event to the SQLite inbox before returning `202`. Duplicate
   message IDs do not start another agent run; reuse with different content is
   rejected. A worker runs the existing agent on each new message and enqueues
   its reply to the originating `from` chat ID.
5. Limits request bodies to 64 KiB and applies a per-process limit of 60 requests
   per minute per client IP. Apply an additional rate limit at the reverse proxy
   in multi-worker/production deployments.

The supplied WaAPI docs do **not** specify an HMAC signature header/algorithm.
This implementation therefore uses a random URL path secret, instance-ID
matching, and an optional configured extra header; it does **not** claim HMAC
verification. If cryptographic event signing is mandatory, confirm the signing
contract with WaAPI before enabling the webhook or processing sensitive actions.

Because the callback secret is in the URL, access logs must redact that path.
The repository's `serve()` entry point disables Uvicorn access logging; configure
any external reverse proxy/load balancer to redact the secret path as well.
The receiver also redacts the path in the ASGI scope after routing. Do not put the
secret in query parameters or logs.

Media is currently not processed; non-`chat` events are acknowledged as ignored.
Each inbound text is handled as a separate agent task; persistent multi-turn chat
history is not implemented.
