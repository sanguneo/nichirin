"""Sending one file through the client, the way a person pastes one.

The Windows build presses the room's file button and fills the open-file
dialog. This build hands the client the file the way Finder does — a file URL
on the pasteboard and the client's own 편집 › 붙여넣기 — and then presses the
send button of the confirmation the client shows for it. No keystroke is
synthesised and no open panel is navigated.

* **The screen is checked first.** A locked session draws no confirmation,
  so a locked screen is refused before any window is touched.
* **Everything before the confirmation's send button sends nothing**, and the
  refusal says so. The pasteboard is put back and the composer's text is
  restored if the paste left anything in it.
* **A confirmation that stays open after its send button was pressed is
  closed and answered with an unknown outcome.** Nothing is retried.
* **``submitted`` is the client taking the file, not delivery.** Read the room
  back for a new ``fromMe`` message.

The paste path was watched on a live client (0.27.1, and again on 2026-10-13:
a PNG sent to 나와의 채팅 and read back as a ``fromMe`` photo). Every step
still checks for the element it is about to use and refuses, sending nothing,
when it is not there.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import subprocess
import time
from typing import Any, Callable, Iterable, Optional, Protocol, TypedDict

from . import ax
from .errors import ErrorCode, KamadoError
from .macauto import AXBackend, RoomOpener, Sender, activate, client_pid, running

_PASTE_TITLES = ("붙여넣기", "Paste")
_EDIT_MENU_TITLES = ("편집", "Edit")
_CONFIRM_MARK = "전송"
"""The confirmation's send button carries this word ("전송", "1개 전송")."""

_CANCEL_TITLES = ("취소", "Cancel")
_CONFIRM_WAIT_SECONDS = 5.0
_CLOSE_WAIT_SECONDS = 5.0
_FOCUS_WAIT_SECONDS = 2.0
_POLL_SECONDS = 0.05


class UploadResult(TypedDict):
    submitted: bool
    room: str
    name: str
    size: int


def _nothing_sent(reason: str, room: Optional[str] = None) -> KamadoError:
    return KamadoError(
        ErrorCode.SEND_FAILED, f"{reason}; nothing was sent",
        **({"room": room} if room else {}),
    )


class UploadBackend(Protocol):
    """What :class:`Uploader` needs from the desktop, one step at a time."""

    def screen_locked(self) -> bool: ...
    def input_handle(self, window: int) -> Optional[int]: ...
    def get_text(self, control: int) -> str: ...
    def set_text(self, control: int, text: str) -> None: ...
    def save_pasteboard(self) -> Any: ...
    def put_file(self, path: str) -> None: ...
    def restore_pasteboard(self, saved: Any) -> None: ...
    def focus(self, control: int) -> Any:
        """Make *control* the client's focused element, or return None."""
    def unfocus(self, token: Any) -> None: ...
    def paste(self) -> bool: ...
    def confirmation(self, control: int) -> Optional[int]:
        """Wait for the file-send confirmation the paste raised, or None."""
    def confirm(self, sheet: int) -> bool:
        """Press its send button; True once the confirmation has closed."""
    def dismiss(self, sheet: int) -> None: ...


