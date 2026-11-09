"""IDispatch: late-bound automation, in both directions.

The types are the runtime's own, as VARIANT is, so that automation works
without generated bindings:

    d = Dispatch(portacom.co_create_instance(clsid, IDispatch))
    d.Add("key", 1)                     # a method
    d.get("Count"), d.Count()           # a property
    d.set("Item", "key", 2)             # an indexed property put
    d.invoke("Run", cmd, named={"WaitOnReturn": True})  # named arguments
    for key in d: ...                   # DISPID_NEWENUM

and a Python object that automation clients (VBScript, event sources) call:

    class Sink(DispatchObject):
        _dispinterfaces_ = ("...",)     # the source interface it answers to
        _dispids_ = {1: "OnFoo"}

        def OnFoo(self, x): ...
"""

from __future__ import annotations

import ctypes
import functools
import inspect
import threading

from ._errors import (
    E_FAIL,
    E_NOINTERFACE,
    E_POINTER,
    S_FALSE,
    S_OK,
    COMError,
    report_callback_error,
    signed32,
)
from ._interface import IUnknown
from ._marshal import OUT, Param
from ._platform import FUNCTYPE
from ._server import ComObject
from ._types import GUID, HRESULT, Ptr
from ._variant import VARIANT

DISPATCH_METHOD, DISPATCH_PROPERTYGET, DISPATCH_PROPERTYPUT, DISPATCH_PROPERTYPUTREF = 1, 2, 4, 8
DISPID_VALUE, DISPID_UNKNOWN, DISPID_PROPERTYPUT, DISPID_NEWENUM = 0, -1, -3, -4
DISP_E_MEMBERNOTFOUND = signed32(0x80020003)
DISP_E_UNKNOWNNAME = signed32(0x80020006)
DISP_E_PARAMNOTFOUND = signed32(0x80020004)
DISP_E_NONAMEDARGS = signed32(0x80020007)
DISP_E_EXCEPTION = signed32(0x80020009)
DISP_E_BADPARAMCOUNT = signed32(0x8002000E)
LOCALE_USER_DEFAULT = 0x400
_PARAMETER_DISPIDS = 0x40000000  # parameter names' DISPIDs, apart from members'

_IID_NULL = GUID("00000000-0000-0000-0000-000000000000")
_VOID = Ptr(None)


class DISPPARAMS(ctypes.Structure):
    _fields_ = [
        ("rgvarg", ctypes.POINTER(VARIANT)),
        ("rgdispidNamedArgs", ctypes.POINTER(ctypes.c_int32)),
        ("cArgs", ctypes.c_uint32),
        ("cNamedArgs", ctypes.c_uint32),
    ]


class EXCEPINFO(ctypes.Structure):
    _fields_ = [
        ("wCode", ctypes.c_uint16),
        ("wReserved", ctypes.c_uint16),
        ("bstrSource", ctypes.c_void_p),  # BSTRs
        ("bstrDescription", ctypes.c_void_p),
        ("bstrHelpFile", ctypes.c_void_p),
        ("dwHelpContext", ctypes.c_uint32),
        ("pvReserved", ctypes.c_void_p),
        ("pfnDeferredFillIn", ctypes.c_void_p),
        ("scode", ctypes.c_int32),
    ]


class IDispatch(IUnknown):
    _iid_ = GUID("00020400-0000-0000-C000-000000000046")
    _methods_ = ("GetTypeInfoCount", "GetTypeInfo", "GetIDsOfNames", "Invoke")


# Raw pointers where the marshaller would get in the way: a failed Invoke still
# fills EXCEPINFO, which the marshaller's NULLing of [out]s would lose
IDispatch._signatures_ = lambda: (
    (HRESULT, (Param("pctinfo", Ptr(ctypes.c_uint32), OUT),)),
    (HRESULT, (Param("iTInfo", ctypes.c_uint32), Param("lcid", ctypes.c_uint32), Param("ppTInfo", _VOID))),
    (
        HRESULT,
        (
            Param("riid", _VOID),
            Param("rgszNames", _VOID),
            Param("cNames", ctypes.c_uint32),
            Param("lcid", ctypes.c_uint32),
            Param("rgDispId", _VOID),
        ),
    ),
    (
        HRESULT,
        (
            Param("dispIdMember", ctypes.c_int32),
            Param("riid", _VOID),
            Param("lcid", ctypes.c_uint32),
            Param("wFlags", ctypes.c_uint16),
            Param("pDispParams", _VOID),
            Param("pVarResult", _VOID),
            Param("pExcepInfo", _VOID),
            Param("puArgErr", _VOID),
        ),
    ),
)


