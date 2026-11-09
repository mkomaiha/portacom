"""The three IUnknown methods, and calling any vtable slot, on raw pointers."""

from __future__ import annotations

import ctypes

from ._platform import FUNCTYPE

QUERY_INTERFACE = FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
ADD_REF = FUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)  # also Release


def entry(this: int, slot: int) -> int:
    """The address of a vtable slot: this points at the vtable pointer."""
    if not this:
        raise ValueError("call through a NULL interface pointer")
    vtable = ctypes.c_void_p.from_address(this).value
    return ctypes.c_void_p.from_address(vtable + slot * ctypes.sizeof(ctypes.c_void_p)).value


def call_slot(this: int, slot: int, argtype, arg) -> int:
    """Call an HRESULT method that takes one argument."""
    return FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, argtype)(entry(this, slot))(this, arg)


def query_interface_hr(this: int, iid) -> tuple[int, int | None]:
    """QueryInterface: (HRESULT, the new reference or None)."""
    out = ctypes.c_void_p()
    hr = QUERY_INTERFACE(entry(this, 0))(this, ctypes.byref(iid), ctypes.byref(out))
    return hr, out.value


def query_interface(this: int, iid) -> int | None:
    """QueryInterface: a new reference to the iid interface, or None (any failure)."""
    hr, pointer = query_interface_hr(this, iid)
    return pointer if hr >= 0 else None


def add_ref(this: int) -> int:
    return ADD_REF(entry(this, 1))(this)


def release(this: int) -> int:
    return ADD_REF(entry(this, 2))(this)