class Uploader:
    """Hand one staged file to one exactly resolved chat window."""

    def __init__(self, backend: UploadBackend, sender: Sender) -> None:
        self._backend = backend
        self._sender = sender

    def upload(
        self,
        room: str,
        path: str,
        name: str,
        open_if_needed: bool = False,
        aliases: Iterable[str] = (),
        hint: Optional[int] = None,
    ) -> UploadResult:
        backend = self._backend
        if backend.screen_locked():
            raise _nothing_sent("the screen is locked, so the client cannot show its confirmation")
        size = os.path.getsize(path)
        window = self._sender.resolve(
            room, open_if_needed=open_if_needed, aliases=aliases, hint=hint
        )
        title = window.title
        control = backend.input_handle(window.handle)
        if control is None:
            raise KamadoError(
                ErrorCode.INPUT_NOT_FOUND,
                f"found the window for {title!r} but not its message box; nothing was sent",
                room=title,
            )
        pressed = False
        try:
            draft = backend.get_text(control)
            saved = backend.save_pasteboard()
            try:
                backend.put_file(path)
                token = backend.focus(control)
                if token is None:
                    raise _nothing_sent(
                        f"could not give {title!r}'s message box the focus a paste needs",
                        title,
                    )
                try:
                    if not backend.paste():
                        raise _nothing_sent("the client offered no Paste to press", title)
                    sheet = backend.confirmation(control)
                    if sheet is None:
                        if backend.get_text(control) != draft:
                            backend.set_text(control, draft)
                        raise _nothing_sent(
                            "the client showed no file confirmation for the paste", title
                        )
                    pressed = True
                    if not backend.confirm(sheet):
                        backend.dismiss(sheet)
                        raise KamadoError(
                            ErrorCode.SEND_FAILED,
                            "the file confirmation stayed open after its send button was "
                            "pressed; it was closed and the outcome is unknown. Read the "
                            "room back before trying again",
                            room=title,
                        )
                finally:
                    backend.unfocus(token)
            finally:
                backend.restore_pasteboard(saved)
        except (OSError, ax.AccessibilityDenied) as error:
            if pressed:
                raise KamadoError(
                    ErrorCode.SEND_FAILED,
                    f"the upload to {title!r} failed after its send button was pressed "
                    f"({error}); the outcome is unknown",
                    room=title,
                ) from error
            raise _nothing_sent(f"could not upload to {title!r}: {error}", title) from error
        return {"submitted": True, "room": title, "name": name, "size": size}


# -- the screen --------------------------------------------------------------


def screen_locked() -> bool:
    """Whether this login session's screen is locked or not on the console.

    ``CGSSessionScreenIsLocked`` is set while the lock screen is up, and a
    session switched away from has ``kCGSSessionOnConsoleKey`` false; neither
    can show the client's confirmation. An unreadable session counts as
    locked, since refusing sends nothing.
    """
    core, services = ax._load()
    services.CGSessionCopyCurrentDictionary.argtypes = []
    services.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
    session = services.CGSessionCopyCurrentDictionary()
    if not session:
        return True
    try:
        def flag(key: str) -> Optional[bool]:
            name = ax._cfstr(key)
            try:
                value = core.CFDictionaryGetValue(session, name)
            finally:
                core.CFRelease(name)
            if not value:
                return None
            unwrapped = ax._unwrap(core.CFRetain(value))
            return None if unwrapped is None else bool(unwrapped)

        return bool(flag("CGSSessionScreenIsLocked")) or flag("kCGSSessionOnConsoleKey") is False
    finally:
        core.CFRelease(session)


# -- the pasteboard ----------------------------------------------------------


class _ObjC:
    """The few Objective-C messages the pasteboard needs, over ctypes."""

    def __init__(self) -> None:
        self.lib = ctypes.CDLL(ctypes.util.find_library("objc"))
        ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
        self.lib.objc_getClass.argtypes = [ctypes.c_char_p]
        self.lib.objc_getClass.restype = ctypes.c_void_p
        self.lib.sel_registerName.argtypes = [ctypes.c_char_p]
        self.lib.sel_registerName.restype = ctypes.c_void_p
        self.lib.objc_autoreleasePoolPush.restype = ctypes.c_void_p
        self.lib.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
        self._address = ctypes.cast(self.lib.objc_msgSend, ctypes.c_void_p).value

    def cls(self, name: str) -> int:
        return self.lib.objc_getClass(name.encode("ascii"))

    def send(self, receiver, selector: str, *args, restype=ctypes.c_void_p, argtypes=()):
        prototype = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
        function = prototype(self._address)
        return function(receiver, self.lib.sel_registerName(selector.encode("ascii")), *args)

    def string(self, text: str) -> int:
        return self.send(
            self.cls("NSString"), "stringWithUTF8String:", text.encode("utf-8"),
            argtypes=(ctypes.c_char_p,),
        )

    def text(self, string: int) -> str:
        raw = self.send(string, "UTF8String", restype=ctypes.c_char_p)
        return raw.decode("utf-8") if raw else ""

    def count(self, array: int) -> int:
        return self.send(array, "count", restype=ctypes.c_ulong) if array else 0

    def item(self, array: int, index: int) -> int:
        return self.send(array, "objectAtIndex:", index, argtypes=(ctypes.c_ulong,))


