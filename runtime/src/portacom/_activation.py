"""Creating COM objects: factories, CoCreateInstance, and coclasses."""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from typing import TypeVar

from ._errors import E_NOINTERFACE, S_OK, COMError, check
from ._interface import Function, IUnknown
from ._loader import export, load_library
from ._marshal import interface_pointer
from ._platform import NATIVE_COM
from ._types import GUID

T = TypeVar("T", bound=IUnknown)

CLSCTX_INPROC_SERVER = 0x1
CLSCTX_ALL = 0x17


class CoClass:
    """A coclass from the metadata, which names its CLSID."""

    def __init__(self, name: str, clsid: str):
        self.__name__ = name
        self._clsid_ = GUID(clsid)

    @property
    def clsid(self) -> GUID:
        return self._clsid_

    def __repr__(self):
        return f"<coclass {self.__name__} {self._clsid_}>"


ClassId = GUID | str | CoClass


def _clsid(value: ClassId) -> GUID:
    if isinstance(value, CoClass):
        return value._clsid_
    return value if isinstance(value, GUID) else GUID(value)


def create_instance(clsid: ClassId, interface: type[T] = IUnknown, factory=None) -> T:
    """Create an object with a factory, as interface.

    factory is one of:
    - a generated Function shaped HRESULT f(REFCLSID, REFIID, void**), such as
      DxcCreateInstance
    - (library, "ExportName"), for an export of that shape
    - a Python callable(clsid: GUID, iid: GUID) returning an interface pointer
      (an int that owns a reference) or an IUnknown
    """
    if factory is None:
        raise TypeError("create_instance() needs a factory; for registered classes, use co_create_instance()")
    clsid = _clsid(clsid)
    iid = interface._iid_
    if isinstance(factory, tuple):
        factory = exported_factory(*factory)
    if isinstance(factory, Function):
        out = ctypes.c_void_p()
        check(factory.raw(ctypes.byref(clsid), ctypes.byref(iid), ctypes.byref(out)), factory.__name__)
        return interface(out.value)
    result = factory(clsid, iid)
    if isinstance(result, IUnknown):
        return result if isinstance(result, interface) else result.QueryInterface(interface)
    if not result:
        raise COMError(E_NOINTERFACE, "factory")
    return interface(result)


def exported_factory(library, name: str) -> Callable[[GUID, GUID], int]:
    """A create_instance() factory for an export shaped HRESULT f(REFCLSID, REFIID, void**)."""
    function = export(
        _library(library), name, ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    )

    def factory(clsid, iid):
        out = ctypes.c_void_p()
        check(function(ctypes.byref(clsid), ctypes.byref(iid), ctypes.byref(out)), name)
        return out.value

    return factory


_com_thread = threading.local()  # COM is initialized per thread


def co_initialize(multithreaded: bool = True) -> int:
    """CoInitializeEx for the calling thread on Windows, where
    co_create_instance() calls it for you.  Does nothing elsewhere."""
    if not NATIVE_COM:
        return S_OK
    _com_thread.initialized = True
    return ctypes.windll.ole32.CoInitializeEx(None, 0 if multithreaded else 2)


def _library(library):
    return library if isinstance(library, ctypes.CDLL) else load_library(library)


def _ole32():
    if not getattr(_com_thread, "initialized", False):
        co_initialize()
    return ctypes.windll.ole32


def co_create_instance(
    clsid: ClassId,
    interface: type[T] = IUnknown,
    library=None,
    context: int = CLSCTX_ALL,
    outer: IUnknown | None = None,
) -> T:
    """CoCreateInstance.

    On Windows, OLE32's, after CoInitializeEx, for the registered class.
    Elsewhere, the extern "C" CoCreateInstance that the library exports
    (MiniCOM does): library, else the one the interface's Abi names
    (Abi(library="Foo.dll")).  library is a DLL name as the metadata gives it,
    a path or a ctypes.CDLL; passing one on Windows calls its export instead
    of OLE32's.
    """
    if library is None:
        if NATIVE_COM:
            library = _ole32()
        else:
            library = getattr(getattr(interface, "_abi_", None), "library", None)
            if library is None:
                raise TypeError(
                    "off Windows, pass the library that exports CoCreateInstance, or name it in the "
                    "bindings' ABI: portacom.use_abi(package, portacom.Abi(..., library='Foo.dll'))"
                )
    function = export(
        _library(library),
        "CoCreateInstance",
        ctypes.c_int32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    out = ctypes.c_void_p()
    check(
        function(
            ctypes.byref(_clsid(clsid)),
            interface_pointer(outer, IUnknown, []),
            context,
            ctypes.byref(interface._iid_),
            ctypes.byref(out),
        ),
        "CoCreateInstance",
    )
    return interface(out.value)


def clsid_from_progid(progid: str, library=None) -> GUID:
    """The CLSID of a ProgID ("Scripting.Dictionary").

    On Windows, OLE32's CLSIDFromProgID, which reads the registry.
    Elsewhere, the CLSIDFromProgID that library exports (MiniCOM's matches
    the C++ class name: "ExampleLib.RegistryImpl" or "RegistryImpl").
    library is a DLL name as the metadata gives it, a path or a ctypes.CDLL,
    as for co_create_instance(); passing one on Windows calls its export
    instead of OLE32's.
    """
    if library is None:
        if not NATIVE_COM:
            raise TypeError("off Windows, pass the library that exports CLSIDFromProgID")
        library = _ole32()
    function = export(_library(library), "CLSIDFromProgID", ctypes.c_int32, ctypes.c_wchar_p, ctypes.POINTER(GUID))
    clsid = GUID()
    check(function(progid, ctypes.byref(clsid)), f"CLSIDFromProgID({progid!r})")
    return clsid
