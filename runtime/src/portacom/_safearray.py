"""SAFEARRAY: COM's self-describing array, seen from Python as a (nested) list.

A SAFEARRAY knows its element type (a VT_ code) and its shape, so the
metadata does not need to: a SAFEARRAY* parameter takes a list (or a
SafeArray, to choose the element type), and a SAFEARRAY** [out] parameter
returns a list.  Several dimensions are nested lists, outermost first.  One
dimension of bytes (VT_UI1 or VT_I1) goes in and comes out as bytes, and
numbers are copied in bulk, so a frame of a few MB crosses in milliseconds.

The layout is the Windows one.  On Windows the arrays come from OLEAUT32;
elsewhere the allocator lays them out the same way on malloc/free, which is
what a library with a Windows-compatible SAFEARRAY expects.  (MiniCOM's
SAFEARRAY is a C++ class with no C API, so its ABI cannot pass them.)
Lower bounds do not move the data: it starts at pvData whatever they are.
"""

from __future__ import annotations

import array
import ctypes
import math
import sys

from ._memory import resolve_allocator
from ._variant import (
    VARIANT,
    VT_BOOL,
    VT_BSTR,
    VT_CY,
    VT_DATE,
    VT_DISPATCH,
    VT_ERROR,
    VT_I1,
    VT_I2,
    VT_I4,
    VT_I8,
    VT_INT,
    VT_R4,
    VT_R8,
    VT_UI1,
    VT_UI2,
    VT_UI4,
    VT_UI8,
    VT_UINT,
    VT_UNKNOWN,
    VT_VARIANT,
    variant_type,
)

FADF_HAVEVARTYPE, FADF_BSTR, FADF_UNKNOWN, FADF_DISPATCH, FADF_VARIANT = 0x80, 0x100, 0x200, 0x400, 0x800

_NUMBERS = {
    VT_I1: ctypes.c_byte,
    VT_UI1: ctypes.c_ubyte,
    VT_I2: ctypes.c_int16,
    VT_UI2: ctypes.c_uint16,
    VT_I4: ctypes.c_int32,
    VT_UI4: ctypes.c_uint32,
    VT_INT: ctypes.c_int32,
    VT_UINT: ctypes.c_uint32,
    VT_I8: ctypes.c_int64,
    VT_UI8: ctypes.c_uint64,
    VT_R4: ctypes.c_float,
    VT_R8: ctypes.c_double,
    VT_ERROR: ctypes.c_int32,
    VT_DATE: ctypes.c_double,
}
_BYTES = (VT_UI1, VT_I1)

# a buffer's elements as (kind, size), and the VARTYPE that holds them
_KINDS = {
    VT_I1: ("i", 1),
    VT_UI1: ("u", 1),
    VT_I2: ("i", 2),
    VT_UI2: ("u", 2),
    VT_I4: ("i", 4),
    VT_UI4: ("u", 4),
    VT_INT: ("i", 4),
    VT_UINT: ("u", 4),
    VT_I8: ("i", 8),
    VT_UI8: ("u", 8),
    VT_R4: ("f", 4),
    VT_R8: ("f", 8),
    VT_ERROR: ("i", 4),
    VT_DATE: ("f", 8),
}
_BUFFER_TYPES = {
    ("i", 1): VT_I1,
    ("u", 1): VT_UI1,
    ("i", 2): VT_I2,
    ("u", 2): VT_UI2,
    ("i", 4): VT_I4,
    ("u", 4): VT_UI4,
    ("i", 8): VT_I8,
    ("u", 8): VT_UI8,
    ("f", 4): VT_R4,
    ("f", 8): VT_R8,
}
# the array module's typecode for each (its sizes vary by platform, so chosen by size)
_TYPECODES = {
    vt: next(code for code in {"i": "bhilq", "u": "BHILQ", "f": "fd"}[kind] if array.array(code).itemsize == size)
    for vt, (kind, size) in _KINDS.items()
}


class SAFEARRAYBOUND(ctypes.Structure):
    _fields_ = [("cElements", ctypes.c_uint32), ("lLbound", ctypes.c_int32)]


