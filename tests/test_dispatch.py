"""IDispatch: Python to Python through the C vtables everywhere, and real automation servers on Windows."""

import ctypes
import sys

import portacom
import pytest
from portacom import VARIANT, COMError, ComObject
from portacom._dispatch import (
    DISP_E_BADPARAMCOUNT,
    DISP_E_EXCEPTION,
    DISP_E_MEMBERNOTFOUND,
    DISP_E_PARAMNOTFOUND,
    DISP_E_UNKNOWNNAME,
    DISPATCH_METHOD,
    DISPATCH_PROPERTYPUT,
    DISPID_NEWENUM,
    DISPID_UNKNOWN,
    DISPPARAMS,
    EXCEPINFO,
    Dispatch,
    DispatchObject,
    EnumVARIANT,
    IDispatch,
    IEnumVARIANT,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs real automation servers")

SINK_IID = "8F2D4C61-3B7A-4E95-9C1D-0A6B5E4F3D21"


class Counter(DispatchObject):
    def __init__(self):
        self.count = 0
        self._label = "start"
        self.items = {}

    def Add(self, n=1):
        self.count += n
        return self.count

    @property
    def Label(self):
        return self._label

    @Label.setter
    def Label(self, value):
        self._label = value

    def Item(self, key):
        return self.items[key]

    def put_Item(self, key, value):
        self.items[key] = value

    def Fail(self, text):
        raise COMError(portacom.E_INVALIDARG, description=text)

    def Broken(self):
        raise ValueError("boom")

    def Echo(self, value):
        return value


def test_invoke_get_set():
    impl = Counter()
    d = Dispatch(impl)
    assert d.invoke("Add", 2) == 2
    assert d.Add(3) == 5 and d.add() == 6  # automation names are case-insensitive
    assert d.get("count") == 6 and d.Count() == 6
    assert d.get("Label") == "start"
    d.set("Label", "renamed")
    assert impl.Label == "renamed" and d.Label() == "renamed"
    d.set("Item", "k", 42)  # put_Item(key, value), an indexed put
    assert d.get("Item", "k") == 42 and d.Item("k") == 42
    d.set("count", 10)  # a plain attribute
    assert impl.count == 10
    assert d.Echo([1, "two", 3.5]) == [1, "two", 3.5]
    assert d.Echo(None) is None
    assert d.interface.GetTypeInfoCount() == 0
    assert d.interface.raw.GetTypeInfo(0, 0, None) == portacom.E_NOTIMPL


def test_dispids_are_stable_and_case_insensitive():
    d = Dispatch(Counter())
    first = d.dispid("Add")
    assert d.dispid("ADD") == first
    other = Dispatch(d.interface)  # a fresh cache asks the object again
    assert other.dispid("add") == first
    assert other.dispid("Label") != first
    assert d.invoke(first, 4) == 4


def test_unknown_names():
    d = Dispatch(Counter())
    assert not hasattr(d, "Missing")
    with pytest.raises(AttributeError) as e:
        d.dispid("Missing")
    assert e.value.hresult == DISP_E_UNKNOWNNAME
    assert not hasattr(d, "GetIDsOfNames")  # the plumbing is not a member
    with pytest.raises(COMError) as e:
        d.invoke(12345)
    assert e.value.hresult == DISP_E_MEMBERNOTFOUND
    with pytest.raises(COMError) as e:
        d.Add(1, 2, 3)
    assert e.value.hresult == DISP_E_BADPARAMCOUNT


def test_exceptions_become_excepinfo():
    d = Dispatch(Counter())
    with pytest.raises(COMError) as e:
        d.Fail("bad input")
    assert e.value.hr == 0x80070057
    assert e.value.description == "bad input" and e.value.source == "Counter"

    seen = []
    old = portacom.set_callback_error_handler(lambda obj, method, exc: seen.append((method, exc)))
    try:
        with pytest.raises(COMError) as e:
            d.Broken()
    finally:
        portacom.set_callback_error_handler(old)
    assert e.value.hresult == portacom.E_FAIL
    assert e.value.description == "boom" and e.value.source == "Counter"
    assert seen and seen[0][0] == "Broken"


def test_raw_invoke_fills_excepinfo():
    """A failed Invoke leaves EXCEPINFO for the caller, whose BSTRs it frees."""
    d = Dispatch(Counter())
    dispid = d.dispid("Fail")
    arg = VARIANT("why")
    params = DISPPARAMS(ctypes.pointer(arg), None, 1, 0)
    info = EXCEPINFO()
    hr = d.interface.raw.Invoke(
        dispid, None, 0, DISPATCH_METHOD, ctypes.addressof(params), None, ctypes.addressof(info), None
    )
    arg.clear()
    assert hr == DISP_E_EXCEPTION and info.scode == portacom.E_INVALIDARG
    allocator = portacom.default_allocator()
    assert allocator.read_bstr(info.bstrDescription) == "why"
    for bstr in (info.bstrDescription, info.bstrSource):
        allocator.free_bstr(bstr)


def test_references_return_to_one():
    impl = Counter()
    d = Dispatch(impl)
    d.Add()
    d.set("Item", "self", impl)  # VT_DISPATCH, AddRef'd
    nested = d.Item("self")
    assert isinstance(nested, Dispatch) and nested.Add() == 2
    assert impl.references > 1
    del nested, d
    impl.items.clear()
    assert impl.references == 1


def test_dispinterfaces_and_fixed_dispids():
    class Sink(DispatchObject):
        _dispinterfaces_ = (SINK_IID,)
        _dispids_ = {1: "OnFoo", 2: "OnBar"}

        def __init__(self):
            self.calls = []

        def OnFoo(self, x):
            self.calls.append(("foo", x))

        def OnBar(self):
            self.calls.append(("bar",))

        def Other(self):
            return "other"

    sink = Sink()
    unknown = sink.QueryInterface(portacom.IUnknown)
    source = unknown.QueryInterface(portacom.GUID(SINK_IID))  # how an event source asks
    disp = unknown.QueryInterface(IDispatch)
    assert source.address == disp.address  # the IDispatch vtable answers the source IID
    d = Dispatch(source)
    d.invoke(1, "x")  # by DISPID, without GetIDsOfNames
    d.invoke(2)
    assert sink.calls == [("foo", "x"), ("bar",)]
    assert d.dispid("onfoo") == 1
    assert d.dispid("Other") not in (1, 2) and d.Other() == "other"
    del d, source, disp, unknown
    assert sink.references == 1


class Formatter(DispatchObject):
    def __init__(self):
        self.items = {}

    def Join(self, first, sep="-", last=""):
        return f"{first}{sep}{last}"

    def Item(self, key):
        return self.items[key]

    def put_Item(self, key, value):
        self.items[key] = value


def test_named_arguments():
    impl = Formatter()
    d = Dispatch(impl)
    assert d.invoke("Join", "a", named={"last": "z"}) == "a-z"
    assert d.Join("a", named={"SEP": "+", "Last": "z"}) == "a+z"  # names are case-insensitive
    assert d.Join(named={"first": "only"}) == "only-"
    d.invoke("Item", 5, named={"key": "k"}, flags=DISPATCH_PROPERTYPUT)  # an index by name, then the value
    assert impl.items == {"k": 5} and d.Item(named={"key": "k"}) == 5
    with pytest.raises(TypeError, match="'nope'"):
        d.Join("a", named={"sep": "+", "nope": 1})
    with pytest.raises(TypeError, match="DISPID"):
        d.invoke(d.dispid("Join"), named={"sep": "+"})
    with pytest.raises(COMError) as e:
        d.Join("a", named={"first": "b"})  # first, twice
    assert e.value.hresult == DISP_E_BADPARAMCOUNT


def test_named_argument_dispids():
    """One GetIDsOfNames call names the member, then its parameters, whose DISPIDs are apart from members'."""
    impl = Formatter()
    disp = impl.QueryInterface(IDispatch)
    names = (ctypes.c_wchar_p * 3)("Join", "last", "sep")
    ids = (ctypes.c_int32 * 3)()
    assert disp.raw.GetIDsOfNames(None, ctypes.addressof(names), 3, 0, ctypes.addressof(ids)) == 0
    member, last, sep = ids
    d = Dispatch(disp)
    assert member == d.dispid("Join") and len({member, last, sep}) == 3
    assert last not in (d.dispid("Item"), d.dispid("put_Item"))
    names[2] = "nope"
    assert disp.raw.GetIDsOfNames(None, ctypes.addressof(names), 3, 0, ctypes.addressof(ids)) == DISP_E_UNKNOWNNAME
    assert list(ids) == [member, last, DISPID_UNKNOWN]  # the known ones are still filled in

    # rgvarg holds the named arguments first, in rgdispidNamedArgs' order, then the positional ones
    args = (VARIANT * 2)(VARIANT("+"), VARIANT("a"))
    named = (ctypes.c_int32 * 1)(sep)
    params = DISPPARAMS(ctypes.cast(args, ctypes.POINTER(VARIANT)), named, 2, 1)
    result = VARIANT()
    hr = disp.raw.Invoke(
        member, None, 0, DISPATCH_METHOD, ctypes.addressof(params), ctypes.addressof(result), None, None
    )
    assert hr == 0 and result.value == "a+"
    result.clear()
    named[0] = 12345  # names no parameter
    argerr = ctypes.c_uint32(99)
    hr = disp.raw.Invoke(
        member, None, 0, DISPATCH_METHOD, ctypes.addressof(params), None, None, ctypes.addressof(argerr)
    )
    assert hr == DISP_E_PARAMNOTFOUND and argerr.value == 0
    for v in args:
        v.clear()


class PyEnum(ComObject):
    """IEnumVARIANT written against the raw signature."""

    _interfaces_ = (IEnumVARIANT,)

    def __init__(self, items):
        self.items = list(items)
        self.position = 0

    def Next(self, celt, rgVar, pCeltFetched):
        chunk = self.items[self.position : self.position + celt]
        self.position += len(chunk)
        for k, item in enumerate(chunk):
            v = VARIANT.from_value(item)
            ctypes.memmove(rgVar + k * ctypes.sizeof(VARIANT), ctypes.byref(v), ctypes.sizeof(VARIANT))
        if pCeltFetched:
            ctypes.c_uint32.from_address(pCeltFetched).value = len(chunk)
        if len(chunk) < celt:
            raise COMError(portacom.S_FALSE)

    def Reset(self):
        self.position = 0


def test_iteration():
    class Collection(DispatchObject):
        def __init__(self, enum):
            self.enum = enum

        def _NewEnum(self):
            return self.enum(["a", 2, None])

    assert list(Dispatch(Collection(PyEnum))) == ["a", 2, None]
    assert list(Dispatch(Collection(EnumVARIANT))) == ["a", 2, None]
    assert list(Dispatch(Collection(list))) == ["a", 2, None]  # any iterable becomes an EnumVARIANT
    d = Dispatch(Collection(PyEnum))
    assert d.dispid("_NewEnum") == DISPID_NEWENUM


def test_not_dispatch():
    class Plain(ComObject):
        _interfaces_ = (IEnumVARIANT,)

    with pytest.raises(COMError) as e:
        Dispatch(Plain())
    assert e.value.hresult == portacom.E_NOINTERFACE
    with pytest.raises(COMError):
        Dispatch(Plain().QueryInterface(IEnumVARIANT))


# -- real automation servers -------------------------------------------------------------------

CLSID_Dictionary = "EE09B103-97E0-11CF-978F-00A02463E06F"
CLSID_WshShell = "72C24DD5-D70A-438B-8A42-98424B88AFB8"


@windows_only
def test_scripting_dictionary():
    d = Dispatch(portacom.co_create_instance(CLSID_Dictionary, IDispatch))
    d.Add("a", 1)
    d.Add("b", "two")
    assert d.Count() == 2 and d.get("Count") == 2
    assert d.Item("a") == 1 and d.get("Item", "b") == "two"
    d.set("Item", "a", 10)
    assert d.Item("a") == 10
    assert d.Exists("a") is True and d.Exists("zzz") is False
    assert d.Keys() == ["a", "b"]  # a VARIANT array, through the SAFEARRAY support
    assert d.Items() == [10, "two"]
    with pytest.raises(COMError) as e:
        d.Add("a", 3)
    assert e.value.hr == 0x800A01C9  # "This key is already associated with an element of this collection"
    assert e.value.what == "Add"
    assert list(d) == ["a", "b"]
    d.invoke("Add", named={"Key": "c", "Item": 3})  # named arguments, as the type library names them
    assert d.Item("c") == 3


@windows_only
def test_dispatch_object_round_trip_through_dictionary():
    impl = Counter()
    d = Dispatch(portacom.co_create_instance(CLSID_Dictionary, IDispatch))
    d.Add("counter", impl)
    stored = d.Item("counter")
    assert isinstance(stored, Dispatch)
    assert stored.Add(5) == 5 and impl.count == 5
    del stored
    d.RemoveAll()
    del d
    assert impl.references == 1


@windows_only
def test_wscript_shell():
    shell = Dispatch(portacom.co_create_instance(CLSID_WshShell, IDispatch))
    env = shell.Environment("PROCESS")  # read only
    assert isinstance(env, Dispatch)
    assert env.Item("OS") == "Windows_NT"
    assert shell.ExpandEnvironmentStrings(named={"Src": "%OS%"}) == "Windows_NT"  # a named argument
    with pytest.raises(COMError) as e:
        shell.RegRead("HKCU\\Software\\portacom-missing-key-for-tests\\value")  # read only
    assert e.value.hr == 0x80070002
    assert e.value.source
