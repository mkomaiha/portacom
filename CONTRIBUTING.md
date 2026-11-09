# Contributing

Install the three packages in editable mode, with the test and lint tools:

```bash
pip install -e runtime -e generator -e dxc pytest ruff winmd
```

(`winmd` is the reference .winmd reader that `tests/test_reader_reference.py`
cross-checks winmd2py against; it needs Python 3.11, and the test is skipped
without it.)

Before sending a pull request:

```bash
ruff check . && ruff format --check .
python -m pytest -q -rs tests
```

Tests that need something missing are skipped, and `-rs` says why:

- **Windows.** The DXC tests use `$DXC_LIBRARY`, or the Windows SDK's
  `dxcompiler.dll`. The Win32 tests use `$WIN32_WINMD`, or
  `Windows.Win32.winmd` from the NuGet cache
  ([Microsoft.Windows.SDK.Win32Metadata](https://www.nuget.org/packages/Microsoft.Windows.SDK.Win32Metadata)).
  The D3D12 test uses `$D3D12_WINMD`, or `Microsoft.Direct3D.D3D12.winmd`
  next to `Windows.Win32.winmd`.
- **Linux and macOS.** Set `DXC_LIBRARY` to `libdxcompiler.so` from a
  [DXC release](https://github.com/microsoft/DirectXShaderCompiler/releases),
  `WIN32_WINMD`, `D3D12_WINMD`, and `MINICOM_LIBRARY` to MiniCOM's
  `libExampleLib`. See `.github/workflows/ci.yml` for how CI gets each one.

On any machine with Docker, `tests/linux/Dockerfile` sets all of this up the
way CI does, and runs the tests against a checkout mounted at `/src`:

```bash
docker build -t portacom-linux-tests tests/linux
docker run --rm -v "$PWD:/src:ro" portacom-linux-tests
```

(In Git Bash on Windows: `MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd -W):/src:ro" ...`.) Its
`ARG`s pin the same versions as `ci.yml`'s `env`; change them together.

To regenerate portacom-dxc's bindings after changing the generator, run
`python dxc/generate.py path/to/Windows.Win32.winmd`.

Add a line to `CHANGELOG.md` under `Unreleased` for anything users will
notice.
