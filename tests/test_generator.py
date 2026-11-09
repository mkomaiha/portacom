"""winmd2py: enum names, cross-.winmd references, architecture variants, the CLI."""

import ctypes
import importlib
import os
import pathlib
import subprocess
import sys

import portacom
import pytest
from conftest import DATA, d3d12_winmd, win32_winmd
from portacom import _platform, codegen
from portacom._types import ctype
from winmd2py import Generator, generate
from winmd2py.cli import main
from winmd2py.emit import enum_source

TEST_WINMD = os.path.join(DATA, "TestInterfaces.winmd")
IS_64 = ctypes.sizeof(ctypes.c_void_p) == 8


def _import(root, name):
    sys.path.insert(0, str(root))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(root))


def _need(path, what):
    if not path:
        pytest.skip(f"{what} not found")
    return path


def test_enum_member_names():
    """C names with leading underscores: Enum rejects _sunder_ names and a class body mangles __names."""
    values = [("_X_", 1), ("__Y", 2), ("Z", 3), ("_Z", 4), ("name", 5), ("_1", 6), ("None", 7)]
    text, clashes = enum_source("E", False, "c_int32", values, scoped=False)
    assert clashes == ["_Z"]
    namespace = {"_c": ctypes, "_cg": codegen}
    exec(text, namespace)
    E = namespace["E"]
    assert [m.name for m in E] == ["X_", "Y", "Z", "m_Z", "name_", "m_1", "None_"]
    assert E._ctype_ is ctypes.c_int32
    # module globals keep the C names, which a module never mangles
    assert namespace["_X_"] is E.X_ and namespace["__Y"] is E.Y and namespace["_Z"] is E.m_Z
    assert namespace["name"] is E.name_ and namespace["_1"] is E.m_1 and "None_" not in namespace

    text, _ = enum_source("S", True, "c_uint8", [("_A", 1)], scoped=True)
    namespace = {"_c": ctypes, "_cg": codegen}
    exec(text, namespace)
    assert namespace["S"].A == 1 and "_A" not in namespace and issubclass(namespace["S"], codegen.Flags)


def test_by_arch(monkeypatch):
    for arch, expected in (("x64", 1), ("arm64", 2), ("x86", 2)):
        monkeypatch.setattr(_platform, "ARCH", arch)
        assert portacom.by_arch(x64=1, arm64_x86=2) == expected
    monkeypatch.setattr(_platform, "ARCH", "x86")
    assert portacom.by_arch(x64=1) is None
    monkeypatch.setattr(_platform, "ARCH", "riscv64")
    assert portacom.by_arch(x64=1, x86=2) == (1 if IS_64 else 2)


def test_missing_layout_fails_on_use():
    class Unknown(codegen.Structure):
        pass

    class Embeds(codegen.Structure):
        pass

    class Fine(codegen.Structure):
        pass

    codegen.layout(Unknown, codegen.missing("Unknown: layout unknown, X.Y is not in a.winmd"))
    codegen.layout(Embeds, lambda: [("u", Unknown), ("n", ctypes.c_int)])
    codegen.layout(Fine, lambda: [("p", codegen.Ptr(Unknown))])  # a pointer needs no layout
    codegen.finish_layouts(Unknown, Embeds, Fine)  # on import: no error
    assert ctypes.sizeof(Fine) == ctypes.sizeof(ctypes.c_void_p)
    for _ in range(2):  # every use, not just the first
        for cls in (Unknown, Embeds):
            with pytest.raises(TypeError, match="X.Y is not in a.winmd"):
                ctype(cls)
            with pytest.raises(codegen.MissingType):
                cls()

    class Unavailable(codegen.Structure):
        pass

    codegen.layout(Unavailable, None)  # by_arch() had nothing for this architecture
    codegen.finish_layouts(Unavailable)
    with pytest.raises(TypeError, match=f"not declared for {portacom.ARCH}"):
        Unavailable()


