"""Interface pointers (IUnknown and generated subclasses) and exported functions.

A generated interface class names its methods in _methods_ and gives their
signatures in _signatures_, a function evaluated on first call, so that
modules can refer to each other's interfaces in any order:

    class IDxcBlob(IUnknown):
        _iid_ = GUID("8BA5FB08-5195-40E2-AC58-0D989C3A0102")
        _methods_ = ("GetBufferPointer", "GetBufferSize")

    IDxcBlob._signatures_ = lambda: ((Ptr(None), ()), (c_size_t, ()))

Every method can then be called the Pythonic way, obj.Method(...), or with
its C signature and ctypes arguments, obj.raw.Method(...).
"""

from __future__ import annotations

import contextlib
import ctypes
from typing import TypeVar

from . import _vtable
from ._errors import E_NOINTERFACE, COMError
from ._platform import FUNCTYPE
from ._types import GUID

T = TypeVar("T", bound="IUnknown")


def _compile(name, spec, abi):
    from ._marshal import Signature, Unsupported

    if isinstance(spec, Unsupported):
        return spec
    restype, params = spec
    if abi is not None:
        restype = abi.override(name, restype)
        params = [p.with_type(abi.override(f"{name}.{p.name}", p.type)) for p in params]
    return Signature(name.rsplit(".", 1)[-1], restype, params, abi)


def _supported(signature, name):
    from ._marshal import Unsupported

    if isinstance(signature, Unsupported):
        raise NotImplementedError(f"{name}: {signature.reason}")
    return signature


class Method:
    """A vtable slot of an interface class; bound to an instance, it is callable."""

    def __init__(self, owner, name, index, slot):
        self.owner = owner
        self.name = name
        self.index = index  # position in owner._methods_
        self.slot = slot
        self._signature = None
        self._functions = {}  # vtable entry address -> ctypes function

    @property
    def qualname(self) -> str:
        return f"{self.owner.__name__}.{self.name}"

    def signature(self):
        if self._signature is None:
            spec = self.owner.__dict__["_signatures_"]()[self.index]
            self._signature = _compile(self.qualname, spec, self.owner._abi_)
        return self._signature

    def function(self, this: int):
        address = _vtable.entry(this, self.slot)
        function = self._functions.get(address)
        if function is None:
            function = _supported(self.signature(), self.qualname).method_prototype()(address)
            self._functions[address] = function
        return function

    def __get__(self, obj, cls=None):
        return self if obj is None else BoundMethod(self, obj)

    def __repr__(self):
        return f"<COM method {self.qualname}, slot {self.slot}>"


class BoundMethod:
    __slots__ = ("method", "obj")

    def __init__(self, method, obj):
        self.method = method
        self.obj = obj

    def __call__(self, *args, **kwargs):
        return self._call(args, kwargs, with_hresult=False)

    def with_hresult(self, *args, **kwargs):
        """Call, and return (HRESULT, outputs...): for methods whose success
        codes matter, such as S_FALSE.  A failure still raises COMError."""
        return self._call(args, kwargs, with_hresult=True)

    def _call(self, args, kwargs, with_hresult):
        method = self.method
        signature = _supported(method.signature(), method.qualname)
        this = self.obj.address
        if not this:
            raise ValueError(f"{method.qualname} called on a NULL {method.owner.__name__}")
        return signature.call(
            method.function(this), this, args, kwargs, method.qualname, method.owner._iid_, with_hresult
        )

    @property
    def __doc__(self):
        signature = self.method.signature()
        if hasattr(signature, "reason"):
            return f"{self.method.qualname}: not supported ({signature.reason})"
        return f"{self.method.qualname}{signature.python_signature()}"

    def __repr__(self):
        return f"<bound {self.method.qualname} of {self.obj!r}>"


class _Raw:
    """obj.raw.Method(*ctypes_args): the method with its C signature, returning
    the raw result (the HRESULT)."""

    __slots__ = ("_obj",)

    def __init__(self, obj):
        self._obj = obj

    def __getattr__(self, name):
        method = getattr(type(self._obj), name, None)
        if not isinstance(method, Method):
            raise AttributeError(name)
        this = self._obj._this(name)
        function = method.function(this)
        return lambda *args: function(this, *args)


