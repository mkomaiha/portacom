"""BSTR, task memory (CoTaskMem) and SAFEARRAY allocation, which are part of a library's ABI.

On Windows these are OLEAUT32/OLE32.  Elsewhere every COM-style library
brings its own, and they do not agree:

- WindowsLayoutAllocator (the default off Windows): a BSTR points just past a
  4-byte length in bytes, and is freed with free(p - 4).  Task memory is
  malloc/free.  This is what DirectX Shader Compiler's WinAdapter does, and
  what hassle-rs's src/os.rs implements.
- PlainAllocator: a BSTR is a plain wcsdup()'d wchar_t string, freed with
  free(), whose length is wcslen().  This is MiniCOM, which exports no
  SysAllocString functions.
- LibraryAllocator: the library's own exported SysAllocStringLen,
  SysFreeString, SysStringLen, CoTaskMemAlloc, CoTaskMemFree, SafeArrayCreate,
  SafeArrayDestroy and SafeArrayGetVartype, each family only when the library
  exports all of it, and the default allocator's otherwise.

SAFEARRAYs use OLEAUT32 on Windows and the Windows layout on the allocator's
memory elsewhere, except with PlainAllocator (see there).

All lengths here are in wchar_t units (UTF-16 code units on Windows).
"""

import ctypes
import functools

from ._platform import NATIVE_COM, WCHAR_SIZE, wchar_count


@functools.cache
def _libc():
    libc = ctypes.CDLL("ucrtbase" if NATIVE_COM else None)
    libc.wcslen.restype = ctypes.c_size_t
    libc.wcslen.argtypes = [ctypes.c_void_p]
    libc.malloc.restype = ctypes.c_void_p
    libc.malloc.argtypes = [ctypes.c_size_t]
    libc.free.restype = None
    libc.free.argtypes = [ctypes.c_void_p]
    return libc


def _copy_text(address, text, count):
    """Write count wchar_t units of text and a terminating NUL at address."""
    buffer = ctypes.create_unicode_buffer(text, count + 1)
    ctypes.memmove(address, buffer, (count + 1) * WCHAR_SIZE)


class Allocator:
    """Base class: subclasses provide the five primitive operations."""

    name = "abstract"

    def alloc_bstr(self, text):  # -> address, 0 for None
        raise NotImplementedError

    def free_bstr(self, address):
        raise NotImplementedError

    def bstr_len(self, address):  # in wchar_t units
        raise NotImplementedError

    def alloc(self, size):
        raise NotImplementedError

    def free(self, address):
        raise NotImplementedError

    def safearray_create(self, vt, shape):  # -> descriptor address
        return self._safearrays().create(vt, shape)

    def safearray_destroy(self, address):
        self._safearrays().destroy(address)

    def safearray_vartype(self, address):
        return self._safearrays().vartype(address)

    def _safearrays(self):
        from ._safearray import WindowsLayout

        return WindowsLayout(self)

    # -- derived operations ----------------------------------------------------------

    def read_bstr(self, address):
        if not address:
            return None
        return ctypes.wstring_at(address, self.bstr_len(address))

    def alloc_wstr(self, text):
        """A NUL-terminated wide string in task memory (CoTaskMemAlloc)."""
        if text is None:
            return 0
        count = wchar_count(text)
        address = self.alloc((count + 1) * WCHAR_SIZE)
        if not address:
            raise MemoryError
        _copy_text(address, text, count)
        return address

    def __repr__(self):
        return f"<{type(self).__name__}>"


