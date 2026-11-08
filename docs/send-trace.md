# Text send traces

Both builds accept `send --trace <dir>` and `serve --trace <dir>`.
Tracing is off by default. It creates no files when off and adds nothing to
CLI stdout or HTTP response envelopes. The server host chooses the directory;
an HTTP body cannot enable tracing or select a destination. Only text sends
are traced, not uploads, reads, or streams.

Each attempt exclusively creates `<random-attempt-id>.jsonl` with mode `0600`
where the OS supports it. The directory is created if necessary. Files remain
for the operator to retain or remove; there is no automatic rotation.
Each authenticated send request has its own context and file, including
concurrent HTTP requests. Authentication precedes tracing; unauthorized
requests do not create attempt files.

## Schema 1

Every line uses this exact field allowlist:

| Field | Meaning |
| --- | --- |
| `schema` | `1` |
| `attempt_id` | Random, opaque per-attempt ID; also the filename stem |
| `platform`, `build`, `version` | Installed producer: `windows`/`kanao` or `macos`/`kamado`, and its build version |
| `route` | `cli`, `daemon`, or `http` |
| `http_route` | `POST /send` for HTTP events, otherwise `null` |
| `stage` | One of the stages below |
| `event` | `start` or `end` |
| `start_ms`, `end_ms`, `elapsed_ms` | Nonnegative monotonic milliseconds relative to this attempt's origin; elapsed is the stage's duration |
| `outcome` | `pass`, `rejected`, `unknown`, or `error` |
| `code` | An allowlisted error code, `UNEXPECTED`, or `null`; never exception text |
| `conditions` | E/D/U/W/S/L/B observations, each `pass` or `not_checked`; L can also be `unknown` |
| `room`, `window`, `control` | Optional attempt-local pseudonyms, never raw IDs |

No other fields are serialized. Message bodies, titles, search terms, tokens,
paths, filenames, arguments, candidate lists, raw exception messages and
screenshots are excluded. No content hashes are used as identifiers. The
random alias prefix and per-attempt mapping prevent matching identifiers
across attempts.

Stages are `attempt`, `validation`, `catalog`, `discovery`, `input`, `write`, `submit`,
`confirm`, `button_guard`, `ax_button`, `focus_guard`, `focused_return`,
`forward`, `daemon`, `queue`, `execution`, `queue_full`, `shutdown`, and `log`.
Nested durations overlap: do not sum them. Start events have zero elapsed
time; an end event measures the actual enclosed work. A start without an end
is incomplete evidence, not a successful operation.

`validation` records a handled request refusal before queue admission.
It never replaces an already recorded native or queue failure.
`catalog` resolves the room. `discovery` resolves the native window; `input`
finds and reads the composer. `write` covers the native text placement.
`submit` and `confirm` distinguish handing submission to the adapter from
the client's composer-empty observation. Windows records W only after its
native normalized-text readback agrees. macOS records W after AXValue
readback agrees, and distinguishes the enabled AX-button path
(`button_guard`, `ax_button`) from focused Return
(`focus_guard`, `focused_return`). The latter raises the window and activates
the client; it must not be described as preserving the desktop.

E is an observed composer, D a discovered window, U the unique target chosen
by discovery, W verified text placement, and S observed composer emptying
after submission. These are diagnostic predicates, not recipient delivery.
L is independent local-history confirmation and B is preservation of desktop
state throughout the send (see `macos/docs/background-sending.md`).
Without optional `send --confirm[=seconds]` (HTTP `confirm` seconds), L stays
`not_checked`. Confirmation's `log` stage records L as `pass` only for a new,
exact-text from-me local row above the pre-input baseline; timeout or a read
failure records `unknown`, without changing the send outcome. B always stays
`not_checked`. A synthetic adapter that does
not perform native checks does not earn W or S from its return value alone.

`queue` includes acquiring the send lock and pacing wait, but ends before
`execution` starts. Waiting normally passes; `queue_full` is the distinct
`RATE_LIMITED` refusal and `shutdown` is a distinct `BUSY` refusal. Neither
executes a send. `rejected` at discovery/input/write is a pre-submission
failure. A failure after submission may have happened is `unknown`, including
an unreadable composer after Enter. `error` denotes another unclassified
failure; it is not proof of non-submission. The first failed boundary remains
visible even when an outer layer converts the exception to an envelope.

## Daemon correlation and failure policy

A traced macOS CLI send forwards the attempt ID, monotonic origin and
caller-created file to its local daemon as IPC metadata. The daemon appends
categorical native events to that same existing file under `route: daemon`;
it does not create another attempt file. Event conditions are observations
in that producer, not a claim that the caller independently checked them.
The CLI records `forward` while waiting for the reply. A lost or malformed
reply after the request was written is `unknown`; it never falls back to a
local send. Only a daemon that did not receive the request permits the
existing local path.

The requested destination must open and accept the first trace record before
any UI mutation. An invalid destination returns `INVALID_REQUEST` with
`not_delivered/safe`. `serve` also probes its host configuration before
starting. If a later request can no longer open its attempt, it is refused
before entering the queue or calling the sender.

Once an attempt starts, a failed or short trace write disables that writer.
It does not retry a native operation, submit again, or change a known-success
response into a resend instruction. Closing a trace cannot change the send
result either. A disk failure, abrupt termination, or lost daemon response
can leave an incomplete file. Missing events never prove that a message was
not submitted. Use the existing disposition and check the conversation
before deciding whether to retry an uncertain send; tracing performs no
automatic retry and is not a delivery receipt.
