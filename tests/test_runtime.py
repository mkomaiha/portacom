import ctypes
import sys

import portacom
import pytest
from portacom import GUID, PlainAllocator, WindowsLayoutAllocator, default_allocator, wchar_count

TEXT = "abc é\U0001f642"  # the last character is outside the BMP: two UTF-16 code units


def test_platform_flag():
    assert (sys.platform == "win32") == portacom.NATIVE_COM
    assert portacom.FUNCTYPE is (ctypes.WINFUNCTYPE if sys.platform == "win32" else ctypes.CFUNCTYPE)


def test_wchar_count_is_in_wchar_units():
    assert wchar_count("abc") == 3
    if ctypes.sizeof(ctypes.c_wchar) == 2:
        assert wchar_count(TEXT) == len(TEXT) + 1
    else:
        assert wchar_count(TEXT) == len(TEXT)


@pytest.mark.parametrize(
    "allocator", [default_allocator(), WindowsLayoutAllocator(), PlainAllocator()], ids=lambda a: a.name
)
def test_bstr_round_trip_keeps_non_bmp_text(allocator):
    # py-win32more passes len(text) as the length and truncates this to 'abc \xe9\ud83d' on Windows
    p = allocator.alloc_bstr(TEXT)
    try:
        assert allocator.read_bstr(p) == TEXT
        assert allocator.bstr_len(p) == wchar_count(TEXT)
    finally:
        allocator.free_bstr(p)
    assert allocator.alloc_bstr(None) == 0
    assert allocator.read_bstr(0) is None


def test_windows_layout_has_byte_length_prefix():
    a = WindowsLayoutAllocator()
    p = a.alloc_bstr("hello")
    try:
        assert ctypes.c_uint32.from_address(p - 4).value == 5 * ctypes.sizeof(ctypes.c_wchar)
        assert ctypes.wstring_at(p) == "hello"
    finally:
        a.free_bstr(p)


def test_abi_bstr_type():
    bstr = portacom.WINDOWS.BSTR.alloc(TEXT)
    assert str(bstr) == TEXT and len(bstr) == wchar_count(TEXT)
    bstr.free()
    assert not bstr.value


def test_task_memory_string():
    a = default_allocator()
    p = a.alloc_wstr(TEXT)
    assert ctypes.wstring_at(p) == TEXT
    a.free(p)


def test_guid():
    g = GUID("{8BA5FB08-5195-40E2-AC58-0D989C3A0102}")
    assert str(g) == "{8BA5FB08-5195-40E2-AC58-0D989C3A0102}"
    assert g == GUID("8ba5fb08-5195-40e2-ac58-0d989c3a0102")
    assert g.Data1 == 0x8BA5FB08 and g.Data4[0] == 0xAC
    assert ctypes.sizeof(GUID) == 16


def test_com_error():
    e = portacom.COMError(0x80004005, "Thing")
    assert e.hresult == -0x7FFFBFFB and e.hr == 0x80004005
    assert "E_FAIL" in str(e) and "Thing" in str(e)


def test_interface_wrapper():
    impl = portacom.EnumVARIANT([1, 2])
    enum = impl.QueryInterface(portacom.IEnumVARIANT)
    # query() takes what QueryInterface() takes: a class, a GUID or a string
    assert enum.query(portacom.IDispatch) is None
    unknown = enum.query(str(portacom.IUnknown._iid_))
    assert type(unknown) is portacom.IUnknown and unknown == enum.QueryInterface(portacom.IUnknown._iid_)
    other = portacom.IEnumVARIANT(enum)  # a second reference to the same pointer
    assert other == enum and hash(other) == hash(enum)
    key = hash(enum)
    enum.release()
    assert hash(enum) == key and enum != other and enum in {enum}  # the hash survives release()
    with pytest.raises(TypeError, match="QueryInterface"):
        portacom.IUnknown(impl)


@pytest.mark.skipif(sys.platform == "win32", reason="library names are mapped off Windows only")
def test_library_candidates():
    names = portacom.library_candidates("dxcompiler.dll")
    expected = "libdxcompiler.dylib" if sys.platform == "darwin" else "libdxcompiler.so"
    assert names[0] == expected
    assert "dxcompiler.so" in names and "libdxcompiler.dylib" in names


def test_register_library_override():
    lib = ctypes.CDLL(None) if sys.platform != "win32" else ctypes.WinDLL("kernel32")
    portacom.register_library("nonexistent-test.dll", lib)
    assert portacom.load_library("NONEXISTENT-TEST.DLL") is lib


def test_unknown_library_error_lists_candidates():
    with pytest.raises(OSError, match="register_library"):
        portacom.load_library("surely-not-a-library-xyz.dll")


def test_abi_for_default_is_windows(abi_registry):
    assert portacom.abi_for("Test.Default.Namespace", "default_pkg.Test.Default.Namespace") is portacom.WINDOWS
    assert portacom.abi_for("Test.Default.Other") is portacom.WINDOWS


