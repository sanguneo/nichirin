"""The macOS Accessibility and event APIs, reached through ctypes alone.

This is the counterpart of the Windows build's ``user32`` layer, and it exists
for the same reason: a client that is already running has to be addressed
exactly, and the only supported way to address it is the accessibility tree.
Nothing here knows what KakaoTalk is; :mod:`.macauto` supplies that.

Two properties are carried over from the Windows build deliberately.

**Nothing takes the foreground.**  ``CGEventPostToPid`` delivers a key event to
one process without activating it, which is the macOS analogue of posting a
message to a window rather than synthesising global input.  No
``SetFrontProcess``, no cursor movement, no clipboard.

**A hung client must not hang the caller.**  Every accessibility call is a
synchronous IPC round trip into the target application, so the messaging
timeout is set on every element this module hands out; an unresponsive
KakaoTalk fails the command instead of wedging the terminal.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from typing import Any, Final, Optional

_CF_PATH: Final = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_AS_PATH: Final = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"

_UTF8: Final = 0x08000100
_kCFNumberSInt64Type: Final = 4

MESSAGING_TIMEOUT_SECONDS: Final = 5.0
"""How long one accessibility round trip may take before it is a failure."""

KEY_RETURN: Final = 36
KEY_ESCAPE: Final = 53
KEY_DOWN_ARROW: Final = 125

AX_ERROR_SUCCESS: Final = 0
AX_ERROR_API_DISABLED: Final = -25211
AX_ERROR_NOT_IMPLEMENTED: Final = -25208
AX_ERROR_NO_VALUE: Final = -25212
AX_ERROR_ATTRIBUTE_UNSUPPORTED: Final = -25205
AX_ERROR_CANNOT_COMPLETE: Final = -25204

_core: ctypes.CDLL | None = None
_services: ctypes.CDLL | None = None


class AccessibilityDenied(RuntimeError):
    """This process has not been trusted to drive other applications."""


def _load() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    """Load and configure both frameworks once, lazily so imports work anywhere."""
    global _core, _services
    if _core is not None and _services is not None:
        return _core, _services

    core = ctypes.CDLL(_CF_PATH)
    services = ctypes.CDLL(_AS_PATH)

    core.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    core.CFStringCreateWithCString.restype = ctypes.c_void_p
    core.CFStringGetLength.argtypes = [ctypes.c_void_p]
    core.CFStringGetLength.restype = ctypes.c_long
    core.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
    core.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
    core.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32
    ]
    core.CFStringGetCString.restype = ctypes.c_bool
    core.CFRelease.argtypes = [ctypes.c_void_p]
    core.CFRelease.restype = None
    core.CFRetain.argtypes = [ctypes.c_void_p]
    core.CFRetain.restype = ctypes.c_void_p
    core.CFGetTypeID.argtypes = [ctypes.c_void_p]
    core.CFGetTypeID.restype = ctypes.c_ulong
    for name in (
        "CFStringGetTypeID", "CFArrayGetTypeID", "CFBooleanGetTypeID", "CFNumberGetTypeID",
    ):
        getattr(core, name).argtypes = []
        getattr(core, name).restype = ctypes.c_ulong
    core.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    core.CFArrayGetCount.restype = ctypes.c_long
    core.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    core.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    core.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    core.CFNumberGetValue.restype = ctypes.c_bool
    core.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    core.CFBooleanGetValue.restype = ctypes.c_bool

    services.AXIsProcessTrusted.argtypes = []
    services.AXIsProcessTrusted.restype = ctypes.c_bool
    services.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
    services.AXUIElementCreateApplication.restype = ctypes.c_void_p
    services.AXUIElementGetTypeID.argtypes = []
    services.AXUIElementGetTypeID.restype = ctypes.c_ulong
    services.AXUIElementCopyAttributeValue.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    ]
    services.AXUIElementCopyAttributeValue.restype = ctypes.c_int
    services.AXUIElementSetAttributeValue.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    ]
    services.AXUIElementSetAttributeValue.restype = ctypes.c_int
    services.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    services.AXUIElementPerformAction.restype = ctypes.c_int
    services.AXUIElementCopyActionNames.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    ]
    services.AXUIElementCopyActionNames.restype = ctypes.c_int
    services.AXUIElementCopyAttributeNames.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    ]
    services.AXUIElementCopyAttributeNames.restype = ctypes.c_int
    services.AXUIElementSetMessagingTimeout.argtypes = [ctypes.c_void_p, ctypes.c_float]
    services.AXUIElementSetMessagingTimeout.restype = ctypes.c_int

    services.CGEventCreateKeyboardEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool
    ]
    services.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    services.CGEventPostToPid.argtypes = [ctypes.c_int, ctypes.c_void_p]
    services.CGEventPostToPid.restype = None
    services.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    services.CGEventPost.restype = None
    services.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    services.CGEventSetFlags.restype = None
    services.CGEventKeyboardSetUnicodeString.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)
    ]
    services.CGEventKeyboardSetUnicodeString.restype = None
    services.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32
    ]
    services.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    services.CGEventCreate.argtypes = [ctypes.c_void_p]
    services.CGEventCreate.restype = ctypes.c_void_p
    services.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    services.CGEventGetLocation.restype = CGPoint
    services.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    services.AXValueGetValue.restype = ctypes.c_bool

    _core, _services = core, services
    return core, services


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


_kAXValueCGPointType: Final = 1
_kAXValueCGSizeType: Final = 2
_kCGHIDEventTap: Final = 0
_kCGSessionEventTap: Final = 1
_kCGEventMouseMoved: Final = 5
_kCGEventLeftMouseDown: Final = 1
_kCGEventLeftMouseUp: Final = 2
_kCGMouseButtonLeft: Final = 0
KEY_A: Final = 0
KEY_DELETE: Final = 51
FLAG_COMMAND: Final = 0x00100000


def is_trusted() -> bool:
    """Whether this process may drive other applications."""
    _core_, services = _load()
    return bool(services.AXIsProcessTrusted())


# -- CoreFoundation values ---------------------------------------------------


def _cfstr(text: str) -> ctypes.c_void_p:
    core, _services = _load()
    created = core.CFStringCreateWithCString(None, text.encode("utf-8"), _UTF8)
    if not created:
        raise RuntimeError(f"could not represent {text!r} as a CFString")
    return ctypes.c_void_p(created)


def _to_str(ref: int) -> Optional[str]:
    core, _services = _load()
    length = core.CFStringGetLength(ref)
    size = core.CFStringGetMaximumSizeForEncoding(length, _UTF8) + 1
    buffer = ctypes.create_string_buffer(int(size))
    if not core.CFStringGetCString(ref, buffer, size, _UTF8):
        return None
    return buffer.value.decode("utf-8", "replace")


def _unwrap(ref: Optional[int]) -> Any:
    """Turn a CoreFoundation value into a Python one, or an :class:`Element`.

    Ownership matters here: the value came from a ``Copy`` call, so this
    function owns it.  Scalars are converted and the reference released;
    elements and arrays are wrapped in objects that release their own.
    """
    core, services = _load()
    if not ref:
        return None
    type_id = core.CFGetTypeID(ref)
    if type_id == core.CFStringGetTypeID():
        try:
            return _to_str(ref)
        finally:
            core.CFRelease(ref)
    if type_id == services.AXUIElementGetTypeID():
        return Element(ref)
    if type_id == core.CFArrayGetTypeID():
        try:
            count = core.CFArrayGetCount(ref)
            items = []
            for index in range(count):
                item = core.CFArrayGetValueAtIndex(ref, index)
                # Array members are borrowed, so each is retained before being
                # handed to a wrapper that will release it.
                items.append(_unwrap(core.CFRetain(item)) if item else None)
            return items
        finally:
            core.CFRelease(ref)
    if type_id == core.CFBooleanGetTypeID():
        try:
            return bool(core.CFBooleanGetValue(ref))
        finally:
            core.CFRelease(ref)
    if type_id == core.CFNumberGetTypeID():
        try:
            out = ctypes.c_int64(0)
            if core.CFNumberGetValue(ref, _kCFNumberSInt64Type, ctypes.byref(out)):
                return int(out.value)
            return None
        finally:
            core.CFRelease(ref)
    core.CFRelease(ref)
    return None


class Element:
    """One node of an application's accessibility tree.

    Reading an attribute that an element does not have is not an error here:
    the tree is another program's and its shape is observed rather than
    promised, so a missing attribute answers None and lets the caller look
    somewhere else.
    """

    __slots__ = ("_ref",)

    def __init__(self, ref: int) -> None:
        self._ref = ctypes.c_void_p(ref)
        _core_, services = _load()
        services.AXUIElementSetMessagingTimeout(self._ref, MESSAGING_TIMEOUT_SECONDS)

    def __del__(self) -> None:
        try:
            core, _services = _load()
            if self._ref:
                core.CFRelease(self._ref)
                self._ref = ctypes.c_void_p()
        except Exception:
            pass

    @property
    def ref(self) -> ctypes.c_void_p:
        return self._ref

    def attribute(self, name: str) -> Any:
        """Return one attribute, or None when this element has no such thing."""
        _core, services = _load()
        key = _cfstr(name)
        try:
            out = ctypes.c_void_p()
            status = services.AXUIElementCopyAttributeValue(
                self._ref, key, ctypes.byref(out)
            )
            if status == AX_ERROR_API_DISABLED:
                raise AccessibilityDenied(
                    "macOS has not granted this terminal permission to control other "
                    "applications"
                )
            if status != AX_ERROR_SUCCESS:
                return None
            return _unwrap(out.value)
        finally:
            _load()[0].CFRelease(key)

    def attribute_names(self) -> list[str]:
        """Return every attribute this element advertises."""
        _core, services = _load()
        out = ctypes.c_void_p()
        if services.AXUIElementCopyAttributeNames(self._ref, ctypes.byref(out)) != 0:
            return []
        names = _unwrap(out.value)
        return [name for name in (names or []) if isinstance(name, str)]

    def set_attribute(self, name: str, value: str) -> bool:
        """Write a string attribute, reporting whether the client accepted it."""
        _core, services = _load()
        key = _cfstr(name)
        payload = _cfstr(value)
        try:
            return services.AXUIElementSetAttributeValue(self._ref, key, payload) == 0
        finally:
            core = _load()[0]
            core.CFRelease(payload)
            core.CFRelease(key)

    def actions(self) -> list[str]:
        """Return every action this element says it can perform."""
        _core, services = _load()
        out = ctypes.c_void_p()
        if services.AXUIElementCopyActionNames(self._ref, ctypes.byref(out)) != 0:
            return []
        names = _unwrap(out.value)
        return [name for name in (names or []) if isinstance(name, str)]

    def perform(self, action: str) -> bool:
        """Perform an action, reporting whether the client accepted it."""
        _core, services = _load()
        key = _cfstr(action)
        try:
            return services.AXUIElementPerformAction(self._ref, key) == 0
        finally:
            _load()[0].CFRelease(key)

    # -- the handful of attributes this project actually reads ---------------

    @property
    def role(self) -> str:
        value = self.attribute("AXRole")
        return value if isinstance(value, str) else ""

    @property
    def subrole(self) -> str:
        value = self.attribute("AXSubrole")
        return value if isinstance(value, str) else ""

    @property
    def title(self) -> str:
        value = self.attribute("AXTitle")
        return value if isinstance(value, str) else ""

    @property
    def identifier(self) -> str:
        value = self.attribute("AXIdentifier")
        return value if isinstance(value, str) else ""

    @property
    def value(self) -> Any:
        return self.attribute("AXValue")

    @property
    def description(self) -> str:
        value = self.attribute("AXDescription")
        return value if isinstance(value, str) else ""

    def children(self) -> list["Element"]:
        found = self.attribute("AXChildren")
        return [item for item in (found or []) if isinstance(item, Element)]

    def windows(self) -> list["Element"]:
        found = self.attribute("AXWindows")
        return [item for item in (found or []) if isinstance(item, Element)]

    def descendants(self, depth: int = 12) -> "list[Element]":
        """Walk the subtree breadth-first, bounded so a cycle cannot trap us."""
        found: list[Element] = []
        frontier = [(self, 0)]
        while frontier:
            element, level = frontier.pop(0)
            if level >= depth:
                continue
            for child in element.children():
                found.append(child)
                frontier.append((child, level + 1))
        return found

    def _geometry(self, name: str, kind: int, struct):
        _core, services = _load()
        key = _cfstr(name)
        out = ctypes.c_void_p()
        try:
            if services.AXUIElementCopyAttributeValue(self._ref, key, ctypes.byref(out)) != 0:
                return None
        finally:
            _load()[0].CFRelease(key)
        if not out.value:
            return None
        value = struct()
        ok = services.AXValueGetValue(out.value, kind, ctypes.byref(value))
        _load()[0].CFRelease(out.value)
        return value if ok else None

    def frame(self) -> Optional[tuple[float, float, float, float]]:
        """Return this element's screen rectangle as (x, y, width, height)."""
        position = self._geometry("AXPosition", _kAXValueCGPointType, CGPoint)
        size = self._geometry("AXSize", _kAXValueCGSizeType, CGSize)
        if position is None or size is None:
            return None
        return (position.x, position.y, size.width, size.height)

    def set_focused_element(self, element: "Element") -> bool:
        """Point this (application) element's focus at *element*."""
        _core, services = _load()
        key = _cfstr("AXFocusedUIElement")
        try:
            return services.AXUIElementSetAttributeValue(self._ref, key, element._ref) == 0
        finally:
            _load()[0].CFRelease(key)