class SAFEARRAY(ctypes.Structure):
    """The SAFEARRAY descriptor.  Each Abi has a subclass (Abi.SAFEARRAY) that
    knows its allocator.  A struct field of type SAFEARRAY* reads as the
    address, for these to read or take:

        values = api._abi.SAFEARRAY.take(result.Data)  # read, then destroy
    """

    _fields_ = [
        ("cDims", ctypes.c_uint16),
        ("fFeatures", ctypes.c_uint16),
        ("cbElements", ctypes.c_uint32),
        ("cLocks", ctypes.c_uint32),
        ("pvData", ctypes.c_void_p),
        ("rgsabound", SAFEARRAYBOUND * 1),  # cDims of them, the last dimension first
    ]
    _allocator_ = None

    @classmethod
    def allocator(cls):
        return resolve_allocator(cls._allocator_)

    @classmethod
    def read(cls, pointer):
        """The values of a SAFEARRAY (an address or pointer), as a SAFEARRAY**
        returns them; the array stays the caller's."""
        return to_list(_address(pointer), cls.allocator())

    @classmethod
    def take(cls, pointer):
        """read(), then destroy the array with this ABI's allocator."""
        address = _address(pointer)
        try:
            return to_list(address, cls.allocator())
        finally:
            if address:
                cls.allocator().safearray_destroy(address)


class SafeArray:
    """Values to pass as a SAFEARRAY with a chosen element type:

        obj.SetWeights(portacom.SafeArray([0.5, 0.25], portacom.VT_R4))

    A plain list works too; its element type is then inferred (bool, int,
    float, str, interfaces, or VARIANT when they are mixed).  So does a
    buffer: bytes are VT_UI1, an array.array its own element type."""

    def __init__(self, values, vt: int | None = None):
        self.values = values
        self.vt = vt if vt is not None else infer_vartype(values)

    def __repr__(self):
        return f"SafeArray({self.values!r}, vt={self.vt})"


def infer_vartype(values) -> int:
    buffer = _buffer(values)
    if buffer is not None:
        return _BUFFER_TYPES.get(_kind(buffer), VT_UI1)
    items = list(_flatten(values))
    if not items:
        return VT_VARIANT
    if all(isinstance(v, bool) for v in items):
        return VT_BOOL
    if all(isinstance(v, int) and not isinstance(v, bool) for v in items):
        return VT_I4 if all(-(2**31) <= v < 2**31 for v in items) else VT_I8
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in items):
        return VT_R8
    if all(isinstance(v, str) for v in items):
        return VT_BSTR
    if all(_is_interface_value(v) for v in items):
        return VT_UNKNOWN
    return VT_VARIANT


def _is_interface_value(value):
    from ._interface import IUnknown

    return value is None or isinstance(value, IUnknown) or hasattr(value, "_com_query_")


def _flatten(values):
    for v in values:
        if isinstance(v, (list, tuple)):
            yield from _flatten(v)
        else:
            yield v


def _buffer(values):
    """A memoryview of bytes, a bytearray, an array.array and the like; None for a list."""
    if isinstance(values, (list, tuple, str)):
        return None
    try:
        return memoryview(values)
    except TypeError:
        return None


def _kind(view):
    """("i", "u" or "f", item size) of a buffer's elements, or None."""
    code = view.format.lstrip("@=" + ("<" if sys.byteorder == "little" else ">"))
    if code == "c":
        code = "B"
    if len(code) != 1:
        return None
    kind = "i" if code in "bhilqn" else "u" if code in "BHILQN" else "f" if code in "fd" else None
    return kind and (kind, view.itemsize)


def _shape(values):
    shape = []
    while isinstance(values, (list, tuple)):
        shape.append(len(values))
        values = values[0] if values else None
    return tuple(shape) or (0,)


def _element_size(vt):
    if vt in _NUMBERS:
        return ctypes.sizeof(_NUMBERS[vt])
    if vt in (VT_BOOL,):
        return 2
    if vt == VT_CY:
        return 8
    if vt in (VT_BSTR, VT_UNKNOWN, VT_DISPATCH):
        return ctypes.sizeof(ctypes.c_void_p)
    if vt == VT_VARIANT:
        return ctypes.sizeof(VARIANT)
    raise NotImplementedError(f"SAFEARRAY of VARTYPE {vt} is not supported")


def _address(pointer):
    """The address of a SAFEARRAY given as an int, a pointer or a descriptor."""
    if pointer is None or isinstance(pointer, int):
        return pointer or 0
    if isinstance(pointer, SAFEARRAY):
        return ctypes.addressof(pointer)
    if isinstance(pointer, (ctypes._Pointer, ctypes.c_void_p)):
        return ctypes.cast(pointer, ctypes.c_void_p).value or 0
    raise TypeError(f"expected a SAFEARRAY address or pointer, not {type(pointer).__name__}")


# -- creating and reading ------------------------------------------------------------------