class OleAllocator(Allocator):
    """Windows: SysAllocStringLen & co. from OLEAUT32, CoTaskMemAlloc from OLE32."""

    name = "ole"

    def __init__(self):
        oleaut32 = ctypes.WinDLL("oleaut32")
        ole32 = ctypes.WinDLL("ole32")
        self._alloc_bstr = oleaut32.SysAllocStringLen
        self._alloc_bstr.restype = ctypes.c_void_p
        self._alloc_bstr.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self._free_bstr = oleaut32.SysFreeString
        self._free_bstr.restype = None
        self._free_bstr.argtypes = [ctypes.c_void_p]
        self._bstr_len = oleaut32.SysStringLen
        self._bstr_len.restype = ctypes.c_uint
        self._bstr_len.argtypes = [ctypes.c_void_p]
        self._alloc = ole32.CoTaskMemAlloc
        self._alloc.restype = ctypes.c_void_p
        self._alloc.argtypes = [ctypes.c_size_t]
        self._free = ole32.CoTaskMemFree
        self._free.restype = None
        self._free.argtypes = [ctypes.c_void_p]
        self._oleaut32 = oleaut32

    def alloc_bstr(self, text):
        if text is None:
            return 0
        # the length is in UTF-16 code units, not len(text)
        count = wchar_count(text)
        address = self._alloc_bstr(ctypes.create_unicode_buffer(text, count + 1), count)
        if not address:
            raise MemoryError
        return address

    def free_bstr(self, address):
        if address:
            self._free_bstr(address)

    def bstr_len(self, address):
        return self._bstr_len(address) if address else 0

    def alloc(self, size):
        return self._alloc(size)

    def free(self, address):
        if address:
            self._free(address)

    def safearray_create(self, vt, shape):
        from ._safearray import SAFEARRAYBOUND

        bounds = (SAFEARRAYBOUND * len(shape))(*[SAFEARRAYBOUND(n, 0) for n in shape])
        create = self._oleaut32.SafeArrayCreate
        create.restype = ctypes.c_void_p
        address = create(vt, len(shape), bounds)
        if not address:
            raise MemoryError(f"SafeArrayCreate failed for VARTYPE {vt}")
        return address

    def safearray_destroy(self, address):
        if address:
            self._oleaut32.SafeArrayDestroy(ctypes.c_void_p(address))

    def safearray_vartype(self, address):
        vt = ctypes.c_ushort()
        self._oleaut32.SafeArrayGetVartype(ctypes.c_void_p(address), ctypes.byref(vt))
        return vt.value


class WindowsLayoutAllocator(Allocator):
    """BSTRs laid out as on Windows (4-byte byte length, text, NUL), on malloc/free."""

    name = "windows-layout"

    def alloc_bstr(self, text):
        if text is None:
            return 0
        count = wchar_count(text)
        block = _libc().malloc(4 + (count + 1) * WCHAR_SIZE)
        if not block:
            raise MemoryError
        ctypes.c_uint32.from_address(block).value = count * WCHAR_SIZE
        _copy_text(block + 4, text, count)
        return block + 4

    def free_bstr(self, address):
        if address:
            _libc().free(address - 4)

    def bstr_len(self, address):
        return ctypes.c_uint32.from_address(address - 4).value // WCHAR_SIZE if address else 0

    def alloc(self, size):
        return _libc().malloc(size)

    def free(self, address):
        if address:
            _libc().free(address)


class PlainAllocator(Allocator):
    """BSTRs as plain wcsdup()'d strings, freed with free(); length is wcslen()."""

    name = "plain"

    def alloc_bstr(self, text):
        if text is None:
            return 0
        return self.alloc_wstr(text)

    def free_bstr(self, address):
        if address:
            _libc().free(address)

    def bstr_len(self, address):
        return _libc().wcslen(address) if address else 0

    def alloc(self, size):
        return _libc().malloc(size)

    def free(self, address):
        if address:
            _libc().free(address)

    def _safearrays(self):
        raise NotImplementedError(
            "PlainAllocator has no SAFEARRAY (MiniCOM's is a C++ class without a C API): the library "
            "needs a Windows-layout SAFEARRAY, with SafeArrayCreate/Destroy/GetVartype exported "
            "(Abi(library=...)), or an allocator that provides them (WindowsLayoutAllocator)"
        )


