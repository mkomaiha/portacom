"""VARIANT, with conversions to and from Python values.

The layout is the Windows one: a 16-bit type tag, three reserved words, and a
union as large as two pointers (24 bytes on 64-bit, 16 on 32-bit).  Generated
bindings use this class wherever the metadata says
Windows.Win32.System.Variant.VARIANT, and calls convert Python values:

    None -> VT_EMPTY        bool -> VT_BOOL     int -> VT_I4 or VT_I8
    float -> VT_R8          str -> VT_BSTR      interface -> VT_UNKNOWN
    IDispatch, Dispatch, DispatchObject -> VT_DISPATCH
    list, tuple, SafeArray -> VT_ARRAY | element type
    bytes, bytearray, memoryview, array.array -> VT_ARRAY | VT_UI1 (or the array's type)

and back, for VT_I1..VT_UI8, VT_INT/UINT, VT_R4/R8, VT_BOOL, VT_BSTR,
VT_UNKNOWN/VT_DISPATCH, VT_ERROR, VT_DATE (a float), VT_CY (a float),
VT_NULL (None) and arrays of those (bytes for one dimension of VT_UI1 or
VT_I1), also by reference (VT_BYREF, as automation callers pass
variables).  Other types stay the VARIANT itself.

A VARIANT allocates its BSTRs and arrays with its type's allocator: the
default one for portacom.VARIANT, the library's for the Abi.VARIANT that
generated bindings use.  With OLE's allocator, clearing is OLEAUT32's
VariantClear.  Otherwise BSTRs and arrays are freed with the allocator and
interfaces are released, and records are cleared through their
IRecordInfo, as Wine's VariantClear does.
"""

import array
import ctypes

from ._platform import NATIVE_COM

VT_EMPTY, VT_NULL, VT_I2, VT_I4, VT_R4, VT_R8, VT_CY, VT_DATE, VT_BSTR = 0, 1, 2, 3, 4, 5, 6, 7, 8
VT_DISPATCH, VT_ERROR, VT_BOOL, VT_VARIANT, VT_UNKNOWN, VT_DECIMAL = 9, 10, 11, 12, 13, 14
VT_I1, VT_UI1, VT_UI2, VT_UI4, VT_I8, VT_UI8, VT_INT, VT_UINT = 16, 17, 18, 19, 20, 21, 22, 23
VT_RECORD, VT_ARRAY, VT_BYREF = 36, 0x2000, 0x4000


class _Value(ctypes.Union):
    _fields_ = [
        ("llVal", ctypes.c_int64),
        ("lVal", ctypes.c_int32),
        ("bVal", ctypes.c_ubyte),
        ("iVal", ctypes.c_int16),
        ("fltVal", ctypes.c_float),
        ("dblVal", ctypes.c_double),
        ("boolVal", ctypes.c_int16),
        ("scode", ctypes.c_int32),
        ("cyVal", ctypes.c_int64),
        ("date", ctypes.c_double),
        ("bstrVal", ctypes.c_void_p),
        ("punkVal", ctypes.c_void_p),
        ("pdispVal", ctypes.c_void_p),
        ("parray", ctypes.c_void_p),
        ("byref", ctypes.c_void_p),
        ("cVal", ctypes.c_byte),
        ("uiVal", ctypes.c_uint16),
        ("ulVal", ctypes.c_uint32),
        ("ullVal", ctypes.c_uint64),
        ("intVal", ctypes.c_int),
        ("uintVal", ctypes.c_uint),
        ("record", ctypes.c_void_p * 2),
    ]


_READERS = {
    VT_I1: "cVal",
    VT_UI1: "bVal",
    VT_I2: "iVal",
    VT_UI2: "uiVal",
    VT_I4: "lVal",
    VT_UI4: "ulVal",
    VT_I8: "llVal",
    VT_UI8: "ullVal",
    VT_INT: "intVal",
    VT_UINT: "uintVal",
    VT_R4: "fltVal",
    VT_R8: "dblVal",
    VT_ERROR: "scode",
    VT_DATE: "date",
}