class Pasteboard:
    """Save, replace and restore one pasteboard's whole contents.

    The general pasteboard is the person's clipboard, so whatever was on it is
    read type by type before the file goes on, and written back afterwards.
    A *name* addresses a private pasteboard instead, which is how the tests
    exercise this without touching the real one.
    """

    def __init__(self, name: Optional[str] = None) -> None:
        self._objc = _ObjC()
        self._name = name

    def _board(self) -> int:
        objc = self._objc
        board_class = objc.cls("NSPasteboard")
        if self._name is None:
            return objc.send(board_class, "generalPasteboard")
        return objc.send(
            board_class, "pasteboardWithName:", objc.string(self._name),
            argtypes=(ctypes.c_void_p,),
        )

    def _pooled(self, work: Callable[[], Any]) -> Any:
        pool = self._objc.lib.objc_autoreleasePoolPush()
        try:
            return work()
        finally:
            self._objc.lib.objc_autoreleasePoolPop(pool)

    def save(self) -> list[list[tuple[str, bytes]]]:
        def work() -> list[list[tuple[str, bytes]]]:
            objc = self._objc
            items = objc.send(self._board(), "pasteboardItems")
            saved: list[list[tuple[str, bytes]]] = []
            for index in range(objc.count(items)):
                item = objc.item(items, index)
                types = objc.send(item, "types")
                kept: list[tuple[str, bytes]] = []
                for position in range(objc.count(types)):
                    kind = objc.item(types, position)
                    data = objc.send(item, "dataForType:", kind, argtypes=(ctypes.c_void_p,))
                    if not data:
                        continue
                    length = objc.send(data, "length", restype=ctypes.c_ulong)
                    pointer = objc.send(data, "bytes")
                    kept.append((objc.text(kind), ctypes.string_at(pointer, length) if length else b""))
                saved.append(kept)
            return saved

        return self._pooled(work)

    def restore(self, saved: list[list[tuple[str, bytes]]]) -> None:
        def work() -> None:
            objc = self._objc
            board = self._board()
            objc.send(board, "clearContents", restype=ctypes.c_long)
            if not saved:
                return
            array = objc.send(objc.cls("NSMutableArray"), "array")
            for kept in saved:
                item = objc.send(objc.send(objc.cls("NSPasteboardItem"), "alloc"), "init")
                for kind, payload in kept:
                    data = objc.send(
                        objc.cls("NSData"), "dataWithBytes:length:", payload, len(payload),
                        argtypes=(ctypes.c_char_p, ctypes.c_ulong),
                    )
                    objc.send(
                        item, "setData:forType:", data, objc.string(kind),
                        restype=ctypes.c_bool, argtypes=(ctypes.c_void_p, ctypes.c_void_p),
                    )
                objc.send(array, "addObject:", item, restype=None, argtypes=(ctypes.c_void_p,))
                objc.send(item, "release", restype=None)
            if not objc.send(
                board, "writeObjects:", array, restype=ctypes.c_bool,
                argtypes=(ctypes.c_void_p,),
            ):
                raise OSError("could not put the clipboard back")

        self._pooled(work)

    def put_file(self, path: str) -> None:
        def work() -> None:
            objc = self._objc
            board = self._board()
            objc.send(board, "clearContents", restype=ctypes.c_long)
            url = objc.send(
                objc.cls("NSURL"), "fileURLWithPath:", objc.string(path),
                argtypes=(ctypes.c_void_p,),
            )
            array = objc.send(
                objc.cls("NSArray"), "arrayWithObject:", url, argtypes=(ctypes.c_void_p,)
            )
            if not objc.send(
                board, "writeObjects:", array, restype=ctypes.c_bool,
                argtypes=(ctypes.c_void_p,),
            ):
                raise OSError("could not put the file on the clipboard")

        self._pooled(work)

    def file_url(self) -> Optional[str]:
        """The file URL on the pasteboard, if any (what the client will read)."""
        def work() -> Optional[str]:
            objc = self._objc
            value = objc.send(
                self._board(), "stringForType:", objc.string("public.file-url"),
                argtypes=(ctypes.c_void_p,),
            )
            return objc.text(value) if value else None

        return self._pooled(work)

    def release(self) -> None:
        """Give a private pasteboard back to the system."""
        if self._name is not None:
            self._pooled(lambda: self._objc.send(self._board(), "releaseGlobally", restype=None))


