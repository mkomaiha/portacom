"""HRESULTs, COMError, and Windows error info (IErrorInfo)."""

from __future__ import annotations

import contextlib
import ctypes
import sys
import traceback
from collections.abc import Callable

from ._platform import NATIVE_COM

S_OK = 0
S_FALSE = 1
E_NOTIMPL = -0x7FFFBFFF  # 0x80004001
E_NOINTERFACE = -0x7FFFBFFE  # 0x80004002
E_POINTER = -0x7FFFBFFD  # 0x80004003
E_FAIL = -0x7FFFBFFB  # 0x80004005
E_INVALIDARG = -0x7FF8FFA9  # 0x80070057
E_OUTOFMEMORY = -0x7FF8FFF2  # 0x8007000E
E_ABORT = -0x7FFFBFFC  # 0x80004004
E_ACCESSDENIED = -0x7FF8FFFB  # 0x80070005
E_BOUNDS = -0x7FFFFFF5  # 0x8000000B
E_HANDLE = -0x7FF8FFFA  # 0x80070006
E_NOT_SET = -0x7FF8FB70  # 0x80070490
E_PENDING = -0x7FFFFFF6  # 0x8000000A
E_UNEXPECTED = -0x7FFF0001  # 0x8000FFFF
CLASS_E_NOAGGREGATION = -0x7FFBFEF0  # 0x80040110
CO_E_NOTINITIALIZED = -0x7FFBFE10  # 0x800401F0
REGDB_E_CLASSNOTREG = -0x7FFBFEAC  # 0x80040154

_NAMES = {
    0x00000001: "S_FALSE",
    0x8000000A: "E_PENDING",
    0x8000000B: "E_BOUNDS",
    0x80004001: "E_NOTIMPL",
    0x80004002: "E_NOINTERFACE",
    0x80004003: "E_POINTER",
    0x80004004: "E_ABORT",
    0x80004005: "E_FAIL",
    0x8000FFFF: "E_UNEXPECTED",
    0x80070002: "HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND)",
    0x80070005: "E_ACCESSDENIED",
    0x80070006: "E_HANDLE",
    0x8007000E: "E_OUTOFMEMORY",
    0x80070057: "E_INVALIDARG",
    0x80070490: "E_NOT_SET",
    0x80020003: "DISP_E_MEMBERNOTFOUND",
    0x80020005: "DISP_E_TYPEMISMATCH",
    0x80020006: "DISP_E_UNKNOWNNAME",
    0x80020007: "DISP_E_NONAMEDARGS",
    0x80020009: "DISP_E_EXCEPTION",
    0x8002000E: "DISP_E_BADPARAMCOUNT",
    0x80040110: "CLASS_E_NOAGGREGATION",
    0x80040154: "REGDB_E_CLASSNOTREG",
    0x800401F0: "CO_E_NOTINITIALIZED",
    0x800401F3: "CO_E_CLASSSTRING",
}


def signed32(value: int) -> int:
    """An HRESULT as C sees it: a signed 32-bit int."""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def hresult_text(hr: int) -> str:
    """'0x80004005 E_FAIL Unspecified error', as much of it as is known."""
    hr &= 0xFFFFFFFF
    parts = [f"0x{hr:08X}"]
    if hr in _NAMES:
        parts.append(_NAMES[hr])
    if NATIVE_COM:
        text = ctypes.FormatError(signed32(hr)).strip()
        if text and "Unknown error" not in text:
            parts.append(text)
    return " ".join(parts)


class COMError(OSError):
    """A failed HRESULT.

    hresult is signed, as in C; hr is the same value unsigned, as it is usually
    written (0x80004005).  description and source come from IErrorInfo, or are
    given when raising from a Python COM method.
    """

    def __init__(
        self, hresult: int, what: str | None = None, description: str | None = None, source: str | None = None
    ):
        self.hresult = signed32(hresult)
        self.what = what
        self.description = description
        self.source = source
        message = hresult_text(hresult)
        if what:
            message = f"{what} failed: {message}"
        if description:
            message = f"{message}: {description}"
        super().__init__(message)

    @property
    def hr(self) -> int:
        return self.hresult & 0xFFFFFFFF

    def __reduce__(self):
        return (COMError, (self.hresult, self.what, self.description, self.source))


