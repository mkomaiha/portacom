"""DirectX Shader Compiler from Python, on Windows, Linux and macOS.

    import portacom_dxc

    result = portacom_dxc.compile(source, "ps_6_0")         # raises CompileError on errors
    result.object                                            # DXIL container, bytes

    compiler = portacom_dxc.Compiler()                       # reuse for many shaders
    compiler.compile(source, "cs_6_6", entry="CSMain", defines={"N": "64"},
                     includes={"common.hlsli": "..."}, args=["-O3"])
    compiler.disassemble(result.object)

The whole COM API is in portacom_dxc.dxc (the generated bindings), with
portacom_dxc.ABI, how DXC built off Windows differs from the Windows ABI.

The library is found from $DXC_LIBRARY, then the system's library search
(dxcompiler.dll, libdxcompiler.so, libdxcompiler.dylib), then the Vulkan SDK
($VULKAN_SDK) and the Windows SDK; or call load("/path/to/library").
"""

import ctypes
import glob
import os
import sys

import portacom
from portacom import COMError, ComObject

# DXC off Windows (include/dxc/WinAdapter.h): BOOL is C++ bool (1 byte), and
# LONG/ULONG are long (8 bytes on 64-bit Linux and macOS) where the metadata
# says Int32/UInt32 without a typedef name.  DXC's own headers use LONG/ULONG
# only for AddRef/Release (read as 32 bits, which is harmless: the count is in
# the low half of the register); the overrides are WinAdapter's
# ISequentialStream, which the bindings include for IDxcLibrary's IStream.
# BSTRs and task memory are the Windows layout on malloc/free, which is the
# default allocator.  libdxcompiler exports only DxcCreateInstance(2), so
# none of this can be detected.  Upstream PRs (#6856, #8431) would make BOOL
# and LONG match Windows, so later DXC versions may need ABI = WINDOWS.  On
# Windows DXC follows the metadata, and use_abi() binds WINDOWS there.
ABI = portacom.Abi(
    "dxc",
    typedefs={"BOOL": ctypes.c_bool},
    overrides={
        "ISequentialStream.Read.cb": ctypes.c_ulong,
        "ISequentialStream.Read.pcbRead": portacom.Ptr(ctypes.c_ulong),
        "ISequentialStream.Write.cb": ctypes.c_ulong,
        "ISequentialStream.Write.pcbWritten": portacom.Ptr(ctypes.c_ulong),
    },
)
portacom.use_abi("portacom_dxc._bindings", ABI)

from ._bindings.Windows.Win32.Graphics.Direct3D import Dxc as dxc  # noqa: E402

__version__ = "0.1.0"
__all__ = [
    "ABI",
    "dxc",
    "load",
    "windows_sdk_libraries",
    "Compiler",
    "CompileResult",
    "CompileError",
    "compile",
    "disassemble",
    "version",
]

_loaded = None


def _candidates():
    path = os.environ.get("DXC_LIBRARY")
    if path:
        yield path
    yield None  # portacom's own search for dxcompiler.dll
    sdk = os.environ.get("VULKAN_SDK")
    if sdk:
        for sub in ("lib", "Lib", "Bin", "bin"):
            for name in ("libdxcompiler.so", "libdxcompiler.dylib", "dxcompiler.dll"):
                yield os.path.join(sdk, sub, name)
    yield from windows_sdk_libraries()


def windows_sdk_libraries():
    """dxcompiler.dll from the installed Windows 10/11 SDKs, newest first."""
    if sys.platform != "win32":
        return []
    program_files = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles", "")
    root = os.environ.get("WindowsSdkDir") or os.path.join(program_files, "Windows Kits", "10")
    pattern = os.path.join(root, "bin", "10.*", portacom.ARCH, "dxcompiler.dll")

    def version(path):  # ...\bin\10.0.26100.0\x64\dxcompiler.dll -> (10, 0, 26100, 0)
        folder = os.path.basename(os.path.dirname(os.path.dirname(path)))
        return tuple(int(part) for part in folder.split(".") if part.isdigit())

    return sorted(glob.glob(pattern), key=version, reverse=True)


