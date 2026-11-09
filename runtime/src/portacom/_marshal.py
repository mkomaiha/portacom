"""Converting between Python values and C arguments, in both directions.

A generated signature is (restype, (Param, ...)).  Signature turns it into a
ctypes prototype, and into the Pythonic convention:

- [in] parameters are the Python arguments; [out] ones are returned (after the
  return value, for a method that does not return an HRESULT): nothing, one
  value, or a tuple
- an array or buffer whose length is another [in] parameter takes a sequence
  or bytes, and the length is filled in
- REFIID riid + void** ppv takes an interface class and returns an instance
- interfaces, BSTRs, VARIANTs, GUIDs, enums and VARIANT_BOOL convert to and
  from their Python equivalents, with COM's ownership rules: an [in, out]
  BSTR or interface belongs to the callee, which may free or replace it
- a delegate (function pointer) parameter takes a Python callable, kept alive
  for the call, and one that comes back is a callable Delegate

Signature.call() is the calling direction; Signature.dispatch() is the
implementing one, where C calls a Python method (see _server.py).
"""

from __future__ import annotations

import array
import contextlib
import ctypes

from . import _interface, _safearray, _vtable
from ._errors import E_NOINTERFACE, E_POINTER, S_OK, COMError, get_error_info, signed32
from ._platform import FUNCTYPE, NATIVE_COM
from ._types import (
    GUID,
    HRESULT,
    PSTR,
    PWSTR,
    VARIANT_BOOL,
    Array,
    BSTRBase,
    Ptr,
    ctype,
    describe,
    is_delegate,
    is_enum,
    is_interface,
    is_safearray,
    is_struct,
)
from ._variant import VARIANT

# Param flags
IN, OUT, OPTIONAL, RETVAL, COMOUTPTR, CONST = 1, 2, 4, 8, 16, 32

_CDATA = (ctypes.Structure, ctypes.Union, ctypes.Array, ctypes._SimpleCData)


class Param:
    """One parameter of a generated signature.

    count: index of the parameter holding this array's length
    size:  fixed array length
    iid:   index of the REFIID parameter that types this void** [out]
    bytes: index of the parameter holding this buffer's size in bytes
    """

    __slots__ = ("name", "type", "flags", "count", "size", "iid", "bytes")

    def __init__(self, name, type, flags=IN, count=None, size=None, iid=None, bytes=None):
        self.name = name
        self.type = type
        self.flags = flags
        self.count = count
        self.size = size
        self.iid = iid
        self.bytes = bytes

    @property
    def is_in(self) -> bool:
        return bool(self.flags & IN) or not self.flags & OUT

    @property
    def is_out(self) -> bool:
        return bool(self.flags & OUT)

    @property
    def target(self):
        """What a pointer parameter points at."""
        return self.type.target if isinstance(self.type, Ptr) else None

    def with_type(self, t) -> Param:
        return Param(self.name, t, self.flags, self.count, self.size, self.iid, self.bytes)

    def __repr__(self):
        return f"Param({self.name!r}, {describe(self.type)}, {self.flags})"


class Unsupported:
    """A method the bindings cannot describe.  It keeps its vtable slot, raises
    NotImplementedError when called, and answers E_NOTIMPL when implemented."""

    def __init__(self, reason: str):
        self.reason = reason


def is_bstr(t) -> bool:
    return isinstance(t, type) and issubclass(t, BSTRBase)


def is_variant(t) -> bool:
    return isinstance(t, type) and issubclass(t, VARIANT)


def is_string_pointer(t) -> bool:
    return isinstance(t, type) and issubclass(t, (PWSTR, PSTR))


def is_safearray_pointer(t) -> bool:
    """SAFEARRAY*, which Python sees as a list."""
    return isinstance(t, Ptr) and is_safearray(t.target)


def plain(value):
    """The Python value of a ctypes simple value (also of its subclasses)."""
    return value.value if isinstance(value, ctypes._SimpleCData) else value