def test_public_api():
    plumbing = ["abi_for", "set_fields", "bitfields", "LazyModule", "Param", "IN", "OUT", "Function"]
    plumbing += ["Unsupported", "FUNCTYPE", "HRESULT", "VARIANT_BOOL", "Structure", "Enum"]
    for name in plumbing:
        assert name not in portacom.__all__
        assert getattr(portacom, name) is getattr(codegen, name)  # still importable for one release
    # codegen's also handle missing layouts; portacom's still serve bindings generated before
    assert "layout" not in portacom.__all__ and callable(portacom.layout) and callable(portacom.finish_layouts)
    assert set(codegen.__all__) <= set(dir(codegen))
    for name in ("IUnknown", "ComObject", "GUID", "VARIANT", "use_abi", "Abi", "ARCH", "by_arch", "Ptr"):
        assert name in portacom.__all__


def test_typedefs_once_per_module():
    text = Generator(TEST_WINMD, ["Example"]).module("Example")
    assert text.count("_abi.typedef('HRESULT'") == 1
    assert "_t_HRESULT = _abi.typedef('HRESULT', _c.c_int32)\n" in text
    assert "from portacom import codegen as _cg" in text and "_rt.layout" not in text


def test_cli(tmp_path, capsys):
    assert main([TEST_WINMD, "--list"]) == 0
    assert capsys.readouterr().out.split() == ["Example", "Registry"]
    assert main([str(tmp_path / "missing.winmd"), "-o", str(tmp_path / "out")]) == 1
    error = capsys.readouterr().err
    assert error.startswith("winmd2py: error:") and error.count("\n") == 1
    assert main([os.path.join(DATA, "..", "conftest.py"), "-o", str(tmp_path / "out")]) == 1
    assert "not a .winmd file" in capsys.readouterr().err
    assert main([TEST_WINMD, "-n", "No.Such", "-o", str(tmp_path / "out")]) == 1
    assert "no namespace No.Such" in capsys.readouterr().err
    with pytest.raises(ValueError, match="no namespace"):
        Generator(TEST_WINMD, ["No.Such"])
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    written = generate(TEST_WINMD, str(tmp_path / "api_bindings"), quiet=True)
    assert sorted(os.path.basename(os.path.dirname(p)) for p in written) == ["Example", "Registry"]


def _problems(notes):
    return [n for n in notes if "is not in" in n or "unknown" in n]


def test_cross_winmd_reference(tmp_path):
    """D3D12 lives in its own .winmd (Win32Metadata 71+) and uses Windows.Win32 types."""
    d3d12 = _need(d3d12_winmd(), "Microsoft.Direct3D.D3D12.winmd")
    win32 = _need(win32_winmd(), "Windows.Win32.winmd")
    generator = Generator(d3d12, refs=[win32])
    generator.generate(str(tmp_path / "d3d12_ref"))
    assert _problems(generator.notes) == []
    D = _import(tmp_path, "d3d12_ref.Windows.Win32.Graphics.Direct3D12")
    import d3d12_ref.Windows.Win32.Graphics.Dxgi.Common as DxgiCommon  # partial, from the reference file

    assert "partial" in pathlib.Path(DxgiCommon.__file__).read_text(encoding="utf-8").splitlines()[0]
    desc = D.D3D12_GRAPHICS_PIPELINE_STATE_DESC
    if IS_64:  # from d3d12.h: pointers, SIZE_T and the alignment they bring
        assert ctypes.sizeof(desc) == 656
        assert desc.SampleDesc.offset == 616 and desc.CachedPSO.offset == 632
        assert ctypes.sizeof(D.D3D12_RESOURCE_DESC) == 56
    assert desc().SampleDesc.Count == 0
    assert D.D3D12_RESOURCE_DESC().Format == DxgiCommon.DXGI_FORMAT_UNKNOWN
    assert D.ID3D12Device.GetNodeCount.slot == 7  # after IUnknown's 3 and ID3D12Object's 4

    generator = Generator(d3d12)  # without the reference: notes that say what to do, and loud failures
    generator.generate(str(tmp_path / "d3d12_alone"))
    assert any("it is in Windows.Win32.winmd, pass that file with --ref" in n for n in generator.notes)
    D = _import(tmp_path, "d3d12_alone.Windows.Win32.Graphics.Direct3D12")
    with pytest.raises(TypeError, match="D3D12_GRAPHICS_PIPELINE_STATE_DESC: layout unknown.*--ref"):
        D.D3D12_GRAPHICS_PIPELINE_STATE_DESC()


