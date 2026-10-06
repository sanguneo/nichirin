# PROTOCOL — what this CLI promises a consumer

Every command prints one JSON document to standard output and exits. That
document, not this file, is the interface: an agent, a script or a wrapper
reads it and never imports `kanao`. `skill/SKILL.md` is how an agent *uses*
the commands; this is what their output is guaranteed to look like.

`kamado-cli`, the macOS sibling, emits the same shapes. A consumer written
against this document runs against either by changing which binary it starts.

**The HTTP half has a machine-readable copy: [`openapi.json`](openapi.json).**
It is an OpenAPI 3.1 document covering every `serve` route, parameter,
payload and status, and Swagger UI or Postman opens it as it is. The same
file ships in the sibling **byte for byte** — one contract, two builds — and
the handful of members that genuinely differ carry `x-platform` saying which
build they belong to. `tests/serve/test_openapi.py` asks the running handler
and the constants whether the document is still telling the truth, so it
fails here rather than in somebody's client.

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

`kanao serve` relays the same envelope over HTTP. `GET /stream` is
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

- **Adding a key is not a breaking change.** A consumer must ignore keys it
  does not know. The macOS sibling already carries one this build does not
  (`typeCode` on a room) and one more error code (`PERMISSION_REQUIRED`).
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
| `RATE_LIMITED` | `serve` only: the send queue was full. A send that merely arrives early waits its turn instead |
| `PERMISSION_REQUIRED` | **macOS (`kamado-cli`) only**: the permissions the client needs were not granted. This build never raises it |

`kamado-cli` adds `PERMISSION_REQUIRED` for the macOS permissions it needs.

## Sending

`send` returns `submitted: true` when the client **accepted** the text — the
input box emptied — which is not confirmed delivery. A caller that needs proof
reads the room back and looks for a new `fromMe` message. `clearedDraft`
reports that an unfinished line was in the box and is now gone — it is
reported rather than refused, so a caller can say so.

Keep at least two seconds between sends. `serve` holds that spacing for you:
a send that arrives sooner waits its turn rather than being refused, and the
answer still comes back on the same request, so the wire contract does not
change. `429 RATE_LIMITED` is what a caller gets when the queue itself is
full — four sends, counting the one being sent.

## The samples are the normative copy

`tests/envelopes/*.json` holds one real sample of each shape, generated from
the synthetic fixtures in `tests/fixtures.py` — **never from a real account**.
`tests/test_protocol.py` regenerates them and fails when they differ, so a
change to any envelope is a failing test in this repository before it is a
surprise in a consumer. A consumer's own conformance tests should read these
files rather than hand-written literals.

Regenerate after an intended change:

```powershell
python -m tests.test_protocol --write
```

One sample is assembled rather than captured: the two `follow` lines, because
capturing them means running a loop that does not end. They are built exactly
as `cli._follow` builds them and the `message` line reuses the same
`Message.as_json()` the other commands use, but a change to the `watching`
line's own keys would not fail this test. Everything else is the command's
real output.
