"""Type descriptors that generated bindings use, and their ctypes types.

A generated signature describes each type with:

- a ctypes type (c_int32, ...), or None for void
- a generated Structure/Union subclass, Enum subclass or interface class
- a generated Delegate subclass: a function pointer type
- Ptr(x) and Array(x, n)
- the special types below: GUID, PWSTR, PSTR, HRESULT, VARIANT_BOOL, and the
  BSTR class of an Abi (BSTR allocation depends on the library)
"""

from __future__ import annotations

import ctypes
import enum
import importlib
import threading
import uuid


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, value=None):
        super().__init__()
        if value is None:
            return
        if isinstance(value, GUID):
            ctypes.memmove(ctypes.byref(self), ctypes.byref(value), 16)
            return
        if isinstance(value, str):
            value = uuid.UUID(value.strip("{}"))
        if isinstance(value, uuid.UUID):
            value = value.bytes_le
        if isinstance(value, (bytes, bytearray)) and len(value) == 16:
            ctypes.memmove(ctypes.byref(self), bytes(value), 16)
            return
        raise TypeError(f"cannot make a GUID from {value!r}")

    @property
    def bytes_le(self):
        return ctypes.string_at(ctypes.byref(self), 16)

    def __str__(self):
        return "{" + str(uuid.UUID(bytes_le=self.bytes_le)).upper() + "}"

    def __repr__(self):
        return f"GUID('{self}')"

    def __eq__(self, other):
        if isinstance(other, str):
            try:
                other = GUID(other)
            except ValueError:
                return False
        return isinstance(other, GUID) and self.bytes_le == other.bytes_le

    def __hash__(self):
        return hash(self.bytes_le)


class PWSTR(ctypes.c_wchar_p):
    """A wide string pointer that keeps its address (a plain c_wchar_p turns
    into a str when read, and the pointer is lost)."""

    def __str__(self):
        return self.value or ""

    def __repr__(self):
        return f"PWSTR({self.value!r})" if self else "PWSTR(NULL)"

    @property
    def address(self):
        return ctypes.cast(self, ctypes.c_void_p).value or 0


class PSTR(ctypes.c_char_p):
    """A narrow string pointer that keeps its address."""

    def __str__(self):
        return (self.value or b"").decode("utf-8", "replace")

    def __repr__(self):
        return f"PSTR({self.value!r})" if self else "PSTR(NULL)"

    @property
    def address(self):
        return ctypes.cast(self, ctypes.c_void_p).value or 0


class HRESULT(ctypes.c_int32):
    """A return type that raises COMError on failure."""


class VARIANT_BOOL(ctypes.c_short):
    """VARIANT_TRUE (-1) or VARIANT_FALSE (0), seen from Python as a bool."""


class BSTRBase(ctypes.c_void_p):
    """Base of each Abi's BSTR class, which knows how to allocate and free.

    A BSTR read from a struct field or an [out] argument keeps its address;
    str() reads the text.  free() releases it with the Abi's allocator.
    """

    _allocator_ = None

    @classmethod
    def alloc(cls, text):
        return cls(cls._allocator_.alloc_bstr(text))

    def __str__(self):
        return self._allocator_.read_bstr(self.value) or ""

    def __len__(self):
        return self._allocator_.bstr_len(self.value)

    @property
    def text(self):
        return self._allocator_.read_bstr(self.value)

    def free(self):
        if self.value:
            self._allocator_.free_bstr(self.value)
            self.value = None

    @classmethod
    def take(cls, value):
        """The text of a BSTR (an address, or one read from a struct field), which is then freed."""
        address = value.value if isinstance(value, ctypes.c_void_p) else value
        if not address:
            return None
        try:
            return cls._allocator_.read_bstr(address)
        finally:
            cls._allocator_.free_bstr(address)

    def __repr__(self):
        return f"BSTR({self.text!r})" if self.value else "BSTR(NULL)"


class Ptr:
    """A pointer to a described type; Ptr(None) is void*."""

    __slots__ = ("target",)

    def __init__(self, target):
        self.target = target

    def __eq__(self, other):
        return isinstance(other, Ptr) and other.target == self.target

    def __hash__(self):
        return hash(("Ptr", self.target))

    def __repr__(self):
        return f"Ptr({describe(self.target)})"


class Array:
    """A fixed-size array, as in a struct field "float values[3]"."""

    __slots__ = ("element", "size")

    def __init__(self, element, size):
        self.element = element
        self.size = size

    def __eq__(self, other):
        return isinstance(other, Array) and (other.element, other.size) == (self.element, self.size)

    def __hash__(self):
        return hash(("Array", self.element, self.size))

    def __repr__(self):
        return f"Array({describe(self.element)}, {self.size})"