def _activate_pid(pid: int) -> None:
    objc = _ObjC()
    pool = objc.lib.objc_autoreleasePoolPush()
    try:
        app = objc.send(
            objc.cls("NSRunningApplication"), "runningApplicationWithProcessIdentifier:",
            pid, argtypes=(ctypes.c_int,),
        )
        if app:
            objc.send(app, "activateWithOptions:", 0, restype=ctypes.c_bool,
                      argtypes=(ctypes.c_ulong,))
    finally:
        objc.lib.objc_autoreleasePoolPop(pool)


def _frontmost_pid(runner=subprocess.run) -> Optional[int]:
    """The application in front now, asked of LaunchServices each time.

    ``NSWorkspace.frontmostApplication`` is kept current by notifications
    delivered on the main run loop, which neither this process nor ``serve``'s
    handler threads run: it answered the first app it saw for the life of the
    process, so a server that once uploaded with KakaoTalk in front never put
    the terminal back again. Draining the run loop helped on the main thread
    only. ``lsappinfo`` asks the same service afresh, from any thread.
    """
    try:
        front = runner(
            ["/usr/bin/lsappinfo", "front"], capture_output=True, text=True, check=False
        ).stdout.strip()
        if not front:
            return None
        info = runner(
            ["/usr/bin/lsappinfo", "info", "-only", "pid", front],
            capture_output=True, text=True, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'"pid"\s*=\s*(\d+)', info)
    return int(match.group(1)) if match else None


# -- the accessibility backend -----------------------------------------------