class IUnknown:
    """An interface pointer that owns one reference.

    The reference is released when the object is garbage collected, or by
    release().  Construct with addref=True to take a new reference to a
    pointer you do not own.

    Two objects are equal when they hold the same pointer.  The hash is the
    pointer the object was made with, so it does not change when release()
    or detach() makes the object NULL.
    """

    _iid_ = GUID("00000000-0000-0000-C000-000000000046")
    _methods_: tuple = ()
    _vtable_: tuple = ()  # Method objects after IUnknown's three, base interfaces first
    _abi_ = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        base = next(b for b in cls.__mro__[1:] if issubclass(b, IUnknown))
        if "_methods_" not in cls.__dict__:
            cls._vtable_ = base._vtable_
            return
        first = 3 + len(base._vtable_)
        own = tuple(Method(cls, name, i, first + i) for i, name in enumerate(cls.__dict__["_methods_"]))
        for method in own:
            if method.name not in cls.__dict__:
                setattr(cls, method.name, method)
        cls._vtable_ = base._vtable_ + own

    def __init__(self, pointer: int | None = None, addref: bool = False):
        if isinstance(pointer, IUnknown):
            pointer, addref = pointer.address, True
        elif isinstance(pointer, ctypes.c_void_p):
            pointer = pointer.value
        elif pointer is not None and not isinstance(pointer, int):
            hint = " (for a ComObject, use obj.QueryInterface(interface))" if hasattr(pointer, "_com_query_") else ""
            raise TypeError(
                f"{type(self).__name__}() takes an address or an interface, not {type(pointer).__name__}{hint}"
            )
        self._ptr = pointer or 0
        self._key = self._ptr  # the hash: the pointer as made, kept after release()
        if addref and self._ptr:
            _vtable.add_ref(self._ptr)

    def __del__(self):
        with contextlib.suppress(Exception):
            IUnknown.release(self)

    @property
    def address(self) -> int:
        """The interface pointer, 0 for NULL."""
        return self._ptr

    @property
    def _as_parameter_(self):
        return ctypes.c_void_p(self._ptr or None)

    @property
    def raw(self) -> _Raw:
        """obj.raw.Method(*ctypes_args): the C signature, returning the HRESULT."""
        return _Raw(self)

    def __bool__(self):
        return bool(self._ptr)

    def __eq__(self, other):
        # _key too, so that released objects are equal only if their hashes are
        return isinstance(other, IUnknown) and (self._ptr, self._key) == (other._ptr, other._key)

    def __hash__(self):
        return hash(self._key)

    def __repr__(self):
        name = type(self).__name__
        return f"<{name} 0x{self._ptr:x}>" if self._ptr else f"<{name} NULL>"

    def QueryInterface(self, interface: type[T] | GUID | str) -> T:
        """Another interface of the same object: an interface class, or an IID
        (a GUID or a string), which returns an IUnknown.  Raises COMError
        (E_NOINTERFACE) if it has none; see query() for None instead."""
        cls, iid = _interface_and_iid(interface)
        return cls(self._query(iid))

    def query(self, interface: type[T] | GUID | str) -> T | None:
        """Like QueryInterface, but None if the object does not implement it."""
        cls, iid = _interface_and_iid(interface)
        pointer = self._query(iid, "query", missing_ok=True)
        return None if pointer is None else cls(pointer)

    def release(self) -> int:
        """Release the reference now; the object becomes NULL."""
        pointer, self._ptr = self._ptr, 0
        return _vtable.release(pointer) if pointer else 0

    def detach(self) -> int:
        """Give up the reference without releasing it, and return the pointer."""
        pointer, self._ptr = self._ptr, 0
        return pointer

    def AddRef(self) -> int:
        return _vtable.add_ref(self._this("AddRef"))

    def Release(self) -> int:
        """IUnknown::Release itself, which leaves this object dangling; prefer release()."""
        return _vtable.release(self._this("Release"))

    def _this(self, what: str) -> int:
        """The pointer, which a call needs to be non-NULL."""
        if not self._ptr:
            raise ValueError(f"{type(self).__name__}.{what} called on a NULL or released interface")
        return self._ptr

    def _query(self, iid: GUID, what: str = "QueryInterface", missing_ok: bool = False) -> int | None:
        """A new reference to iid; None for E_NOINTERFACE if missing_ok."""
        hr, pointer = _vtable.query_interface_hr(self._this(what), iid)
        if missing_ok and hr == E_NOINTERFACE:
            return None
        if hr < 0:
            raise COMError(hr, f"{type(self).__name__}.QueryInterface")
        return pointer


def _interface_and_iid(interface):
    """(the class to wrap the result in, the IID) for an interface class or an IID."""
    if isinstance(interface, (GUID, str)):
        return IUnknown, GUID(interface)
    return interface, interface._iid_


class Function:
    """A function exported from a shared library, as the metadata declares it.

    Callable the Pythonic way; f.raw is the C function, which takes ctypes
    arguments (f.raw(...)), as obj.raw.Method is for a method.  The library
    is loaded on the first call (see portacom.register_library).
    """

    def __init__(self, abi, library: str, name: str, signature, cdecl: bool = False):
        self.abi = abi
        self.library = library
        self.__name__ = name
        self._spec = signature
        self._cdecl = cdecl
        self._signature = None
        self._function = None
        self._generation = None  # the library registrations _function was bound under

    def signature(self):
        if self._signature is None:
            self._signature = _compile(self.__name__, self._spec(), self.abi)
        return self._signature

    def function(self):
        from . import _loader

        generation = _loader.generation()
        if self._function is None or self._generation != generation:
            signature = _supported(self.signature(), self.__name__)
            address = ctypes.cast(getattr(_loader.load_library(self.library), self.__name__), ctypes.c_void_p).value
            prototype = ctypes.CFUNCTYPE if self._cdecl else FUNCTYPE
            self._function = prototype(signature.c_restype, *signature.argtypes)(address)
            self._generation = generation
        return self._function

    def __call__(self, *args, **kwargs):
        signature = _supported(self.signature(), self.__name__)
        return signature.call(self.function(), None, args, kwargs, self.__name__)

    def with_hresult(self, *args, **kwargs):
        """Call, and return (HRESULT, outputs...)."""
        signature = _supported(self.signature(), self.__name__)
        return signature.call(self.function(), None, args, kwargs, self.__name__, with_hresult=True)

    @property
    def raw(self):
        """The C function, called with ctypes arguments: f.raw(...)."""
        return self.function()

    @property
    def __doc__(self):
        signature = self.signature()
        if hasattr(signature, "reason"):
            return f"{self.__name__}: not supported ({signature.reason})"
        return f"{self.__name__}{signature.python_signature()}"

    def __repr__(self):
        return f"<function {self.__name__} from {self.library}>"