def test_missing_base_interface():
    win32 = _need(win32_winmd(), "Windows.Win32.winmd")
    generator = Generator(win32, ["Windows.Win32.Graphics.Direct3D11on12"])
    if "Windows.Win32.Graphics.Direct3D12" in generator.winmd.namespaces():
        pytest.skip("this Windows.Win32.winmd still has D3D12 (before Win32Metadata 71)")
    text = generator.module("Windows.Win32.Graphics.Direct3D11on12")
    assert "D3D12_RESOURCE_STATES is not in Windows.Win32.winmd; it is in Microsoft.Direct3D.D3D12.winmd" in "\n".join(
        generator.notes
    )
    assert "_cg.Unsupported(" in text  # CreateWrappedResource takes a D3D12 enum by value


def test_architecture_variants(tmp_path):
    win32 = _need(win32_winmd(), "Windows.Win32.winmd")
    namespaces = ["Windows.Win32.UI.Shell", "Windows.Win32.System.Diagnostics.Debug"]
    generator = Generator(win32, namespaces)
    generator.generate(str(tmp_path / "arch_bindings"))
    Shell = _import(tmp_path, "arch_bindings.Windows.Win32.UI.Shell")
    Debug = _import(tmp_path, "arch_bindings.Windows.Win32.System.Diagnostics.Debug")
    shell_source = pathlib.Path(Shell.__file__).read_text(encoding="utf-8")
    assert "pack=_cg.by_arch(x86=1)" in shell_source  # SHFILEOPSTRUCTW: one field list, packing by architecture
    if portacom.ARCH in ("x64", "arm64"):
        assert ctypes.sizeof(Shell.SHFILEOPSTRUCTW) == 56
        assert ctypes.sizeof(Debug.CONTEXT) == {"x64": 1232, "arm64": 912}[portacom.ARCH]
        assert hasattr(Debug.CONTEXT, "Rip" if portacom.ARCH == "x64" else "Pc")
        entry = {"x64": "IMAGE_RUNTIME_FUNCTION_ENTRY", "arm64": "IMAGE_ARM64_RUNTIME_FUNCTION_ENTRY"}
        assert Debug.RtlAddFunctionTable.signature().params[0].type.target.__name__ == entry[portacom.ARCH]
        assert Debug.PGET_RUNTIME_FUNCTION_CALLBACK.signature().restype.target.__name__ == entry[portacom.ARCH]

    # the x86 variants, in a process that says it is x86
    script = (
        "import ctypes, sys; from portacom import _platform; _platform.ARCH = 'x86'; sys.path.insert(0, sys.argv[1]);"
        "import arch_bindings.Windows.Win32.UI.Shell as S, arch_bindings.Windows.Win32.System.Diagnostics.Debug as D;"
        "print(ctypes.sizeof(S.SHFILEOPSTRUCTW), S.SHFILEOPSTRUCTW._pack_, hasattr(D.CONTEXT, 'Eip'),"
        "D.RtlAddFunctionTable.signature().reason, '|', D.PGET_RUNTIME_FUNCTION_CALLBACK.signature().reason)"
    )
    out = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, check=True)
    size, pack, eip, reason = out.stdout.split(" ", 3)
    assert (pack, eip) == ("1", "True")
    if IS_64:
        assert int(size) == 50  # packed to 1, with 8-byte pointers
    assert [r.strip() for r in reason.split("|")] == [
        "RtlAddFunctionTable is not declared for x86",
        "PGET_RUNTIME_FUNCTION_CALLBACK is not declared for x86",
    ]


