"""COM objects implemented in Python."""

from __future__ import annotations

import ctypes
import threading
import weakref
from typing import TypeVar

from ._errors import (
    E_FAIL,
    E_NOINTERFACE,
    E_NOTIMPL,
    E_POINTER,
    S_FALSE,
    S_OK,
    COMError,
    report_callback_error,
    set_error_info,
)
from ._interface import IUnknown
from ._marshal import Unsupported
from ._platform import ARCH, FUNCTYPE, NATIVE_COM
from ._types import GUID, is_struct
from ._vtable import ADD_REF, QUERY_INTERFACE

T = TypeVar("T", bound=IUnknown)

IID_ISupportErrorInfo = GUID("DF0B3D60-548F-101B-8E65-08002B2BD119")

# Objects that C holds references to.  C only has their address, so without
# this they could be garbage collected while it still calls them.
_held = set()


class ComObject:
    """Base class for COM objects implemented in Python.

        class IncludeHandler(portacom.ComObject):
            _interfaces_ = (IDxcIncludeHandler,)

            def LoadSource(self, filename):
                return blob                     # the [out] IDxcBlob

    Methods take the [in] arguments and return the [out] ones, exactly like
    calls through the bindings.  Raising COMError(hr) returns hr (with its
    description as error info on Windows); a missing method returns
    E_NOTIMPL; any other exception returns E_FAIL and goes to the handler
    set with portacom.set_callback_error_handler().  No exception ever
    reaches the C caller.

    QueryInterface, AddRef and Release are handled here.  Python holds the
    first reference, and the object stays alive while C holds others.
    """

    _interfaces_: tuple = ()

    @property
    def references(self) -> int:
        """The COM reference count, including Python's own reference."""
        return self._com_state_().refs

    def QueryInterface(self, interface: type[T]) -> T:
        """This object as a client sees it, through one of its interfaces."""
        return interface(self._com_pointer_(interface), addref=True)

    def query(self, interface: type[T]) -> T | None:
        """Like QueryInterface, but None if the object does not implement interface."""
        pointer = self._com_state_().iids.get(interface._iid_.bytes_le)
        return interface(pointer, addref=True) if pointer else None

    # used by the marshaling code

    def _com_state_(self) -> _ComState:
        state = self.__dict__.get("_com_")
        if state is None:
            state = self.__dict__["_com_"] = _ComState(self)
        return state

    def _com_pointer_(self, interface=IUnknown) -> int:
        """The pointer for interface, without a new reference."""
        return self._com_state_().pointer_for(interface)

    def _com_query_(self, iid: GUID) -> int | None:
        """The pointer for iid with a new reference, or None."""
        state = self._com_state_()
        pointer = state.iids.get(iid.bytes_le)
        if pointer is not None:
            state.count(+1)
        return pointer


