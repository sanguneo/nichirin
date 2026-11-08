# PROTOCOL — what this CLI promises a consumer

Every command prints one JSON document to standard output and exits. That
document, not this file, is the interface: an agent, a script or a wrapper
reads it and never imports `kamado`. `kamado/agent_docs/SKILL.md` is how an agent *uses*
the commands; this is what their output is guaranteed to look like.

`kanao-cli`, the Windows sibling, emits the same shapes. A consumer written
against this document runs against either by changing which binary it starts.

**The HTTP half has a machine-readable copy: [`openapi.json`](../openapi.json).**
It is an OpenAPI 3.1 document covering every `serve` route, parameter,
payload and status, and Swagger UI or Postman opens it as it is. The same
file ships in the sibling **byte for byte** — one contract, two builds — and
the handful of members that genuinely differ carry `x-platform` saying which
build they belong to: today `typeCode` on a room and `PERMISSION_REQUIRED`,
both of which are this build's. `tests/serve/test_openapi.py` asks the
running handler and the constants whether the document is still telling the
truth, so it fails here rather than in somebody's client.

## The envelope

Success:

```json
{ "ok": true, "...": "command-specific keys" }
```

Failure:

```json
{ "ok": false, "error": { "code": "CHAT_NOT_FOUND", "message": "…" } }
```

`ok` is always present and always a boolean. An error carries `code` and
`message`, and may carry more — `candidates` on an ambiguous room, for
instance. **`message` is written for a person and its wording may change at
any time; branch on `code`, never on `message`.**

## Offline agent documentation (CLI only)

`kamado skills get [send|read|serve]` returns
`{"ok": true, "topic": "send", "version": "0.29.3", "content": "..."}`.
With no topic it returns the full installed `SKILL.md` and `topic: "all"`.
An explicit topic includes the matching sections plus common setup, limits,
errors and data-handling guidance. `content` is Markdown with YAML frontmatter;
its `version` field and the response's `version` equal the runtime
`kamado --version`. Markdown prose is not a machine-stable contract.

This command reads only bundled package resources, before creating a Reader
or probing the daemon. It requires no KakaoTalk, account, permission, token or
network access. There is no HTTP route for it. Unknown topics/actions or a
missing action use argparse's exit 2. Resource-read failures use the existing
failure envelope. The canonical document is `kamado/agent_docs/SKILL.md`;
both the wheel and `install.py` ship it, and the installer also places it at
the skill root. The repository's `skill/SKILL.md` points there rather than
maintaining a copy. HTML `skills:` markers in the source delimit topics and
are consumed by the CLI; they do not render in the installed full document.

## Exit status

| Code | Meaning |
| --- | --- |
| 0 | Success. Standard output holds an `ok: true` envelope |
| 1 | A handled failure. Standard output holds an `ok: false` envelope |
| 2 | The command line itself was wrong. Argparse wrote to standard error |

Exit 2 prints usage to **standard error**, not an envelope. A consumer that
parses standard output must treat empty output as a malformed call rather than
as an empty result.

## Values that cross the boundary

- **Every identifier is a string.** KakaoTalk ids exceed 2^53 and ordinary
  JSON parsing would corrupt them. `id`, `roomId`, `senderId`,
  `lastMessageId`, and the ids inside `attention` are all strings.
- **Every instant is ISO-8601 with an explicit offset** — `sentAt`,
  `lastActivityAt`, and a digest slice's `from` and `to`. Convert to the
  user's zone and label it; never print an offset as though it were local.
- **Messages are oldest-first**, in every command that returns a list of them.
- **A missing key is a negative.** `replyTo`, `mentions`, `mentionsMe`,
  `mentionsAll` and `repliesToMe` appear only when they apply. Absence means
  no, and denying five things on every line would triple the payload.
- `text` may be empty with a non-`text` `kind`: that is an attachment this
  tool can name but not read.

## Optional text-send diagnostics

`send --trace <dir>` and host-owned `serve --trace <dir>` write masked,
per-attempt JSONL using the shared [schema and failure policy](../docs/send-trace.md).
They default off, leave stdout/HTTP envelopes unchanged, and accept no trace
destination from HTTP callers. Queue waiting, queue rejection, native write
refusal and post-submit uncertainty are distinct stages. AX-button submission
and guarded focused Return have different stages; Return activates the client.
Unobserved L/B remain `not_checked`; `submitted` does not establish either.
With confirmation, L is `pass` for an observed local row, or `unknown` on
timeout/read failure.

`send ... "text" --confirm[=seconds]` optionally observes the local room log
after submission (bare flag: 3 seconds; finite `0 < seconds <= 30`).
HTTP `POST /send` accepts numeric `confirm` seconds under the same bounds.
The daemon carries this option; capture, submission and observation stay
serialized. The latest room logId is captured immediately before actual input;
only a later `fromMe` row with exactly equal text matches. Successful sends add
`logged: true` and string `messageId`, or `logged: false` on timeout/read failure.
This never changes `submitted` or `disposition` and never resends. Omitted means
byte-identical existing JSON; dry runs do not read or report confirmation.
L proves local recording, not receipt by another person.