def _open(path):
    return ctypes.WinDLL(str(path)) if portacom.NATIVE_COM else ctypes.CDLL(str(path))


def load(path=None):
    """Load DXC (from path, or found as described above); returns the path or name used.
    Loading another library later switches to it."""
    global _loaded
    if path is not None:
        portacom.register_library("dxcompiler.dll", _open(path))  # registered only once it loads
        _loaded = str(path)
        return _loaded
    if _loaded is not None:
        return _loaded
    errors = []
    for candidate in _candidates():
        try:
            if candidate is None:
                portacom.load_library("dxcompiler.dll")
            else:
                portacom.register_library("dxcompiler.dll", _open(candidate))
            _loaded = candidate or "dxcompiler.dll"
            return _loaded
        except OSError as e:
            errors.append(str(e).splitlines()[0])
    raise OSError(
        "DirectX Shader Compiler not found; set DXC_LIBRARY or call portacom_dxc.load(path):\n  " + "\n  ".join(errors)
    )


class CompileError(Exception):
    """The shader did not compile; .errors has DXC's diagnostics."""

    def __init__(self, errors, hresult):
        self.errors = errors
        self.hresult = hresult
        super().__init__(errors.strip() or f"compilation failed: 0x{hresult & 0xFFFFFFFF:08X}")


class CompileResult:
    """What a compilation produced.  Outputs are bytes (None when not produced)."""

    def __init__(self, result):
        self._result = result
        self.status = result.GetStatus()
        self.errors = _text(_output(result, dxc.DXC_OUT_ERRORS, dxc.IDxcBlobUtf8)) or ""
        self.object = _bytes(_output(result, dxc.DXC_OUT_OBJECT, dxc.IDxcBlob))
        self.pdb = _bytes(_output(result, dxc.DXC_OUT_PDB, dxc.IDxcBlob))
        self.reflection = _bytes(_output(result, dxc.DXC_OUT_REFLECTION, dxc.IDxcBlob))
        self.hash = _bytes(_output(result, dxc.DXC_OUT_SHADER_HASH, dxc.IDxcBlob))
        self.text = _text(_output(result, dxc.DXC_OUT_HLSL, dxc.IDxcBlobUtf8)) or _text(
            _output(result, dxc.DXC_OUT_DISASSEMBLY, dxc.IDxcBlobUtf8)
        )

    @property
    def ok(self):
        return self.status >= 0

    def __repr__(self):
        size = len(self.object) if self.object else 0
        return f"<CompileResult {'ok' if self.ok else 'failed'}, {size} bytes, {len(self.errors)} chars of diagnostics>"


def _output(result, kind, interface):
    if not result.HasOutput(kind):
        return None
    blob, _name = result.GetOutput(kind, interface)
    return blob


def _bytes(blob):
    if blob is None:
        return None
    import ctypes

    size = blob.GetBufferSize()
    return ctypes.string_at(blob.GetBufferPointer(), size) if size else None


def _text(blob):
    if blob is None:
        return None
    return (blob.GetStringPointer().value or b"").decode("utf-8", "replace")


class _Includes(ComObject):
    """An IDxcIncludeHandler over a dict, a callable and/or directories."""

    _interfaces_ = (dxc.IDxcIncludeHandler,)

    def __init__(self, utils, includes, include_dirs):
        self.utils = utils
        self.includes = includes or {}
        self.include_dirs = list(include_dirs or [])
        self.loaded = []

    def LoadSource(self, filename):
        name = filename.replace("\\", "/")
        while name.startswith("./"):
            name = name[2:]
        source = None
        if callable(self.includes):
            source = self.includes(name)
        else:
            for key in (name, os.path.basename(name)):
                if key in self.includes:
                    source = self.includes[key]
                    break
        if source is None:
            for directory in self.include_dirs:
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    with open(path, "rb") as f:
                        source = f.read()
                    break
        if source is None:
            raise COMError(0x80070002, description=f"include not found: {filename}")
        if isinstance(source, str):
            source = source.encode("utf-8")
        self.loaded.append(name)
        return self.utils.CreateBlob(source, dxc.DXC_CP_UTF8)