class IEnumVARIANT(IUnknown):
    _iid_ = GUID("00020404-0000-0000-C000-000000000046")
    _methods_ = ("Next", "Skip", "Reset", "Clone")


# Next returns S_FALSE at the end, which callers must see: raw pointers here too
IEnumVARIANT._signatures_ = lambda: (
    (HRESULT, (Param("celt", ctypes.c_uint32), Param("rgVar", _VOID), Param("pCeltFetched", _VOID))),
    (HRESULT, (Param("celt", ctypes.c_uint32),)),
    (HRESULT, ()),
    (HRESULT, (Param("ppEnum", Ptr(Ptr(IEnumVARIANT)), OUT),)),
)


def _variant_for(obj):
    """The VARIANT type of obj's bindings: its Abi's, else the default."""
    abi = getattr(type(obj), "_abi_", None)
    return abi.VARIANT if abi is not None else VARIANT


def _wrap(value, variant):
    """value, or a Dispatch if it is an interface that implements IDispatch."""
    if isinstance(value, IUnknown) and value:
        disp = value if isinstance(value, IDispatch) else value.query(IDispatch)
        if disp is not None:
            return Dispatch._adopt(disp, variant)
    return value


def _take(v, variant):
    """The Python value of a VARIANT result, which is then cleared."""
    value = v.value
    if value is v:  # a type with no conversion: move it out, uncleared
        moved = variant()
        ctypes.memmove(ctypes.byref(moved), ctypes.byref(v), ctypes.sizeof(VARIANT))
        v.vt = 0
        return moved
    v.clear()
    return _wrap(value, variant)


# -- calling ---------------------------------------------------------------------------


