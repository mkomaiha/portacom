"""The one place where the platform is decided.

On Windows the real OLE32/OLEAUT32 are used and functions follow the Windows
calling convention (WINFUNCTYPE, which only differs from CFUNCTYPE on 32-bit
x86).  Everywhere else there is no COM runtime: vtables are called from
Python with CFUNCTYPE, and BSTR/task memory come from an Allocator.
"""

import ctypes
import platform
import sys

NATIVE_COM = sys.platform == "win32"

FUNCTYPE = ctypes.WINFUNCTYPE if NATIVE_COM else ctypes.CFUNCTYPE

WCHAR_SIZE = ctypes.sizeof(ctypes.c_wchar)

_machine = platform.machine().lower()
if _machine in ("x86_64", "amd64", "x64"):
    ARCH = "x64"
elif _machine in ("arm64", "aarch64"):
    ARCH = "arm64"
elif _machine in ("x86", "i386", "i686"):
    ARCH = "x86"
else:
    ARCH = _machine
if ARCH in ("x64", "arm64") and ctypes.sizeof(ctypes.c_void_p) == 4:
    # platform.machine() is the OS's: a 32-bit Python on 64-bit Windows (WOW64) says AMD64
    ARCH = "x86"


def wchar_count(text):
    """The length of text in wchar_t units: UTF-16 code units where wchar_t is
    2 bytes (Windows), code points where it is 4 bytes (Linux, macOS).

    Python's len() counts code points, so passing it as a BSTR length on
    Windows truncates any text with characters outside the BMP.
    """
    if WCHAR_SIZE == 2:
        return len(text.encode("utf-16-le")) // 2
    return len(text)


def by_arch(**variants):
    """The variant for this process's architecture, or None if there is none.

    Keys are "x64", "arm64" and "x86", or several joined by "_"
    (x64_arm64=...).  Generated bindings use it for what the metadata declares
    once per architecture.  An architecture the metadata does not know (riscv64)
    takes x64's variant, or x86's in a 32-bit process.  ARCH is read on each
    call, so a test can set portacom._platform.ARCH before importing bindings.
    """
    arch = ARCH
    if arch not in ("x86", "x64", "arm64"):
        arch = "x64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "x86"
    for key, value in variants.items():
        if arch in key.split("_"):
            return value
    return None