def test_use_abi_module_path_before_namespace(abi_registry):
    mine, deeper, shipped, inner = (
        portacom.Abi(name, on_windows=True) for name in ("mine", "deeper", "shipped", "inner")
    )
    portacom.use_abi("Test.Keys", mine)
    portacom.use_abi("Test.Keys.Inner", deeper)
    portacom.use_abi("shipped_pkg._bindings", shipped)
    portacom.use_abi("shipped_pkg._bindings.Test.Keys.Inner", inner)
    # a module-path key wins over any namespace key, however long
    assert portacom.abi_for("Test.Keys.Other", "shipped_pkg._bindings.Test.Keys.Other") is shipped
    # among module-path keys, and among namespace keys, the longest wins
    assert portacom.abi_for("Test.Keys.Inner.X", "shipped_pkg._bindings.Test.Keys.Inner.X") is inner
    assert portacom.abi_for("Test.Keys.Inner.X", "user_pkg.Test.Keys.Inner.X") is deeper
    assert portacom.abi_for("Test.Keys.Other", "user_pkg.Test.Keys.Other") is mine
    assert portacom.abi_for("Test.KeysNot", "user_pkg.Test.KeysNot") is portacom.WINDOWS  # whole components only
    with pytest.raises(TypeError):
        portacom.use_abi("Test.Keys", "windows")


def test_use_abi_after_import_is_an_error(abi_registry):
    portacom.abi_for("Test.Bound.Namespace", "bound_pkg.Test.Bound.Namespace")
    other = portacom.Abi("other", on_windows=True)
    with pytest.raises(RuntimeError, match=r"bound_pkg\.Test\.Bound\.Namespace was already imported.*'Test\.Bound'"):
        portacom.use_abi("Test.Bound", other)
    with pytest.raises(RuntimeError, match="before importing bound_pkg"):
        portacom.use_abi("bound_pkg", other)
    portacom.use_abi("Test.Bound", portacom.WINDOWS)  # changes nothing for it

    # bindings that a package registered by module path are not affected by a
    # namespace registered for other copies of that namespace
    shipped = portacom.Abi("shipped", on_windows=True)
    portacom.use_abi("shipped_pkg", shipped)
    assert portacom.abi_for("Test.Shared", "shipped_pkg.Test.Shared") is shipped
    portacom.use_abi("Test.Shared", portacom.Abi("mine", on_windows=True))
    portacom.use_abi("shipped_pkg", shipped)
    with pytest.raises(RuntimeError, match="shipped_pkg.Test.Shared"):
        portacom.use_abi("shipped_pkg.Test", other)


def test_use_abi_follows_the_metadata_on_windows(abi_registry):
    """An Abi describes the library built off Windows; on Windows the bindings get WINDOWS."""
    elsewhere = portacom.Abi("elsewhere", typedefs={"BOOL": ctypes.c_long})
    portacom.use_abi("Test.Platform", elsewhere)
    expected = portacom.WINDOWS if portacom.NATIVE_COM else elsewhere
    assert portacom.abi_for("Test.Platform.X", "pkg.Test.Platform.X") is expected
    everywhere = portacom.Abi("everywhere", on_windows=True)
    portacom.use_abi("Test.Everywhere", everywhere)
    assert portacom.abi_for("Test.Everywhere.X", "pkg.Test.Everywhere.X") is everywhere


def test_abi_library_loads_lazily():
    abi = portacom.Abi("missing", library="surely-not-a-library-xyz.dll")
    assert abi.BSTR and abi.SAFEARRAY and abi.VARIANT  # nothing is loaded yet
    assert isinstance(abi.allocator, portacom.LibraryAllocator)
    with pytest.raises(OSError):
        abi.BSTR.alloc("x")


def test_abi_library_without_exports_falls_back():
    import ctypes.util

    name = "ucrtbase.dll" if sys.platform == "win32" else ctypes.util.find_library("c")
    abi = portacom.Abi("libc", library=name)
    allocator = abi.allocator
    assert [allocator.exports(family) for family in ("bstr", "task", "safearray")] == [None, None, None]
    assert allocator.fallback is default_allocator()
    b = abi.BSTR.alloc(TEXT)
    assert str(b) == TEXT
    b.free()
    # an allocator given with a library is the fallback for what the library does not export
    allocator = portacom.Abi("explicit", allocator=PlainAllocator(), library=name).allocator
    assert isinstance(allocator, portacom.LibraryAllocator) and allocator.fallback.name == "plain"
    b = portacom.Abi("explicit", allocator=PlainAllocator(), library=name).BSTR.alloc("plain")
    assert ctypes.wstring_at(b.value) == "plain"
    b.free()
    assert portacom.Abi("alone", allocator=PlainAllocator()).allocator.name == "plain"


@pytest.mark.skipif(sys.platform != "win32", reason="OLEAUT32 is Windows-only")
def test_library_allocator_falls_back_per_family():
    # OLEAUT32 has the BSTR and SafeArray functions but not CoTaskMemAlloc
    allocator = portacom.LibraryAllocator(ctypes.WinDLL("oleaut32"), fallback=PlainAllocator())
    assert allocator.exports("bstr") and allocator.exports("safearray")
    assert allocator.exports("task") is None
    address = allocator.alloc_bstr(TEXT)
    assert allocator.bstr_len(address) == wchar_count(TEXT)
    allocator.free_bstr(address)