def create(values, allocator, vt: int | None = None) -> int:
    """A new SAFEARRAY holding values (which it owns); returns its address."""
    if isinstance(values, SafeArray):
        values, vt = values.values, values.vt
    if vt is None:
        vt = infer_vartype(values)
    buffer = _buffer(values)
    if buffer is not None:
        same = _kind(buffer) == _KINDS.get(vt) or (vt in _BYTES and buffer.itemsize == 1)
        if same and buffer.ndim == 1 and buffer.c_contiguous:
            return _create_raw(vt, buffer.cast("B"), allocator)
        values = buffer.tolist()  # bytes as other numbers, or several dimensions
    shape = _shape(values)
    address = allocator.safearray_create(vt, shape)
    try:
        descriptor = SAFEARRAY.from_address(address)
        flat = _data_order(values, shape)
        if vt in _NUMBERS and _pack(vt, flat, descriptor):
            return address
        size = descriptor.cbElements
        for offset, value in enumerate(flat):
            _write(vt, descriptor.pvData + offset * size, value, allocator)
    except BaseException:
        allocator.safearray_destroy(address)
        raise
    return address


def _create_raw(vt, data, allocator):
    """A one-dimensional array of vt holding the bytes of data, in one copy."""
    size = len(data)
    address = allocator.safearray_create(vt, (size // ctypes.sizeof(_NUMBERS[vt]),))
    if size:
        if isinstance(data.obj, bytes) and len(data.obj) == size:
            source = data.obj
        elif data.readonly:
            source = data.tobytes()
        else:
            source = (ctypes.c_char * size).from_buffer(data)
        ctypes.memmove(SAFEARRAY.from_address(address).pvData, source, size)
    return address


def _pack(vt, flat, descriptor):
    """Copy numbers into the array in one go; False if one does not fit (each
    is then written by ctypes, which truncates as C does)."""
    try:
        packed = array.array(_TYPECODES[vt], flat)
    except OverflowError:
        return False
    if flat:
        ctypes.memmove(descriptor.pvData, packed.buffer_info()[0], len(flat) * packed.itemsize)
    return True


def to_list(address: int, allocator):
    """The values of a SAFEARRAY, as a list (nested for several dimensions),
    or bytes for one dimension of VT_UI1 or VT_I1."""
    if not address:
        return None
    vt = allocator.safearray_vartype(address)
    descriptor = SAFEARRAY.from_address(address)
    shape, size = _dimensions(address), descriptor.cbElements
    count = math.prod(shape)
    if vt in _NUMBERS and size == ctypes.sizeof(_NUMBERS[vt]):
        if vt in _BYTES and len(shape) == 1:
            return ctypes.string_at(descriptor.pvData, count) if count else b""
        flat = array.array(_TYPECODES[vt], ctypes.string_at(descriptor.pvData, count * size)).tolist()
    else:
        flat = [_read(vt, descriptor.pvData + i * size, allocator) for i in range(count)]
    return _nest(flat, shape)


def _dimensions(address):
    """The elements in each dimension, outermost first.  The lower bounds do
    not move the data, which starts at pvData whatever they are."""
    dims = SAFEARRAY.from_address(address).cDims
    bounds = (SAFEARRAYBOUND * dims).from_address(address + SAFEARRAY.rgsabound.offset)
    return tuple(bounds[dims - 1 - d].cElements for d in range(dims))


def _data_order(values, shape):
    """Nested lists' elements in SAFEARRAY order: the first index varies fastest."""
    if len(shape) == 1:
        return list(values)
    flat = [None] * math.prod(shape)
    for i, inner in enumerate(values):
        flat[i :: shape[0]] = _data_order(inner, shape[1:])  # a ValueError if they are ragged
    return flat


def _nest(flat, shape):
    if len(shape) == 1:
        return flat
    return [_nest(flat[i :: shape[0]], shape[1:]) for i in range(shape[0])]


def _read(vt, address, allocator):
    if vt in _NUMBERS:
        return _NUMBERS[vt].from_address(address).value
    if vt == VT_BOOL:
        return ctypes.c_int16.from_address(address).value != 0
    if vt == VT_CY:
        return ctypes.c_int64.from_address(address).value / 10000
    if vt == VT_BSTR:
        return allocator.read_bstr(ctypes.c_void_p.from_address(address).value) or ""
    if vt in (VT_UNKNOWN, VT_DISPATCH):
        from ._interface import IUnknown

        pointer = ctypes.c_void_p.from_address(address).value
        return IUnknown(pointer, addref=True) if pointer else None
    if vt == VT_VARIANT:
        return variant_type(allocator).from_address(address).value
    raise NotImplementedError(f"SAFEARRAY of VARTYPE {vt} is not supported")


def _write(vt, address, value, allocator):
    if vt in _NUMBERS:
        _NUMBERS[vt].from_address(address).value = value
    elif vt == VT_BOOL:
        ctypes.c_int16.from_address(address).value = -1 if value else 0
    elif vt == VT_CY:
        ctypes.c_int64.from_address(address).value = round(value * 10000)
    elif vt == VT_BSTR:
        ctypes.c_void_p.from_address(address).value = allocator.alloc_bstr(value)
    elif vt in (VT_UNKNOWN, VT_DISPATCH):
        ctypes.c_void_p.from_address(address).value = _new_reference(value)
    elif vt == VT_VARIANT:
        fresh = variant_type(allocator).from_value(value)  # its contents now belong to the array
        ctypes.memmove(address, ctypes.byref(fresh), ctypes.sizeof(VARIANT))
    else:
        raise NotImplementedError(f"SAFEARRAY of VARTYPE {vt} is not supported")


def _new_reference(value):
    from ._interface import IUnknown

    if value is None:
        return None
    if hasattr(value, "_com_query_"):
        return value._com_query_(IUnknown._iid_)
    value.AddRef()
    return value.address


# -- the Windows layout, for allocators off Windows ----------------------------------------------


class WindowsLayout:
    """SafeArrayCreate/Destroy/GetVartype on malloc/free, laid out as on Windows:
    the VARTYPE in the 4 bytes before the descriptor (FADF_HAVEVARTYPE)."""

    HEADER = 16  # room in front of the descriptor, as SafeArrayAllocDescriptorEx leaves

    def __init__(self, allocator):
        self.allocator = allocator

    def create(self, vt, shape):
        dims = len(shape)
        size = self.HEADER + SAFEARRAY.rgsabound.offset + dims * ctypes.sizeof(SAFEARRAYBOUND)
        block = self.allocator.alloc(size)
        if not block:
            raise MemoryError
        ctypes.memset(block, 0, size)
        address = block + self.HEADER
        ctypes.c_uint32.from_address(address - 4).value = vt
        descriptor = SAFEARRAY.from_address(address)
        descriptor.cDims = dims
        descriptor.fFeatures = FADF_HAVEVARTYPE | {
            VT_BSTR: FADF_BSTR,
            VT_UNKNOWN: FADF_UNKNOWN,
            VT_DISPATCH: FADF_DISPATCH,
            VT_VARIANT: FADF_VARIANT,
        }.get(vt, 0)
        descriptor.cbElements = _element_size(vt)
        bounds = (SAFEARRAYBOUND * dims).from_address(address + SAFEARRAY.rgsabound.offset)
        for d, n in enumerate(shape):
            bounds[dims - 1 - d].cElements = n  # stored last dimension first
        count = math.prod(shape)
        if count:
            descriptor.pvData = self.allocator.alloc(count * descriptor.cbElements)
            if not descriptor.pvData:
                self.allocator.free(block)
                raise MemoryError
            ctypes.memset(descriptor.pvData, 0, count * descriptor.cbElements)
        return address

    def destroy(self, address):
        if not address:
            return
        descriptor = SAFEARRAY.from_address(address)
        vt = self.vartype(address)
        if descriptor.pvData and vt in (VT_BSTR, VT_UNKNOWN, VT_DISPATCH, VT_VARIANT):
            for i in range(math.prod(_dimensions(address))):
                element = descriptor.pvData + i * descriptor.cbElements
                if vt == VT_BSTR:
                    self.allocator.free_bstr(ctypes.c_void_p.from_address(element).value)
                elif vt == VT_VARIANT:
                    variant_type(self.allocator).from_address(element).clear()
                else:
                    pointer = ctypes.c_void_p.from_address(element).value
                    if pointer:
                        from ._vtable import release

                        release(pointer)
        self.allocator.free(descriptor.pvData)
        self.allocator.free(address - self.HEADER)

    def vartype(self, address):
        descriptor = SAFEARRAY.from_address(address)
        if descriptor.fFeatures & FADF_HAVEVARTYPE:
            return ctypes.c_uint32.from_address(address - 4).value
        for flag, vt in (
            (FADF_BSTR, VT_BSTR),
            (FADF_UNKNOWN, VT_UNKNOWN),
            (FADF_DISPATCH, VT_DISPATCH),
            (FADF_VARIANT, VT_VARIANT),
        ):
            if descriptor.fFeatures & flag:
                return vt
        return {1: VT_UI1, 2: VT_I2, 4: VT_I4, 8: VT_R8}.get(descriptor.cbElements, VT_UI1)