class Dispatch:
    """An automation object, called by name.

    d.invoke(name, *args) calls a method or reads a property, and
    named={"Param": value} adds named arguments, resolved in the same
    GetIDsOfNames call as the name.  get() and set() are property get and put
    (set's last argument is the value); d.Name(...) is invoke("Name", ...),
    so a property also reads as d.Count().  d(...) invokes DISPID_VALUE, and
    iterating uses DISPID_NEWENUM.

    Unknown names raise AttributeError; failures raise COMError, with the
    description and source of the object's EXCEPINFO.  Interfaces that
    implement IDispatch come back as Dispatch.
    """

    def __init__(self, obj, abi=None):
        if isinstance(obj, Dispatch):
            obj = obj.interface
        variant = abi.VARIANT if abi is not None else _variant_for(obj)
        if isinstance(obj, ComObject):
            pointer = obj._com_query_(IDispatch._iid_)
            if pointer is None:
                raise COMError(E_NOINTERFACE, f"{type(obj).__name__}.QueryInterface(IDispatch)")
            disp = IDispatch(pointer)
        elif isinstance(obj, IUnknown):
            disp = obj.QueryInterface(IDispatch)
        else:
            raise TypeError(f"Dispatch() takes an interface or a ComObject, not {type(obj).__name__}")
        self._init(disp, variant)

    @classmethod
    def _adopt(cls, disp, variant):
        self = cls.__new__(cls)
        self._init(disp, variant)
        return self

    def _init(self, disp, variant):
        d = self.__dict__  # __getattr__ must not see these missing
        d["interface"] = disp  # the IDispatch, which holds the reference
        d["_variant"] = variant
        d["_dispids"] = {}

    def release(self):
        self.interface.release()

    def dispid(self, name: str) -> int:
        """The DISPID of a member name (GetIDsOfNames, cached)."""
        return self._ids_of(name)[0]

    def _ids_of(self, name, params=()):
        """DISPIDs of a member and of its parameters' names, in one
        GetIDsOfNames call (cached, case-insensitively, as automation names are)."""
        key = (name.lower(), *(p.lower() for p in params)) if params else name.lower()
        ids = self._dispids.get(key)
        if ids is None:
            count = 1 + len(params)
            names = (ctypes.c_wchar_p * count)(name, *params)
            out = (ctypes.c_int32 * count)(*[DISPID_UNKNOWN] * count)
            hr = self.interface.raw.GetIDsOfNames(
                ctypes.addressof(_IID_NULL), ctypes.addressof(names), count, LOCALE_USER_DEFAULT, ctypes.addressof(out)
            )
            if hr == DISP_E_UNKNOWNNAME:
                if out[0] == DISPID_UNKNOWN:
                    e = AttributeError(f"{self!r} has no member {name!r}")
                    e.hresult = hr
                    raise e
                unknown = [p for p, dispid in zip(params, out[1:], strict=True) if dispid == DISPID_UNKNOWN]
                raise TypeError(f"{name}() has no parameter named {', '.join(map(repr, unknown or params))}")
            if hr < 0:
                raise COMError(hr, f"GetIDsOfNames({name!r})")
            ids = self._dispids[key] = tuple(out)
        return ids

    def invoke(self, member, *args, named=None, flags=DISPATCH_METHOD | DISPATCH_PROPERTYGET):
        """Invoke a member by name or DISPID with positional arguments, and
        named ones as named={"Name": value} (which need the member's name)."""
        if named:
            if not isinstance(member, str):
                raise TypeError("named arguments need the member's name, not a DISPID")
            dispid, *named_ids = self._ids_of(member, tuple(named))
            named_values = list(named.values())
        else:
            dispid = member if isinstance(member, int) else self.dispid(member)
            named_ids, named_values = [], []
        what = member if isinstance(member, str) else f"DISPID {member}"
        put = flags & (DISPATCH_PROPERTYPUT | DISPATCH_PROPERTYPUTREF)
        positional = list(args)
        if put:
            if not positional:
                raise TypeError("a property put needs a value")
            # the value is named DISPID_PROPERTYPUT, first
            named_ids.insert(0, DISPID_PROPERTYPUT)
            named_values.insert(0, positional.pop())
        # rgvarg: the named arguments, in rgdispidNamedArgs' order, then the positional ones last first
        values = named_values + positional[::-1]
        variant = self._variant
        argv = (variant * max(len(values), 1))()
        result = variant()
        excepinfo = EXCEPINFO()
        argerr = ctypes.c_uint32()
        ids = (ctypes.c_int32 * max(len(named_ids), 1))(*named_ids)
        try:
            for k, value in enumerate(values):
                argv[k].set(value)
            params = DISPPARAMS(
                ctypes.cast(argv, ctypes.POINTER(VARIANT)) if values else None,
                ctypes.cast(ids, ctypes.POINTER(ctypes.c_int32)) if named_ids else None,
                len(values),
                len(named_ids),
            )
            hr = self.interface.raw.Invoke(
                dispid,
                ctypes.addressof(_IID_NULL),
                LOCALE_USER_DEFAULT,
                flags,
                ctypes.addressof(params),
                None if put else ctypes.addressof(result),
                ctypes.addressof(excepinfo),
                ctypes.addressof(argerr),
            )
        finally:
            for v in argv:
                v.clear()
        if hr < 0:
            result.clear()
            if hr == DISP_E_EXCEPTION:
                raise self._exception(excepinfo, what)
            raise COMError(hr, what)
        return _take(result, variant)

    def _exception(self, excepinfo, what):
        if excepinfo.pfnDeferredFillIn:
            FUNCTYPE(ctypes.c_int32, ctypes.c_void_p)(excepinfo.pfnDeferredFillIn)(ctypes.addressof(excepinfo))
        allocator = self._variant._allocator()
        texts = []
        for field in ("bstrDescription", "bstrSource", "bstrHelpFile"):
            address = getattr(excepinfo, field)
            texts.append(allocator.read_bstr(address) if address else None)
            if address:
                allocator.free_bstr(address)
        description, source, _ = texts
        hr = excepinfo.scode
        if not hr:  # a wCode is a Visual Basic error number, which VB writes as 0x800Annnn
            hr = signed32(0x800A0000 | excepinfo.wCode) if excepinfo.wCode else DISP_E_EXCEPTION
        return COMError(hr, what, description.strip() if description else None, source)

    def get(self, name, *index):
        """A property's value (DISPATCH_PROPERTYGET)."""
        return self.invoke(name, *index, flags=DISPATCH_PROPERTYGET)

    def set(self, name, *args):
        """set(name, *index, value): DISPATCH_PROPERTYPUT."""
        if not args:
            raise TypeError("set() needs a value")
        self.invoke(name, *args, flags=DISPATCH_PROPERTYPUT)

    def set_ref(self, name, *args):
        """set_ref(name, *index, value): DISPATCH_PROPERTYPUTREF, for objects."""
        if not args:
            raise TypeError("set_ref() needs a value")
        self.invoke(name, *args, flags=DISPATCH_PROPERTYPUTREF)

    def __getattr__(self, name):
        if name.startswith("_"):  # Python's own probes (__len__, _repr_html_, ...)
            raise AttributeError(name)
        self.dispid(name)  # AttributeError now, so that hasattr() works
        return functools.partial(self.invoke, name)

    def __call__(self, *args):
        return self.invoke(DISPID_VALUE, *args)

    def __iter__(self):
        enum = self.invoke(DISPID_NEWENUM)
        if isinstance(enum, Dispatch):
            enum = enum.interface
        if not isinstance(enum, IUnknown) or not enum:
            raise TypeError(f"{self!r} returned no enumerator")
        enum = enum.QueryInterface(IEnumVARIANT)
        while True:
            v = self._variant()
            fetched = ctypes.c_uint32()
            hr = enum.raw.Next(1, ctypes.addressof(v), ctypes.addressof(fetched))
            if hr < 0:
                v.clear()
                raise COMError(hr, "IEnumVARIANT.Next")
            if not fetched.value:
                v.clear()
                return
            yield _take(v, self._variant)

    def __repr__(self):
        return f"<Dispatch 0x{self.interface.address:x}>" if self.interface else "<Dispatch NULL>"