class LibraryAllocator(Allocator):
    """The library's own exports, where it has them, and fallback's otherwise.

    library is a ctypes.CDLL, a path, or a DLL name for portacom.load_library.
    It is loaded on first use.  Each family of functions (the BSTR trio, the
    task memory pair, the SafeArray trio) comes from the library only when it
    exports the whole family: half of one and half of another layout would
    free memory with the wrong function.
    """

    name = "library"

    _FAMILIES = {
        "bstr": (
            ("SysAllocStringLen", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_uint]),
            ("SysFreeString", None, [ctypes.c_void_p]),
            ("SysStringLen", ctypes.c_uint, [ctypes.c_void_p]),
        ),
        "task": (
            ("CoTaskMemAlloc", ctypes.c_void_p, [ctypes.c_size_t]),
            ("CoTaskMemFree", None, [ctypes.c_void_p]),
        ),
        "safearray": (
            ("SafeArrayCreate", ctypes.c_void_p, [ctypes.c_uint16, ctypes.c_uint, ctypes.c_void_p]),
            ("SafeArrayDestroy", ctypes.c_int32, [ctypes.c_void_p]),
            ("SafeArrayGetVartype", ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p]),
        ),
    }

    def __init__(self, library, fallback=None):
        self.library = library
        self._fallback = fallback
        self._families = None  # family -> {name: function}, or None where it falls back

    def _load(self):
        from ._loader import export, load_library

        library = self.library if isinstance(self.library, ctypes.CDLL) else load_library(self.library)
        families = {}
        for family, exports in self._FAMILIES.items():
            functions = {}
            for name, restype, argtypes in exports:
                if getattr(library, name, None) is None:
                    functions = None
                    break
                functions[name] = export(library, name, restype, *argtypes)
            families[family] = functions
        self._families = families

    def exports(self, family):
        """{name: function} for a family ("bstr", "task", "safearray"), or None
        if the library does not export all of it."""
        if self._families is None:
            self._load()
        return self._families[family]

    @property
    def fallback(self):
        if self._fallback is None:
            self._fallback = default_allocator()
        return self._fallback

    def alloc_bstr(self, text):
        if text is None:
            return 0
        bstr = self.exports("bstr")
        if bstr is None:
            return self.fallback.alloc_bstr(text)
        count = wchar_count(text)
        address = bstr["SysAllocStringLen"](ctypes.create_unicode_buffer(text, count + 1), count)
        if not address:
            raise MemoryError
        return address

    def free_bstr(self, address):
        if address:
            bstr = self.exports("bstr")
            (bstr["SysFreeString"] if bstr else self.fallback.free_bstr)(address)

    def bstr_len(self, address):
        if not address:
            return 0
        bstr = self.exports("bstr")
        return (bstr["SysStringLen"] if bstr else self.fallback.bstr_len)(address)

    def alloc(self, size):
        task = self.exports("task")
        return (task["CoTaskMemAlloc"] if task else self.fallback.alloc)(size)

    def free(self, address):
        if address:
            task = self.exports("task")
            (task["CoTaskMemFree"] if task else self.fallback.free)(address)

    def safearray_create(self, vt, shape):
        arrays = self.exports("safearray")
        if arrays is None:
            return self.fallback.safearray_create(vt, shape)
        from ._safearray import SAFEARRAYBOUND

        bounds = (SAFEARRAYBOUND * len(shape))(*[SAFEARRAYBOUND(n, 0) for n in shape])
        address = arrays["SafeArrayCreate"](vt, len(shape), bounds)
        if not address:
            raise MemoryError(f"SafeArrayCreate failed for VARTYPE {vt}")
        return address

    def safearray_destroy(self, address):
        if address:
            arrays = self.exports("safearray")
            (arrays["SafeArrayDestroy"] if arrays else self.fallback.safearray_destroy)(address)

    def safearray_vartype(self, address):
        arrays = self.exports("safearray")
        if arrays is None:
            return self.fallback.safearray_vartype(address)
        vt = ctypes.c_ushort()
        arrays["SafeArrayGetVartype"](address, ctypes.byref(vt))
        return vt.value


class LazyAllocator:
    """Stands for the allocator that factory() makes, on first use, so that
    an Abi's types can be made before its library is loaded."""

    def __init__(self, factory):
        self._factory = factory

    def resolve(self):
        return self._factory()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._factory(), name)

    def __repr__(self):
        return f"<LazyAllocator {self._factory!r}>"


def resolve_allocator(allocator):
    """The real allocator behind a LazyAllocator; None is the default one."""
    if allocator is None:
        return default_allocator()
    if isinstance(allocator, LazyAllocator):
        return allocator.resolve()
    return allocator


def free_task_memory(pointer, abi=None) -> None:
    """CoTaskMemFree memory a library returned: a PWSTR, a PSTR, a ctypes
    pointer or an address.  abi: the bindings' Abi (module._abi) or the
    generated module itself, whose allocator the library uses; without it,
    the default allocator."""
    if pointer is None:
        return
    address = getattr(pointer, "address", None)
    if address is None:
        if isinstance(pointer, int):
            address = pointer
        elif isinstance(pointer, (ctypes._Pointer, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_wchar_p)):
            address = ctypes.cast(pointer, ctypes.c_void_p).value
        else:
            address = getattr(pointer, "value", None)
    if address:
        abi = getattr(abi, "_abi", abi)  # a generated module
        (abi.allocator if abi is not None else default_allocator()).free(address)


@functools.cache
def default_allocator():
    """OLE on Windows, the Windows BSTR layout on malloc/free elsewhere."""
    return OleAllocator() if NATIVE_COM else WindowsLayoutAllocator()