class _ComState:
    """The vtables of one ComObject, and its reference count."""

    def __init__(self, obj: ComObject):
        interfaces = tuple(obj._interfaces_)
        if not interfaces:
            raise TypeError(f"{type(obj).__name__} lists no _interfaces_")
        self.obj = weakref.ref(obj)
        self.name = type(obj).__name__
        self.refs = 1
        self._lock = threading.Lock()  # C may AddRef and Release from any thread
        self.iids = {}  # IID bytes -> pointer
        self.interfaces = []  # (interface class, pointer)
        self._keep = []  # vtables and the callbacks in them

        for interface in interfaces:
            entries = [_thunk(self.obj, method) for method in interface._vtable_]
            pointer = self._add_vtable(entries)
            self.interfaces.append((interface, pointer))
            for cls in interface.__mro__:
                if isinstance(cls, type) and issubclass(cls, IUnknown):
                    self.iids.setdefault(cls._iid_.bytes_le, pointer)

        if NATIVE_COM and IID_ISupportErrorInfo.bytes_le not in self.iids:
            # Windows callers only read error info from objects that support it
            supported = set(self.iids)
            supports_error_info = FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p)(
                lambda this, riid: S_OK if ctypes.string_at(riid, 16) in supported else S_FALSE
            )
            self.iids[IID_ISupportErrorInfo.bytes_le] = self._add_vtable([supports_error_info])

    def _add_vtable(self, entries) -> int:
        """A vtable with IUnknown's three methods first; returns the interface pointer."""
        entries = [
            QUERY_INTERFACE(lambda this, riid, out: self.query_interface(riid, out)),
            ADD_REF(lambda this: self.count(+1)),
            ADD_REF(lambda this: self.count(-1)),
            *entries,
        ]
        vtable = (ctypes.c_void_p * len(entries))(*[ctypes.cast(e, ctypes.c_void_p).value for e in entries])
        cell = ctypes.c_void_p(ctypes.addressof(vtable))  # what an interface pointer points at
        self._keep.append((entries, vtable, cell))
        return ctypes.addressof(cell)

    def pointer_for(self, interface) -> int:
        for cls, pointer in self.interfaces:
            if issubclass(cls, interface):
                return pointer
        pointer = self.iids.get(interface._iid_.bytes_le)
        if pointer is None:
            raise TypeError(f"{self.name} does not implement {interface.__name__}")
        return pointer

    def query_interface(self, riid, out) -> int:
        if not out:
            return E_POINTER
        pointer = self.iids.get(ctypes.string_at(riid, 16))
        out[0] = pointer
        if pointer is None:
            return E_NOINTERFACE
        self.count(+1)
        return S_OK

    def count(self, change: int) -> int:
        with self._lock:
            self.refs += change
            refs = self.refs
            obj = self.obj()
            if obj is not None:
                if refs > 1:
                    _held.add(obj)
                else:
                    _held.discard(obj)
        return refs


# 32-bit Windows methods are stdcall: the callee pops the arguments, so a
# vtable entry must know how many there are
_STDCALL = NATIVE_COM and ctypes.sizeof(ctypes.c_void_p) == 4
# the System V x86-64 ABI (Linux, macOS), whose struct returns some callbacks can do
_SYSV_X64 = not NATIVE_COM and ARCH == "x64" and ctypes.sizeof(ctypes.c_void_p) == 8


def _thunk(owner, method):
    """The vtable entry that calls the Python object's method."""
    name = method.name
    signature = method.signature()
    if isinstance(signature, Unsupported):
        if _STDCALL:
            return None  # its arguments are unknown: a NULL entry, rather than a corrupted stack
        # its arguments are unknown, but the caller pops them (cdecl, and x64)
        return FUNCTYPE(ctypes.c_int32, ctypes.c_void_p)(lambda this: E_NOTIMPL)

    hresult = signature.returns_hresult
    restype = signature.restype
    struct_return = is_struct(restype)
    result_first = False  # System V: the hidden result pointer comes before this
    register = None  # System V: the integer or float type a small struct comes back in
    if struct_return and not signature.hidden_return:
        register = _sysv_struct_return(restype) if _SYSV_X64 else None
        if register is None:
            return _unsupported_struct_return(owner, name, signature)
        if register == "memory":
            register, result_first = None, True

    def entry(*raw):  # raw: this, and the arguments
        result_address = None
        if signature.hidden_return:  # MSVC: (this, result, args...)
            result_address, raw = raw[1], raw[2:]
        elif result_first:  # System V: (result, this, args...)
            result_address, raw = raw[0], raw[2:]
        else:
            raw = raw[1:]
        if hresult:
            fallback = E_NOTIMPL
        elif result_address is not None:
            fallback = result_address  # the caller reads the result through the returned pointer
        else:
            fallback = 0

        def failure(hr, description=None, source=None):
            if not hresult:
                return fallback
            set_error_info(description, source)
            return hr

        obj = owner()
        implementation = getattr(obj, name, None) if obj is not None else None
        if implementation is None:
            return fallback
        try:
            result = signature.dispatch(implementation, raw)
        except COMError as e:
            return failure(e.hresult, e.description, e.source)
        except NotImplementedError:
            return failure(E_NOTIMPL)
        except BaseException as e:  # never let it into the C caller
            report_callback_error(obj, name, e)
            return failure(E_FAIL, f"{type(e).__name__}: {e}", type(obj).__name__)
        if result_address is not None:
            ctypes.memmove(result_address, ctypes.byref(result), ctypes.sizeof(restype))
            return result_address
        if register is not None:  # the struct's bytes, in rax or xmm0
            return register.from_buffer_copy(bytes(result).ljust(8, b"\0")).value
        return result

    if signature.hidden_return or result_first:  # this and the result pointer, in either order
        return FUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, *signature.argtypes)(entry)
    return FUNCTYPE(register or _callback_restype(signature), ctypes.c_void_p, *signature.argtypes)(entry)


