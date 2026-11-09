"""Ownership, NULL pointers, threads, struct returns and delegates, through vtables.

Both sides are Python, so this runs on every platform.  A "raw" class gives
the same vtable another signature, to play a C callee that does exactly what
the test needs.
"""

import ctypes
import subprocess
import sys
import textwrap
import threading

import portacom
import pytest
from portacom import GUID, HRESULT, IN, OPTIONAL, OUT, ComObject, Param, Ptr, WindowsLayoutAllocator, codegen

STRUCT_RETURNS = portacom.NATIVE_COM or (portacom.ARCH == "x64" and ctypes.sizeof(ctypes.c_void_p) == 8)


class Counting(WindowsLayoutAllocator):
    """Records BSTRs; frees each only once, so a double free shows instead of crashing."""

    def __init__(self):
        self.allocated, self.freed = [], []

    def alloc_bstr(self, text):
        address = super().alloc_bstr(text)
        self.allocated.append(address)
        return address

    def free_bstr(self, address):
        if address:
            self.freed.append(address)
            if self.freed.count(address) == 1:
                super().free_bstr(address)

    def read_bstr(self, address):
        text = super().read_bstr(address)
        if text == "unreadable":
            raise ValueError("unreadable")
        return text

    def balanced(self):
        return sorted(self.freed) == sorted(self.allocated)


def make_interfaces():
    allocator = Counting()
    abi = portacom.Abi("counting", allocator=allocator)
    BSTR = abi.BSTR
    methods = ("Append", "Swap", "Needs", "Fill", "Pair", "Fail")
    iid = GUID("2F4C1B7A-9E3D-4A51-8B6C-0D7E1F2A3B4C")

    class IStrings(portacom.IUnknown):
        _iid_ = iid
        _abi_ = abi
        _methods_ = methods

    IStrings._signatures_ = lambda: (
        (HRESULT, (Param("text", Ptr(BSTR), IN | OUT),)),
        (HRESULT, (Param("obj", Ptr(portacom.IUnknown), IN | OUT),)),
        (HRESULT, (Param("value", Ptr(ctypes.c_int32), IN),)),
        (
            HRESULT,
            (
                Param("pv", Ptr(None), OUT, bytes=1),
                Param("cb", ctypes.c_uint32, IN),
                Param("read", Ptr(ctypes.c_uint32), OUT | OPTIONAL),
            ),
        ),
        (HRESULT, (Param("first", Ptr(BSTR), OUT), Param("second", Ptr(BSTR), OUT), Param("obj", Ptr(IStrings), OUT))),
        (HRESULT, (Param("text", Ptr(BSTR), IN | OUT), Param("obj", Ptr(portacom.IUnknown), IN | OUT))),
    )

    class IRaw(portacom.IUnknown):  # the same vtable, as plain pointers
        _iid_ = iid
        _methods_ = methods

    IRaw._signatures_ = lambda: tuple((HRESULT, (Param("p", Ptr(None), IN),)) for _ in methods)
    return allocator, IStrings, IRaw


class Strings(ComObject):
    def __init__(self, interface, other=None):
        self._interfaces_ = (interface,)
        self.other = other
        self.seen = []

    def Append(self, text):
        return text + "!"

    def Swap(self, obj):
        self.seen.append(obj)
        return self.other

    def Needs(self, value):
        self.seen.append(value)

    def Fill(self, cb):
        return b"hello world"[:cb], min(cb, 11)

    def Pair(self):
        return "unreadable", "second", self.other

    def Fail(self, text, obj):
        raise portacom.COMError(portacom.E_INVALIDARG)


class Raw(ComObject):
    """A callee that leaves an [in, out] BSTR as it is, and releases an [in, out] interface."""

    def __init__(self, interface):
        self._interfaces_ = (interface,)

    def Append(self, p):
        pass

    def Swap(self, p):
        cell = ctypes.c_void_p.from_address(p)
        portacom._vtable.release(cell.value)
        cell.value = None


def test_in_out_bstr_is_freed_once():
    allocator, IStrings, IRaw = make_interfaces()
    unchanged = IStrings(Raw(IRaw).QueryInterface(IRaw))
    assert unchanged.Append("text") == "text"
    assert len(allocator.allocated) == 1 and allocator.balanced(), allocator.freed

    replaced = Strings(IStrings).QueryInterface(IStrings)
    assert replaced.Append("text") == "text!"  # the callee freed ours, we free its
    assert replaced.Append(IStrings._abi_.BSTR.alloc("own")) == "own!"  # a copy: the BSTR object stays the caller's
    assert len(allocator.allocated) == 6 and len(allocator.freed) == 5


