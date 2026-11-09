"""MiniCOM's TestInterfaces, implemented in Python and called through their vtables.

Both sides are Python, so this runs on every platform.  It exercises the
calling direction (Python values -> C arguments) and the implementing
direction (C arguments -> Python method -> [out] values) at once.
"""

import gc

import portacom
import pytest
from portacom import COMError, ComObject


def make_classes(Example, Registry):
    class Calculator(ComObject):
        _interfaces_ = (Example.ICalcExt, Example.ICalc2)

        def __init__(self):
            self.callback = None

        def GetValue(self):
            return 42

        def Add(self, a, b):
            return a + b

        def SetCallback(self, cb):
            if cb is None:
                raise COMError(portacom.E_INVALIDARG)
            self.callback = cb

        def GetValue2(self):
            if self.callback:
                self.callback.Message("GetValue2 called")
            return 43

    class RegistryImpl(ComObject):
        _interfaces_ = (Registry.IRegistry,)

        def __init__(self):
            self.stored = None

        def NewCalculator(self):
            return Calculator()

        def Store(self, obj):
            self.stored = obj

        def Fetch(self):
            return self.stored

        def Describe(self, source):
            source.Message("describing")
            name = source.Name()
            calc = source.Calculator()
            low, high = source.Range()
            return f"{name} uses {calc.GetValue2()}, range {low}-{high}, adjusted {source.Adjust(1.5):g}"

        def MakeRecord(self, name):
            r = Registry.Record()
            r.name = Registry._abi.BSTR.alloc(name).value  # the caller frees it
            r.shape = Registry.SHAPE_BOX
            r.span = Registry.Span(1, 2)
            r.weights[:] = [0.5, 0.25, 0.25]
            r.valid = -1
            r.flag = 1
            r.after = 2.5
            return r

        def CheckRecord(self, record):
            return f"{record.name} {record.shape} {record.span.low:g}-{record.span.high:g} {list(record.weights)}"

        def MinMax(self, values):
            return min(values), max(values)

        def Bytes(self, value):
            return [(value >> (8 * i)) & 0xFF for i in range(4)]

        def Scale(self, value, negate):
            return -2 * value if negate else 2 * value

        def Widen(self, span, by):
            return Registry.Span(span.low - by, span.high + by)

        def Next(self, shape):
            return shape << 1

    return Calculator, RegistryImpl


def test_calculator(minicom):
    Example, Registry = minicom
    Calculator, _ = make_classes(Example, Registry)
    impl = Calculator()
    calc = impl.QueryInterface(Example.ICalcExt)
    assert calc.GetValue() == 42
    assert calc.Add(1, 2) == 3

    messages = []

    class Callback(ComObject):
        _interfaces_ = (Example.ICalcCb,)

        def Message(self, text):
            messages.append(text)

    cb = Callback()
    calc.SetCallback(cb)
    assert cb.references == 2  # the calculator holds it
    calc2 = calc.QueryInterface(Example.ICalc2)
    assert calc2.GetValue2() == 43
    assert messages == ["GetValue2 called"]
    impl.callback = None
    gc.collect()
    assert cb.references == 1

    with pytest.raises(COMError) as e:
        calc.SetCallback(None)
    assert e.value.hr == 0x80070057
    assert calc.query(Registry.IRegistry) is None


def test_registry_conversions(minicom):
    Example, Registry = minicom
    _, RegistryImpl = make_classes(Example, Registry)
    registry = RegistryImpl().QueryInterface(Registry.IRegistry)

    calc = registry.NewCalculator()
    assert type(calc) is Example.ICalcExt
    assert calc.Add(2, 3) == 5

    registry.Store(calc)
    fetched = registry.Fetch()
    assert type(fetched) is portacom.IUnknown
    assert fetched.QueryInterface(Example.ICalc2).GetValue2() == 43

    assert registry.MinMax([3.0, -1.0, 2.0]) == (-1.0, 3.0)
    assert registry.Bytes(0x04030201) == [1, 2, 3, 4]
    assert registry.Scale(2.5, True) == -5.0
    assert registry.Scale(2.5, False) == 5.0
    wider = registry.Widen(Registry.Span(1.0, 2.0), 0.5)
    assert (wider.low, wider.high) == (0.5, 2.5)
    assert registry.Next(Registry.SHAPE_LINE) is Registry.Shape.SHAPE_BOX
    assert registry.Next(Registry.SHAPE_BOX) == 4

    record = registry.MakeRecord("made in Python")
    assert str(record.name) == "made in Python"
    assert record.shape == Registry.SHAPE_BOX and list(record.weights) == [0.5, 0.25, 0.25]
    assert record.valid == -1 and record.flag == 1 and record.after == 2.5
    summary = registry.CheckRecord(record)
    assert summary.startswith("made in Python 2 1-2"), summary
    record.name.free()

    with pytest.raises(COMError) as e:
        registry.Opaque(None)  # the Python object has no Opaque method
    assert e.value.hr == 0x80004001  # E_NOTIMPL


def test_python_source_called_from_python_registry(minicom):
    Example, Registry = minicom
    Calculator, RegistryImpl = make_classes(Example, Registry)
    registry = RegistryImpl().QueryInterface(Registry.IRegistry)

    class Source(ComObject):
        _interfaces_ = (Registry.ISource,)

        def __init__(self):
            self.messages = []

        def Message(self, text):
            self.messages.append(text)

        def Name(self):
            return "python source"

        def Calculator(self):
            return Calculator()

        def Range(self):
            return 1, 5

        def Adjust(self, value):
            return value * 2

    source = Source()
    assert registry.Describe(source) == "python source uses 43, range 1-5, adjusted 3"
    assert source.messages == ["describing"]

    class Failing(Source):
        def Name(self):
            raise ValueError("expected failure, for the test")

    errors = []
    old = portacom.set_callback_error_handler(lambda obj, method, exc: errors.append((method, type(exc))))
    try:
        with pytest.raises(COMError) as e:
            registry.Describe(Failing())
    finally:
        portacom.set_callback_error_handler(old)
    assert e.value.hr == 0x80004005  # E_FAIL, through two levels of Python COM objects
    assert ("Name", ValueError) in errors


def test_field_and_param_overrides(tmp_path, abi_registry):
    """Overrides for uses the metadata does not name (an IDL long is just Int32)."""
    import ctypes
    import importlib
    import os
    import sys

    from conftest import DATA, generate

    generate(str(tmp_path / "overridden"), os.path.join(DATA, "TestInterfaces.winmd"), None)
    portacom.use_abi(
        "overridden.Registry",
        portacom.Abi(
            "test-overrides",
            on_windows=True,  # so that Windows uses it too
            overrides={
                "Record.after": ctypes.c_float,
                "IRegistry.Bytes.value": ctypes.c_uint16,
            },
        ),
    )
    sys.path.insert(0, str(tmp_path))
    try:
        Registry = importlib.import_module("overridden.Registry")
        assert Registry.Record.after.size == 4
        signature = Registry.IRegistry.Bytes.signature()
        assert signature.argtypes[0] is ctypes.c_uint16
        assert Registry._abi.name == "test-overrides"
    finally:
        sys.path.remove(str(tmp_path))