def test_variant_uses_its_abi_allocator():
    abi = portacom.Abi("plain", allocator=PlainAllocator())
    assert abi.VARIANT is abi.VARIANT and issubclass(abi.VARIANT, portacom.VARIANT)
    v = abi.VARIANT("héllo")
    assert ctypes.wstring_at(v.u.bstrVal) == "héllo"  # a plain wcsdup'd string, no length prefix
    assert v.value == "héllo"
    v.clear()
    assert portacom.VARIANT._allocator() is default_allocator()


def test_variants_and_strings_through_vtables_use_the_abi_allocator():
    from portacom import HRESULT, IN, OUT, ComObject, Param, Ptr

    class Counting(WindowsLayoutAllocator):
        def __init__(self):
            self.calls = []

        def alloc_bstr(self, text):
            self.calls.append("alloc_bstr")
            return super().alloc_bstr(text)

        def free_bstr(self, address):
            self.calls.append("free_bstr")
            super().free_bstr(address)

        def alloc(self, size):
            self.calls.append("alloc")
            return super().alloc(size)

    allocator = Counting()
    abi = portacom.Abi("counting", allocator=allocator)

    class IEcho(portacom.IUnknown):
        _iid_ = GUID("0B8E5D21-7C4A-4E39-9F12-3A6B5C7D8E90")
        _abi_ = abi
        _methods_ = ("Echo", "Name")

    IEcho._signatures_ = lambda: (
        (HRESULT, (Param("value", abi.VARIANT, IN), Param("result", Ptr(abi.VARIANT), OUT))),
        (HRESULT, (Param("name", Ptr(portacom.PWSTR), OUT),)),
    )

    class Echo(ComObject):
        _interfaces_ = (IEcho,)

        def Echo(self, value):
            return value + "!"

        def Name(self):
            return "echo"

    echo = Echo().QueryInterface(IEcho)
    assert echo.Echo("hi") == "hi!"
    # the [in] VARIANT's BSTR and the [out] one, each allocated and freed with the Abi's allocator
    assert allocator.calls == ["alloc_bstr", "alloc_bstr", "free_bstr", "free_bstr"]
    name = echo.Name()
    assert name.value == "echo" and allocator.calls[-1] == "alloc"  # the callee's PWSTR is the Abi's task memory
    portacom.free_task_memory(name, abi)


def test_free_task_memory_takes_pointers():
    allocator = default_allocator()
    portacom.free_task_memory(ctypes.cast(allocator.alloc(16), ctypes.POINTER(ctypes.c_int)))
    portacom.free_task_memory(allocator.alloc(16), portacom.WINDOWS)
    portacom.free_task_memory(None)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses OLE32's")
def test_activation_needs_a_library_off_windows():
    from portacom import IUnknown

    class IThing(IUnknown):
        _iid_ = GUID("6C1F0A52-2E3B-4D7C-9A8E-1B2C3D4E5F60")
        _abi_ = portacom.Abi("no-library")

    with pytest.raises(TypeError, match="library="):
        portacom.co_create_instance(GUID(), IThing)
    with pytest.raises(TypeError, match="CLSIDFromProgID"):
        portacom.clsid_from_progid("Some.Thing")
    # a library named by its Abi is the one called; this one exports no CoCreateInstance
    IThing._abi_ = portacom.Abi("missing", library="surely-not-a-library-xyz.dll")
    with pytest.raises(OSError, match="surely-not-a-library-xyz"):
        portacom.co_create_instance(GUID(), IThing)


def test_error_names():
    assert portacom.COMError(0x8000000B).hresult == portacom.E_BOUNDS
    for hr, name in [
        (portacom.E_ABORT, "E_ABORT"),
        (portacom.E_ACCESSDENIED, "E_ACCESSDENIED"),
        (portacom.E_NOT_SET, "E_NOT_SET"),
        (portacom.E_UNEXPECTED, "E_UNEXPECTED"),
        (portacom.REGDB_E_CLASSNOTREG, "REGDB_E_CLASSNOTREG"),
        (0x800401F0, "CO_E_NOTINITIALIZED"),
        (0x80040110, "CLASS_E_NOAGGREGATION"),
        (0x8000000A, "E_PENDING"),
        (0x80070006, "E_HANDLE"),
    ]:
        assert name in portacom.hresult_text(hr)
    assert portacom.REGDB_E_CLASSNOTREG & 0xFFFFFFFF == 0x80040154


@pytest.mark.skipif(sys.platform == "win32", reason="library names are mapped off Windows only")
def test_library_file_names_are_tried_as_given():
    assert portacom.library_candidates("libExampleLib.so") == ["libExampleLib.so"]
    assert portacom.library_candidates("libfoo.so.1") == ["libfoo.so.1"]
    assert portacom.library_candidates("/opt/x/libfoo.dylib") == ["/opt/x/libfoo.dylib"]
