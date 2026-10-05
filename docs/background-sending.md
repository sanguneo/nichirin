# Background sending: constraints and measured behavior

Measured on 2026-09-19, macOS, KakaoTalk 26.7.0 (1194), one active display.
No private conversation content or identifiers are recorded here.

These measurements describe the AX send-button path. KakaoTalk for Mac 26.4.1
was separately observed on 2026-10-02 without an accessible send button. For
that layout, submission uses a guarded Return after raising the target window,
activating the app and verifying the focused composer, its owning window and
the placed text. That path does not preserve `B` below. Synthetic submission
tests passed. A live dry-run was attempted, but the previously open target was
unavailable (`WINDOW_NOT_FOUND`); no live message was sent, and the new Return
path has not been exercised against the live client in this change.

## The condition to preserve

For room `r`, message `m`, and desktop state `s`, distinguish:

- `E`: the room has a live composer.
- `D`: discovery returns its window.
- `U`: the intended room and controls are uniquely identified.
- `W`: the composer reads back exactly `m` after the write.
- `S`: its enabled Send button is pressed and its composer is observed empty.
- `L`: a new matching outgoing row is independently observed in the local log.
- `B`: the sender does not change foreground, focus, active Space, window
  visibility, pointer or clipboard throughout the operation.

The required outcome is `E AND D AND U AND W AND S AND L AND B`.
These are conditions, not independent probabilities. Local observation `L`
does not prove recipient receipt. `submitted` reports `S`, not `L`.
The live checks below record before/after desktop state, not every intermediate
frame. The implemented send path issues no activation, Space switch, keyboard
or pointer operations. Its off-Space fallback invokes the exact target's Cocoa
Window-menu item; that may reorder the target within its own Space, but it did
not make KakaoTalk frontmost or move the current Space in the live check.

Restoring the desktop at the end is weaker than `B`: a window that appears
briefly and is then minimized already violates it. Opening once and minimizing
can amortize setup (`average cost = send cost + opening cost / send count`),
but it does not make the initial opening invisible.

Within this UI adapter, a genuinely closed room has no composer (`NOT E`).
No amount of better enumeration can create that composer. A separate supported
send API or a verified background creation mechanism would be needed to remove
that condition; this is not a proof that every possible implementation is
impossible.

## What was broken

1. `AXMinimized` was written as a CFString. `"true"` returned success but left
   the state false and emitted no minimization notification. CFBoolean true
   and false produced the correct state and corresponding AX notifications.
2. `AXWindows` was treated as the complete set of windows. A hidden chat
   remained addressable through an existing AX reference while missing from
   both ordinary and navigation lists.
3. Empty enumeration once pressed an arbitrary `makeKeyAndOrderFront:` item
   and then re-read `AXWindows`. That list still omitted another-Space windows.
   The usable result is instead the exact title's direct `AXFocusedWindow`
   reference, after recipient resolution has supplied that title.
4. A failed composer read (`None`) counted as successful emptying. A disabled
   button's wait result was ignored, and a missing button posted Return to a
   process shared by multiple chats.
5. A client running with **every window closed** was refused outright:
   `RoomOpener.open` raised `WINDOW_NOT_FOUND` on `main_state == "missing"`
   before trying anything, so the interface could not send to a closed room —
   and it refused even when the room's own window was already open. Removing
   that refusal alone was not enough: `show_main_window` asked the client to
   reopen and then read the window list **once**, after `activate`'s fixed
   0.6 s settle, while the client draws the window at 0.828 s.

## Implemented discovery

Let `A` be `AXWindows`, `N` the window elements in
`AXChildrenInNavigationOrder`, and `H` the windows recovered by application-local
hit-testing at the centers of that process's normal WindowServer windows.
The initial candidate set is `A UNION N UNION H`, deduplicated with `CFEqual`.

Hit-testing calls `AXUIElementCopyElementAtPosition` on the **KakaoTalk
application element**, not the system-wide element. It reads an element; it
does not post an event, click, move a pointer, capture a screen or unhide a
window. The resulting element's `AXWindow` supplies the window reference.
Coordinates are only discovery hints. The existing exact-title and catalog
ambiguity checks still determine the recipient.

WindowServer bounds include offscreen windows, and negative coordinates are
preserved. Another monitor changes coordinates; another Space can change what
the client exposes. When the initial set has no exact title match, the sender
finds only matching `makeKeyAndOrderFront:` Window-menu items, presses them,
and reads `AXFocusedWindow` and `AXMainWindow` directly. The live off-Space
window remained absent from `A`, `N`, and `H` even after the press, but both
direct attributes named its usable `AXWindow`; the terminal's frontmost PID
was unchanged. A missing or mismatched direct reference still fails rather
than substituting another window.

## Alternatives

