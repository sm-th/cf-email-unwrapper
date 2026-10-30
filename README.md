# cf-email-unwrapper

A small Python service that runs next to one A2A agent and lets trusted people talk to it by
S/MIME email. It is the second half of an email ↔ A2A bridge; the first half is
`cf-email-gateway`, a Cloudflare Worker that receives mail, keeps it in one mailbox Durable Object
per agent address and sends the replies this service produces. This service connects to its
mailbox over a WebSocket.

```
author ──S/MIME──▶ Email Routing ─▶ gateway ─▶ mailbox Durable Object (per address)
                                                    ▲ │
                                     send / ack     │ │ mail
                                                    │ ▼  (WebSocket)
                          agent ◀──A2A SendMessage── unwrapper (this repo)
                            │                         ▲
                            └──push notification──────┘
author ◀── send_email ◀── gateway ◀── signed (+ encrypted) reply
```

The gateway has no keys and checks nothing; this service holds the bot key and performs every
S/MIME check. The agent only speaks plain A2A.

## What it does

1. Connects to `<GATEWAY_URL>/mailbox/<AGENT_ADDRESS>` (see [Gateway protocol](#gateway-protocol))
   and keeps the connection open, reconnecting with backoff (1 s doubling to 60 s). On every
   connection the gateway sends all stored mail, oldest first, then new mail as it arrives.
2. Handles each mail in its own task, so a slow agent never blocks receiving.
3. Opens the message with the OpenSSL CLI:
   - decrypts `application/pkcs7-mime` enveloped data with the bot encryption certificate and key
     (RSA key transport, PKCS#1 v1.5 or OAEP, or EC key agreement; signed-but-unencrypted mail is
     accepted too);
   - requires an S/MIME signature (`multipart/signed` or opaque signed-data) that verifies against
     `CA_CERT_FILE` with purpose `smimesign`; unsigned mail is rejected;
   - requires the signer certificate's email (SAN rfc822Name or subject emailAddress) to equal the
     `From` address, and that address to be in `ALLOWED_SENDERS`;
   - requires a CMS `signingTime` no older than `MAX_SIGNATURE_AGE` (and not more than five
     minutes in the future); a missing signingTime is rejected;
   - picks the author's reply encryption certificate from the certificates carried in the
     signature: issued for the author's address, an RSA key with keyUsage `keyEncipherment` or an
     EC key with keyUsage `keyAgreement`, chains to `CA_CERT_FILE` (`openssl verify`). The most recently
     issued match wins.
4. Rejected mail is logged and acknowledged, which deletes it from the mailbox. Unauthenticated
   senders never get a reply.
5. Authenticated mail is sent to the agent with A2A `SendMessage` (non-blocking,
   `returnImmediately: true`) with a push notification config pointing to this service's push URL
   and the configured `PUSH_TOKEN`. The message text is the email's `text/plain` body, or its HTML
   body converted to text; the message carries no email metadata. The email context needed for the
   reply (author, subject, Message-ID, References, reply certificate) is stored in the local state
   file under the task id.

   Once the agent has accepted the task, the mail is acknowledged. If the agent cannot be reached,
   the mail is not acknowledged and delivery is retried every 30 s until it succeeds; if the
   process stops meanwhile, the gateway sends the mail again on the next connection. A mail whose
   Message-ID the agent already accepted is acknowledged without a second `SendMessage`.
6. When the agent sends a push notification, the service checks the `X-A2A-Notification-Token`
   header, fetches the task with `GetTask` and replies only for the states `completed`,
   `input-required`, `failed`, `rejected` and `canceled`, using the email context from the state
   file. The reply text is the text of the task's artifacts plus its status message.
7. The reply is signed (detached, SHA-256) with the bot signing certificate; the signature also
   carries the bot encryption certificate, so the author's client can encrypt to the bot. It is
   then encrypted with AES-256 for the author's reply certificate when one was found (OpenSSL
   defaults: RSA PKCS#1 v1.5 key transport, or for EC the ECDH KDF
   `dhSinglePass-stdDH-sha1kdf-scheme`, which iOS Mail requires). Without a reply certificate the
   reply is signed only. Replies larger than 20 MB are replaced by a short notice.
8. The finished reply is stored in the local outbox in the same transaction that records the
   (task, state) answer; the push endpoint then returns 200 (502 if the task could not be fetched
   or the reply not stored, so the agent may retry). A sender sends due outbox entries over the
   WebSocket whenever it is connected (right after each connection and whenever a reply is
   stored) and deletes an entry once the gateway reports it `sent`. On an `error` or no answer
   within 60 s the entry is retried after 30 s, doubling up to 1 h; an entry still failing after
   24 h is dropped with an error log.

Each (task, state) pair is answered at most once, also across restarts. On startup the service
fetches every task that has not reached a final reply (`completed`, `failed`, `rejected`,
`canceled`) with `GetTask` and answers those that finished while it was down, so a notification
lost during downtime does not lose the reply.

### State

One SQLite file per instance (`STATE_PATH`) holds the email context of every mail handed to the
agent, the replies made and the outbox of replies not yet confirmed by the gateway. Keep it on
persistent storage; losing it loses the replies for tasks still running, unsent replies and the
protection against answering twice. Mail and reply rows are not pruned; outbox rows are deleted
once sent or dropped.

### Delivery guarantees

The gateway delivers mail at least once: a mail stays in the mailbox until this service
acknowledges it and is sent again on the next connection otherwise. Duplicates are recognised by
Message-ID, so each email reaches the agent once. Replies are at least once as well: a reply whose
`sent` confirmation was lost (connection drop, timeout) is sent again.

### Threads and ids

- `contextId` = SHA-256 hex of `thread:<root Message-ID>`, where the root is the first Message-ID
  in `References`, else `In-Reply-To`, else the message's own `Message-ID`. Every mail in a thread
  therefore lands in the same agent conversation.
- `messageId` = SHA-256 hex of `message:<Message-ID>`, so a redelivered email keeps its A2A id.
- Reply headers: `From` = agent address, `To` = author, `Subject: Re: <subject>` (RFC 2047 encoded
  when needed, no repeated `Re:`), `In-Reply-To: <parent Message-ID>`,
  `References: <parent References> <parent Message-ID>`, a new `Message-ID`, `Date`,
  `Auto-Submitted: auto-replied`.
- Priority: agent answers (`completed`, `input-required`) get `X-Priority: 4 (Low)` and
  `Importance: low`; failures (`failed`, `rejected`, `canceled`) get `X-Priority: 2 (High)` and
  `Importance: high`.

### Limitations (v1)

- Text only: attachments are ignored inbound, and replies are a single `text/plain` part.
- Every email starts a new A2A task in the thread's context; replies to an `input-required`
  question are not attached to the waiting task.

## Gateway protocol

`GET /mailbox/<percent-encoded address>` with `Authorization: Bearer <GATEWAY_TOKEN>`, upgraded to
a WebSocket. The token is the lowercase hex HMAC-SHA256 of the lowercased address under the
gateway's `GATEWAY_SECRET`:

```sh
printf %s hermes@example.org | openssl dgst -sha256 -hmac "$GATEWAY_SECRET" -r | cut -d' ' -f1
```

Binary frames are a UTF-8 JSON header, a newline (`\n`) and raw bytes.

| Direction | Frame | Meaning |
| --- | --- | --- |
| gateway → unwrapper | binary `{"type":"mail","id","from","to","receivedAt","size"}` + raw `.eml` | A stored mail |
| unwrapper → gateway | text `{"type":"ack","id"}` | Mail handled; the gateway deletes it |
| unwrapper → gateway | binary `{"type":"send","id","to"}` + raw reply `.eml` | Send a reply from the agent address |
| gateway → unwrapper | text `{"type":"sent","id"}` or `{"type":"error","id","message"}` | Result of a `send` |

The gateway keeps one connection per mailbox; a new connection replaces the old one (close code
4000). The unwrapper sends WebSocket pings every 20 s.

## Configuration

Environment variables only.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `GATEWAY_URL` | yes | | Gateway Worker URL, e.g. `https://cf-email-gateway.example.workers.dev` (`http`/`https`/`ws`/`wss`) |
| `GATEWAY_TOKEN` | yes | | Mailbox token for `AGENT_ADDRESS` (see [Gateway protocol](#gateway-protocol)) |
| `AGENT_ADDRESS` | yes | | The agent's email address; selects the mailbox |
| `BOT_SIGNING_KEY_FILE` | yes | | Path to the bot signing private key (unencrypted PEM) |
| `BOT_SIGNING_CERT_FILE` | yes | | Path to the bot signing certificate (PEM) |
| `BOT_ENCRYPTION_KEY_FILE` | yes | | Path to the bot encryption private key (unencrypted PEM) |
| `BOT_ENCRYPTION_CERT_FILE` | yes | | Path to the bot encryption certificate (PEM); inbound mail is encrypted to it |
| `CA_CERT_FILE` | yes | | Path to the trusted CA certificate(s) (PEM) |
| `ALLOWED_SENDERS` | yes | | Comma or whitespace separated author addresses |
| `A2A_URL` | yes | | Agent JSON-RPC endpoint (e.g. `http://127.0.0.1:8000/`) |
| `A2A_TOKEN` | yes | | Bearer token for the agent |
| `PUSH_HOST` | no | `127.0.0.1` | Bind address of the push endpoint |
| `PUSH_PORT` | no | `8090` | Port of the push endpoint |
| `PUSH_URL` | no | `http://<PUSH_HOST>:<PUSH_PORT>/a2a/push` (`127.0.0.1` when bound to a wildcard) | URL the agent posts notifications to |
| `PUSH_TOKEN` | yes | | Shared secret the agent echoes in `X-A2A-Notification-Token`; generate once (e.g. `openssl rand -hex 32`) and keep it across restarts |
| `MAX_SIGNATURE_AGE` | no | `1h` | Maximum signingTime age; seconds or `s`/`m`/`h` suffix |
| `OPENSSL` | no | `openssl` | Path to an OpenSSL 3 binary (LibreSSL is not supported) |
| `STATE_PATH` | no | `$XDG_STATE_HOME/cf-email-unwrapper/<AGENT_ADDRESS>.sqlite3` (`~/.local/state` without `XDG_STATE_HOME`) | SQLite state file; parent directories are created |

## Running

With Nix (the package brings Python, all dependencies and OpenSSL 3; `OPENSSL` defaults to it):

```sh
set -a; . ./unwrapper.env; set +a
nix run github:sm-th/cf-email-unwrapper
```

From a checkout:

```sh
uv sync
uv run python -m cf_email_unwrapper
```

or with Docker (the image contains Debian's OpenSSL 3 and binds the push endpoint to `0.0.0.0`):

```sh
docker build -t cf-email-unwrapper .
docker run --env-file unwrapper.env -p 8090:8090 \
  -v "$PWD/keys:/keys:ro" -v unwrapper-state:/state -e STATE_PATH=/state/state.sqlite3 \
  cf-email-unwrapper   # with BOT_*_FILE and CA_CERT_FILE pointing into /keys
```

## Development

```sh
nix develop          # python 3.13, uv, OpenSSL 3; exports OPENSSL
uv run pytest
```

The tests create a throwaway EC P-256 PKI (CA, author signing and encryption certificates, bot
certificate, a second untrusted CA) with the configured OpenSSL, seal test mail with
`openssl cms -sign | openssl cms -encrypt`, and run the whole loop against a local WebSocket server
implementing the gateway mailbox protocol and a fake A2A agent.

## Implementation notes

- All cryptography runs in the OpenSSL CLI (`openssl cms`, `openssl verify`). Keys and messages
  are written to a private temporary directory (0700, files 0600) per call. `asn1crypto` only reads
  certificate fields and CMS signed attributes.
- The A2A client is the official `a2a-sdk` (1.3) `JsonRpcTransport`, which speaks the A2A v1.0
  JSON-RPC method names (`SendMessage`, `GetTask`) and enum values (`ROLE_USER`,
  `TASK_STATE_COMPLETED`). The agent URL is configured directly, so no agent card is fetched.
- Push notifications follow the a2a-sdk sender: a POST of a `StreamResponse` JSON object with the
  config token in `X-A2A-Notification-Token`. The task is always re-read with `GetTask`.
- The gateway connection uses the `websockets` library without a message size limit.
