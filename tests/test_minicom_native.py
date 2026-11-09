"""MiniCOM's C++ test classes (CtypesTests/ExampleLib), driven from Python.

The library is built from MiniCOM's python-bindings branch on Linux or macOS;
set MINICOM_LIBRARY to its path.  This is MiniCOM's CtypesTests/tests.py,
moved onto these bindings.
"""

import ctypes
import gc
import os
import sys

import portacom
import pytest
from conftest import MINICOM_ABI
from portacom import COMError, ComObject

LIBRARY = os.environ.get("MINICOM_LIBRARY")
pytestmark = pytest.mark.skipif(not LIBRARY or sys.platform == "win32", reason="set MINICOM_LIBRARY (Linux/macOS)")


@pytest.fixture(scope="module")
def lib():
    return ctypes.CDLL(LIBRARY)


class CalcCb(ComObject):
    def __init__(self, Example):
        self._interfaces_ = (Example.ICalcCb,)
        self.messages = []

    def Message(self, msg):
        self.messages.append(msg)


def test_abi_profile(minicom):
    _, Registry = minicom
    assert Registry._abi is (portacom.WINDOWS if portacom.NATIVE_COM else MINICOM_ABI)
    allocator = Registry._abi.allocator
    assert isinstance(allocator, portacom.LibraryAllocator) and allocator.fallback.name == "plain"
    if allocator.exports("bstr") is None:  # MiniCOM exports no SysAllocStringLen (yet): wcsdup'd BSTRs
        bstr = Registry._abi.BSTR.alloc("plain")
        assert ctypes.wstring_at(bstr.value) == "plain"
        bstr.free()
    # BOOL is long in MiniCOM (8 bytes on 64-bit Linux and macOS), not the 4 bytes the .winmd says
    assert Registry.Record.flag.size == ctypes.sizeof(ctypes.c_long)


def test_calculator(minicom, lib):
    Example, Registry = minicom
    cb = CalcCb(Example)
    # MiniCOM's CoCreateInstance is declared extern "C", so ctypes can call it
    calc = portacom.co_create_instance(Registry.Calculator, Example.ICalcExt, library=lib)
    calc.SetCallback(cb)
    assert calc.GetValue() == 42
    assert calc.Add(1, 2) == 3
    calc2 = calc.QueryInterface(Example.ICalc2)
    assert calc2.GetValue2() == 43
    assert cb.messages == ["GetValue2 called"]  # a BSTR from C++ (wcsdup'd), read by the plain allocator
    assert cb.references == 2
    del calc, calc2
    gc.collect()
    assert cb.references == 1


def test_user_factory(minicom, lib):
    Example, Registry = minicom
    create = lib.CoCreateInstance
    create.restype = ctypes.c_int32
    create.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]

    def factory(clsid, iid):
        out = ctypes.c_void_p()
        portacom.check(create(ctypes.byref(clsid), None, portacom.CLSCTX_ALL, ctypes.byref(iid), ctypes.byref(out)))
        return out.value

    registry = portacom.create_instance(Registry.Registry, Registry.IRegistry, factory)
    assert registry.NewCalculator().Add(2, 3) == 5


def test_registry(minicom, lib):
    Example, Registry = minicom
    # no library: the Abi's, "ExampleLib.dll", which is libExampleLib.so
    registry = portacom.co_create_instance(Registry.Registry, Registry.IRegistry)

    calc = registry.NewCalculator()
    assert type(calc) is Example.ICalcExt
    assert calc.Add(2, 3) == 5

    registry.Store(calc)
    del calc
    calc2 = registry.Fetch().QueryInterface(Example.ICalc2)
    assert calc2.GetValue2() == 43
    del calc2

    made = registry.MakeRecord("made in C++")
    assert str(made.name) == "made in C++"
    assert made.shape == Registry.SHAPE_BOX and (made.span.low, made.span.high) == (1.0, 2.0)
    assert list(made.weights) == [0.5, 0.25, 0.25]
    assert made.valid == -1 and made.flag == 1 and made.after == 2.5  # after is right only if BOOL is sized right
    assert registry.CheckRecord(made) == "made in C++ 2 1-2 0.5,0.25,0.25 valid 1 2.5"
    made.name.free()

    assert registry.MinMax([3.0, -1.0, 2.0]) == (-1.0, 3.0)
    assert registry.Bytes(0x04030201) == [1, 2, 3, 4]
    assert registry.Scale(2.5, True) == -5.0
    assert registry.Scale(2.5, False) == 5.0
    wider = registry.Widen(Registry.Span(1.0, 2.0), 0.5)
    assert (wider.low, wider.high) == (0.5, 2.5)
    assert registry.Next(Registry.SHAPE_LINE) is Registry.Shape.SHAPE_BOX
    assert registry.Next(Registry.SHAPE_BOX) == 4

    cb2 = CalcCb(Example)
    registry.Store(cb2)
    assert cb2.references == 2
    fetched = registry.Fetch()
    assert fetched.address == cb2._com_pointer_(Example.ICalcCb)
    del fetched


