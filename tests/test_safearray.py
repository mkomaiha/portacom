"""SAFEARRAY: lists in, lists out, in every direction; bytes and numbers in bulk."""

import array
import ctypes
import sys
import time

import portacom
import pytest
from portacom import IN, OUT, ComObject, Param, Ptr, SafeArray, WindowsLayoutAllocator
from portacom import _safearray as safearray

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs OLEAUT32")

ALLOCATORS = [portacom.default_allocator(), WindowsLayoutAllocator()]
VALUES = [
    [1, 2, 3],
    [-(2**40), 2**40],
    [1.5, -2.25],
    ["text", "é🙂", ""],
    [True, False],
    [1, "two", 3.5, None, True],
    [[1, 2, 3], [4, 5, 6]],
    [[["a", "b"], ["c", "d"]], [["e", "f"], ["g", "h"]]],
    [[[1, 2], [3, 4], [5, 6]], [[7, 8], [9, 10], [11, 12]]],
    [],
]


@pytest.mark.parametrize("allocator", ALLOCATORS, ids=lambda a: a.name)
@pytest.mark.parametrize("values", VALUES, ids=repr)
def test_round_trip(allocator, values):
    address = safearray.create(values, allocator)
    try:
        assert safearray.to_list(address, allocator) == values
    finally:
        allocator.safearray_destroy(address)


@pytest.mark.parametrize("allocator", ALLOCATORS, ids=lambda a: a.name)
def test_element_types(allocator):
    cases = [
        (SafeArray([1, 2], portacom.VT_R4), portacom.VT_R4, [1.0, 2.0]),
        (SafeArray([1, 2], portacom.VT_UI2), portacom.VT_UI2, [1, 2]),
        (SafeArray([1.25], portacom.VT_CY), portacom.VT_CY, [1.25]),
        (b"\x00\xff", portacom.VT_UI1, b"\x00\xff"),
    ]
    for values, vt, expected in cases:
        address = safearray.create(values, allocator)
        assert allocator.safearray_vartype(address) == vt
        assert safearray.to_list(address, allocator) == expected
        allocator.safearray_destroy(address)


BULK = [
    # (what goes in, VARTYPE, what comes out)
    (b"\x00\x01\xfe\xff", portacom.VT_UI1, b"\x00\x01\xfe\xff"),
    (bytearray(b"ab"), portacom.VT_UI1, b"ab"),
    (memoryview(b"xabcx")[1:4], portacom.VT_UI1, b"abc"),
    (SafeArray(b"\x80\x7f", portacom.VT_I1), portacom.VT_I1, b"\x80\x7f"),  # bytes as they are
    (SafeArray([0, 255], portacom.VT_UI1), portacom.VT_UI1, b"\x00\xff"),
    (SafeArray(b"\x01\x02", portacom.VT_I4), portacom.VT_I4, [1, 2]),  # each byte a number
    (SafeArray([0.5, -1.25, 3e38], portacom.VT_R4), portacom.VT_R4, [0.5, -1.25, array.array("f", [3e38])[0]]),
    (SafeArray([-(2**31), 2**31 - 1], portacom.VT_I4), portacom.VT_I4, [-(2**31), 2**31 - 1]),
    (SafeArray([2**32 - 1], portacom.VT_I4), portacom.VT_I4, [-1]),  # too large: truncated, as C does
    (SafeArray([0, 2**64 - 1], portacom.VT_UI8), portacom.VT_UI8, [0, 2**64 - 1]),
    (SafeArray([-1, 1], portacom.VT_I2), portacom.VT_I2, [-1, 1]),
    (array.array("f", [1.5, 2.5]), portacom.VT_R4, [1.5, 2.5]),
    (array.array("d", [1.5]), portacom.VT_R8, [1.5]),
    (array.array("i", [7, -7]), portacom.VT_I4, [7, -7]),
    (SafeArray(array.array("d", [2.0]), portacom.VT_R4), portacom.VT_R4, [2.0]),  # converted
    (SafeArray([[1, 2, 3], [4, 5, 6]], portacom.VT_UI1), portacom.VT_UI1, [[1, 2, 3], [4, 5, 6]]),
    (SafeArray([[1.5, 2.5], [3.5, 4.5]], portacom.VT_R4), portacom.VT_R4, [[1.5, 2.5], [3.5, 4.5]]),
    (b"", portacom.VT_UI1, b""),
    (SafeArray([], portacom.VT_R8), portacom.VT_R8, []),
]


