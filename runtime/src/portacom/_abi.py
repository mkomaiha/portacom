"""ABI profiles: what a library was compiled with that the .winmd cannot say.

A .winmd describes the Windows ABI, and the format has no way to say that a
library built for Linux or macOS differs.  So winmd2py generates ABI-neutral
bindings, and whoever knows the library declares its differences with an Abi
before importing them (in the bindings' own __init__.py, which winmd2py
creates once for this):

    typedefs   {typedef name: ctypes type}, for every use of a named typedef
               from the metadata (BOOL, BSTR, ...)
    overrides  {"Interface.Method": restype, "Interface.Method.param": type,
               "Function.param": type, "Struct.field": type}, for uses that
               the metadata does not name (LONG, and an IDL "long", are just
               Int32)
    allocator  how BSTRs, VARIANTs, task memory and SAFEARRAYs are allocated
               and freed
    library    the shared library behind the bindings ("Foo.dll"): where
               co_create_instance() finds CoCreateInstance, and whose own
               exported SysAllocStringLen & co. are used where it has them

An Abi describes the library as built for Linux and macOS.  On Windows the
metadata is the truth, so use_abi() binds WINDOWS there, unless the Abi was
made with on_windows=True.  Each generated module asks abi_for() for its Abi
when it is imported; see use_abi() for how the registration is chosen.
Without one it is WINDOWS.
"""

from ._memory import Allocator, LazyAllocator, LibraryAllocator, default_allocator
from ._platform import NATIVE_COM
from ._types import HRESULT, PSTR, PWSTR, VARIANT_BOOL, BSTRBase


class Abi:
    """How a library built for Linux and macOS differs from the Windows ABI
    that its .winmd describes.

    On Windows the library follows the metadata, so use_abi() binds WINDOWS
    instead; on_windows=True is for the rare library that deviates on
    Windows too, and makes use_abi() bind this Abi there as well.

    library: the shared library behind the bindings, named as the metadata
    names it ("Foo.dll", which is libFoo.so or libFoo.dylib off Windows; see
    load_library), or a path or a loaded ctypes.CDLL.  co_create_instance()
    calls its exported CoCreateInstance when not given a library (off
    Windows; on Windows OLE32's is used), and its own exported
    SysAllocStringLen, SysFreeString, SysStringLen, CoTaskMemAlloc,
    CoTaskMemFree and SafeArray* functions allocate the BSTRs and memory, in
    each family that it exports whole (a LibraryAllocator).  It is loaded on
    first use, so making an Abi loads nothing.

    allocator: how BSTRs, task memory and SAFEARRAYs are allocated.  With
    library too, it is the fallback for what the library does not export;
    alone, it is used outright.  Without either, the default allocator (OLE
    on Windows, the Windows layout on malloc/free elsewhere).
    """

    def __init__(self, name, typedefs=None, overrides=None, allocator=None, library=None, on_windows=False):
        self.name = name
        self.typedefs = dict(typedefs or {})
        self.overrides = dict(overrides or {})
        if allocator is not None and not isinstance(allocator, Allocator):
            raise TypeError("allocator must be a portacom.Allocator")
        self.library = library
        self.on_windows = on_windows
        self._fallback = allocator
        self._allocator = allocator if library is None else None
        self._default_allocator = allocator is None and library is None
        # what this ABI's types allocate with, resolved on first use
        self._lazy = LazyAllocator(lambda: self.allocator)
        self._bstr = None
        self._safearray = None
        self._variant = None

    @property
    def allocator(self):
        if self._allocator is None:
            if self.library is not None:
                self._allocator = LibraryAllocator(self.library, fallback=self._fallback)
            else:
                self._allocator = default_allocator()
        return self._allocator

    @property
    def BSTR(self):
        """The BSTR type of this ABI, which allocates with its allocator."""
        if self._bstr is None:
            self._bstr = type("BSTR", (BSTRBase,), {"_allocator_": self._lazy, "__qualname__": f"BSTR[{self.name}]"})
        return self._bstr

    @property
    def SAFEARRAY(self):
        """The SAFEARRAY type of this ABI, which allocates with its allocator."""
        if self._safearray is None:
            from ._safearray import SAFEARRAY

            self._safearray = type(f"SAFEARRAY[{self.name}]", (SAFEARRAY,), {"_allocator_": self._lazy})
        return self._safearray

    @property
    def VARIANT(self):
        """The VARIANT type of this ABI, whose BSTRs and arrays use its allocator."""
        if self._variant is None:
            from ._variant import VARIANT, variant_type

            if self._default_allocator:
                self._variant = VARIANT  # the default allocator, as portacom.VARIANT has
            else:
                self._variant = variant_type(self._lazy, self.name)
        return self._variant

    def typedef(self, name, default):
        """The type for a use of the named typedef."""
        if name in self.typedefs:
            return self.typedefs[name]
        special = _SPECIAL.get(name)
        if special is not None:
            return special(self)
        return default

    def override(self, key, default):
        return self.overrides.get(key, default)

    def __repr__(self):
        return f"<Abi {self.name!r}>"


_SPECIAL = {
    "BSTR": lambda abi: abi.BSTR,
    "SAFEARRAY": lambda abi: abi.SAFEARRAY,
    "PWSTR": lambda abi: PWSTR,
    "PCWSTR": lambda abi: PWSTR,
    "PSTR": lambda abi: PSTR,
    "PCSTR": lambda abi: PSTR,
    "HRESULT": lambda abi: HRESULT,
    "VARIANT_BOOL": lambda abi: VARIANT_BOOL,
}

# The Windows ABI, as the metadata describes it
WINDOWS = Abi("windows")

_registered = {}  # key (module path or namespace) -> Abi
_bound = {}  # (module path, namespace) -> Abi, for modules already imported


def _matches(key, name):
    return name == key or name.startswith(key + ".")


def _longest(registry, name):
    keys = [key for key in registry if _matches(key, name)]
    return registry[max(keys, key=len)] if keys else None


def _resolve(registry, namespace, module):
    """A key matching the module path wins; then a key matching the namespace."""
    found = _longest(registry, module) if module else None
    if found is None:
        found = _longest(registry, namespace)
    return found or WINDOWS


def use_abi(key, abi):
    """Use abi for the generated bindings that key names.  Call it before
    importing them (in the bindings' __init__.py): a module's Abi is fixed
    when it is imported.

    An Abi describes the library as built for Linux and macOS.  On Windows
    the metadata is the truth, so there the bindings get WINDOWS, unless abi
    was made with on_windows=True.

    key is a generated package's import path ("my_bindings", or
    "my_bindings.Windows.Win32"), or a metadata namespace ("Windows.Win32").
    For a module, the longest key that matches its import path wins; only if
    none does, the longest key that matches its namespace.  So a package that
    ships bindings (portacom_dxc registers "portacom_dxc._bindings") is not
    affected by a namespace registered for other bindings of that namespace.
    """
    if not isinstance(abi, Abi):
        raise TypeError(f"use_abi() takes a portacom.Abi, not {type(abi).__name__}")
    if NATIVE_COM and not abi.on_windows:
        abi = WINDOWS
    trial = {**_registered, key: abi}
    for (module, namespace), used in _bound.items():
        if _resolve(trial, namespace, module) is not used:
            raise RuntimeError(
                f"{module} was already imported with {used!r}; call portacom.use_abi({key!r}, ...) "
                f"before importing {module}"
            )
    _registered[key] = abi


def abi_for(namespace, module=None):
    """The Abi for a generated module, called by the module when imported."""
    abi = _resolve(_registered, namespace, module)
    _bound[(module or namespace, namespace)] = abi
    return abi
