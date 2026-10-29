# cf-email-unwrapper

A small Python service that runs next to one A2A agent and lets trusted people talk to it by
S/MIME email. It is the second half of an email ↔ A2A bridge; the first half is
`cf-email-gateway`, a Cloudflare Worker that receives mail, stores it in R2, queues a reference
and sends the replies this service produces.

```
author ──S/MIME──▶ Email Routing ─▶ gateway ─▶ R2 + queue mail-<slug>
                                                    │ (HTTP pull)
                                                    ▼
                          agent ◀──A2A SendMessage── unwrapper (this repo)
                            │                         ▲
                            └──push notification──────┘
                                                      │ signed (+ encrypted) reply
author ◀── send_email ◀── gateway ◀── queue email-outbox
```

The gateway has no keys and checks nothing; this service holds the bot key and performs every
S/MIME check. The agent only speaks plain A2A.

## What it does

1. Looks up its inbound queue `mail-<slug>` (derived from `AGENT_ADDRESS`, see
   [Queue naming](#queue-naming)). The gateway creates the queue on the first email, so until
   then the service keeps polling for it.
2. Pulls batches over the Cloudflare Queues HTTP pull API and, for each message, reads the raw
   `.eml` from R2 (S3 API).
3. Opens the message with the OpenSSL CLI:
   - decrypts `application/pkcs7-mime` enveloped data with the bot certificate and key
     (signed-but-unencrypted mail is accepted too);
   - requires an S/MIME signature (`multipart/signed` or opaque signed-data) that verifies against
     `CA_CERT` with purpose `smimesign`; unsigned mail is rejected;
   - requires the signer certificate's email (SAN rfc822Name or subject emailAddress) to equal the
     `From` address, and that address to be in `ALLOWED_SENDERS`;
   - requires a CMS `signingTime` no older than `MAX_SIGNATURE_AGE` (and not more than five
     minutes in the future); a missing signingTime is rejected;
   - picks the author's reply encryption certificate from the certificates carried in the
     signature: issued for the author's address, EC key, keyUsage `keyAgreement`, chains to
     `CA_CERT` (`openssl verify`). The most recently issued match wins.
4. Rejected mail is logged, acknowledged and deleted from R2. Unauthenticated senders never get a
   reply.
5. Authenticated mail is sent to the agent with A2A `SendMessage` (non-blocking,
   `returnImmediately: true`) with a push notification config pointing to this service's push URL
   and a random per-process token. The message text is the email's `text/plain` body, or its HTML
   body converted to text. The message carries metadata:

   ```json
   {"email": {"author": "...", "agentAddress": "...", "subject": "...",
              "messageId": "<parent Message-ID>", "references": "<parent References>",
              "replyCertPem": "-----BEGIN CERTIFICATE-----..."}}
   ```

   Once the agent has accepted the task, the queue message is acknowledged and the R2 object
   deleted. If the agent cannot be reached, the queue message is retried after 30 s and the R2
   object is kept.
6. When the agent sends a push notification, the service checks the `X-A2A-Notification-Token`
   header, fetches the task with `GetTask` and replies only for the states `completed`,
   `input-required`, `failed`, `rejected` and `canceled`. The email metadata is read back from the
   task history (falling back to task metadata). The reply text is the text of the task's
   artifacts plus its status message.
7. The reply is signed (detached, SHA-256) with the bot certificate and encrypted with AES-256 for
   the author's reply certificate when one was found (OpenSSL's default ECDH KDF,
   `dhSinglePass-stdDH-sha1kdf-scheme`, which iOS Mail requires). Without a reply certificate the
   reply is signed only. The finished message is pushed to `email-outbox` as
   `{"version": 1, "from", "to", "raw": "<base64>"}`; the push endpoint returns 200 only after that
   succeeded, otherwise 502 so the agent may retry. Replies whose JSON would exceed 120 KB are
   replaced by a short notice.

Each (task, state) pair is answered at most once per process (in-memory only; there is no
persistent state).

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
- The push token is random per process. Notifications for tasks submitted before a restart are
  rejected with 401, so those replies are lost.

## Queue naming

Must match the gateway exactly:

1. `a = address.trim().lower()`
2. `slug` = `a` with every run of characters outside `[a-z0-9]` replaced by `-`, leading/trailing
   `-` removed
3. `name = "mail-" + slug`
4. if `len(name) > 63`: `name = name[:54].rstrip("-") + "-" + sha256_hex(a)[:8]`

`hermes@example.org` → `mail-hermes-example-org`.

## Configuration

Environment variables only.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `CF_ACCOUNT_ID` | yes | | Cloudflare account id |
| `CF_API_TOKEN` | yes | | API token with Queues read + write (pull, ack, push to outbox) |
| `R2_ENDPOINT` | no | `https://<CF_ACCOUNT_ID>.r2.cloudflarestorage.com` | R2 S3 endpoint |
| `R2_ACCESS_KEY_ID` | yes | | R2 S3 access key id (read + delete on the bucket) |
| `R2_SECRET_ACCESS_KEY` | yes | | R2 S3 secret |
| `R2_BUCKET` | no | `email-inbox` | Bucket holding the raw messages |
| `AGENT_ADDRESS` | yes | | The agent's email address; also selects the inbound queue |
| `BOT_KEY` | yes | | Bot private key, PEM (real newlines, `\n` escapes or one line) |
| `BOT_CERT` | yes | | Bot certificate, PEM (same formats) |
| `CA_CERT` | yes | | Trusted CA certificate(s), PEM (same formats) |
| `ALLOWED_SENDERS` | yes | | Comma or whitespace separated author addresses |
| `A2A_URL` | yes | | Agent JSON-RPC endpoint (e.g. `http://127.0.0.1:8000/`) |
| `A2A_TOKEN` | yes | | Bearer token for the agent |
| `PUSH_HOST` | no | `127.0.0.1` | Bind address of the push endpoint |
| `PUSH_PORT` | no | `8090` | Port of the push endpoint |
| `PUSH_URL` | no | `http://<PUSH_HOST>:<PUSH_PORT>/a2a/push` (`127.0.0.1` when bound to a wildcard) | URL the agent posts notifications to |
| `OUTBOX_QUEUE` | no | `email-outbox` | Outbox queue name |
| `MAX_SIGNATURE_AGE` | no | `1h` | Maximum signingTime age; seconds or `s`/`m`/`h` suffix |
| `POLL_INTERVAL` | no | `5` | Pause between empty pulls; seconds or `s`/`m`/`h` suffix |
| `OPENSSL` | no | `openssl` | Path to an OpenSSL 3 binary (LibreSSL is not supported) |

## Running

```sh
uv sync
uv run python -m cf_email_unwrapper
```

or with Docker (the image contains Debian's OpenSSL 3 and binds the push endpoint to `0.0.0.0`):

```sh
docker build -t cf-email-unwrapper .
docker run --env-file unwrapper.env -p 8090:8090 cf-email-unwrapper
```

## Development

```sh
nix develop          # python 3.13, uv, OpenSSL 3; exports OPENSSL
uv run pytest
```

The tests create a throwaway EC P-256 PKI (CA, author signing and encryption certificates, bot
certificate, a second untrusted CA) with the configured OpenSSL, seal test mail with
`openssl cms -sign | openssl cms -encrypt`, and run the whole loop against a mocked Cloudflare API,
an in-memory R2 and a fake A2A agent.

## Implementation notes

- All cryptography runs in the OpenSSL CLI (`openssl cms`, `openssl verify`). Keys and messages
  are written to a private temporary directory (0700, files 0600) per call. `asn1crypto` only reads
  certificate fields and CMS signed attributes.
- The A2A client is the official `a2a-sdk` (1.3) `JsonRpcTransport`, which speaks the A2A v1.0
  JSON-RPC method names (`SendMessage`, `GetTask`) and enum values (`ROLE_USER`,
  `TASK_STATE_COMPLETED`). The agent URL is configured directly, so no agent card is fetched.
- Push notifications follow the a2a-sdk sender: a POST of a `StreamResponse` JSON object with the
  config token in `X-A2A-Notification-Token`. The task is always re-read with `GetTask`.
- Pull consumers receive `json` queue bodies base64-encoded; they are decoded before use.