@pytest.mark.parametrize("allocator", ALLOCATORS, ids=lambda a: a.name)
@pytest.mark.parametrize("values, vt, expected", BULK, ids=repr)
def test_bulk(allocator, values, vt, expected):
    address = safearray.create(values, allocator)
    try:
        assert allocator.safearray_vartype(address) == vt
        result = safearray.to_list(address, allocator)
        assert type(result) is type(expected) and result == expected
    finally:
        allocator.safearray_destroy(address)


@pytest.mark.parametrize("allocator", ALLOCATORS, ids=lambda a: a.name)
def test_data_order(allocator):
    """Nested lists are outermost first; the data varies the first index fastest."""
    address = safearray.create(SafeArray([[1, 2, 3], [4, 5, 6]], portacom.VT_I4), allocator)
    data = safearray.SAFEARRAY.from_address(address).pvData
    assert list((ctypes.c_int32 * 6).from_address(data)) == [1, 4, 2, 5, 3, 6]
    allocator.safearray_destroy(address)
    with pytest.raises(ValueError):
        safearray.create([[1, 2], [3]], allocator)  # ragged


@pytest.mark.parametrize("allocator", ALLOCATORS, ids=lambda a: a.name)
def test_lower_bounds_do_not_move_the_data(allocator):
    address = safearray.create(b"abc", allocator)
    descriptor = safearray.SAFEARRAY.from_address(address)
    descriptor.rgsabound[0].lLbound = 5
    assert safearray.to_list(address, allocator) == b"abc"
    allocator.safearray_destroy(address)


def test_variant_arrays():
    for values in ([1, 2, 3], ["a", "b"], [[1.5, 2.5], [3.5, 4.5]]):
        v = portacom.VARIANT(values)
        assert v.vt & portacom.VT_ARRAY
        assert v.value == values
        v.clear()
    for values, vt, expected in [
        (b"\x00\xff", portacom.VT_UI1, b"\x00\xff"),
        (memoryview(b"xy"), portacom.VT_UI1, b"xy"),
        (array.array("f", [0.5]), portacom.VT_R4, [0.5]),
    ]:
        v = portacom.VARIANT(values)
        assert v.vt == portacom.VT_ARRAY | vt
        assert v.value == expected
        v.clear()


@pytest.mark.parametrize("allocator", ALLOCATORS, ids=lambda a: a.name)
def test_interfaces_are_referenced(allocator, minicom):
    Example, _ = minicom

    class Callback(ComObject):
        _interfaces_ = (Example.ICalcCb,)

    obj = Callback()
    address = safearray.create([obj, obj], allocator)
    assert obj.references == 3
    items = safearray.to_list(address, allocator)
    assert all(isinstance(item, portacom.IUnknown) for item in items)
    del items
    allocator.safearray_destroy(address)
    assert obj.references == 1


def test_plain_allocator_says_why():
    with pytest.raises(NotImplementedError, match="MiniCOM.*Windows-layout SAFEARRAY"):
        portacom.PlainAllocator().safearray_create(portacom.VT_I4, (1,))


# -- through vtables ---------------------------------------------------------------------------


class Counting(WindowsLayoutAllocator):
    """Counts the arrays it destroys."""

    name = "counting"

    def __init__(self):
        self.destroyed = []

    def safearray_destroy(self, address):
        self.destroyed.append(address)
        super().safearray_destroy(address)