class VARIANT(ctypes.Structure):
    """portacom.VARIANT uses the default allocator; each Abi has a subclass
    (Abi.VARIANT) whose BSTRs and arrays use the Abi's."""

    _fields_ = [
        ("vt", ctypes.c_uint16),
        ("wReserved1", ctypes.c_uint16),
        ("wReserved2", ctypes.c_uint16),
        ("wReserved3", ctypes.c_uint16),
        ("u", _Value),
    ]
    _allocator_ = None  # the default allocator

    @classmethod
    def _allocator(cls):
        from ._memory import resolve_allocator

        return resolve_allocator(cls._allocator_)

    def __init__(self, value=None):
        super().__init__()
        if value is not None:
            self.set(value)

    @classmethod
    def from_value(cls, value):
        """A new VARIANT holding value (which it then owns: clear() it)."""
        v = cls()
        v.set(value)
        return v

    def set(self, value):
        """Clear, then hold value: a BSTR is allocated, an interface AddRef'd."""
        from ._dispatch import Dispatch, IDispatch
        from ._interface import IUnknown
        from ._safearray import SafeArray, create
        from ._server import ComObject

        self.clear()
        if value is None:
            self.vt = VT_EMPTY
        elif isinstance(value, VARIANT):
            inner = value.value
            if inner is value:  # a type with no conversion, which would recurse forever
                raise TypeError(f"cannot copy a VARIANT of type 0x{value.vt:04x}")
            self.set(inner)
        elif isinstance(value, bool):
            self.vt = VT_BOOL
            self.u.boolVal = -1 if value else 0
        elif isinstance(value, int):
            if not -(1 << 63) <= value < (1 << 64):
                raise OverflowError(f"{value} does not fit in a VARIANT's 64 bits")
            if -(1 << 31) <= value < (1 << 31):
                self.vt, self.u.lVal = VT_I4, value
            elif value < 0 or value < (1 << 63):
                self.vt, self.u.llVal = VT_I8, value
            else:
                self.vt, self.u.ullVal = VT_UI8, value
        elif isinstance(value, float):
            self.vt, self.u.dblVal = VT_R8, value
        elif isinstance(value, str):
            self.vt = VT_BSTR
            self.u.bstrVal = self._allocator().alloc_bstr(value)
        elif isinstance(value, (list, tuple, bytes, bytearray, memoryview, array.array, SafeArray)):
            values = value if isinstance(value, SafeArray) else SafeArray(value)
            self.u.parray = create(values, self._allocator())
            self.vt = VT_ARRAY | values.vt
        elif isinstance(value, ComObject):
            # an object that implements IDispatch goes as one, as automation expects
            pointer = value._com_query_(IDispatch._iid_)
            self.vt = VT_UNKNOWN if pointer is None else VT_DISPATCH
            self.u.punkVal = value._com_query_(IUnknown._iid_) if pointer is None else pointer
        elif isinstance(value, Dispatch):
            self.set(value.interface)
        elif isinstance(value, IUnknown):
            self.vt = VT_DISPATCH if isinstance(value, IDispatch) else VT_UNKNOWN
            if value:
                value.AddRef()
            self.u.punkVal = value.address or None
        else:
            raise TypeError(f"cannot hold a {type(value).__name__} in a VARIANT")

    @property
    def value(self):
        """The Python value (an interface gets a reference of its own)."""
        vt = self.vt
        if not vt & VT_BYREF:
            return self._read(vt, self.u)
        address = self.u.byref  # VT_BYREF | X: the X is at this address
        if not address:
            return None
        if vt & ~VT_BYREF == VT_VARIANT:
            return type(self).from_address(address).value
        return self._read(vt & ~VT_BYREF, _Value.from_address(address))

    def _read(self, vt, u):
        """The value of type vt in the union u (this VARIANT's, or one it points at)."""
        from ._interface import IUnknown

        if vt in (VT_EMPTY, VT_NULL):
            return None
        if vt & VT_ARRAY:
            from ._safearray import to_list

            return to_list(u.parray, self._allocator())
        if vt == VT_BOOL:
            return u.boolVal != 0
        if vt == VT_BSTR:
            return self._allocator().read_bstr(u.bstrVal) if u.bstrVal else ""
        if vt in (VT_UNKNOWN, VT_DISPATCH):
            return IUnknown(u.punkVal, addref=True) if u.punkVal else None
        if vt == VT_CY:
            return u.cyVal / 10000
        field = _READERS.get(vt)
        if field:
            return getattr(u, field)
        return self

    def clear(self):
        """VariantClear: free what the VARIANT owns, and make it VT_EMPTY."""
        if self.vt == VT_EMPTY:
            return
        from ._memory import OleAllocator

        allocator = self._allocator()
        if NATIVE_COM and isinstance(allocator, OleAllocator):
            ctypes.oledll.oleaut32.VariantClear(ctypes.byref(self))
            return
        if self.vt & VT_ARRAY and not self.vt & VT_BYREF:
            allocator.safearray_destroy(self.u.parray)
        elif self.vt == VT_BSTR and self.u.bstrVal:
            allocator.free_bstr(self.u.bstrVal)
        elif self.vt in (VT_UNKNOWN, VT_DISPATCH) and self.u.punkVal:
            from ._interface import IUnknown

            IUnknown(self.u.punkVal).release()
        elif self.vt == VT_RECORD and self.u.record[1]:
            from . import _vtable

            record, info = self.u.record  # pvRecord, IRecordInfo*; the record's memory stays the caller's
            _vtable.call_slot(info, 4, ctypes.c_void_p, record)  # IRecordInfo::RecordClear
            _vtable.release(info)
        self.vt = VT_EMPTY
        self.u.llVal = 0

    def __repr__(self):
        value = self.value
        if value is self:
            return f"VARIANT(vt=0x{self.vt:04x})"
        return f"VARIANT({value!r})"


_variant_types = {}  # allocator -> VARIANT subclass


def variant_type(allocator, name=None):
    """The VARIANT subclass whose BSTRs and arrays use allocator (None: VARIANT)."""
    if allocator is None:
        return VARIANT
    cls = _variant_types.get(allocator)
    if cls is None:
        label = name or getattr(allocator, "name", "?")
        cls = type("VARIANT", (VARIANT,), {"_allocator_": allocator, "__qualname__": f"VARIANT[{label}]"})
        _variant_types[allocator] = cls
    return cls