# -- implementing ----------------------------------------------------------------------


class EnumVARIANT(ComObject):
    """IEnumVARIANT over a Python iterable, for a DispatchObject's _NewEnum."""

    _interfaces_ = (IEnumVARIANT,)

    def __init__(self, items, variant=VARIANT):
        self._items = list(items)
        self._position = 0
        self._variant = variant

    def Next(self, celt, rgVar, pCeltFetched):
        if not rgVar:
            raise COMError(E_POINTER)
        chunk = self._items[self._position : self._position + celt]
        self._position += len(chunk)
        size = ctypes.sizeof(VARIANT)
        for k, item in enumerate(chunk):
            fresh = self._variant.from_value(item)
            ctypes.memmove(rgVar + k * size, ctypes.byref(fresh), size)
        if pCeltFetched:
            ctypes.c_uint32.from_address(pCeltFetched).value = len(chunk)
        if len(chunk) < celt:
            raise COMError(S_FALSE)  # the way a Python method returns a success code other than S_OK

    def Skip(self, celt):
        self._position += celt
        if self._position > len(self._items):
            self._position = len(self._items)
            raise COMError(S_FALSE)

    def Reset(self):
        self._position = 0

    def Clone(self):
        clone = EnumVARIANT(self._items, self._variant)
        clone._position = self._position
        return clone


def _dispatch_interface(dispinterfaces):
    """The IDispatch class to implement: one that also answers the extra IIDs.

    _ComState maps every IID in an interface's MRO to its vtable, so IDispatch
    subclasses with those IIDs and no methods share the IDispatch vtable.
    """
    bases = []
    for iid in dispinterfaces:
        iid = iid._iid_ if isinstance(iid, type) else GUID(iid)
        bases.append(type(f"IDispatch_{iid}", (IDispatch,), {"_iid_": iid, "_portacom_dispatch_": True}))
    if len(bases) < 2:
        return bases[0] if bases else IDispatch
    return type("IDispatch_multi", tuple(bases), {})