class AXUploadBackend(AXBackend):
    """:class:`UploadBackend` over the same AX tree the text sender uses."""

    def __init__(self, pid: int, pasteboard: Optional[Pasteboard] = None) -> None:
        super().__init__(pid)
        self._pasteboard = pasteboard or Pasteboard()
        self._before: list[ax.Element] = []
        self._confirm_buttons: dict[int, ax.Element] = {}

    def screen_locked(self) -> bool:
        return screen_locked()

    def save_pasteboard(self) -> Any:
        return self._pasteboard.save()

    def put_file(self, path: str) -> None:
        self._pasteboard.put_file(path)

    def restore_pasteboard(self, saved: Any) -> None:
        self._pasteboard.restore(saved)

    def _poll(self, ready: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if ready():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SECONDS)

    def focus(self, control: int) -> Any:
        """Bring the room forward and prove its message box has the focus.

        A paste goes to the focused element of the key window, so unlike a
        text send this one has to change the foreground. The application's
        focused element and window are read back with ``same_as`` before
        anything is pasted; the app that was in front is remembered.
        """
        composer = self._element(control)
        window = self._windows_by_control.get(control)
        if window is None:
            return None
        previous = _frontmost_pid()
        window.perform("AXRaise")
        activate()
        composer.set_attribute("AXFocused", True)

        def focused() -> bool:
            element = self._app.attribute("AXFocusedUIElement")
            front = self._app.attribute("AXFocusedWindow")
            return (
                isinstance(element, ax.Element) and element.same_as(composer)
                and isinstance(front, ax.Element) and front.same_as(window)
            )

        if not self._poll(focused, _FOCUS_WAIT_SECONDS):
            self.unfocus({"previous": previous})
            return None
        self._before = self._app.windows()
        return {"previous": previous}

    def unfocus(self, token: Any) -> None:
        previous = token.get("previous") if isinstance(token, dict) else None
        if previous and previous != self._pid:
            _activate_pid(previous)

    def paste(self) -> bool:
        menubar = self._app.attribute("AXMenuBar")
        if not isinstance(menubar, ax.Element):
            return False
        for bar_item in menubar.children():
            if bar_item.title not in _EDIT_MENU_TITLES:
                continue
            for menu in bar_item.children():
                for item in menu.children():
                    if item.title.strip() in _PASTE_TITLES and item.attribute("AXEnabled") is True:
                        item.perform("AXPress")
                        return True
        return False

    @staticmethod
    def _confirm_button(container: ax.Element) -> Optional[ax.Element]:
        for child in container.children():
            if child.role == "AXButton" and _CONFIRM_MARK in child.title:
                return child
            if child.role in ("AXGroup", "AXSheet"):
                for grandchild in child.children():
                    if grandchild.role == "AXButton" and _CONFIRM_MARK in grandchild.title:
                        return grandchild
        return None

    def _candidates(self, control: int) -> list[ax.Element]:
        """Containers the confirmation may be: a sheet on the room, or a new window.

        The room window itself is never a candidate, because its own direct
        children hold the text send button, which also says 전송.
        """
        found: list[ax.Element] = []
        room = self._windows_by_control.get(control)
        if room is not None:
            found += [child for child in room.children() if child.role == "AXSheet"]
        for window in self._app.windows():
            if room is not None and window.same_as(room):
                continue
            if any(window.same_as(known) for known in self._before):
                continue
            found.append(window)
        return found

    def confirmation(self, control: int) -> Optional[int]:
        held: dict[str, ax.Element] = {}

        def appeared() -> bool:
            for container in self._candidates(control):
                button = self._confirm_button(container)
                if button is not None and button.attribute("AXEnabled") is not False:
                    held["container"], held["button"] = container, button
                    return True
            return False

        if not self._poll(appeared, _CONFIRM_WAIT_SECONDS):
            return None
        handle = self._register(held["container"])
        self._confirm_buttons[handle] = held["button"]
        return handle

    @staticmethod
    def _gone(container: ax.Element) -> bool:
        return container.role not in ("AXSheet", "AXWindow")

    def confirm(self, sheet: int) -> bool:
        container = self._element(sheet)
        self._confirm_buttons[sheet].perform("AXPress")
        return self._poll(lambda: self._gone(container), _CLOSE_WAIT_SECONDS)

    def dismiss(self, sheet: int) -> None:
        container = self._element(sheet)
        for child in container.children():
            if child.role == "AXButton" and child.title.strip() in _CANCEL_TITLES:
                child.perform("AXPress")
                return
            if child.subrole == "AXCloseButton":
                child.perform("AXPress")
                return
        container.perform("AXCancel")


def make_uploader() -> Uploader:
    """Build the uploader this machine can use, or explain what is missing."""
    if not ax.is_trusted():
        raise KamadoError(
            ErrorCode.PERMISSION_REQUIRED,
            "macOS has not granted this terminal permission to control other "
            "applications. Add it under System Settings › Privacy & Security › "
            "Accessibility, then run the command again.",
            permission="accessibility",
        )
    pid = client_pid()
    if pid is None:
        raise KamadoError(
            ErrorCode.KAKAO_NOT_RUNNING,
            "KakaoTalk is not running; start it and sign in first",
        )
    backend = AXUploadBackend(pid)
    sender = Sender(backend, running, RoomOpener(backend), recoverer=backend.recover_windows)
    return Uploader(backend, sender)

