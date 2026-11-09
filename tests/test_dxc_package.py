"""portacom-dxc: the pre-generated bindings and the compile API."""

import portacom
import pytest
from conftest import dxc_library

pytestmark = pytest.mark.skipif(not dxc_library(), reason="dxcompiler not found (set DXC_LIBRARY)")


@pytest.fixture(scope="module")
def dxc():
    import portacom_dxc

    portacom_dxc.load(dxc_library())
    return portacom_dxc


def test_compile_and_disassemble(dxc):
    result = dxc.compile("float4 main() : SV_Target { return float4(1, 0, 0, 1); }", "ps_6_0")
    assert result.ok and result.object[:4] == b"DXBC"
    assert result.hash and len(result.hash) == 20  # DxcShaderHash: flags + 16-byte digest
    text = dxc.disassemble(result.object)
    assert "define void @main()" in text


def test_includes_defines_and_errors(dxc):
    source = '#include "lib/tint.hlsli"\nfloat4 main() : SV_Target { return Tint() * SCALE; }'
    result = dxc.compile(
        source, "ps_6_0", defines={"SCALE": "0.5"}, includes={"lib/tint.hlsli": "float4 Tint() { return 1; }"}
    )
    assert result.ok

    with pytest.raises(dxc.CompileError) as e:
        dxc.compile(source, "ps_6_0")  # no includes: DXC's default handler finds no file
    assert "tint.hlsli" in e.value.errors

    failed = dxc.compile("float4 main() : SV_Target { return nope; }", "ps_6_0", check=False)
    assert not failed.ok and "nope" in failed.errors and failed.object is None


def test_include_directories_and_callable(dxc, tmp_path):
    (tmp_path / "a.hlsli").write_text("#define VALUE 3\n")
    source = '#include "a.hlsli"\n[numthreads(1,1,1)] void CSMain() { int x = VALUE; }'
    assert dxc.compile(source, "cs_6_0", entry="CSMain", include_dirs=[tmp_path]).ok
    seen = []

    def provide(name):
        seen.append(name)
        return "#define VALUE 4\n" if name.endswith("a.hlsli") else None

    assert dxc.compile(source, "cs_6_0", entry="CSMain", includes=provide).ok
    assert seen and seen[0].endswith("a.hlsli")


def test_abi_reads_stream_counts_as_long(dxc):
    """DXC's ULONG is 8 bytes off Windows: IStream.Read's counts go through ABI's overrides."""
    import ctypes
    import sys

    api = dxc.dxc
    assert api._abi is (portacom.WINDOWS if portacom.NATIVE_COM else dxc.ABI)  # Windows follows the metadata
    utils = api.DxcCreateInstance(api.CLSID_DxcUtils, api.IDxcUtils)
    library = api.DxcCreateInstance(api.CLSID_DxcLibrary, api.IDxcLibrary)
    stream = library.CreateStreamFromBlobReadOnly(utils.CreateBlob(b"hello world", api.DXC_CP_UTF8))
    expected = ctypes.c_uint32 if sys.platform == "win32" else ctypes.c_ulong
    assert type(stream).Read.signature().argtypes[1] is expected
    buffer, count = ctypes.create_string_buffer(16), expected(0)
    assert stream.raw.Read(buffer, 5, ctypes.byref(count)) == 0
    assert (buffer.raw[:5], count.value) == (b"hello", 5)
    assert stream.raw.Read(buffer, 16, ctypes.byref(count)) in (0, 1)  # S_FALSE: fewer than asked
    assert (buffer.raw[:6], count.value) == (b" world", 6)
    stream = library.CreateStreamFromBlobReadOnly(utils.CreateBlob(b"hello world", api.DXC_CP_UTF8))
    assert stream.Read(5) == (b"hello", 5)  # and the Pythonic call returns the bytes


def test_version(dxc):
    major, minor, count, commit = dxc.version()
    assert major >= 1 and minor >= 0
    assert commit is None or (len(commit) >= 7 and int(commit, 16) >= 0), commit


def test_load_switches_library_and_skips_bad_candidates(dxc, tmp_path, monkeypatch):
    import portacom
    from portacom import _loader

    good = dxc_library()
    bad = str(tmp_path / "not-dxcompiler.dll")
    registered, loaded = dict(_loader._registered), dict(_loader._loaded)
    monkeypatch.setattr(dxc, "_loaded", None)
    try:
        dxc.dxc.DxcCreateInstance(dxc.dxc.CLSID_DxcUtils, dxc.dxc.IDxcUtils)  # bound to the good library
        portacom.register_library("dxcompiler.dll", bad)
        with pytest.raises(OSError):  # rebound: the registration counts after the first call
            dxc.dxc.DxcCreateInstance(dxc.dxc.CLSID_DxcUtils, dxc.dxc.IDxcUtils)
        assert dxc.load(good) == good
        assert dxc.dxc.DxcCreateInstance(dxc.dxc.CLSID_DxcUtils, dxc.dxc.IDxcUtils)

        # a candidate that fails to load is not left registered for the next one
        monkeypatch.setattr(dxc, "_loaded", None)
        monkeypatch.setattr(dxc, "_candidates", lambda: iter([bad, None]))
        assert dxc.load() == "dxcompiler.dll"  # the registration from load(good)
        monkeypatch.setattr(dxc, "_loaded", None)
        monkeypatch.setattr(dxc, "_candidates", lambda: iter([bad]))
        with pytest.raises(OSError) as e:
            dxc.load()
        assert str(e.value).count("not-dxcompiler") == 1
    finally:
        _loader._registered.clear()
        _loader._registered.update(registered)
        _loader._loaded.clear()
        _loader._loaded.update(loaded)
        _loader._generation += 1


def test_missing_required_argument(dxc):
    # it was NULL, and an access violation: so in a child process
    import subprocess
    import sys

    code = "\n".join(
        [
            "import portacom_dxc",
            f"portacom_dxc.load({dxc_library()!r})",
            "try:",
            "    portacom_dxc.dxc.DxcCreateInstance()",
            "except TypeError as e:",
            "    print(e)",
        ]
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and "missing argument 'rclsid'" in done.stdout, done.stdout + done.stderr
