import ctypes
import glob
import os
import sys

import portacom
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# MiniCOM (https://github.com/forderud/MiniCOM) off Windows (NonWindows.hpp):
# BOOL is long (8 bytes on 64-bit Linux and macOS), and a BSTR is a plain
# wcsdup()'d string freed with free(), unless the library exports Sys*
# functions.  On Windows it is ATL and real COM, and use_abi() binds WINDOWS.
MINICOM_ABI = portacom.Abi(
    "minicom", library="ExampleLib.dll", typedefs={"BOOL": ctypes.c_long}, allocator=portacom.PlainAllocator()
)
MINICOM_LIBRARY = os.environ.get("MINICOM_LIBRARY")


def win32_winmd():
    """Windows.Win32.winmd from Microsoft.Windows.SDK.Win32Metadata: $WIN32_WINMD, or the NuGet cache."""
    path = os.environ.get("WIN32_WINMD")
    if path and os.path.exists(path):
        return path
    pattern = os.path.join(
        os.path.expanduser("~"), ".nuget", "packages", "microsoft.windows.sdk.win32metadata", "*", "Windows.Win32.winmd"
    )
    found = sorted(glob.glob(pattern))
    return found[-1] if found else None


def d3d12_winmd():
    """Microsoft.Direct3D.D3D12.winmd (Win32Metadata 71+): $D3D12_WINMD, or next to Windows.Win32.winmd."""
    path = os.environ.get("D3D12_WINMD")
    if path and os.path.exists(path):
        return path
    win32 = win32_winmd()
    path = win32 and os.path.join(os.path.dirname(win32), "Microsoft.Direct3D.D3D12.winmd")
    return path if path and os.path.exists(path) else None


def dxc_library():
    """dxcompiler: $DXC_LIBRARY, else the Windows SDK's copy on Windows."""
    path = os.environ.get("DXC_LIBRARY")
    if path and os.path.exists(path):
        return path
    from portacom_dxc import windows_sdk_libraries

    found = windows_sdk_libraries()
    return found[0] if found else None


def generate(outdir, winmd, namespaces, refs=()):
    import winmd2py

    return winmd2py.generate(winmd, outdir, namespaces, refs=refs, quiet=True)


@pytest.fixture(scope="session")
def bindings_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("bindings")
    sys.path.insert(0, str(root))
    yield root
    sys.path.remove(str(root))


@pytest.fixture
def abi_registry():
    """use_abi() registrations made in the test are undone after it."""
    from portacom import _abi

    registered, bound = dict(_abi._registered), dict(_abi._bound)
    yield
    _abi._registered.clear()
    _abi._registered.update(registered)
    _abi._bound.clear()
    _abi._bound.update(bound)


@pytest.fixture(scope="session")
def minicom(bindings_root):
    """Bindings for MiniCOM's TestInterfaces.winmd, as package minicom_bindings."""
    generate(str(bindings_root / "minicom_bindings"), os.path.join(DATA, "TestInterfaces.winmd"), None)
    if not portacom.NATIVE_COM:
        # ExampleLib.dll is libExampleLib.so; without it (the Python-only tests) the process stands
        # in, which exports no Sys* functions, so BSTRs come from the fallback allocator as they would
        portacom.register_library("ExampleLib.dll", MINICOM_LIBRARY or ctypes.CDLL(None))
    portacom.use_abi("minicom_bindings", MINICOM_ABI)
    import minicom_bindings.Example as Example
    import minicom_bindings.Registry as Registry

    return Example, Registry


@pytest.fixture(scope="session")
def dxc(bindings_root):
    """Bindings for Windows.Win32.Graphics.Direct3D.Dxc, with the library registered."""
    winmd = win32_winmd()
    if not winmd:
        pytest.skip("Windows.Win32.winmd not found (set WIN32_WINMD)")
    library = dxc_library()
    if not library:
        pytest.skip("dxcompiler not found (set DXC_LIBRARY)")
    import portacom_dxc

    portacom.register_library("dxcompiler.dll", library)
    generate(str(bindings_root / "dxc_bindings"), winmd, ["Windows.Win32.Graphics.Direct3D.Dxc"])
    portacom.use_abi("dxc_bindings", portacom_dxc.ABI)
    import dxc_bindings.Windows.Win32.Graphics.Direct3D.Dxc as Dxc

    return Dxc


WIN32_NAMESPACES = [
    "Windows.Win32.System.Com",
    "Windows.Win32.System.Com.StructuredStorage",
    "Windows.Win32.System.Variant",
    "Windows.Win32.Graphics.Direct2D",
    "Windows.Win32.System.Ole",
]


@pytest.fixture(scope="session")
def win32(bindings_root):
    """Bindings for a few large Windows.Win32 namespaces, as package win32_bindings."""
    winmd = win32_winmd()
    if not winmd:
        pytest.skip("Windows.Win32.winmd not found (set WIN32_WINMD)")
    generate(str(bindings_root / "win32_bindings"), winmd, WIN32_NAMESPACES)
    import importlib

    return {ns.rsplit(".", 1)[-1]: importlib.import_module("win32_bindings." + ns) for ns in WIN32_NAMESPACES}
