"""DirectX Shader Compiler, through bindings generated from Windows.Win32.winmd.

Runs on Windows (the Windows SDK's dxcompiler.dll) and Linux/macOS (the
official libdxcompiler.so from the DXC releases; set DXC_LIBRARY).
"""

import ctypes
import sys

import portacom
import pytest
from portacom import COMError, ComObject

INCLUDE = b"float4 Tint() { return float4(1, 0.5, 0.25, 1); }\n"
SHADER = '#include "common.hlsli"\nfloat4 main() : SV_Target { return Tint(); }\n'


def buffer(dxc, text):
    data = text.encode() if isinstance(text, str) else text
    keep = ctypes.create_string_buffer(data, len(data))
    b = dxc.DxcBuffer(ctypes.addressof(keep), len(data), dxc.DXC_CP_UTF8)
    b._keep = keep
    return b


@pytest.fixture(scope="module")
def tools(dxc):
    utils = dxc.DxcCreateInstance(dxc.CLSID_DxcUtils, dxc.IDxcUtils)
    compiler = dxc.DxcCreateInstance(dxc.CLSID_DxcCompiler, dxc.IDxcCompiler3)
    assert type(utils) is dxc.IDxcUtils and type(compiler) is dxc.IDxcCompiler3
    return utils, compiler


def test_compile_with_python_include_handler(dxc, tools):
    utils, compiler = tools
    included = []

    class IncludeHandler(ComObject):
        _interfaces_ = (dxc.IDxcIncludeHandler,)

        def LoadSource(self, filename):
            included.append(filename)
            if not filename.endswith("common.hlsli"):
                raise COMError(0x80070002)  # file not found
            return utils.CreateBlob(INCLUDE, dxc.DXC_CP_UTF8)  # pData and its size from one bytes object

    handler = IncludeHandler()
    result = compiler.Compile(
        buffer(dxc, SHADER), ["-E", "main", "-T", "ps_6_0", "-Qstrip_reflect"], handler, dxc.IDxcResult
    )
    errors, _name = result.GetOutput(dxc.DXC_OUT_ERRORS, dxc.IDxcBlobUtf8)
    status = result.GetStatus()
    assert status == 0, errors and str(errors.GetStringPointer())
    assert len(included) == 1 and included[0].endswith("common.hlsli")

    obj, _name = result.GetOutput(dxc.DXC_OUT_OBJECT, dxc.IDxcBlob)
    size = obj.GetBufferSize()
    assert size > 100
    assert ctypes.string_at(obj.GetBufferPointer(), 4) == b"DXBC"
    assert handler.references == 1  # DXC released the handler


def test_compile_error_and_query(dxc, tools):
    utils, compiler = tools
    result = compiler.Compile(
        buffer(dxc, "float4 main() : SV_Target { return undefined_thing; }"),
        ["-E", "main", "-T", "ps_6_0"],
        None,
        dxc.IDxcResult,
    )
    assert result.GetStatus() < 0
    errors, _ = result.GetOutput(dxc.DXC_OUT_ERRORS, dxc.IDxcBlobUtf8)
    assert "undefined_thing" in str(errors.GetStringPointer())
    assert errors.GetStringLength() > 0
    assert result.query(dxc.IDxcResult) is not None
    assert result.query(dxc.IDxcCompiler3) is None  # E_NOINTERFACE


def test_raising_include_handler_returns_e_fail(dxc, tools):
    utils, compiler = tools
    seen = []

    class Broken(ComObject):
        _interfaces_ = (dxc.IDxcIncludeHandler,)

        def LoadSource(self, filename):
            raise RuntimeError("boom")

    old = portacom.set_callback_error_handler(lambda obj, method, exc: seen.append(str(exc)))
    try:
        result = compiler.Compile(
            buffer(dxc, '#include "x.h"\nfloat4 main() : SV_Target { return 0; }'),
            ["-E", "main", "-T", "ps_6_0"],
            Broken(),
            dxc.IDxcResult,
        )
    finally:
        portacom.set_callback_error_handler(old)
    assert result.GetStatus() < 0
    assert seen == ["boom"]


def test_dxc_allocated_memory(dxc, tools):
    utils, compiler = tools
    # a string DXC allocates with CoTaskMemAlloc, freed with the ABI's task allocator
    info = compiler.QueryInterface(dxc.IDxcVersionInfo2)
    major, minor = info.GetVersion()
    count, commit = info.GetCommitInfo()  # char**: a pointer, for the caller to read and free
    text = ctypes.string_at(ctypes.cast(commit, ctypes.c_void_p).value).decode()
    assert major >= 1 and count > 0 and int(text, 16) >= 0
    portacom.free_task_memory(commit, dxc._abi)

    # BSTRs that DXC allocates, read and freed 1000 times
    result = compiler.Compile(
        buffer(dxc, "float4 main() : SV_Target { return 1; }"),
        ["-E", "main", "-T", "ps_6_0", "-Zi", "-Qembed_debug"],
        None,
        dxc.IDxcResult,
    )
    assert result.GetStatus() == 0
    pdb, _ = result.GetOutput(dxc.DXC_OUT_PDB, dxc.IDxcBlob)
    pdbutils = dxc.DxcCreateInstance(dxc.CLSID_DxcPdbUtils, dxc.IDxcPdbUtils)
    pdbutils.Load(pdb)
    for _ in range(1000):
        assert pdbutils.GetEntryPoint() == "main"

    # BOOL: 1 byte in DXC off Windows, so the dxc ABI reads it as c_bool
    full = pdbutils.IsFullPDB()
    if sys.platform == "win32":
        assert full in (0, 1)
    else:
        assert full is True or full is False, hex(full)
        raw = pdbutils.raw.IsFullPDB()  # the same call, through the profile's c_bool
        assert raw in (True, False)


def test_bstr_with_dxc_abi(dxc):
    text = "abc é\U0001f642"
    b = dxc._abi.BSTR.alloc(text)
    assert str(b) == text and len(b) == portacom.wchar_count(text)
    b.free()


def test_stream_read_returns_bytes(dxc, tools):
    utils, _ = tools
    library = dxc.DxcCreateInstance(dxc.CLSID_DxcLibrary, dxc.IDxcLibrary)
    stream = library.CreateStreamFromBlobReadOnly(utils.CreateBlob(b"hello world", dxc.DXC_CP_UTF8))
    assert stream.Read(5) == (b"hello", 5)  # pv is cb bytes, returned
    data, read = stream.Read(16)
    assert (data[:read], read) == (b" world", 6) and len(data) == 16


def test_create_instance_with_a_generated_function(dxc):
    utils = portacom.create_instance(dxc.CLSID_DxcUtils, dxc.IDxcUtils, dxc.DxcCreateInstance)
    assert isinstance(utils, dxc.IDxcUtils) and utils.CreateBlob(b"abc", dxc.DXC_CP_UTF8)
    out = ctypes.c_void_p()  # Function.raw is the C function, like obj.raw.Method
    clsid, iid = dxc.CLSID_DxcUtils, dxc.IDxcUtils._iid_
    assert dxc.DxcCreateInstance.raw(ctypes.byref(clsid), ctypes.byref(iid), ctypes.byref(out)) == 0
    dxc.IDxcUtils(out.value).release()