def succeeded(hr: int) -> bool:
    return signed32(hr) >= 0


def failed(hr: int) -> bool:
    return signed32(hr) < 0


def check(hr: int, what: str | None = None) -> int:
    """Raise COMError for a failed HRESULT; return it otherwise."""
    if signed32(hr) < 0:
        raise COMError(hr, what)
    return hr


# -- exceptions in Python COM methods ----------------------------------------------


def _print_callback_error(obj, method: str, exc: BaseException) -> None:
    sys.stderr.write(f"portacom: {type(obj).__name__}.{method} raised, returning E_FAIL\n")
    traceback.print_exception(type(exc), exc, exc.__traceback__)


_callback_error_handler: Callable = _print_callback_error


def set_callback_error_handler(handler: Callable | None) -> Callable:
    """Choose what sees an unexpected exception in a Python COM method:
    handler(obj, method_name, exception).  None restores the default, which
    prints the traceback.  Returns the previous handler.

    Whatever the handler does, the exception never reaches the C caller,
    which gets E_FAIL.
    """
    global _callback_error_handler
    previous = _callback_error_handler
    _callback_error_handler = handler or _print_callback_error
    return previous


def report_callback_error(obj, method: str, exc: BaseException) -> None:
    with contextlib.suppress(BaseException):
        _callback_error_handler(obj, method, exc)


# -- IErrorInfo (Windows only: there is no OLEAUT32 elsewhere) -----------------------


def get_error_info(this: int, iid) -> tuple[str | None, str | None]:
    """(description, source) that a failing object left with SetErrorInfo,
    if it says through ISupportErrorInfo that it reports errors for iid."""
    if not NATIVE_COM or not this:
        return None, None
    from ._memory import default_allocator
    from ._types import GUID
    from ._vtable import call_slot, query_interface, release

    try:
        support = query_interface(this, GUID("DF0B3D60-548F-101B-8E65-08002B2BD119"))  # ISupportErrorInfo
        if not support:
            return None, None
        supported = call_slot(support, 3, ctypes.c_void_p, ctypes.byref(iid))  # InterfaceSupportsErrorInfo
        release(support)
        if supported != S_OK:
            return None, None
        info = ctypes.c_void_p()
        if ctypes.windll.oleaut32.GetErrorInfo(0, ctypes.byref(info)) != S_OK or not info.value:
            return None, None
        allocator = default_allocator()
        texts = []
        for slot in (5, 4):  # IErrorInfo::GetDescription, GetSource
            bstr = ctypes.c_void_p()
            call_slot(info.value, slot, ctypes.c_void_p, ctypes.byref(bstr))
            texts.append(allocator.read_bstr(bstr.value))
            allocator.free_bstr(bstr.value)
        release(info.value)
        description, source = texts
        return (description.strip() if description else None), source
    except Exception:
        return None, None


def set_error_info(description: str | None, source: str | None = None) -> None:
    """SetErrorInfo, so that the caller of a failing Python COM object sees why."""
    if not NATIVE_COM or not description:
        return
    from ._types import GUID
    from ._vtable import call_slot, query_interface, release

    try:
        oleaut32 = ctypes.windll.oleaut32
        create = ctypes.c_void_p()
        if oleaut32.CreateErrorInfo(ctypes.byref(create)) != S_OK:
            return
        call_slot(create.value, 5, ctypes.c_wchar_p, description)  # ICreateErrorInfo::SetDescription
        if source:
            call_slot(create.value, 4, ctypes.c_wchar_p, source)  # SetSource
        info = query_interface(create.value, GUID("1CF2B120-547D-101B-8E65-08002B2BD119"))  # IErrorInfo
        if info:
            oleaut32.SetErrorInfo(0, ctypes.c_void_p(info))
            release(info)
        release(create.value)
    except Exception:
        pass