An invalid requested destination is refused before UI mutation with
`INVALID_REQUEST`, `not_delivered/safe`. A later logging failure disables
logging without repeating submission or changing a known success. CLI/daemon
events share one attempt file; a lost daemon response never triggers a local resend.

## Streaming: `follow`

`follow` writes **newline-delimited JSON** — one complete object per line,
flushed as it happens, until interrupted. The first line states what is being
watched; each later line is one message.

```json
{"ok": true, "event": "watching", "rooms": ["1001"], "intervalSeconds": 1.0}
{"ok": true, "event": "message", "id": "5003", "roomId": "1001", "...": "…"}
```

A `message` event is the same object `messages` and `context` put in their
lists, with `ok` and `event` added. A consumer reads a line at a time and must
not assume the stream ends.

## Streaming: `serve`'s `/stream`

`kamado serve` relays the same envelope over HTTP. `GET /stream` is
Server-Sent Events, and its `data:` payloads are the same objects — a
`message` event carries the `Message.as_json()` above under `message`.

| Event | Payload | When |
| --- | --- | --- |
| `ready` | `{"ok": true, "resumed": bool, "cursor": {roomId: logId}}` | Once, as the stream opens |
| `message` | `{"ok": true, "message": {…}}` | Per message, oldest first |
| `cursor` | `{"ok": true, "cursor": {roomId: logId}}` | After the messages it covers |
| `ping` | `{"ok": true}` | Each poll that found nothing to say |
| `error` | The error envelope | Once, and the stream then ends |

**The cursor is how a reconnection stops being a hole.** A consumer stores the
map from the last `cursor` event and hands it back as repeated
`since=<roomId>:<logId>` parameters; the server pages each named room forward
from there before going live, and says `resumed: true`. A room the cursor does
not name starts from the moment of connection, so a consumer that wants no gap
must carry every room it has seen. One id cannot stand for the whole account —
the stream covers many rooms, and the newest message says nothing about where
the others had reached.

Over HTTP the status code carries the failure and the body carries the same
envelope: `401` for `UNAUTHORIZED`, `429` for `RATE_LIMITED`, `408` for a body
that never arrived, and `503` with `BUSY` when the server already holds as many
streams as it will.

## Compatibility

### macOS session diagnostics

`status` reports `ready: false`, `stale: true` and
`detail: "kakaotalk-logged-out"` when read-only Accessibility inspection finds
both a password field and a login button in KakaoTalk's main window. This does not disable
local history commands. Closing KakaoTalk still permits offline reads.
An inaccessible or absent main window cannot prove either login or logout.

For authenticated requests the macOS server adds private response headers on `/health`, `/rooms` and
`/stream`: `X-Kamado-Ready`, `X-Kamado-Stale` (`true`, `false`, or `unknown`)
and `X-Kamado-Detail` (`kakaotalk-logged-out`, `kakaotalk-closed`,
`kakaotalk-signed-in`, or `kakaotalk-unknown`). These describe the client's
session, not a guarantee that a server sync finished. The JSON/SSE contract
is unchanged: `/health` still reports process liveness through `ok`, and
offline rooms remain readable. Windows need not emit these private headers.
No age threshold treats a quiet conversation as stale. Unauthenticated health
remains liveness-only and runs no native probe. Stream headers describe its
opening session, not later changes during that connection. The session probe
stops starting AX reads after three seconds; an in-flight read may take the
AX messaging timeout (five seconds), in addition to the bounded process lookup.

- **Adding a key is not a breaking change.** A consumer must ignore keys it
  does not know. This build carries one the Windows sibling does not (`typeCode` on a
  room) and one more error code (`PERMISSION_REQUIRED`).
- **Removing a key, renaming one, or changing what an existing one means is
  breaking**, and lands in a minor version with the reason in `CHANGELOG.md`.
- **Adding an error code is not breaking.** A consumer must have a default
  branch for a code it does not recognise, because the sibling's set and this
  one's are not identical and both may grow.

## Error codes

| Code | What it means |
| --- | --- |
| `AMBIGUOUS_CHAT` | Several rooms matched. `candidates` is attached; retry with `--room-id` |
| `CHAT_NOT_FOUND` | No room by that title |
| `KEY_UNAVAILABLE` | KakaoTalk has not opened that room this session, so its key is not in memory |
| `KAKAO_NOT_RUNNING` | The client is not running or not signed in |
| `NO_USER_DATA` | No signed-in profile directory was found |
| `READ_FAILED` | A database could not be read or decrypted |
| `BUSY` | A concurrent write while reading; retry once |
| `INVALID_REQUEST` | The arguments were understood but unusable (a span the slicer cannot take, say) |
| `WINDOW_NOT_FOUND` | The room's chat window is not open in the client |
| `INPUT_NOT_FOUND` | The window was found but its input box was not |
| `SEND_FAILED` | The client refused or the write could not be verified |
| `UNAUTHORIZED` | `serve` only: the bearer token was missing or wrong |
| `RATE_LIMITED` | `serve` only: the send queue was full, so this send was not accepted |