class DispatchObject(ComObject):
    """A COM object that automation clients call by name, through IDispatch.

    Public methods are methods, public attributes and properties are
    properties, and a put_Name(*index, value) method handles an (indexed)
    property put.  DISPIDs are given out on first GetIDsOfNames, matching
    names case-insensitively; _dispids_ fixes some ({1: "OnFoo"}), for event
    sources that invoke by DISPID.  _NewEnum, if defined, is DISPID_NEWENUM.
    Callers can also name a method's arguments: GetIDsOfNames answers for its
    parameter names, with DISPIDs apart from the members'.

    _dispinterfaces_ lists IIDs (or interface classes) that QueryInterface
    answers with the IDispatch vtable, such as an event source's.  Exceptions
    reach the caller through EXCEPINFO as DISP_E_EXCEPTION.
    """

    _dispinterfaces_: tuple = ()
    _dispids_: dict = {}
    _abi_ = None  # whose VARIANTs and BSTRs the object hands out
    _interfaces_ = (IDispatch,)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        others = tuple(
            i for i in cls._interfaces_ if not getattr(i, "_portacom_dispatch_", False) and i is not IDispatch
        )
        disp = _dispatch_interface(cls._dispinterfaces_)
        cls._interfaces_ = (disp, *others)

    def _dispatch_state_(self):
        """(lock, {DISPID: member}, {lower-case member: DISPID}, {lower-case parameter: DISPID})"""
        state = self.__dict__.get("_dispatch_")
        if state is None:
            ids = {dispid: name for dispid, name in type(self)._dispids_.items()}
            ids.setdefault(DISPID_NEWENUM, "_NewEnum")
            by_name = {n.lower(): i for i, n in ids.items()}
            state = self.__dict__["_dispatch_"] = (threading.Lock(), ids, by_name, {})
        return state

    def _variant_(self):
        return self._abi_.VARIANT if self._abi_ is not None else VARIANT

    # IDispatch

    def GetTypeInfoCount(self):
        return 0

    def GetTypeInfo(self, iTInfo, lcid, ppTInfo):
        if ppTInfo:
            ctypes.c_void_p.from_address(ppTInfo).value = None
        raise NotImplementedError

    def GetIDsOfNames(self, riid, rgszNames, cNames, lcid, rgDispId):
        if not rgszNames or not rgDispId:
            raise COMError(E_POINTER)
        names = (ctypes.c_wchar_p * cNames).from_address(rgszNames)
        ids = (ctypes.c_int32 * cNames).from_address(rgDispId)
        for k in range(cNames):
            ids[k] = DISPID_UNKNOWN
        dispid = self._dispid_for(names[0] or "")
        if dispid is None:
            raise COMError(DISP_E_UNKNOWNNAME)
        ids[0] = dispid
        if cNames > 1:  # the names of the member's parameters, for named arguments
            known = self._parameters_(self._dispatch_state_()[1][dispid])
            missing = False
            for k in range(1, cNames):
                parameter = known.get((names[k] or "").lower())
                if parameter is None:
                    missing = True
                else:
                    ids[k] = self._parameter_dispid(parameter)
            if missing:
                raise COMError(DISP_E_UNKNOWNNAME)

    def _parameters_(self, member, put=False):
        """{lower-case name: name} of the parameters a member takes by name:
        the method's, or put_Member's for a put (or a member that is not a method)."""
        call = getattr(self, member, None)
        if put or not callable(call) or isinstance(call, _VALUES):
            call = getattr(self, f"put_{member}", None)
        try:
            signature = inspect.signature(call)
        except (TypeError, ValueError):
            return {}
        kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        return {p.name.lower(): p.name for p in signature.parameters.values() if p.kind in kinds}

    def _parameter_dispid(self, parameter):
        """A parameter name's DISPID, the same for every member that has one by that name."""
        lock, _, _, parameters = self._dispatch_state_()
        key = parameter.lower()
        with lock:
            dispid = parameters.get(key)
            if dispid is None:
                dispid = parameters[key] = _PARAMETER_DISPIDS + len(parameters)
            return dispid

    def _dispid_for(self, name):
        lock, ids, by_name, _ = self._dispatch_state_()
        key = name.lower()
        with lock:
            dispid = by_name.get(key)
            if dispid is not None:
                return dispid
            members = [n for n in dir(self) if n.lower() == key and not n.startswith("_") and n not in _RESERVED]
            if not members:
                return None
            member = name if name in members else members[0]
            dispid = max([1000, *(i + 1 for i in ids)])  # after any fixed ones
            ids[dispid] = member
            by_name[key] = dispid
            return dispid

    def Invoke(self, dispIdMember, riid, lcid, wFlags, pDispParams, pVarResult, pExcepInfo, puArgErr):
        hr = self._invoke(dispIdMember, wFlags, pDispParams, pVarResult, pExcepInfo, puArgErr)
        if hr != S_OK:
            raise COMError(hr)  # how a Python method returns an HRESULT

    def _invoke(self, dispid, flags, params, result, excepinfo, argerr):
        if not params:
            return E_POINTER
        _, ids, _, parameters = self._dispatch_state_()
        name = ids.get(dispid)
        if name is None:
            return DISP_E_MEMBERNOTFOUND
        variant = self._variant_()
        dp = DISPPARAMS.from_address(params)
        if dp.cNamedArgs > dp.cArgs:
            return DISP_E_BADPARAMCOUNT
        base = ctypes.cast(dp.rgvarg, ctypes.c_void_p).value
        size = ctypes.sizeof(VARIANT)
        # rgvarg holds the named arguments first, then the positional ones, last first;
        # VT_BYREF arguments read through their pointer
        values = [_wrap(variant.from_address(base + k * size).value, variant) for k in range(dp.cArgs)]
        named = [dp.rgdispidNamedArgs[k] for k in range(dp.cNamedArgs)]
        args = values[len(named) :][::-1]
        put = flags & (DISPATCH_PROPERTYPUT | DISPATCH_PROPERTYPUTREF)
        kwargs = {}
        if named:
            names = {dispid: key for key, dispid in parameters.items()}
            known = self._parameters_(name, put)
            for k, named_dispid in enumerate(named):
                if put and named_dispid == DISPID_PROPERTYPUT:
                    args.append(values[k])  # the value, after any index
                    continue
                parameter = known.get(names.get(named_dispid, ""))
                if parameter is None:
                    if argerr:
                        ctypes.c_uint32.from_address(argerr).value = k
                    return DISP_E_PARAMNOTFOUND
                kwargs[parameter] = values[k]
        try:
            if put:
                if not args:
                    return DISP_E_BADPARAMCOUNT
                setter = getattr(self, f"put_{name}", None)
                if setter is not None:
                    call, call_args = setter, args
                    if kwargs:  # an index passed by name: the value goes by name too, to the last parameter
                        call_args = args[:-1]
                        kwargs[list(inspect.signature(setter).parameters)[-1]] = args[-1]
                elif len(args) == 1 and not callable(getattr(self, name, None)):
                    call, call_args = functools.partial(setattr, self, name), args
                else:
                    return DISP_E_MEMBERNOTFOUND
            else:
                member = getattr(self, name, _MISSING)
                if member is _MISSING:
                    return DISP_E_MEMBERNOTFOUND
                if not callable(member) or isinstance(member, _VALUES):  # an attribute, or a property already read
                    if args:
                        return DISP_E_BADPARAMCOUNT
                    call, call_args = (lambda: member), ()
                else:
                    call, call_args = member, args
            if not _arity_ok(call, call_args, kwargs):
                if argerr:
                    ctypes.c_uint32.from_address(argerr).value = 0
                return DISP_E_BADPARAMCOUNT
            value = call(*call_args, **kwargs)
            if dispid == DISPID_NEWENUM and not isinstance(value, _VALUES):
                value = EnumVARIANT(value, variant)  # any iterable
        except BaseException as e:
            return self._exception(e, name, excepinfo, variant)
        if result and not put:
            fresh = variant.from_value(value)
            ctypes.memmove(result, ctypes.byref(fresh), size)
        return S_OK

    def _exception(self, e, name, excepinfo, variant):
        """Fill EXCEPINFO for an exception from a member; returns the HRESULT."""
        source = type(self).__name__
        if isinstance(e, COMError):
            scode, description, source = e.hresult, e.description or str(e), e.source or source
        else:  # unexpected, like any Python COM method's: the handler sees it too
            report_callback_error(self, name, e)
            scode, description = E_FAIL, str(e) or type(e).__name__
        if not excepinfo:
            return scode
        ctypes.memset(excepinfo, 0, ctypes.sizeof(EXCEPINFO))
        info = EXCEPINFO.from_address(excepinfo)
        allocator = variant._allocator()
        info.scode = scode
        info.bstrDescription = allocator.alloc_bstr(description)
        info.bstrSource = allocator.alloc_bstr(source)
        return DISP_E_EXCEPTION


_MISSING = object()
_VALUES = (Dispatch, ComObject, IUnknown)  # callable, but values here
_RESERVED = frozenset(dir(DispatchObject))  # the plumbing, not automation members


def _arity_ok(call, args, kwargs):
    try:
        signature = inspect.signature(call)
    except (TypeError, ValueError):  # builtins without one: let the call decide
        return True
    try:
        signature.bind(*args, **kwargs)
    except TypeError:
        return False
    return True