def make_interface(abi):
    """An interface with SAFEARRAY [in], [out] and [in,out] parameters, and a struct of them."""

    class Frame(portacom.Structure):
        """An [out] struct that hands over a SAFEARRAY and a BSTR, for the caller to free."""

    class IArrays(portacom.IUnknown):
        _iid_ = portacom.GUID("6D1F3E2A-0B5C-4F7E-9A1D-2C3B4A5E6F70")
        _abi_ = abi
        _methods_ = ("Sum", "Range", "Reverse", "Length", "Bytes", "GetFrame")
        _frame_ = Frame

    SA = abi.SAFEARRAY
    portacom.set_fields(Frame, [("Data", Ptr(SA)), ("Name", abi.BSTR), ("Count", ctypes.c_int32)])
    IArrays._signatures_ = lambda: (
        (portacom.HRESULT, (Param("values", Ptr(SA), IN), Param("total", Ptr(ctypes.c_double), OUT))),
        (portacom.HRESULT, (Param("count", ctypes.c_int32, IN), Param("values", Ptr(Ptr(SA)), OUT))),
        (portacom.HRESULT, (Param("values", Ptr(Ptr(SA)), IN | OUT),)),
        (portacom.HRESULT, (Param("values", Ptr(SA), IN), Param("length", Ptr(ctypes.c_int32), OUT))),
        (portacom.HRESULT, (Param("count", ctypes.c_int32, IN), Param("values", Ptr(Ptr(SA)), OUT))),
        (portacom.HRESULT, (Param("vt", ctypes.c_int32, IN), Param("frame", Ptr(Frame), OUT))),
    )
    return IArrays


ABIS = [portacom.WINDOWS, portacom.Abi("layout", allocator=WindowsLayoutAllocator())]
MB = bytes(range(256)) * 4096  # 1 MB


class Arrays(ComObject):
    def Sum(self, values):
        return float(sum(values))

    def Range(self, count):
        return [f"item {i}" for i in range(count)]

    def Reverse(self, values):
        return values[::-1]

    def Length(self, values):
        assert type(values) is bytes
        return len(values)

    def Bytes(self, count):
        return MB[:count]

    def GetFrame(self, vt):
        IArrays = self._interfaces_[0]
        abi = IArrays._abi_
        values = SafeArray(b"\x01\x02\x03", portacom.VT_UI1) if vt == portacom.VT_UI1 else SafeArray([1.5], vt)
        return IArrays._frame_(safearray.create(values, abi.allocator), abi.BSTR.alloc("frame"), 3)


def arrays_for(abi):
    IArrays = make_interface(abi)
    return type("Arrays", (Arrays,), {"_interfaces_": (IArrays,)})().QueryInterface(IArrays)


@pytest.mark.parametrize("abi", ABIS, ids=lambda a: a.name)
def test_through_vtables(abi):
    arrays = arrays_for(abi)
    assert arrays.Sum([1, 2, 3.5]) == 6.5
    assert arrays.Sum(SafeArray([1, 2], portacom.VT_I2)) == 3.0
    assert arrays.Range(3) == ["item 0", "item 1", "item 2"]
    assert arrays.Range(0) == []
    assert arrays.Reverse(["a", "b", "c"]) == ["c", "b", "a"]  # the old array is destroyed by the callee
    assert arrays.Reverse(b"abc") == b"cba"


@pytest.mark.parametrize("abi", ABIS, ids=lambda a: a.name)
def test_a_megabyte_in_milliseconds(abi):
    """A 1 MB frame crosses as bytes, in one copy each way."""
    arrays = arrays_for(abi)
    assert arrays.Length(MB) == len(MB)
    assert arrays.Bytes(len(MB)) == MB

    def best(call):
        times = []
        for _ in range(3):
            start = time.perf_counter()
            call()
            times.append(time.perf_counter() - start)
        return min(times)

    into, out_of = best(lambda: arrays.Length(MB)), best(lambda: arrays.Bytes(len(MB)))
    print(f"1 MB in: {into * 1000:.2f} ms, out: {out_of * 1000:.2f} ms")
    assert into < 0.05 and out_of < 0.05