`PERMISSION_REQUIRED` is this build's own: Full Disk Access or Accessibility
has not been granted to the terminal running this tool. `kanao-cli` has no
equivalent and does not emit it.

## Sending

### Optional durable operation identifiers

`send --operation-id ID`, JSON `POST /send` member `operationId`, and text
`POST /upload` form field `operationId` opt into at-most-once dispatch.
IDs are 1-128 printable ASCII characters without spaces. Omission preserves
the existing envelopes and behavior, with no receipt access.

Receipts live in the user's `%LOCALAPPDATA%/kanao-cli/operations` (Windows)
or `~/Library/Application Support/kamado-cli/operations` (macOS). Names use
the ID's SHA-256. An atomic per-ID directory claim prevents concurrent
processes from both dispatching. Before input, a temporary record is flushed,
fsynced and replaced as `receipt.json` with phase `dispatching`. The fingerprint
hashes operation kind, exact room selector and selector kind (CLI ID,
CLI title, or HTTP ID-or-title), payload
SHA-256 and options, including `confirm` seconds; uploads also bind their cleaned name. Message/file bytes
are not recorded; any quoted message text in a failure message is redacted.
Receipts retain result metadata and failure disposition,
with terminal phase `submitted`, `unknown` or `not_delivered`.

The same ID and fingerprint returns its recorded result, including confirmation
fields, without input or another log observation.
A changed fingerprint is `INVALID_REQUEST`. A claim left `dispatching`,
empty or unreadable returns `SEND_FAILED`, `unknown/after_check`, with no
input. A failed final receipt update does not turn a known success into a
failure; its replay remains unknown. No automatic resubmission occurs.
There is no expiry or automatic cleanup: removing a receipt forfeits its
protection. Use a new ID only after checking an uncertain operation.

### Delivery and retry decisions

Every failure carries `error.disposition`. Existing codes, messages, top-level
details and exit statuses are unchanged. Both builds use
`kakaocommon/disposition.py` for these defaults:

| ErrorCode | delivery | retry |
| --- | --- | --- |
| `KAKAO_NOT_RUNNING`, `NO_USER_DATA`, `PERMISSION_REQUIRED`, `KEY_UNAVAILABLE` | omitted | `safe` |
| `CHAT_NOT_FOUND`, `AMBIGUOUS_CHAT`, `MESSAGE_NOT_FOUND`, `FILE_EXPIRED` | omitted | `safe` |
| `BUSY`, `READ_FAILED`, `INVALID_REQUEST` | omitted | `safe` |
| `WINDOW_NOT_FOUND`, `INPUT_NOT_FOUND`, `UNAUTHORIZED`, `RATE_LIMITED` | `not_delivered` | `safe` |
| `SEND_FAILED` | `unknown` | `after_check` |

A proven failure before submission overrides `SEND_FAILED` with
`not_delivered` / `safe`. Never infer this from its message. Only `safe`
permits resubmission; `after_check` requires reading the room first.
Known safe write refusals, including empty/NUL text and a stopped client,
carry `delivery: not_delivered`. CLI/daemon writing boundaries and HTTP
`POST /send`/`POST /upload` preserve this operation context; read defaults
still omit delivery. No explicit delivery value or retry decision is replaced.
Write-queue shutdown and upload admission refusals also carry
`not_delivered` / `safe`, rather than the read-only `BUSY` default.
Unexpected exceptions at a write boundary become `SEND_FAILED` /
`unknown` / `after_check`; unexpected read exceptions remain `READ_FAILED`.
Successful sends/uploads carry top-level `disposition`:
`submitted` / `unsafe`, or `not_delivered` / `safe` for a dry run.
Submission is condition S, not log verification L; it does not prove delivery.
`suggestion` is not required and existing detail fields remain where they were.

`send` returns `submitted: true` when the client **accepted** the text, which
is queue acceptance and not confirmed delivery. A caller that needs proof
reads the room back and looks for a new `fromMe` message. `clearedDraft`
reports that an unfinished line was in the box and is now gone — it is
reported rather than refused, so a caller can say so.

Keep at least two seconds between sends.

## The samples are the normative copy

`tests/envelopes/*.json` holds one real sample of each shape, generated from
the synthetic fixtures in `tests/fixtures.py` — **never from a real account**.
`tests/test_protocol.py` regenerates them and fails when they differ, so a
change to any envelope is a failing test in this repository before it is a
surprise in a consumer. A consumer's own conformance tests should read these
files rather than hand-written literals.

Regenerate after an intended change:

```bash
python3 -m tests.test_protocol --write
```

One sample is assembled rather than captured: the two `follow` lines, because
capturing them means running a loop that does not end. They are built exactly
as `cli._follow` builds them and the `message` line reuses the same
`Message.as_json()` the other commands use, but a change to the `watching`
line's own keys would not fail this test. Everything else is the command's
real output.