def application(pid: int) -> Element:
    """Return the accessibility root of one running process."""
    _core, services = _load()
    created = services.AXUIElementCreateApplication(pid)
    if not created:
        raise RuntimeError(f"no accessibility element for process {pid}")
    return Element(created)


def post_key(pid: int, keycode: int, flags: int = 0) -> None:
    """Deliver one keystroke to a process without bringing it to the front.

    ``CGEventPostToPid`` addresses the process rather than the system-wide
    event stream, so the frontmost application, the focused window and the
    cursor are all left exactly where they were.  This reaches a control that
    is already handling the app's key input; a control that has to be focused
    first is driven through :func:`send_key`, which posts to the session.
    """
    _core, services = _load()
    for pressed in (True, False):
        event = services.CGEventCreateKeyboardEvent(None, keycode, pressed)
        if not event:
            raise OSError("could not create a keyboard event")
        try:
            if flags:
                services.CGEventSetFlags(event, flags)
            services.CGEventPostToPid(pid, event)
        finally:
            _load()[0].CFRelease(event)


def send_key(keycode: int, flags: int = 0) -> None:
    """Post a keystroke to the session, i.e. to whatever is frontmost.

    KakaoTalk's composer only reacts to input once it is the focused control
    of the frontmost app, and a keystroke aimed at the process id does not
    reach it; the session event stream does.  The caller is responsible for
    having brought the client forward first.
    """
    _core, services = _load()
    for pressed in (True, False):
        event = services.CGEventCreateKeyboardEvent(None, keycode, pressed)
        if not event:
            raise OSError("could not create a keyboard event")
        try:
            if flags:
                services.CGEventSetFlags(event, flags)
            services.CGEventPost(_kCGSessionEventTap, event)
        finally:
            _load()[0].CFRelease(event)


