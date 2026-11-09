"""VARIANT, IErrorInfo, success HRESULTs, struct returns and bit fields."""

import ctypes
import sys

import portacom
import pytest
from portacom import VARIANT, COMError, ComObject

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")


def test_variant_values():
    for value in (None, True, False, 42, -(1 << 40), 2.5, "text é\U0001f642"):
        v = VARIANT.from_value(value)
        assert v.value == value and type(v.value) is type(value)
        v.clear()
        assert v.vt == 0
    assert ctypes.sizeof(VARIANT) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)


class PropertyBag(ComObject):
    def __init__(self, StructuredStorage):
        self._interfaces_ = (StructuredStorage.IPropertyBag,)
        self.values = {}

    def Read(self, name, value, error_log):
        if name not in self.values:
            raise COMError(portacom.E_INVALIDARG, description=f"no property {name}")
        return self.values[name]

    def Write(self, name, value):
        self.values[name] = value


def test_variant_through_vtables(win32):
    """IPropertyBag implemented in Python: VARIANT [in] and [in,out] in both directions."""
    StructuredStorage = win32["StructuredStorage"]
    impl = PropertyBag(StructuredStorage)
    bag = impl.QueryInterface(StructuredStorage.IPropertyBag)
    bag.Write("name", "portacom")
    bag.Write("count", 3)
    bag.Write("ratio", 0.5)
    bag.Write("flag", True)
    bag.Write("self", impl)  # an interface in a VARIANT: AddRef'd by the conversion
    assert impl.values["name"] == "portacom" and impl.values["count"] == 3
    assert impl.values["ratio"] == 0.5 and impl.values["flag"] is True
    assert isinstance(impl.values["self"], portacom.IUnknown)
    assert bag.Read("name", None, None) == "portacom"
    assert bag.Read("count", "replaced", None) == 3  # [in,out]: the old value is cleared
    assert bag.Read("flag", None, None) is True
    unknown = bag.Read("self", None, None)
    assert unknown.address == impl._com_pointer_(portacom.IUnknown)
    del unknown
    impl.values.clear()
    assert impl.references == 2  # Python's own, and the one "bag" holds
    del bag
    assert impl.references == 1


def test_error_description(win32):
    StructuredStorage = win32["StructuredStorage"]
    bag = PropertyBag(StructuredStorage).QueryInterface(StructuredStorage.IPropertyBag)
    with pytest.raises(COMError) as e:
        bag.Read("missing", None, None)
    assert e.value.hr == 0x80070057
    if sys.platform == "win32":  # through SetErrorInfo/GetErrorInfo and ISupportErrorInfo
        assert e.value.description == "no property missing"
        assert "no property missing" in str(e.value)


def test_unexpected_exception_reaches_windows_error_info(win32):
    StructuredStorage = win32["StructuredStorage"]

    class Broken(PropertyBag):
        def Write(self, name, value):
            raise KeyError("boom")

    bag = Broken(StructuredStorage).QueryInterface(StructuredStorage.IPropertyBag)
    old = portacom.set_callback_error_handler(lambda *args: None)
    try:
        with pytest.raises(COMError) as e:
            bag.Write("x", 1)
    finally:
        portacom.set_callback_error_handler(old)
    assert e.value.hr == 0x80004005
    if sys.platform == "win32":
        assert "KeyError" in e.value.description and e.value.source == "Broken"


def test_success_codes(minicom):
    Example, _ = minicom

    class Calc(ComObject):
        _interfaces_ = (Example.ICalc,)

        def GetValue(self):
            return 7

    calc = Calc().QueryInterface(Example.ICalc)
    assert calc.GetValue.with_hresult() == (0, 7)


@windows_only
def test_variant_change_type(win32):
    Variant = win32["Variant"]
    # pvargDest is [in, out]: None starts it empty
    assert Variant.VariantChangeType(None, 42, 0, Variant.VT_BSTR) == "42"
    assert Variant.VariantChangeType(None, "2.5", 0, Variant.VT_R8) == 2.5
    assert Variant.VariantChangeType(None, 1, 0, Variant.VT_BOOL) is True
    assert Variant.VARIANT is portacom.VARIANT


@windows_only
def test_struct_returns_from_direct2d(win32):
    """MSVC returns structs from methods through a hidden pointer, even 8-byte ones."""
    D2D = win32["Direct2D"]
    import win32_bindings.Windows.Win32.Graphics.Direct2D.Common as Common

    factory = D2D.D2D1CreateFactory(D2D.D2D1_FACTORY_TYPE_SINGLE_THREADED, D2D.ID2D1Factory, None)
    props = D2D.D2D1_RENDER_TARGET_PROPERTIES()
    props.pixelFormat.format = 87  # DXGI_FORMAT_B8G8R8A8_UNORM
    props.pixelFormat.alphaMode = Common.D2D1_ALPHA_MODE_PREMULTIPLIED
    target = factory.CreateDCRenderTarget(props)
    size = target.GetSize()  # D2D_SIZE_F, 8 bytes
    assert (size.width, size.height) == (0.0, 0.0)
    assert target.GetPixelFormat().format == 87
    brush = target.CreateSolidColorBrush(Common.D2D1_COLOR_F(0.25, 0.5, 0.75, 1.0), None)
    color = brush.GetColor()  # D2D1_COLOR_F, 16 bytes
    assert (color.r, color.g, color.b, color.a) == (0.25, 0.5, 0.75, 1.0)


@windows_only
def test_struct_return_implemented_in_python(win32):
    D2D = win32["Direct2D"]
    import win32_bindings.Windows.Win32.Graphics.Direct2D.Common as Common

    class Brush(ComObject):
        _interfaces_ = (D2D.ID2D1SolidColorBrush,)

        def GetColor(self):
            return Common.D2D1_COLOR_F(1.0, 0.0, 0.5, 0.25)

    color = Brush().QueryInterface(D2D.ID2D1SolidColorBrush).GetColor()
    assert (color.r, color.g, color.b, color.a) == (1.0, 0.0, 0.5, 0.25)


def test_bitfields():
    class Packed(portacom.Structure):
        pass

    portacom.set_fields(Packed, [("_bitfield", ctypes.c_uint32)])
    portacom.bitfields(Packed, "_bitfield", [("InstanceID", 0, 24), ("InstanceMask", 24, 8)])
    p = Packed()
    p.InstanceID = 0x123456
    p.InstanceMask = 0xFF
    assert p._bitfield == 0xFF123456
    p.InstanceID = 0x1FFFFFF  # masked to 24 bits
    assert p.InstanceID == 0xFFFFFF and p.InstanceMask == 0xFF


def test_generated_bitfields(tmp_path):
    """The metadata's NativeBitfieldAttribute becomes properties on the struct."""
    import importlib

    from conftest import generate, win32_winmd

    winmd = win32_winmd()
    if not winmd:
        pytest.skip("Windows.Win32.winmd not found")
    generate(str(tmp_path / "sysinfo"), winmd, ["Windows.Win32.System.SystemInformation"])
    sys.path.insert(0, str(tmp_path))
    try:
        info = importlib.import_module("sysinfo.Windows.Win32.System.SystemInformation")
    finally:
        sys.path.remove(str(tmp_path))
    s = info.SYSTEM_SUPPORTED_PROCESSOR_ARCHITECTURES_INFORMATION()
    s.Machine = 0x8664
    s.UserMode = 1
    assert s._bitfield == 0x8664 | 1 << 17
    assert s.Machine == 0x8664 and s.KernelMode == 0 and s.UserMode == 1