@pytest.mark.parametrize("vt, expected", [(portacom.VT_UI1, b"\x01\x02\x03"), (portacom.VT_R4, [1.5])])
def test_take_struct_fields(vt, expected):
    """An [out] struct's SAFEARRAY* reads as its address, for Abi.SAFEARRAY.take()."""
    allocator = Counting()
    abi = portacom.Abi("counting", allocator=allocator)
    frame = arrays_for(abi).GetFrame(vt)
    assert isinstance(frame.Data, int)
    assert abi.SAFEARRAY.read(frame.Data) == expected  # still the caller's
    assert allocator.destroyed == []
    address = frame.Data
    assert abi.SAFEARRAY.take(frame.Data) == expected
    assert allocator.destroyed == [address]
    assert str(frame.Name) == "frame"
    frame.Name.free()
    assert abi.SAFEARRAY.take(None) is None
    assert portacom.WINDOWS.SAFEARRAY.take(safearray.create(b"x", portacom.default_allocator())) == b"x"


# -- interop with OLEAUT32 ---------------------------------------------------------------------


@windows_only
def test_oleaut32_reads_ours_and_we_read_its(win32):
    Ole = win32["Ole"]
    assert Ole.SafeArrayGetDim([1, 2, 3]) == 1
    assert Ole.SafeArrayGetDim([[1, 2], [3, 4]]) == 2
    assert Ole.SafeArrayGetVartype(["a"]) == portacom.VT_BSTR
    assert Ole.SafeArrayGetVartype(b"ab") == portacom.VT_UI1
    assert Ole.SafeArrayGetVartype(SafeArray([1.5], portacom.VT_R4)) == portacom.VT_R4
    assert Ole.SafeArrayGetUBound([1, 2, 3], 1) == 2
    assert Ole.SafeArrayCopy([1.5, "x", None]) == [1.5, "x", None]  # OLEAUT32's copy, read back
    assert Ole.SafeArrayCopy(MB) == MB
    assert Ole.SafeArrayCopy(SafeArray([0.5, 2.5], portacom.VT_R4)) == [0.5, 2.5]
    assert Ole.SafeArrayCopy(SafeArray([[1, 2], [3, 4]], portacom.VT_UI1)) == [[1, 2], [3, 4]]
    assert Ole.VectorFromBstr("hi") == "hi".encode("utf-16-le")
    assert Ole.BstrFromVector(SafeArray(list("hi".encode("utf-16-le")), portacom.VT_UI1)) == "hi"
    assert Ole.BstrFromVector("hi".encode("utf-16-le")) == "hi"


@windows_only
def test_oleaut32_vector_with_a_lower_bound():
    """SafeArrayCreateVector's array reads cElements bytes from pvData, whatever lLbound is."""
    oleaut32 = ctypes.WinDLL("oleaut32")
    oleaut32.SafeArrayCreateVector.restype = ctypes.c_void_p
    oleaut32.SafeArrayCreateVector.argtypes = [ctypes.c_uint16, ctypes.c_int32, ctypes.c_uint32]
    address = oleaut32.SafeArrayCreateVector(portacom.VT_UI1, 5, 4)
    descriptor = safearray.SAFEARRAY.from_address(address)
    assert descriptor.rgsabound[0].lLbound == 5
    ctypes.memmove(descriptor.pvData, b"abcd", 4)
    assert portacom.WINDOWS.SAFEARRAY.take(address) == b"abcd"


@windows_only
def test_multidimensional_order_matches_oleaut32(win32):
    """Our nested lists are outermost dimension first; OLE's indexes say so too."""
    Ole = win32["Ole"]
    rows = [[1, 2, 3], [4, 5, 6]]
    assert Ole.SafeArrayGetUBound(rows, 1) == 1  # first dimension: 2 rows
    assert Ole.SafeArrayGetUBound(rows, 2) == 2  # second dimension: 3 columns
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            # rgIndices are in dimension order (whatever the docs suggest; probed on
            # Windows 11), and pv is an [out] void*, so the element comes back in
            # the low bytes of a pointer-sized value
            element = Ole.SafeArrayGetElement(rows, (ctypes.c_int32 * 2)(i, j))
            assert element & 0xFFFFFFFF == value, (i, j)