| Route | Decision |
| --- | --- |
| AX value + exact Send button | Keep. Existing minimized and hidden controls worked. Repair discovery, not text transport. |
| Process-directed keyboard events | Reject for sending: a PID does not identify one chat, and Return after an AX write is ignored by this client. |
| Window-associated mouse events | No event arrived as a room opening in the tested process-directed/hidden setup. Do not ship a speculative click transport. |
| AppleScript or URL scheme | No published arbitrary-room send command was established. Registered schemes alone do not specify a send API. System Events scripting remains UI automation. |
| Official Kakao Message API | Supports constrained self/friend messaging with app permissions/templates, not arbitrary existing group-room IDs. Not a replacement for this CLI's contract. |
| App-internal calls or protocol implementation | Could change the model, but no stable interface was established. Requires a separate implementation and verification, not a fallback guessed from local database access. |

References: the native `AXUIElementCopyElementAtPosition` API,
[WindowServer metadata](https://developer.apple.com/documentation/coregraphics/cgwindowlistcopywindowinfo(_:_:)),
[process-directed events](https://developer.apple.com/documentation/coregraphics/cgevent/posttopid(_:)),
[Kakao Message API](https://developers.kakao.com/docs/en/kakaotalk-message/common).

## Evidence

Actual sends were explicitly authorized for one room. Each successful attempt
was checked against a cursor captured before that attempt; exactly one new
matching `fromMe` row was found.

| State | Observation |
| --- | --- |
| Chat and main window minimized; app inactive | Sent and read back once. Both remained minimized; foreground and pointer unchanged. |
| App hidden; chat not minimized | Actual CLI sent and read back once. App remained hidden and inactive; pointer unchanged. |
| Chat minimized then restored while app remained hidden | Before the final repair, a fresh CLI returned `WINDOW_NOT_FOUND`; both AX lists contained only the main window. After the repair, a fresh CLI sent and read back once in 3.317 s, including account/database work. Both lists still lacked the target before the send. Hidden/inactive state and pointer were unchanged. |
| Every window closed, process only | Before the repair a fresh CLI returned `WINDOW_NOT_FOUND` without touching anything. After it, a fresh CLI drew the main window, opened the room and sent, read back once, in 67.9 s — of which ~41 s is the client filling its 264-row list for the first read on a newly drawn window. |
| App hidden, room already open, no main window | Sent and read back once in 3.055 s. The app stayed hidden and in the background, the main window stayed absent, and the pointer did not move. |
| Chat on another Space, terminal frontmost | Ordinary discovery returned `WINDOW_NOT_FOUND`. Pressing the exact Window-menu item kept the frontmost PID unchanged; `AXFocusedWindow` and `AXMainWindow` both returned the target while `AXWindows` still omitted it. A real CLI `--dry-run` then resolved the composer without switching focus or Space. |
| Closed room | Row `AXPress`, `AXConfirm`, `AXOpen` were rejected. Process-directed double-click, including window-number fields, did not create the room. The live test room was initially prepared with the existing visible opener. |
| Second physical display | Not live-verified. Only one display was attached. Synthetic geometry tests cover negative coordinates; this does not replace a multi-display test. |

Restoration parks a window it had to draw rather than closing it back. A
client left with no windows is one the system may take away: AppKit turns on
automatic termination for it (`_NSEnableAutomaticTerminationAndLog ... No
windows open yet`, then `_kLSApplicationWouldBeTerminatedByTALKey=1`), and
`runningboardd` then terminated KakaoTalk with `CacheDeleteAppContainerCaches
requesting termination assertion` — exit reason `0xBADDD15C
OS_REASON_RUNNINGBOARD`, no crash report. The machine was at 99% disk with the
cache daemon unable to reclaim 11 GB, which is what kept it hunting; the same
client sat windowless for 4 m 32 s untouched when it was not. So this is a
hazard to avoid rather than a delay to wait out, and closing the window back
would make handing the messenger to the system part of the tidying. A
minimized window leaves the screen just as clear and keeps it out of reach.

Regression sequence:

- Initial repair: 36 tests, 10 failures and 1 error before the change.
- Navigation discovery: 6 tests, 2 failures and 1 error before the change.
- Application-local discovery: 18 tests, 3 errors before the change, including
  the real resolver seam returning `WINDOW_NOT_FOUND` for the synthetic target.
- Final related suite: 79 tests, `OK`.
- Windowless client: `FAILED (failures=2, errors=4)` before the change — four
  on the opener refusing `main_state == "missing"` outright, one on a window
  drawn 0.828 s after the request being missed by a single read, one on
  restoration leaving the drawn window where it stood.
- Full suite: `Ran 1005 tests in 44.157s`, `OK`, no skips; `Ran 1034 tests in
  45.953s`, `OK`, after the windowless-client repair.
- Targeted basedpyright: `0 errors, 0 warnings, 0 notes`.
- `python3 -m compileall -q kamado kakaoread`: exit 0.

No automated test sends to a live application. Native payload/identity tests only use
CoreFoundation and process references, and are explicitly skipped off macOS.
Retry/wait tests use virtual time. A failed or uncertain real submission is
not retried automatically.