def test_in_out_interface_gives_the_callee_a_reference():
    _, IStrings, IRaw = make_interfaces()
    thing = Strings(IStrings)
    unknown = thing.QueryInterface(portacom.IUnknown)
    releasing = IStrings(Raw(IRaw).QueryInterface(IRaw))
    assert releasing.Swap(unknown) is None  # the callee released the reference it was given
    assert thing.references == 2  # Python's, and unknown's, which is still good
    del unknown
    assert thing.references == 1

    other = Strings(IStrings)
    swapper = Strings(IStrings, other=other)
    result = swapper.QueryInterface(IStrings).Swap(thing)
    assert result.address == other._com_pointer_(portacom.IUnknown) and other.references == 2
    swapper.seen.clear()
    assert thing.references == 1  # the callee's reference was released when it replaced it
    del result
    assert other.references == 1


def test_failed_call_frees_in_out_values():
    allocator, IStrings, _ = make_interfaces()
    thing = Strings(IStrings)
    with pytest.raises(portacom.COMError):
        Strings(IStrings).QueryInterface(IStrings).Fail("text", thing)
    assert allocator.balanced() and thing.references == 1


def test_failed_conversion_frees_the_other_outputs():
    allocator, IStrings, _ = make_interfaces()
    other = Strings(IStrings)
    pairs = Strings(IStrings, other=other).QueryInterface(IStrings)
    with pytest.raises(ValueError, match="unreadable"):
        pairs.Pair()
    assert len(allocator.allocated) == 2 and allocator.balanced()
    assert other.references == 1


def test_missing_pointer_argument_is_an_error():
    _, IStrings, _ = make_interfaces()
    impl = Strings(IStrings)
    strings = impl.QueryInterface(IStrings)
    with pytest.raises(TypeError, match="'value'"):
        strings.Needs()
    strings.Needs(None)  # NULL, said explicitly
    strings.Needs(ctypes.c_int32(5))
    assert impl.seen == [None, 5]


def test_out_buffer_sized_in_bytes():
    _, IStrings, _ = make_interfaces()
    strings = Strings(IStrings).QueryInterface(IStrings)
    assert strings.Fill(5) == (b"hello", 5)
    data, read = strings.Fill(16)
    assert (data[:11], read) == (b"hello world", 11) and len(data) == 16