def test_package_init_is_written_once(tmp_path):
    """The output package's __init__.py is the user's, for the use_abi() call."""
    out = tmp_path / "mine"
    generate(TEST_WINMD, str(out), quiet=True)
    init = out / "__init__.py"
    text = init.read_text(encoding="utf-8")
    assert text.startswith('"""mine: bindings generated by winmd2py from TestInterfaces.winmd.')
    assert "never overwrites it" in text and "# portacom.use_abi(__name__, portacom.Abi(" in text
    compile(text, str(init), "exec")
    init.write_text("import portacom\n", encoding="utf-8")
    generate(TEST_WINMD, str(out), quiet=True)
    assert init.read_text(encoding="utf-8") == "import portacom\n"
    assert "Generated by winmd2py" in (out / "Example" / "__init__.py").read_text(encoding="utf-8")


def test_delegates(tmp_path):
    """A delegate is a class with its Invoke's signature; one that cannot be described stays void*."""
    win32 = _need(win32_winmd(), "Windows.Win32.winmd")
    ns = "Windows.Win32.UI.WindowsAndMessaging"
    generator = Generator(win32, [ns])
    text = generator.module(ns)
    assert "class WNDENUMPROC(_cg.Delegate):" in text and "_P('lpEnumFunc', WNDENUMPROC, _cg.IN)" in text
    assert "('lpfnWndProc', WNDPROC)," in text  # a struct field
    assert not [n for n in generator.notes if "delegate" in n]

    generator = Generator(win32, [ns])
    invoke = generator.winmd.invoke

    def unsupported_invoke(entity):
        m = invoke(entity)
        if entity.name == "WNDENUMPROC":
            m = type(m)(m.name, ("unsupported", "testing"), m.params, m.row)
        return m

    generator.winmd.invoke = unsupported_invoke
    text = generator.module(ns)
    assert "class WNDENUMPROC(" not in text and "_P('lpEnumFunc', _c.c_void_p, _cg.IN)" in text
    assert f"{ns}.EnumWindows: delegate WNDENUMPROC passed as void*, testing" in generator.notes


@pytest.mark.skipif(sys.platform != "win32", reason="calls user32")
def test_win32_callbacks(tmp_path):
    win32 = _need(win32_winmd(), "Windows.Win32.winmd")
    generate(win32, str(tmp_path / "ui"), ["Windows.Win32.UI.WindowsAndMessaging"], quiet=True)
    W = _import(tmp_path, "ui.Windows.Win32.UI.WindowsAndMessaging")
    found = []

    def collect(hwnd, lparam):
        assert lparam == 42
        found.append(hwnd)
        return len(found) < 3  # FALSE stops the enumeration

    W.EnumWindows(collect, 42)
    assert len(found) == 3 and all(isinstance(hwnd, int) and hwnd for hwnd in found)

    # a function pointer from C, called through its prototype
    default = W.WNDPROC(ctypes.cast(ctypes.windll.user32.DefWindowProcW, ctypes.c_void_p).value)
    assert default(None, W.WM_NULL, 0, 0) == 0
    # a Python WNDPROC in a struct field: the caller keeps the object alive
    calls = []
    proc = W.WNDPROC(lambda hwnd, msg, wparam, lparam: calls.append(msg) or default(hwnd, msg, wparam, lparam))
    wc = W.WNDCLASSW(lpfnWndProc=proc)
    assert isinstance(wc.lpfnWndProc, W.WNDPROC) and wc.lpfnWndProc.value == proc.value
    assert wc.lpfnWndProc(None, W.WM_NULL, 0, 0) == 0 and calls == [W.WM_NULL]