def address_of(pointer) -> int | None:
    return ctypes.cast(pointer, ctypes.c_void_p).value if pointer is not None else None


class Signature:
    def __init__(self, name: str, restype, params, abi=None):
        self.name = name
        self.abi = abi  # whose task memory a Python implementation's PWSTR results go in
        self.restype = restype
        self.params = list(params)
        self.argtypes = [
            ctypes.c_void_p if p.size is not None and isinstance(p.type, Ptr) else ctype(p.type) for p in self.params
        ]
        self.c_restype = ctypes.c_int32 if restype is HRESULT else ctype(restype)
        self.returns_hresult = restype is HRESULT
        self.returns_value = restype is not None and restype is not HRESULT
        # MSVC returns a struct from a C++ method through a hidden pointer
        # after "this", whatever its size.  The Itanium C++ ABI (Linux, macOS)
        # passes it like a C function taking "this" first, as ctypes does.
        self.hidden_return = NATIVE_COM and is_struct(restype)

        # [in] lengths filled in from the [in] array or buffer they describe
        self.lengths = {}  # length param index -> (array param index, "count" or "bytes")
        for i, p in enumerate(self.params):
            for index, unit in ((p.count, "count"), (p.bytes, "bytes")):
                if index is None or index == i or not 0 <= index < len(self.params):
                    continue
                length = self.params[index]
                if p.is_in and not p.is_out and length.is_in and not length.is_out and not isinstance(length.type, Ptr):
                    self.lengths.setdefault(index, (i, unit))
        self.inputs = [i for i, p in enumerate(self.params) if p.is_in and i not in self.lengths]
        self.outputs = [i for i, p in enumerate(self.params) if p.is_out]
        self._method_prototype = None

    def method_prototype(self):
        if self._method_prototype is None:
            if self.hidden_return:
                self._method_prototype = FUNCTYPE(
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(self.restype), *self.argtypes
                )
            else:
                self._method_prototype = FUNCTYPE(self.c_restype, ctypes.c_void_p, *self.argtypes)
        return self._method_prototype

    def python_signature(self) -> str:
        names = [self.params[i].name for i in self.inputs]
        results = (["return"] if self.returns_value else []) + [self.params[i].name for i in self.outputs]
        return f"({', '.join(names)}) -> {', '.join(results) or 'None'}"

    # -- calling ---------------------------------------------------------------------

    def bind(self, args, kwargs, what):
        if len(args) > len(self.inputs):
            raise TypeError(f"{what}{self.python_signature()} takes {len(self.inputs)} arguments, got {len(args)}")
        values = dict(zip(self.inputs, args, strict=False))
        by_name = {self.params[i].name: i for i in self.inputs}
        for name, value in kwargs.items():
            i = by_name.get(name)
            if i is None:
                raise TypeError(f"{what}() has no argument {name!r}")
            if i in values:
                raise TypeError(f"{what}() got {name!r} twice")
            values[i] = value
        for i in self.inputs:
            p = self.params[i]
            if i not in values:
                if p.flags & OPTIONAL or p.is_out:
                    values[i] = None  # NULL
                else:
                    raise TypeError(f"{what}{self.python_signature()} missing argument {p.name!r}")
        return values

    def call(self, function, this, args, kwargs, what, iid=None, with_hresult=False):
        """Call function (this is None for an exported function) with Python values."""
        values = self.bind(args, kwargs, what)
        keep = []  # alive until the call returns; (free, value) pairs are freed then
        c_args = [None] * len(self.params)
        outs = []  # (param index, storage)
        try:
            try:
                for length_index, (array_index, unit) in self.lengths.items():
                    c_args[length_index] = _length(values.get(array_index), unit)
                for i, p in enumerate(self.params):
                    if i in self.lengths:
                        continue
                    if p.is_out:
                        c_args[i] = self._out_argument(i, p, values, c_args, outs, keep)
                    elif p.count is not None or p.size is not None:
                        c_args[i] = _array_argument(p, values.get(i), keep)
                    else:
                        c_args[i] = to_c(p.type, values.get(i), keep)
            except BaseException:
                self._discard(outs, in_out_only=True)  # [in, out] values made for a call that never happened
                raise

            if this is None:
                raw = function(*c_args)
            elif self.hidden_return:
                raw = self.restype()
                function(this, ctypes.byref(raw), *c_args)
            else:
                raw = function(this, *c_args)

            if self.returns_hresult and signed32(raw) < 0:
                # [out] arguments are NULL on failure; [in, out] ones are still the caller's
                self._discard(outs, in_out_only=True)
                description, source = get_error_info(this, iid)
                raise COMError(raw, what, description, source)
            results = [_return_value(self.restype, raw)] if self.returns_value else []
            for n, (i, storage) in enumerate(outs):
                try:
                    results.append(self._out_value(i, storage, values))
                except BaseException:
                    self._discard(outs[n + 1 :])  # those after it would leak
                    raise
        finally:
            for item in keep:
                if isinstance(item, tuple):
                    free, value = item
                    free(value)
        if with_hresult:
            return (signed32(raw) if self.returns_hresult else None, *results)
        if not results:
            return None
        return results[0] if len(results) == 1 else tuple(results)

    def _is_in_length(self, index):
        return index is not None and self.params[index].is_in and not self.params[index].is_out

    def _out_argument(self, i, p, values, c_args, outs, keep):
        target = p.target
        if not p.is_in and p.size is None and p.count is None and self._is_in_length(p.bytes):  # n bytes
            n = values.get(p.bytes)
            if n is None:
                raise TypeError(f"{p.name}: the size {self.params[p.bytes].name!r} is needed")
            element = ctypes.c_char if target is None else ctype(target)
            storage = (element * (int(plain(n)) // ctypes.sizeof(element)))()
            outs.append((i, storage))
            return storage
        if p.size is not None or (p.count is not None and self._is_in_length(p.count)):
            n = p.size
            if n is None:
                n = values.get(p.count, c_args[p.count])
                if n is None:
                    raise TypeError(f"{p.name}: the length {self.params[p.count].name!r} is needed")
            storage = (ctype(target) * n)() if target is not None else (ctypes.c_char * n)()
            outs.append((i, storage))
            return storage
        if not isinstance(p.type, Ptr):
            raise TypeError(f"[out] parameter {p.name} is not a pointer")
        if target is None or is_interface(target) or (isinstance(target, Ptr) and target.target is None):
            storage = ctypes.c_void_p()
        else:
            storage = ctype(target)()
        if p.is_in:  # [in, out]: starts with the caller's value, which the callee then owns
            value = values.get(i)
            if is_variant(target):
                storage = value if isinstance(value, VARIANT) else target.from_value(value)
            elif is_safearray_pointer(target):
                if value is not None:  # the callee may replace it; the result is ours to free
                    storage.value = _safearray.create(value, target.target._allocator_)
            elif value is None and p.flags & OPTIONAL:
                return None
            elif value is None:
                pass
            elif is_bstr(target):  # a copy (an address goes as it is), which the callee may free
                storage.value = value if isinstance(value, int) else _copy_bstr(target, value)
            elif is_interface(target):  # a reference of its own, which the callee may release
                storage.value = _owned_reference(value, target)
            else:
                converted = to_c(target, value, keep)
                if is_struct(target):
                    storage = converted
                else:
                    storage.value = plain(converted)
        outs.append((i, storage))
        return ctypes.byref(storage)

    def _discard(self, outs, in_out_only=False):
        """Free what [out] storage holds when it will not reach Python."""
        for i, storage in outs:
            p = self.params[i]
            if in_out_only and not p.is_in:
                continue
            target = p.target
            if p.iid is not None and isinstance(target, Ptr) and target.target is None:
                target = _interface.IUnknown
            if isinstance(storage, ctypes.Array) and not isinstance(target, Array):
                items = [_element(storage, k, target) for k in range(len(storage))] if target is not None else ()
            else:
                items = (storage,)
            for c in items:
                with contextlib.suppress(Exception):
                    _free(target, c)

    def _out_value(self, i, storage, values):
        p = self.params[i]
        target = p.target
        if isinstance(storage, ctypes.Array) and not isinstance(target, Array):
            if target is None:
                return bytes(storage)
            return [from_c(target, _element(storage, k, target)) for k in range(len(storage))]
        if p.iid is not None and isinstance(target, Ptr) and target.target is None:
            requested = values.get(p.iid)
            cls = requested if is_interface(requested) else _interface.IUnknown
            return cls(storage.value) if storage.value else None
        return from_c(target, storage)

    # -- implementing ------------------------------------------------------------------

    def dispatch(self, implementation, raw):
        """Call a Python implementation with the C arguments raw, and write its
        results through the [out] pointers.  Returns the C return value."""
        args = []
        outputs = []  # (param index, address or None)
        for i, p in enumerate(self.params):
            if i in self.lengths:
                continue
            value = raw[i]
            if p.is_out:
                address = address_of(value)
                if not address:
                    if not p.flags & OPTIONAL:
                        return E_POINTER
                    outputs.append((i, None))
                    if p.is_in:
                        args.append(None)
                    continue
                if p.is_in:
                    if p.size is not None:
                        args.append(_read_array(p.target, address, p.size))
                    else:
                        args.append(from_callback(p.target, _read_at(p.target, address)))
                outputs.append((i, address))
            elif p.size is not None or self.lengths.get(p.count, (None,))[0] == i:
                n = p.size if p.size is not None else raw[p.count]
                args.append(_read_array(p.target if isinstance(p.type, Ptr) else p.type, address_of(value), n))
            elif self.lengths.get(p.bytes, (None,))[0] == i:
                address = address_of(value)
                args.append(ctypes.string_at(address, raw[p.bytes]) if address else b"")
            else:
                args.append(from_callback(p.type, value))

        result = implementation(*args)

        expected = len(outputs) + self.returns_value
        if expected == 0:
            return S_OK if self.returns_hresult else None
        results = [result]
        if expected > 1:
            if not isinstance(result, tuple) or len(result) != expected:
                raise TypeError(f"{self.name} must return a tuple of {expected} values")
            results = list(result)
        returned = results.pop(0) if self.returns_value else None
        for (i, address), value in zip(outputs, results, strict=False):
            if address is not None:
                self._write_output(i, address, value, raw)
        if self.returns_hresult:
            return S_OK
        if isinstance(returned, (PWSTR, PSTR)):
            return returned.address
        if isinstance(returned, ctypes.c_void_p):  # a delegate: its address
            return returned.value
        return returned

    def _write_output(self, i, address, value, raw):
        p = self.params[i]
        target = p.target
        if p.iid is not None and isinstance(target, Ptr) and target.target is None:
            iid = from_callback(self.params[p.iid].type, raw[p.iid])
            write_out(None, address, value, iid=iid, clear_old=p.is_in)
        elif target is None and p.bytes is not None and isinstance(raw[p.bytes], int):  # a buffer
            data = bytes(value or b"")[: raw[p.bytes]]
            ctypes.memmove(address, data, len(data))
        elif p.size is not None or (
            p.count is not None and isinstance(target, type) and not is_interface(target) and not is_struct(target)
        ):
            n = p.size if p.size is not None else raw[p.count]
            step = ctypes.sizeof(ctype(target))
            for k, item in enumerate(list(value)[:n]):
                write_out(target, address + k * step, item, abi=self.abi)
        else:
            write_out(target, address, value, clear_old=p.is_in, abi=self.abi)


# -- Python -> C ------------------------------------------------------------------------


def to_c(t, value, keep):
    """The C argument for an [in] Python value of described type t.  What must
    stay alive for the call goes into keep, and (free, value) pairs are freed
    after it."""
    if value is None:
        return None
    if is_interface(t):
        return interface_pointer(value, t, keep)
    if is_delegate(t):
        return t.from_value(value, keep)
    if is_bstr(t):
        if isinstance(value, str):
            address = t._allocator_.alloc_bstr(value)
            keep.append((t._allocator_.free_bstr, address))
            return address
        return value.value if isinstance(value, BSTRBase) else value
    if t is VARIANT_BOOL and isinstance(value, bool):
        return -1 if value else 0
    if is_variant(t) or (isinstance(t, Ptr) and is_variant(t.target)):
        cls = t if is_variant(t) else t.target
        variant = value
        if not isinstance(value, VARIANT):
            variant = cls.from_value(value)
            keep.append((VARIANT.clear, variant))
        return variant if cls is t else ctypes.byref(variant)
    if t is GUID or (isinstance(t, Ptr) and t.target is GUID):
        return _guid_argument(value, keep, by_reference=t is not GUID)
    if is_safearray_pointer(t) and isinstance(
        value, (list, tuple, bytes, bytearray, memoryview, array.array, _safearray.SafeArray)
    ):
        allocator = t.target._allocator_
        address = _safearray.create(value, allocator)
        keep.append((allocator.safearray_destroy, address))
        return address
    if isinstance(t, Ptr) and not isinstance(value, (ctypes._Pointer, ctypes.Array, ctypes._CFuncPtr, int)):
        return _pointer_argument(t.target, value, keep)
    if is_enum(t):
        return int(value)
    return value


def _pointer_argument(target, value, keep):
    if target is None:  # void*
        if isinstance(value, (bytes, bytearray, memoryview)):
            buffer = (ctypes.c_char * len(value)).from_buffer_copy(value)
            keep.append(buffer)
            return ctypes.cast(buffer, ctypes.c_void_p)
        if isinstance(value, _CDATA):
            keep.append(value)
            return ctypes.cast(ctypes.byref(value), ctypes.c_void_p)
        return value
    if isinstance(value, _CDATA):
        keep.append(value)
        return ctypes.byref(value)
    if is_interface(target):
        storage = ctypes.c_void_p(interface_pointer(value, target, keep))
    else:  # a pointer to a value: pass the address of a copy
        storage = ctype(target)(to_c(target, value, keep))
    keep.append(storage)
    return ctypes.byref(storage)


def _guid_argument(value, keep, by_reference=True):
    if is_interface(value):
        value = value._iid_
    elif hasattr(value, "_clsid_"):
        value = value._clsid_
    elif isinstance(value, (ctypes._Pointer, int)):
        return value
    elif not isinstance(value, GUID):
        value = GUID(value)
    if not by_reference:
        return value
    keep.append(value)
    return ctypes.byref(value)


def _array_argument(p, value, keep):
    if value is None:
        return None
    element = p.target if isinstance(p.type, Ptr) else p.type
    is_byte_buffer = element in (None, ctypes.c_ubyte, ctypes.c_byte, ctypes.c_char)
    if isinstance(value, (ctypes.Array, ctypes._Pointer, int)) or (isinstance(value, bytes) and is_byte_buffer):
        return value
    if element is None:  # void* with a size in bytes
        data = bytes(value)
        buffer = (ctypes.c_char * len(data)).from_buffer_copy(data)
        keep.append(buffer)
        return buffer
    items = list(value)
    if p.size is not None and len(items) != p.size:
        raise ValueError(f"{p.name}: expected {p.size} values, got {len(items)}")
    array = (ctype(element) * len(items))()
    for k, item in enumerate(items):
        converted = to_c(element, item, keep)
        array[k] = converted if is_struct(element) else plain(converted)
    keep.append(array)
    return array


def _copy_bstr(t, value):
    if isinstance(value, BSTRBase):
        value = value.text
    return t._allocator_.alloc_bstr(value)


def _owned_reference(value, t):
    """A new reference to value as interface t, for a callee that takes it over."""
    if isinstance(value, int):
        if value:
            _vtable.add_ref(value)
        return value or None
    if isinstance(value, _interface.IUnknown) and value and not (t is _interface.IUnknown or isinstance(value, t)):
        return value._query(t._iid_)
    return _new_reference(value, t, None)


def _length(value, unit):
    if value is None:
        return 0
    if unit == "bytes" and isinstance(value, _CDATA):
        return ctypes.sizeof(value)
    return len(value)


def interface_pointer(value, t, keep):
    """The pointer for an interface argument (no new reference)."""
    if value is None or value == 0:
        return None
    if isinstance(value, int):
        return value
    if hasattr(value, "_com_pointer_"):  # a ComObject
        return value._com_pointer_(t)
    if isinstance(value, _interface.IUnknown):
        if t is _interface.IUnknown or isinstance(value, t):
            return value.address or None
        converted = value.QueryInterface(t)  # another interface than the parameter's
        keep.append(converted)
        return converted.address
    if isinstance(value, ctypes.c_void_p):
        return value.value
    raise TypeError(f"expected a {t.__name__}, a ComObject or None, got {type(value).__name__}")


# -- C -> Python --------------------------------------------------------------------------


def from_c(t, c):
    """The Python value of an [out] argument's storage c.  Ownership moves to
    Python: BSTRs and VARIANTs are read and freed, interfaces are wrapped."""
    if is_interface(t):
        return t(c.value) if c.value else None
    if is_delegate(t):
        return c if c.value else None
    if is_bstr(t):
        if not c.value:
            return None
        try:
            return t._allocator_.read_bstr(c.value)
        finally:
            t._allocator_.free_bstr(c.value)
    if t is VARIANT_BOOL:
        return c.value != 0
    if is_variant(t):
        value = c.value
        if value is not c:
            c.clear()
        return value
    if is_safearray_pointer(t):
        allocator = t.target._allocator_
        try:
            return _safearray.to_list(c.value, allocator)
        finally:
            allocator.safearray_destroy(c.value)
    if is_string_pointer(t) or is_struct(t):
        return c  # a string keeps its address, for the caller to free
    if is_enum(t):
        return _enum_value(t, c.value)
    if isinstance(t, Ptr):
        return c.value if t.target is None else c
    if isinstance(t, Array):
        return [from_c(t.element, _element(c, i, t.element)) for i in range(t.size)]
    return plain(c)


def _free(t, c):
    """Free what the storage c of an [out] value of type t owns."""
    if is_interface(t):
        if c.value:
            _vtable.release(c.value)
    elif is_bstr(t):
        t._allocator_.free_bstr(c.value)
    elif is_variant(t):
        c.clear()
    elif is_safearray_pointer(t):
        if c.value:
            t.target._allocator_.safearray_destroy(c.value)


def _return_value(restype, raw):
    if restype is VARIANT_BOOL:
        return plain(raw) != 0
    if is_interface(restype):
        address = plain(raw)
        return restype(address, addref=True) if address else None
    if is_delegate(restype):
        return raw if raw.value else None
    if is_enum(restype):
        return _enum_value(restype, plain(raw))
    return raw


def _enum_value(t, value):
    try:
        return t(value)
    except ValueError:
        return value  # e.g. a combination of flags


def _element(array, i, t):
    ct = ctype(t)
    return ct.from_buffer(array, i * ctypes.sizeof(ct))


# -- in Python COM methods ------------------------------------------------------------


def from_callback(t, value):
    """The Python value of an argument C passed to a Python COM method."""
    if value is None:
        return None
    if is_interface(t):
        address = plain(value)
        return t(address, addref=True) if address else None
    if is_bstr(t):
        address = plain(value)
        return t._allocator_.read_bstr(address) if address else None
    if is_string_pointer(t) or t in (ctypes.c_wchar_p, ctypes.c_char_p):
        return plain(value)
    if is_delegate(t):
        return t(value) if plain(value) else None  # a callable
    if t is VARIANT_BOOL:
        return plain(value) != 0
    if is_variant(t):
        return value.value
    if is_enum(t):
        return _enum_value(t, plain(value))
    if is_safearray_pointer(t):
        return _safearray.to_list(plain(value), t.target._allocator_)
    if isinstance(t, Ptr):
        return _pointee(t.target, value)
    return plain(value)


def _pointee(target, pointer):
    if not pointer:
        return None
    if target is None:
        return pointer
    if target is GUID:
        return GUID(ctypes.string_at(address_of(pointer), 16))
    if is_variant(target):
        return pointer.contents.value
    if is_struct(target):
        return pointer.contents
    if is_interface(target):
        address = ctypes.c_void_p.from_address(address_of(pointer)).value
        return target(address, addref=True) if address else None
    if isinstance(target, type) and issubclass(target, ctypes._SimpleCData):
        return pointer[0]
    return pointer


def _read_at(t, address):
    if is_interface(t) or t is None:
        return ctypes.c_void_p.from_address(address).value
    return (t if is_struct(t) else ctype(t)).from_address(address)


def _read_array(t, address, n):
    if not address:
        return None
    if t is None:
        return ctypes.string_at(address, n)
    array = (ctype(t) * n).from_address(address)
    return [from_callback(t, array[k] if is_struct(t) else plain(array[k])) for k in range(n)]


def write_out(t, address, value, iid=None, clear_old=False, abi=None):
    """Write a Python value through an [out] pointer, as a COM callee does:
    the caller gets its own reference to an interface, and its own copy of a
    string or VARIANT.  A string for a PWSTR goes in abi's task memory."""
    if is_interface(t) or (t is None and iid is not None):
        cell = ctypes.c_void_p.from_address(address)
        old = cell.value if clear_old else None  # [in, out]: the callee owns the caller's reference
        cell.value = _new_reference(value, t or _interface.IUnknown, iid)
        if old:
            _vtable.release(old)
    elif is_bstr(t):
        cell = ctypes.c_void_p.from_address(address)
        old = cell.value if clear_old else None
        cell.value = t._allocator_.alloc_bstr(value) if value is not None else None
        t._allocator_.free_bstr(old)
    elif is_string_pointer(t) and issubclass(t, PWSTR):
        if isinstance(value, str):
            from ._memory import default_allocator

            value = (abi.allocator if abi is not None else default_allocator()).alloc_wstr(value)
        ctypes.c_void_p.from_address(address).value = plain(value) or None
    elif t is VARIANT_BOOL:
        ctypes.c_short.from_address(address).value = -1 if value else 0
    elif is_safearray_pointer(t):
        allocator = t.target._allocator_
        old = ctypes.c_void_p.from_address(address)
        if clear_old and old.value:
            allocator.safearray_destroy(old.value)
        old.value = _safearray.create(value, allocator) if value is not None else None
    elif is_variant(t):
        if clear_old:
            t.from_address(address).clear()
        fresh = t.from_value(value)
        ctypes.memmove(address, ctypes.byref(fresh), ctypes.sizeof(VARIANT))
    elif is_struct(t):
        ctypes.memmove(address, ctypes.byref(value), ctypes.sizeof(t))
    elif isinstance(t, Array):
        step = ctypes.sizeof(ctype(t.element))
        for i, item in enumerate(value):
            write_out(t.element, address + i * step, item, abi=abi)
    else:
        ctype(t).from_address(address).value = int(value) if is_enum(t) else plain(value)


def _new_reference(value, interface, iid):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, _interface.IUnknown) and not value:
        return None
    wanted = iid if iid is not None else interface._iid_
    if hasattr(value, "_com_query_"):  # a ComObject
        pointer = value._com_query_(wanted)
        if pointer is None:
            raise COMError(E_NOINTERFACE, "QueryInterface")
        return pointer
    if isinstance(value, _interface.IUnknown):
        if iid is not None and iid != interface._iid_:
            return value._query(iid)
        value.AddRef()
        return value.address
    raise TypeError(f"expected an interface, got {type(value).__name__}")