class Structure(ctypes.Structure):
    """Base of generated structs."""

    def __repr__(self):
        fields = ", ".join(f"{name}={getattr(self, name)!r}" for name, *_ in getattr(self, "_fields_", ()))
        return f"{type(self).__name__}({fields})"


class Union(ctypes.Union):
    """Base of generated unions."""

    def __repr__(self):
        return f"{type(self).__name__}(...)"


_pending = {}  # struct class -> (fields function, pack, anonymous, abi)


def layout(cls, fields, pack=None, anonymous=(), abi=None):
    """Register how a generated struct is laid out: fields() returns
    [(name, described type)].  It is laid out by finish_layouts() at the end
    of its module, or earlier when another struct embeds it by value, so
    that structs in namespaces that import each other lay out in any order."""
    _pending[cls] = (fields, pack, anonymous, abi)


_layout_lock = threading.RLock()
_laying_out = set()


def ensure_layout(cls):
    """Lay out cls now, if it is still pending.  It stays pending until it is
    laid out, so another thread waits for it, and a failure can be retried."""
    if cls not in _pending:
        return
    with _layout_lock:
        spec = _pending.get(cls)
        if spec is None or cls in _laying_out:
            return
        _laying_out.add(cls)
        try:
            fields, pack, anonymous, abi = spec
            fields = fields()
            if abi is not None and abi.overrides:
                fields = [(name, abi.override(f"{cls.__name__}.{name}", t)) for name, t in fields]
            for _, t in fields:
                _embedded_layouts(t)
            set_fields(cls, fields, pack, anonymous)
            del _pending[cls]
        finally:
            _laying_out.discard(cls)


def _embedded_layouts(t):
    while isinstance(t, Array):
        t = t.element
    if isinstance(t, type) and t in _pending:
        ensure_layout(t)


def finish_layouts(*classes):
    for cls in classes:
        ensure_layout(cls)


def set_fields(cls, fields, pack=None, anonymous=()):
    """Lay out a struct now: fields is [(name, described type)]."""
    if pack:
        cls._pack_ = pack
        cls._layout_ = "ms"  # Python 3.14+ warns about _pack_ without it; older versions ignore it
    if anonymous:
        cls._anonymous_ = tuple(anonymous)
    cls._fields_ = [(name, field_ctype(t)) for name, t in fields]


def bitfields(cls, field, bits):
    """Add properties for the bit fields that the metadata packs into one
    integer field: bits is [(name, bit offset, bit count)]."""
    for name, offset, length in bits:
        mask = (1 << length) - 1

        def getter(self, field=field, offset=offset, mask=mask):
            return (getattr(self, field) >> offset) & mask

        def setter(self, value, field=field, offset=offset, mask=mask):
            current = getattr(self, field)
            setattr(self, field, (current & ~(mask << offset)) | ((int(value) & mask) << offset))

        if not hasattr(cls, name):
            setattr(cls, name, property(getter, setter, doc=f"bits {offset}..{offset + length - 1} of {field}"))