def _buffer(source):
    import ctypes

    if isinstance(source, str):
        source = source.encode("utf-8")
    keep = ctypes.create_string_buffer(source, len(source))
    buffer = dxc.DxcBuffer(ctypes.addressof(keep), len(source), dxc.DXC_CP_UTF8)
    return buffer, keep


class Compiler:
    """An IDxcCompiler3 and IDxcUtils, to compile many shaders with."""

    def __init__(self, library=None):
        load(library)
        self.utils = dxc.DxcCreateInstance(dxc.CLSID_DxcUtils, dxc.IDxcUtils)
        self.compiler = dxc.DxcCreateInstance(dxc.CLSID_DxcCompiler, dxc.IDxcCompiler3)

    def compile(
        self,
        source,
        profile,
        entry="main",
        args=(),
        defines=None,
        includes=None,
        include_dirs=None,
        name="shader.hlsl",
        check=True,
    ):
        """Compile HLSL source (str or bytes) for a target profile ("ps_6_0").

        includes: {name: source} or a callable(name) -> source or None, for
        #include; include_dirs: directories searched after them.  With
        neither, DXC's own handler reads files relative to the current
        directory.  args: more dxc command-line arguments.  check: raise
        CompileError when compilation fails (else see result.ok/errors).
        """
        arguments = [name, "-T", profile]
        if entry:
            arguments += ["-E", entry]
        for key, value in (defines or {}).items():
            arguments.append(f"-D{key}" if value is None else f"-D{key}={value}")
        arguments += list(args)
        if includes is None and include_dirs is None:
            handler = self.utils.CreateDefaultIncludeHandler()
        else:
            handler = _Includes(self.utils, includes, include_dirs)
        buffer, keep = _buffer(source)
        result = CompileResult(self.compiler.Compile(buffer, arguments, handler, dxc.IDxcResult))
        del keep
        if check and not result.ok:
            raise CompileError(result.errors, result.status)
        return result

    def disassemble(self, blob):
        """Text of a compiled DXIL container (bytes)."""
        buffer, keep = _buffer(blob)
        buffer.Encoding = 0
        result = self.compiler.Disassemble(buffer, dxc.IDxcResult)
        text = _text(_output(result, dxc.DXC_OUT_DISASSEMBLY, dxc.IDxcBlobUtf8))
        del keep
        if result.GetStatus() < 0:
            raise CompileError(_text(_output(result, dxc.DXC_OUT_ERRORS, dxc.IDxcBlobUtf8)) or "", result.GetStatus())
        return text

    def version(self):
        """(major, minor, commit count, commit hash) of the loaded DXC."""
        info = self.compiler.QueryInterface(dxc.IDxcVersionInfo)
        major, minor = info.GetVersion()
        info2 = info.query(dxc.IDxcVersionInfo2)
        if info2 is None:
            return major, minor, None, None
        count, commit = info2.GetCommitInfo()  # a char* in task memory
        address = ctypes.cast(commit, ctypes.c_void_p).value
        try:
            text = ctypes.string_at(address).decode("ascii", "replace") if address else None
        finally:
            portacom.free_task_memory(commit, dxc._abi)
        return major, minor, count, text


_default = None


def _compiler():
    global _default
    if _default is None:
        _default = Compiler()
    return _default


def compile(source, profile, **kwargs):
    """Compile with a shared Compiler; see Compiler.compile."""
    return _compiler().compile(source, profile, **kwargs)


def disassemble(blob):
    return _compiler().disassemble(blob)


def version():
    return _compiler().version()