def test_null_and_released_interfaces_raise():
    # a regression here is a segfault, so it runs in a child process
    code = textwrap.dedent(
        """
        import portacom
        from portacom import HRESULT, OUT, ComObject, Param, Ptr, IUnknown

        class IThing(IUnknown):
            _iid_ = portacom.GUID("5A0E2C4D-1B3F-4E6A-9C8D-7F6E5D4C3B2A")
            _methods_ = ("Get",)

        IThing._signatures_ = lambda: ((HRESULT, (Param("value", Ptr(portacom.VARIANT), OUT),)),)

        class Thing(ComObject):
            _interfaces_ = (IThing,)

        released = Thing().QueryInterface(IThing)
        released.release()
        calls = [
            lambda: IUnknown().query(IThing),
            lambda: IUnknown().QueryInterface(IThing),
            lambda: IUnknown().AddRef(),
            lambda: IUnknown().Release(),
            lambda: released.query(IThing),
            lambda: released.Get(),
            lambda: released.raw.Get(None),
        ]
        for call in calls:
            try:
                call()
            except ValueError as e:
                assert "NULL" in str(e), e
            else:
                raise AssertionError("no error")
        print("ok", len(calls))
        """
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.split() == ["ok", "7"], done.stderr


def test_references_from_many_threads():
    _, IStrings, _ = make_interfaces()
    impl = Strings(IStrings)
    strings = impl.QueryInterface(IStrings)

    def work(interface):
        for _ in range(10_000):
            interface.AddRef()
            interface.Release()

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=work, args=(strings,)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert impl.references == 2
    del strings
    assert impl.references == 1 and impl not in portacom._server._held


def test_failed_layout_can_be_retried():
    class Late(portacom.Structure):
        pass

    attempts = []

    def fields():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("not yet")
        return [("x", ctypes.c_int32), ("y", ctypes.c_int32)]

    portacom.layout(Late, fields)
    with pytest.raises(RuntimeError):
        portacom.finish_layouts(Late)
    portacom.finish_layouts(Late)
    assert ctypes.sizeof(Late) == 8


@pytest.mark.skipif(sys.platform != "win32", reason="COM is initialized per thread on Windows")
def test_co_create_instance_on_another_thread():
    # in a fresh process: with only an STA on the main thread, another thread
    # has no implicit MTA to fall back on, and must initialize COM itself
    code = textwrap.dedent(
        """
        import threading
        import portacom

        portacom.co_initialize(multithreaded=False)
        results = []

        def create():
            try:
                results.append(portacom.co_create_instance("0002E005-0000-0000-C000-000000000046"))
            except Exception as e:
                results.append(e)

        t = threading.Thread(target=create)
        t.start()
        t.join()
        print(results[0])
        """
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.startswith("<IUnknown 0x"), done.stdout + done.stderr


def test_shared_ctypes_functions_are_not_changed():
    if sys.platform == "win32":
        library, name = ctypes.WinDLL("oleaut32"), "SysAllocStringLen"
        allocator = portacom.LibraryAllocator(library)
        assert allocator.exports("bstr")
        assert library.SysAllocStringLen.argtypes is None and library.SysFreeString.argtypes is None
        ole32 = ctypes.windll.ole32
        ole32.CoCreateInstance.argtypes = None
        portacom.co_create_instance("0002E005-0000-0000-C000-000000000046").release()
        assert ole32.CoCreateInstance.argtypes is None
    else:
        library, name = ctypes.CDLL(None), "strlen"
    portacom.exported_factory(library, name)
    assert getattr(library, name).argtypes is None


def test_function_rebinds_when_its_library_is_registered_again():
    import ctypes.util

    from portacom import Function

    libc = ctypes.CDLL("ucrtbase") if sys.platform == "win32" else ctypes.CDLL(ctypes.util.find_library("c"))
    strlen = Function(
        portacom.WINDOWS,
        "rebind-test.dll",
        "strlen",
        lambda: (ctypes.c_size_t, (Param("s", ctypes.c_char_p),)),
        cdecl=True,
    )
    portacom.register_library("rebind-test.dll", libc)
    assert strlen(b"abc") == 3
    portacom.register_library("rebind-test.dll", "surely-not-a-library-xyz.so")
    with pytest.raises(OSError):
        strlen(b"abc")
    portacom.register_library("rebind-test.dll", libc)
    assert strlen(b"abcd") == 4


# -- VARIANT, GUID ------------------------------------------------------------------------------


def test_variant_integer_out_of_range():
    for value in (1 << 64, -(1 << 63) - 1, 1 << 70):
        with pytest.raises(OverflowError):
            portacom.VARIANT.from_value(value)
    assert portacom.VARIANT.from_value((1 << 64) - 1).value == (1 << 64) - 1
    assert portacom.VARIANT.from_value(-(1 << 63)).value == -(1 << 63)


def test_guid_compares_unequal_to_other_things():
    g = GUID("8BA5FB08-5195-40E2-AC58-0D989C3A0102")
    assert g != "not a guid" and g != 5 and g != None  # noqa: E711
    assert g == "{8BA5FB08-5195-40E2-AC58-0D989C3A0102}"


def test_variant_by_reference():
    VT_BYREF = 0x4000
    number = ctypes.c_int32(-7)
    v = portacom.VARIANT()
    v.vt, v.u.byref = VT_BYREF | portacom.VT_I4, ctypes.addressof(number)
    assert v.value == -7

    inner = portacom.VARIANT("inner é")
    v.vt, v.u.byref = VT_BYREF | portacom.VT_VARIANT, ctypes.addressof(inner)
    assert v.value == "inner é"

    bstr = ctypes.c_void_p(portacom.VARIANT._allocator().alloc_bstr("by ref"))
    v.vt, v.u.byref = VT_BYREF | portacom.VT_BSTR, ctypes.addressof(bstr)
    assert v.value == "by ref"

    flag = ctypes.c_short(-1)
    v.vt, v.u.byref = VT_BYREF | portacom.VT_BOOL, ctypes.addressof(flag)
    assert v.value is True
    v.clear()  # the VARIANT owns none of these
    assert v.vt == portacom.VT_EMPTY and number.value == -7 and inner.value == "inner é"
    assert portacom.VARIANT._allocator().read_bstr(bstr.value) == "by ref"
    portacom.VARIANT._allocator().free_bstr(bstr.value)
    inner.clear()


IID_IRecordInfo = GUID("0000002F-0000-0000-C000-000000000046")


def test_variant_clear_record():
    # portacom's own VariantClear, which clears the record as Wine's does (OLEAUT32's also frees pvRecord)
    variant = portacom.Abi("layout", allocator=WindowsLayoutAllocator()).VARIANT

    class IRecordInfo(portacom.IUnknown):  # its first two methods: all VariantClear calls
        _iid_ = IID_IRecordInfo
        _methods_ = ("RecordInit", "RecordClear")

    IRecordInfo._signatures_ = lambda: (
        (HRESULT, (Param("pvNew", Ptr(None), IN),)),
        (HRESULT, (Param("pvExisting", Ptr(None), IN),)),
    )

    class RecordInfo(ComObject):
        _interfaces_ = (IRecordInfo,)

        def __init__(self):
            self.cleared = []

        def RecordClear(self, address):
            self.cleared.append(address)

    info = RecordInfo()
    record = ctypes.c_int32(5)
    v = variant()
    v.vt = 36  # VT_RECORD
    v.u.record[0] = ctypes.addressof(record)
    v.u.record[1] = info._com_query_(IID_IRecordInfo)  # the VARIANT's reference
    assert info.references == 2
    v.clear()
    assert info.cleared == [ctypes.addressof(record)] and info.references == 1
    assert v.vt == portacom.VT_EMPTY


# -- structs returned by value, from Python methods ------------------------------------------


class Ints(portacom.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class Floats(portacom.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float)]


class Big(portacom.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("z", ctypes.c_double)]


class Mid(portacom.Structure):  # 16 bytes: two registers off Windows
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class IShapes(portacom.IUnknown):
    _iid_ = GUID("7C3B2A19-0E8D-4F6C-B5A4-392817160504")
    _methods_ = ("GetInts", "GetFloats", "GetBig", "GetMid")


IShapes._signatures_ = lambda: (
    (Ints, (Param("a", ctypes.c_int32),)),
    (Floats, (Param("a", ctypes.c_float),)),
    (Big, (Param("a", ctypes.c_double), Param("b", ctypes.c_int32))),
    (Mid, ()),
)


class Shapes(ComObject):
    _interfaces_ = (IShapes,)

    def GetInts(self, a):
        return Ints(a, -a)

    def GetFloats(self, a):
        return Floats(a, a * 2)

    def GetBig(self, a, b):
        return Big(a, a + b, a * b)

    def GetMid(self):
        return Mid(1.0, 2.0)


@pytest.mark.skipif(not STRUCT_RETURNS, reason="Python struct returns: Windows and x86-64 only")
def test_struct_returns_from_python():
    # called through prototypes that return the struct: ctypes does the caller's side of the ABI
    shapes = Shapes().QueryInterface(IShapes)
    ints = shapes.GetInts(7)
    assert (ints.x, ints.y) == (7, -7)
    floats = shapes.GetFloats(1.5)
    assert (floats.x, floats.y) == (1.5, 3.0)
    big = shapes.GetBig(2.5, 4)
    assert (big.x, big.y, big.z) == (2.5, 6.5, 10.0)
    if portacom.NATIVE_COM:
        mid = shapes.GetMid()
        assert (mid.x, mid.y) == (1.0, 2.0)


@pytest.mark.skipif(portacom.NATIVE_COM, reason="MSVC returns every struct through a pointer")
def test_unsupported_struct_return_is_reported():
    seen = []
    old = portacom.set_callback_error_handler(lambda obj, method, exc: seen.append((method, type(exc))))
    try:
        Shapes().QueryInterface(IShapes).GetMid()  # the values are garbage, but the call returns
    finally:
        portacom.set_callback_error_handler(old)
    assert seen == [("GetMid", NotImplementedError)]


# -- delegates: Python callables as C function pointers ---------------------------------


class COMPARE(codegen.Delegate):  # int (*)(const void*, const void*), as qsort takes
    _cdecl_ = True
    _signature_ = (ctypes.c_int, (Param("a", Ptr(None)), Param("b", Ptr(None))))


class UNARY(codegen.Delegate):
    _cdecl_ = True
    _signature_ = (ctypes.c_int, (Param("x", ctypes.c_int),))


class APPLY(codegen.Delegate):  # takes a function pointer
    _cdecl_ = True
    _signature_ = (ctypes.c_int, (Param("f", UNARY), Param("x", ctypes.c_int)))


class CHOOSE(codegen.Delegate):  # returns one
    _cdecl_ = True


CHOOSE._signature_ = lambda: (UNARY, (Param("which", ctypes.c_int),))  # as generated: evaluated on first use


def _compare_ints(a, b):
    a, b = ctypes.c_int.from_address(a).value, ctypes.c_int.from_address(b).value
    return (a > b) - (a < b)


def test_python_callback_through_c():
    """qsort calls a Python comparison through a delegate parameter."""
    libc = ctypes.cdll.msvcrt if sys.platform == "win32" else ctypes.CDLL(None)
    portacom.register_library("portacom-test-libc.dll", libc)
    params = (Param("base", Ptr(None)), Param("count", ctypes.c_size_t), Param("size", ctypes.c_size_t))
    qsort = codegen.Function(
        None, "portacom-test-libc.dll", "qsort", lambda: (None, (*params, Param("compare", COMPARE))), cdecl=True
    )
    size = ctypes.sizeof(ctypes.c_int)
    values = (ctypes.c_int * 6)(5, -1, 4, 2, 3, 0)
    qsort(values, 6, size, _compare_ints)  # a callable, kept for the call
    assert list(values) == [-1, 0, 2, 3, 4, 5]
    descending = COMPARE(lambda a, b: _compare_ints(b, a))  # made once, passed as it is
    qsort(values, 6, size, descending)
    assert list(values) == [5, 4, 3, 2, 0, -1]
    raw = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(_compare_ints)  # a ctypes function pointer
    qsort(values, 6, size, raw)
    assert list(values) == [-1, 0, 2, 3, 4, 5]
    qsort(values, 6, size, descending.value)  # an address
    assert list(values) == [5, 4, 3, 2, 0, -1]
    assert repr(descending).startswith("<COMPARE 0x") and repr(COMPARE()) == "<COMPARE NULL>"


def test_delegates_in_both_directions():
    """Calling a delegate calls through its C prototype; delegates pass to and come back from callbacks."""
    one, two = ctypes.c_int(1), ctypes.c_int(2)
    compare = COMPARE(_compare_ints)
    assert compare(ctypes.addressof(one), ctypes.addressof(two)) == -1
    assert compare(two, one) == 1  # a ctypes object goes by reference
    apply = APPLY(lambda f, x: f(x) + 1)  # f arrives from C as a callable UNARY
    assert apply(lambda x: x * 2, 5) == 11
    double, triple = UNARY(lambda x: x * 2), UNARY(lambda x: x * 3)
    choose = CHOOSE(lambda which: double if which else triple)
    assert isinstance(choose(1), UNARY) and choose(1)(5) == 10 and choose(0)(5) == 15
    assert CHOOSE(lambda which: None)(0) is None
    with pytest.raises(ValueError, match="NULL"):
        UNARY()(1)
    with pytest.raises(TypeError, match="takes a callable"):
        UNARY("not callable")


def test_delegate_errors_are_reported():
    seen = []
    old = portacom.set_callback_error_handler(lambda obj, name, error: seen.append((name, type(error))))
    try:
        assert UNARY(lambda x: 1 // x)(0) == 0  # the C caller gets 0
    finally:
        portacom.set_callback_error_handler(old)
    assert seen == [("UNARY", ZeroDivisionError)]

    class UNKNOWN(codegen.Delegate):
        _signature_ = codegen.Unsupported("no such type")

    with pytest.raises(NotImplementedError, match="no such type"):
        UNKNOWN(len)
    assert UNKNOWN(0x1234).value == 0x1234  # still a pointer

    class Pair(codegen.Structure):
        _fields_ = [("a", ctypes.c_int), ("b", ctypes.c_int)]

    class PAIR(codegen.Delegate):
        _signature_ = (Pair, ())

    with pytest.raises(TypeError, match="by value"):
        PAIR(Pair)


def test_delegate_struct_field():
    """A delegate field holds the pointer: assigning stores it, reading gives a callable."""

    class Table(codegen.Structure):
        pass

    codegen.set_fields(Table, [("compare", COMPARE), ("count", ctypes.c_int)])
    proc = COMPARE(_compare_ints)  # the caller keeps it alive while C may call it
    table = Table(compare=proc, count=2)
    assert isinstance(table.compare, COMPARE) and table.compare.value == proc.value
    copy = Table.from_buffer_copy(bytes(table))
    one, two = ctypes.c_int(1), ctypes.c_int(2)
    assert copy.compare(ctypes.addressof(two), ctypes.addressof(one)) == 1
    table.compare = 0
    assert not table.compare and table.compare.value is None
    table.compare = proc.value
    assert table.compare.value == proc.value