def callback(delegate, function):
    """A C function pointer of a Delegate type that calls the Python callable
    function, with the conversions of a ComObject method.  An exception is
    reported to the callback error handler; the C caller gets the HRESULT of a
    COMError, E_FAIL, or 0."""
    from ._interface import _supported

    name = delegate.__name__
    signature = _supported(delegate.signature(), name)
    if is_struct(signature.restype):
        raise TypeError(f"{name} returns a {signature.restype.__name__} by value, which a Python callback cannot")
    hresult = signature.returns_hresult
    fallback = E_FAIL if hresult else (None if signature.restype is None else 0)

    def entry(*raw):
        try:
            result = signature.dispatch(function, raw)
        except COMError as e:
            if not hresult:
                report_callback_error(function, name, e)
            return e.hresult if hresult else fallback
        except BaseException as e:  # never let it into the C caller
            report_callback_error(function, name, e)
            return fallback
        return result.value if isinstance(result, ctypes._SimpleCData) else result

    return (ctypes.CFUNCTYPE if delegate._cdecl_ else FUNCTYPE)(_callback_restype(signature), *signature.argtypes)(
        entry
    )


def _callback_restype(signature):
    """The restype a ctypes callback can return: pointers it must not keep alive are plain addresses."""
    restype = signature.c_restype
    if isinstance(restype, type) and issubclass(restype, (ctypes.c_wchar_p, ctypes.c_char_p, ctypes.c_void_p)):
        return ctypes.c_void_p  # a str ctypes would keep alive, a delegate or string pointer: its address
    return restype


def _sysv_struct_return(restype):
    """How System V x86-64 returns restype, where a ctypes callback can do it:
    "memory" (through a hidden pointer, for more than 16 bytes), or c_uint64 or
    c_double for up to 8 bytes of only integers or only floats (in rax or
    xmm0).  None otherwise: 9 to 16 bytes come back in two registers."""
    size = ctypes.sizeof(restype)
    if size > 16:
        return "memory"
    if not 0 < size <= 8 or getattr(restype, "_pack_", None):  # packed: maybe unaligned, so in memory
        return None
    kinds = {_leaf_kind(t) for t in _leaf_types(restype)}
    if kinds == {"float"}:
        return ctypes.c_double
    if kinds == {"int"}:
        return ctypes.c_uint64
    return None


def _leaf_types(t):
    if isinstance(t, type) and issubclass(t, ctypes.Array):
        yield from _leaf_types(t._type_)
    elif is_struct(t):
        for field in t._fields_:
            yield from _leaf_types(field[1])
    else:
        yield t


def _leaf_kind(t):
    if t in (ctypes.c_float, ctypes.c_double):
        return "float"
    if t is ctypes.c_longdouble:
        return "x87"
    return "int"  # integers, pointers, enums, bool


def _unsupported_struct_return(owner, name, signature):
    """A method returning a struct by value the way a callback cannot: reported, and 0."""
    what = f"{signature.name} returns a {signature.restype.__name__} by value, which a Python method cannot here"

    def entry(this, *raw):
        report_callback_error(owner(), name, NotImplementedError(what))
        return 0

    return FUNCTYPE(ctypes.c_int32, ctypes.c_void_p, *signature.argtypes)(entry)
