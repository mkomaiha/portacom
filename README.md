# portacom

Call classic COM interfaces from Python on **Windows, Linux and macOS**, and
implement them in Python, using bindings generated from a `.winmd` file.

| Package | What it is |
|---|---|
| [`portacom`](runtime) | The runtime: vtable calls through ctypes, argument conversion, COM objects in Python. Pure Python, no dependencies. |
| [`winmd2py`](generator) | The generator: `.winmd` (Win32 metadata format) → Python modules. Pure Python, no .NET. |
| [`portacom-dxc`](dxc) | Ready-made bindings for the DirectX Shader Compiler, plus a small `compile()` API. |

It targets IUnknown-based COM, such as
[DirectX Shader Compiler](https://github.com/microsoft/DirectXShaderCompiler)'s
`libdxcompiler.so`, [MiniCOM](https://github.com/forderud/MiniCOM) libraries,
and Win32 COM APIs on Windows. It does not cover WinRT.

## Quick start: DXC

```bash
pip install ./runtime ./dxc
```

```python
import portacom_dxc

result = portacom_dxc.compile(
    '#include "common.hlsli"\nfloat4 main() : SV_Target { return Tint(); }',
    "ps_6_0",
    includes={"common.hlsli": "float4 Tint() { return 1; }"},
)
dxil = result.object  # bytes; also .pdb, .reflection, .hash, .errors
print(portacom_dxc.disassemble(dxil))
```

`portacom_dxc` looks for DXC in `$DXC_LIBRARY`, the library search path,
`$VULKAN_SDK` and the Windows SDK, in that order. You can also call
`portacom_dxc.load(path)`. For the full COM API, use `portacom_dxc.dxc`.

## Your own interfaces

Generate bindings once, from the `.winmd` that describes your interfaces
(see [Making a .winmd](#making-a-winmd-for-your-own-idl)). For Windows APIs,
[Microsoft.Windows.SDK.Win32Metadata](https://www.nuget.org/packages/Microsoft.Windows.SDK.Win32Metadata)
ships `Windows.Win32.winmd`.

```bash
pip install ./generator
winmd2py --list MyInterfaces.winmd
winmd2py MyInterfaces.winmd -o my_bindings
```

A `.winmd` that uses types from another one passes it with `--ref`, as
`Microsoft.Direct3D.D3D12.winmd` does with `Windows.Win32.winmd`:
`winmd2py Microsoft.Direct3D.D3D12.winmd --ref Windows.Win32.winmd -o d3d12`.
Types that differ by architecture (x86, x64, ARM64) are declared once and
chosen when the bindings are imported.

Each namespace becomes a module, `my_bindings/<Namespace>/__init__.py`. The
bindings follow the Windows ABI. Declare the library behind them, and how it
differs when built for Linux and macOS (see [ABI profiles](#abi-profiles)),
in `my_bindings/__init__.py`, which winmd2py creates once and never
overwrites:

```python
# my_bindings/__init__.py (written once by winmd2py; yours to edit)
import ctypes, portacom

portacom.use_abi(
    __name__,
    portacom.Abi(
        "minicom",
        library="ExampleLib.dll",
        typedefs={"BOOL": ctypes.c_long},
        allocator=portacom.PlainAllocator(),
    ),
)
```

On Windows the metadata is the truth, so this changes nothing there. The
same code then runs everywhere:

```python
import portacom
import my_bindings.Example as example
import my_bindings.Registry as api

registry = portacom.co_create_instance(api.Registry, api.IRegistry)
calc = registry.NewCalculator()  # [out] arguments are returned
print(calc.Add(1, 2))  # 3
print(calc.Add.__doc__)  # ICalcExt.Add(a, b) -> result


class Callback(portacom.ComObject):  # implement an interface in Python
    _interfaces_ = (example.ICalcCb,)

    def Message(self, text):
        print("C++ says", text)


calc.SetCallback(Callback())
calc.QueryInterface(example.ICalc2).GetValue2()  # 43; C++ calls back: "C++ says GetValue2 called"
```

### Making a .winmd for your own IDL

- **From IDL:** MiniCOM's `generate_winmd()` CMake function runs MIDL, then
  ClangSharp, into a `.winmd`. It runs on Windows only and needs the .NET SDK;
  see [MiniCOM](https://github.com/forderud/MiniCOM). The `.winmd` then
  works on any platform.
- **From C headers:** Microsoft's
  [win32metadata](https://github.com/microsoft/win32metadata) pipeline
  (ClangSharp, packaged as the `Microsoft.Windows.WinmdGenerator` MSBuild SDK)
  is how `Windows.Win32.winmd` is made.

What the metadata says is what portacom converts, so annotate for it:
`[in]`/`[out]` on every parameter; `NativeArrayInfo` (`CountParamIndex` or
`CountConst`) on a counted array; `MemorySize` (`BytesParamIndex`) on a
buffer sized in bytes; `NativeTypedef` types (or references to
`Windows.Win32.Foundation`'s) for `BOOL`, `BSTR`, `PWSTR` and the like, since
`use_abi()` typedefs match by name; and `SupportedArchitecture` on what
differs by architecture. A `REFIID riid, void** ppv` pair is recognised by
its shape (a `void**` `[out]` and a GUID pointer named like `riid`), with or
without `ComOutPtr`.

## Using the bindings

**Calling.** `obj.Method(...)` takes the `[in]` arguments and returns the
`[out]` ones: nothing, one value, or a tuple.
- Interfaces come back as objects that release their reference when garbage
  collected.
- A `REFIID riid, void** ppv` pair takes an interface class and returns that
  interface.
- Arrays and buffers sized by another parameter take a list or `bytes`, and the
  size is filled in for you. An `[out]` buffer sized in bytes by an `[in]`
  parameter (`IStream.Read(cb)`) comes back as `bytes`.
- A `SAFEARRAY*` takes a list (nested for several dimensions), or `bytes`,
  `bytearray`, `memoryview` or `array.array`, and a `SAFEARRAY**` comes back
  as a list, or as `bytes` for one dimension of `VT_UI1`/`VT_I1`. Numbers and
  bytes are copied in bulk, so a 1 MB frame crosses in about a millisecond.
  The element type is inferred, or chosen with
  `portacom.SafeArray(values, portacom.VT_R4)`. A `VARIANT` holding an array
  converts the same way.
- **Callbacks.** A function pointer (delegate) parameter takes a Python
  callable: `api.EnumWindows(lambda hwnd, lparam: True, 0)`. Its arguments
  and result convert as a `ComObject` method's do; an exception goes to
  `portacom.set_callback_error_handler()` and C gets 0 (or the HRESULT). A
  function pointer from C, in a struct field or a result, is a delegate you
  can call: `wc.lpfnWndProc(hwnd, msg, wparam, lparam)`.
- **Callback lifetime.** A callable passed as an argument lives for the call.
  When C keeps the pointer (a `WNDPROC` in `WNDCLASSW`, a timer), make the
  delegate yourself and keep it as long as C may call it:
  `proc = api.WNDPROC(my_func)`; `wc.lpfnWndProc = proc` stores its address.
- BSTRs, VARIANTs, GUIDs, enums and VARIANT_BOOL convert to and from `str`,
  Python values, `GUID`, enum members and `bool`. BSTR lengths count UTF-16
  code units on Windows, so `"é🙂"` survives a round trip.
- Struct return values work, including MSVC's hidden-pointer convention on
  Windows.
- `obj.Method.with_hresult(...)` also returns the HRESULT, for `S_FALSE`.
- `obj.raw.Method(...)`, and `function.raw(...)` for an exported function,
  are the plain C signature with ctypes arguments.

**Strings you free.** An `[out]` `PWSTR` or `PSTR` comes back as a
`portacom.PWSTR`/`PSTR` that keeps its address (`str(p)` reads it), because
only the API's documentation says who frees it. When the caller does
(usually with `CoTaskMemFree`), call `portacom.free_task_memory(p, api)`,
where `api` is the generated module (or its `Abi`), so that the library's
allocator frees it. A BSTR struct field reads as a `portacom.BSTRBase`
(`str()`, `.free()`); make one with `api._abi.BSTR.alloc(text)`. A
`SAFEARRAY*` struct field reads as its address: `api._abi.SAFEARRAY.take(s.Field)`
returns its values and destroys it with the library's allocator (`.read()`
leaves it to you), and `api._abi.BSTR.take(s.Name)` does the same for a BSTR.

**Interfaces.** `obj.QueryInterface(IOther)` raises `COMError` if the object
doesn't implement the interface; `obj.query(IOther)` returns `None` instead.
Both also take an IID (a `GUID` or string) and then return an `IUnknown`.
`obj.release()` releases the reference early; the object then tests false,
and keeps its hash. For an interface of your own `ComObject`, use
`impl.QueryInterface(IFoo)`, not `IFoo(impl)`.

**Errors.** A failed HRESULT raises `portacom.COMError`, which has `.hresult`
(signed), `.hr` (unsigned), and on Windows the `.description` the object
reported through IErrorInfo. `portacom.check(hr, "what")` raises it for an
HRESULT you got another way (from `.raw`, say), and returns `hr` otherwise.

**Implementing.** Subclass `portacom.ComObject`, list the interfaces in
`_interfaces_`, and write the methods with the same Python arguments and
results that callers use.
- `raise COMError(hr, description=...)` returns that HRESULT.
- A missing method returns E_NOTIMPL.
- Any other exception returns E_FAIL and goes to
  `portacom.set_callback_error_handler()`. It never reaches the C caller.
- The object stays alive while C holds references to it.

**Creating objects.**
- **A factory the library exports**, such as `DxcCreateInstance`: call the
  generated function.
- **Any export shaped `HRESULT f(REFCLSID, REFIID, void**)`**:
  `portacom.create_instance(clsid, IFoo, ("libfoo.so", "Export"))`.
- **A Python callable**: `portacom.create_instance(clsid, IFoo, factory)`.
- **`CoCreateInstance`**: `portacom.co_create_instance(clsid, IFoo)`. On
  Windows it uses OLE32. Elsewhere it calls the `extern "C"` export (which
  MiniCOM provides) of the library that `IFoo`'s ABI names, or of
  `library=...`.
- **A ProgID**: `portacom.clsid_from_progid("Scripting.Dictionary")` is
  OLE32's `CLSIDFromProgID` on Windows; elsewhere pass the library that
  exports one (MiniCOM's matches its C++ class names).

**Libraries.** A library is loaded on the first call into it, by the DLL
name the metadata gives (`DxcCreateInstance` is in `dxcompiler.dll`); the
`library=` of `Abi`, `co_create_instance` and `clsid_from_progid` is such a
name too, or a path. That name, in any case, is `register_library`'s key:
`portacom.register_library("foo.dll", "/path/to/libfoo.so")` points it at a
file, or at an already loaded `ctypes.CDLL`.
- Off Windows, `foo.dll` is searched for as `libfoo.so`, `foo.so`,
  `libfoo.so.N` and `libfoo.dylib`.
- On Windows it is loaded with `ctypes.WinDLL("foo.dll")`, which since Python
  3.8 searches neither `PATH` nor the current directory. Register its path, or
  add its directory with `os.add_dll_directory()`.

**Threads.** A call runs on the thread that makes it, and portacom's own
state is thread-safe. On Windows `co_initialize()` (`CoInitializeEx`,
multithreaded by default) is per thread, and `co_create_instance()` calls it
on each thread that needs it. Nothing is marshalled between apartments: an
interface handed to another thread is called directly from there. An
interface is released on whichever thread the garbage collector runs its
`__del__`; call `release()` to choose. Python objects are called on the C
caller's thread.

### Automation (IDispatch)

`portacom.Dispatch(obj)` calls an automation object by name, with no bindings
needed:

```python
CLSID_Dictionary = "EE09B103-97E0-11CF-978F-00A02463E06F"  # Scripting.Dictionary
d = portacom.Dispatch(portacom.co_create_instance(CLSID_Dictionary, portacom.IDispatch))
d.Add("a", 1)
d.Count()  # or d.get("Count")
d.set("Item", "a", 2)  # property put; the last argument is the value
keys = d.Keys()  # a SAFEARRAY comes back as a list
for key in d:  # DISPID_NEWENUM
    ...
```

- `d.invoke(name_or_dispid, *args, named=None, flags=...)` is the general
  form, and `named={"Param": value}` passes named arguments
  (`shell.Run(cmd, named={"WaitOnReturn": True})`). `d.set_ref(...)` is a
  by-reference put, and `d(...)` invokes DISPID_VALUE.
- Unknown names raise `AttributeError`. Failures raise `COMError` with the
  EXCEPINFO's `.description` and `.source`. Objects that implement IDispatch
  come back as `Dispatch`.

To be called by automation clients (VBScript, event sources), subclass
`portacom.DispatchObject`. Public methods are methods, and attributes and
properties are properties; callers can name a method's arguments. A
`put_Name(*index, value)` method handles an indexed put, and `_NewEnum` can
return any iterable. `_dispids_ = {1: "OnFoo"}`
fixes DISPIDs for callers that invoke by number, and `_dispinterfaces_` lists
source-interface IIDs that QueryInterface answers with IDispatch. Exceptions
reach the caller as DISP_E_EXCEPTION.

## ABI profiles

A `.winmd` describes the Windows ABI, and the format has no way to say that a
library compiled for Linux or macOS differs. They do differ:

| | Windows (.winmd) | DXC off Windows | MiniCOM off Windows |
|---|---|---|---|
| `BOOL` | 4-byte `int` | 1-byte `bool` | `long` (8 bytes) |
| `LONG`, `ULONG` | 4 bytes | `long` (8 bytes) | `int` (4 bytes) |
| `BSTR` | OLEAUT32 | 4-byte length prefix, `malloc`/`free` | `wcsdup`'d string, `free`, `wcslen` |
| `wchar_t` | UTF-16 | UTF-32 | UTF-32 |

So winmd2py generates ABI-neutral bindings, and you declare how the library
built for Linux and macOS differs with
`portacom.use_abi(package_or_namespace, portacom.Abi(...))`, before the
bindings are imported (in their `__init__.py`). On Windows the metadata is the
truth, and `use_abi()` keeps the Windows ABI, unless the `Abi` was made with
`on_windows=True` for a library that deviates there too.

- `typedefs`: the width of a named typedef (`BOOL`), at every use.
- `overrides`: a use the metadata doesn't name. `LONG` and IDL `long` are just
  `Int32` there, so each use is listed as `"IFoo.Method"` (return type),
  `"IFoo.Method.param"`, `"Function.param"` or `"Struct.field"`.
- `library`: the library behind the bindings, as the metadata names it
  (`"ExampleLib.dll"`, found as `libExampleLib.so`). `co_create_instance`
  calls its `CoCreateInstance`, and its own `SysAllocStringLen`,
  `CoTaskMemAlloc` and `SafeArray*` exports allocate, for each family it
  exports whole.
- `allocator`: how BSTRs, VARIANTs, SAFEARRAYs and task memory are
  allocated. With `library`, it is the fallback for what the library doesn't
  export; alone, it is used outright.

DXC's, which `portacom_dxc.ABI` declares for you:

```python
import ctypes, portacom

portacom.use_abi(
    "dxc_bindings",  # the generated package, or a namespace
    portacom.Abi(
        "dxc",
        typedefs={"BOOL": ctypes.c_bool},
        overrides={  # ULONG, in WinAdapter.h's ISequentialStream
            "ISequentialStream.Read.cb": ctypes.c_ulong,
            "ISequentialStream.Read.pcbRead": portacom.Ptr(ctypes.c_ulong),
            "ISequentialStream.Write.cb": ctypes.c_ulong,
            "ISequentialStream.Write.pcbWritten": portacom.Ptr(ctypes.c_ulong),
        },
    ),  # BSTRs: the default allocator, the Windows layout on malloc/free
)
```

MiniCOM's is the one in [Your own interfaces](#your-own-interfaces): its BSTRs
are `wcsdup`'d strings (`PlainAllocator`) until the library exports
`SysAllocStringLen` & co., which are then used with no change to the
declaration.

Without the right declaration, DXC's `IsFullPDB()` on Linux returns values like
`0x721d1c01` instead of `1`. Only what a library exports can be detected
(`library=`); the widths of `BOOL` and `LONG` must be declared, because neither
DXC nor MiniCOM exports anything that tells. Upstream could make this
unnecessary: DXC has open PRs that make `BOOL` and `LONG` match Windows
([#6856](https://github.com/microsoft/DirectXShaderCompiler/pull/6856),
[#8431](https://github.com/microsoft/DirectXShaderCompiler/pull/8431)), after
which later DXC versions may need `portacom.WINDOWS`, and MiniCOM has one that
makes `BOOL` `int32_t` ([#58](https://github.com/forderud/MiniCOM/pull/58)),
with the Windows BSTR and SAFEARRAY layouts and the `Sys*` and `CoTaskMem*`
exports to follow.

A key that matches a module's import path (`"my_bindings"`) wins over one that
matches its namespace, so `portacom_dxc`'s registration doesn't affect your own
bindings of the same namespace. A module's ABI is fixed when it is imported,
and a later `use_abi()` that would change it raises `RuntimeError`.

## Compared with

- **[py-win32more](https://github.com/ynkdir/py-win32more)** ships
  pre-generated Win32 and WinRT bindings made with a .NET tool, and is
  Windows-first. We prototyped a Linux/macOS backend for it
  (ynkdir/py-win32more#227).
- **[comtypes](https://github.com/enthought/comtypes)** is the mature COM
  library for Windows. It works from type libraries and supports IDispatch.
- **[PyWinRT](https://github.com/pywinrt/pywinrt)** generates C++ extensions
  for WinRT. portacom borrows its split between a runtime package and binding
  packages.
- **[py-winmd](https://github.com/ynkdir/py-winmd)** is a pure-Python port of
  Microsoft's C++ `.winmd` reader. winmd2py keeps its own smaller reader, and
  the tests cross-check it against py-winmd on every type of
  `Windows.Win32.winmd`.

## Limitations

- **Not supported:** WinRT; type information in IDispatch; marshalling
  between apartments; a Python callback that returns a struct by value.
- **Off Windows:**
  - There is no IErrorInfo.
  - `VARIANT.clear()` frees BSTRs, arrays and interfaces, and clears records
    through their `IRecordInfo` without freeing their memory, as Wine does.
  - SAFEARRAYs use the Windows layout on the ABI's allocator (`malloc`/`free`
    by default, as DXC does), or the library's own `SafeArray*` exports with
    `library=`. A library with its own array type (MiniCOM) can't exchange
    them.
  - A Python object can implement a method that returns a struct by value
    only on x86-64, for a struct of at most 8 bytes whose fields are all
    integers or all floats, or one larger than 16 bytes. Others (9 to 16
    bytes, mixed fields, ARM64, x86) report a `NotImplementedError` to the
    callback error handler.
- **macOS:** DXC isn't tested there. Microsoft publishes no macOS build of DXC.

## Development

Install as in [CONTRIBUTING.md](CONTRIBUTING.md), then:

```bash
python -m pytest -q -rs tests   # -rs says what was skipped and why
ruff check . && ruff format --check .
```

CI runs on Windows (64- and 32-bit), Linux (Python 3.10 to 3.13) and macOS,
generates and imports every namespace of `Windows.Win32.winmd`, and checks
that winmd2py reads every type of it as py-winmd does (the only signatures it
can't express are the delegates' `.ctor(object, native int)`, which nothing
calls):
- **Windows and Linux** test against the official DXC release.
- **Linux and macOS** build MiniCOM's test library and drive it from Python.
- **Anywhere with Docker**, `tests/linux/Dockerfile` runs the Linux job
  locally; see [CONTRIBUTING.md](CONTRIBUTING.md).

`release.yml` runs CI, checks that the release tag (`vX.Y.Z`) matches every
package's version, and publishes all three packages to PyPI when you publish
a GitHub release. That needs PyPI trusted publishing set up first. Changes
are listed in [CHANGELOG.md](CHANGELOG.md).

MIT license. `tests/data/TestInterfaces.winmd` comes from MiniCOM (MIT, Fredrik
Orderud).