class Delegate(ctypes.c_void_p):
    """Base of function pointer types (the metadata's delegates).

    A generated delegate gives its signature as _signature_, a function
    returning (restype, (Param, ...)) as an interface method's does:

        class WNDENUMPROC(Delegate):
            _cdecl_ = False  # Winapi: stdcall on 32-bit Windows

        WNDENUMPROC._signature_ = lambda: (BOOL, (Param("hwnd", HWND), Param("lParam", LPARAM)))

    WNDENUMPROC(func) is a C function pointer that calls the Python callable
    func, whose arguments and result convert as a ComObject method's do; it
    works as long as the object lives.  WNDENUMPROC(address) wraps a pointer
    from C, and calling a delegate calls through its C prototype.  A pointer
    read from a struct field or returned by a function is one already.
    """

    _abi_ = None
    _cdecl_ = False
    _signature_ = None

    def __init__(self, function=None):
        if isinstance(function, ctypes.c_void_p):  # another delegate: share its callback
            self._callback_ = getattr(function, "_callback_", None)
            function = function.value
        elif isinstance(function, ctypes._CFuncPtr):
            self._callback_ = function
            function = ctypes.cast(function, ctypes.c_void_p).value
        elif function is not None and not isinstance(function, int):
            if not callable(function):
                raise TypeError(
                    f"{type(self).__name__}() takes a callable or an address, not {type(function).__name__}"
                )
            from ._server import callback

            self._callback_ = callback(type(self), function)
            function = ctypes.cast(self._callback_, ctypes.c_void_p).value
        super().__init__(function)

    @classmethod
    def signature(cls):
        """The compiled signature, or an Unsupported."""
        signature = cls.__dict__.get("_compiled_")
        if signature is None:
            from ._interface import _compile

            spec = cls._signature_
            if spec is None:
                raise TypeError(f"{cls.__name__} has no _signature_")
            signature = _compile(cls.__name__, spec() if callable(spec) else spec, cls._abi_)
            cls._compiled_ = signature
        return signature

    @classmethod
    def prototype(cls):
        """The ctypes function pointer type."""
        prototype = cls.__dict__.get("_prototype_")
        if prototype is None:
            from ._interface import _supported
            from ._platform import FUNCTYPE

            signature = _supported(cls.signature(), cls.__name__)
            prototype = (ctypes.CFUNCTYPE if cls._cdecl_ else FUNCTYPE)(signature.c_restype, *signature.argtypes)
            cls._prototype_ = prototype
        return prototype

    @classmethod
    def from_value(cls, value, keep):
        """The argument for a parameter of this type: a callable becomes a
        delegate kept in keep, for the length of the call."""
        if value is None or isinstance(value, (int, cls, ctypes._CFuncPtr)):
            return value
        if isinstance(value, ctypes.c_void_p):  # another delegate type, or a plain pointer
            return value.value
        delegate = cls(value)
        keep.append(delegate)
        return delegate

    def __call__(self, *args, **kwargs):
        from ._interface import _supported

        cls = type(self)
        if not self.value:
            raise ValueError(f"{cls.__name__}: the function pointer is NULL")
        signature = _supported(cls.signature(), cls.__name__)
        return signature.call(cls.prototype()(self.value), None, args, kwargs, cls.__name__)

    def __repr__(self):
        name = type(self).__name__
        return f"<{name} 0x{self.value:x}>" if self.value else f"<{name} NULL>"


def is_delegate(t):
    return isinstance(t, type) and issubclass(t, Delegate)


class Enum(enum.IntEnum):
    """Base of generated enums.  _ctype_ is their underlying C type."""


class Flags(enum.IntFlag):
    """Base of generated flag enums."""


def is_interface(t):
    from ._interface import IUnknown

    return isinstance(t, type) and issubclass(t, IUnknown)


def is_enum(t):
    return isinstance(t, type) and issubclass(t, enum.Enum)


def is_safearray(t):
    from ._safearray import SAFEARRAY

    return isinstance(t, type) and issubclass(t, SAFEARRAY)


def is_struct(t):
    return isinstance(t, type) and issubclass(t, (ctypes.Structure, ctypes.Union))


_pointer_cache = {}


def ctype(t):
    """The ctypes type that passes a described type."""
    if t is None:
        return None
    if is_interface(t):
        return ctypes.c_void_p
    if is_enum(t):
        return t._ctype_
    if isinstance(t, Ptr):
        target = t.target
        if target is None:
            return ctypes.c_void_p
        if target is ctypes.c_wchar:
            return PWSTR
        if target is ctypes.c_char:
            return PSTR
        if is_safearray(target):
            return ctypes.c_void_p  # converted to and from lists by the runtime
        pointer = _pointer_cache.get(target)
        if pointer is None:
            # a pointer to a struct does not need its layout yet; pointers are
            # how structs refer to each other in cycles
            pointer = ctypes.POINTER(target if is_struct(target) else ctype(target))
            _pointer_cache[target] = pointer
        return pointer
    if isinstance(t, Array):
        return ctype(t.element) * t.size
    if t in _pending:
        ensure_layout(t)
    return t


def field_ctype(t):
    """Like ctype(), but plain ctypes where a subclass would read back as an object."""
    if t is VARIANT_BOOL:
        return ctypes.c_short
    if t is HRESULT:
        return ctypes.c_int32
    return ctype(t)


def describe(t):
    if t is None:
        return "void"
    if isinstance(t, (Ptr, Array)):
        return repr(t)
    return getattr(t, "__name__", repr(t))


class LazyModule:
    """Another namespace's module, imported on first attribute access, so that
    namespaces that refer to each other can be imported in any order."""

    def __init__(self, importer, importer_namespace, namespace):
        # the importer is <root>.<its namespace>, or just its namespace when
        # the output directory itself is on sys.path
        root = importer[: -len(importer_namespace) - 1] if importer != importer_namespace else ""
        self.__dict__["_name"] = f"{root}.{namespace}" if root else namespace
        self.__dict__["_module"] = None

    def __getattr__(self, name):
        module = self.__dict__["_module"]
        if module is None:
            module = importlib.import_module(self.__dict__["_name"])
            self.__dict__["_module"] = module
        return getattr(module, name)