_TYPE_CHAR_DELAY: Final = 0.012
"""A short pause between characters.

KakaoTalk's composer drops characters when a whole string is delivered on one
event, and even a burst of back-to-back events loses some; one grapheme per
key-down/up pair with a small gap is what arrives intact.
"""


def send_text(text: str) -> None:
    """Type *text* into the frontmost app's focused control, one char at a time.

    The characters are carried on the event itself rather than as key codes,
    so any Unicode — Hangul, emoji — arrives exactly, with no dependence on the
    keyboard layout or the input method.  They are sent one grapheme at a time
    because delivering the whole string at once, or too fast, loses characters
    in this client.
    """
    if not text:
        return
    import time as _time

    _core, services = _load()
    for character in text:
        units = character.encode("utf-16-le")
        count = len(units) // 2
        buffer = (ctypes.c_uint16 * count).from_buffer_copy(units)
        for pressed in (True, False):
            event = services.CGEventCreateKeyboardEvent(None, 0, pressed)
            if not event:
                raise OSError("could not create a keyboard event")
            try:
                services.CGEventKeyboardSetUnicodeString(event, count, buffer)
                services.CGEventPost(_kCGSessionEventTap, event)
            finally:
                _load()[0].CFRelease(event)
        _time.sleep(_TYPE_CHAR_DELAY)


def cursor_location() -> "tuple[float, float]":
    """Return the pointer's current screen position."""
    _core, services = _load()
    event = services.CGEventCreate(None)
    point = services.CGEventGetLocation(event)
    if event:
        _load()[0].CFRelease(event)
    return (point.x, point.y)


def click(x: float, y: float, restore_to: "Optional[tuple[float, float]]" = None) -> None:
    """Click at a screen point, then put the pointer back where it was.

    A click is the one thing that reliably focuses KakaoTalk's composer, which
    is why it is here at all; the pointer is restored immediately so the click
    is the only visible effect.
    """
    _core, services = _load()
    for kind in (_kCGEventLeftMouseDown, _kCGEventLeftMouseUp):
        event = services.CGEventCreateMouseEvent(None, kind, CGPoint(x, y), _kCGMouseButtonLeft)
        if event:
            services.CGEventPost(_kCGHIDEventTap, event)
            _load()[0].CFRelease(event)
    if restore_to is not None:
        event = services.CGEventCreateMouseEvent(
            None, _kCGEventMouseMoved, CGPoint(restore_to[0], restore_to[1]), _kCGMouseButtonLeft
        )
        if event:
            services.CGEventPost(_kCGHIDEventTap, event)
            _load()[0].CFRelease(event)