def test_clsid_from_progid(minicom):
    _, Registry = minicom
    # MiniCOM's CLSIDFromProgID matches the C++ class name, after an optional "Program." prefix
    assert portacom.clsid_from_progid("ExampleLib.RegistryImpl", "ExampleLib.dll") == Registry.Registry.clsid
    assert portacom.clsid_from_progid("CalculatorImpl", LIBRARY) == Registry.Calculator.clsid
    with pytest.raises(COMError) as e:
        portacom.clsid_from_progid("ExampleLib.Missing", LIBRARY)
    assert e.value.hr == 0x800401F3  # CO_E_CLASSSTRING


def test_python_source_called_from_cpp(minicom, lib):
    Example, Registry = minicom
    registry = portacom.co_create_instance(Registry.Registry, Registry.IRegistry, library=LIBRARY)
    calc2 = registry.NewCalculator().QueryInterface(Example.ICalc2)

    class Source(ComObject):
        _interfaces_ = (Registry.ISource,)

        def __init__(self, calc):
            self.calc = calc
            self.messages = []
            self.accepted = None

        def Message(self, msg):
            self.messages.append(msg)

        def Name(self):
            return "python source"

        def Calculator(self):
            return self.calc

        def Accept(self, calc):
            self.accepted = calc  # an [in] interface arrives as its class, and may be kept
            return calc.GetValue2() == 43

        def Range(self):
            return 1, 5

        def Adjust(self, value):
            return value * 2

        def Normalize(self, values):
            total = sum(values)
            return [v / total for v in values]

        def GetSpan(self):
            return Registry.Span(0.5, 4.0)

        def Label(self, words):
            r = Registry.Record()
            r.name = Registry._abi.BSTR.alloc(words.upper()).value  # C++ frees it
            r.shape = Registry.SHAPE_LINE
            r.span = Registry.Span(0.0, len(words.split()))
            r.weights[:] = [0.5, 0.25, 0.25]
            r.valid = -1
            r.flag = 1
            r.after = 2.5
            return r

    source = Source(calc2)
    text = registry.Describe(source)
    assert text == (
        "python source uses 43, range 1-5, adjusted 3, normalized 0.125 0.25 0.625, span 0.5-4, label TWO WORDS 2 0.5"
    ), text
    assert source.messages == ["describing"]
    assert source.references == 1
    assert type(source.accepted) is Example.ICalc2
    assert source.accepted.GetValue2() == 43  # kept past the call that passed it
    assert calc2.GetValue2() == 43

    class Failing(Source):
        def Name(self):
            raise ValueError("expected failure, for the test")

    class Incomplete(ComObject):
        _interfaces_ = (Registry.ISource,)

        def Message(self, msg):
            pass

    old = portacom.set_callback_error_handler(lambda *args: None)
    try:
        for handler, hr in [(Failing(calc2), 0x80004005), (Incomplete(), 0x80004001)]:
            with pytest.raises(COMError) as e:
                registry.Describe(handler)
            assert e.value.hr == hr
    finally:
        portacom.set_callback_error_handler(old)
    del e  # its traceback holds the registry

    # an object kept only by C++ stays alive although Python let go of it
    registry.Store(Source(calc2))
    gc.collect()
    kept = registry.Fetch().QueryInterface(Registry.ISource)
    assert kept.Name() == "python source"
    del kept
    del registry
    gc.collect()
    assert not any(isinstance(o, Source) for o in portacom._server._held)
