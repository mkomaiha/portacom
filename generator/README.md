# winmd2py

Generates [portacom](https://pypi.org/project/portacom/) ctypes bindings from
a `.winmd` file in the Win32 metadata format, in pure Python (no .NET).

```bash
pip install "winmd2py[runtime]"  # the generator, and portacom, which the bindings import
winmd2py Windows.Win32.winmd -n Windows.Win32.Graphics.Direct3D.Dxc -o bindings
winmd2py Microsoft.Direct3D.D3D12.winmd --ref Windows.Win32.winmd -o d3d12
```

or from Python:

```python
import winmd2py

winmd2py.generate("Microsoft.Direct3D.D3D12.winmd", "d3d12", refs=["Windows.Win32.winmd"])
```

- `--ref OTHER.winmd` (repeatable) is a file whose types the input uses. Since
  Win32Metadata 71, D3D12 is in `Microsoft.Direct3D.D3D12.winmd` and uses
  `Windows.Win32.winmd`'s types. What is used from a reference file is
  generated too, into partial modules, so the output needs nothing else.
- Without the file that declares a type, a struct that holds it by value, or an
  interface derived from it, still imports but raises `TypeError` when used,
  and the generator says which file to pass with `--ref`.
- Types and functions that the metadata declares once per architecture (x86,
  x64, ARM64) are declared once; what differs is chosen on import by
  `portacom.ARCH`.
- Delegates become classes with their signatures, so callbacks take Python
  callables; one whose signature can't be described is passed as `void*`, and
  reported.

The bindings follow the Windows ABI that the metadata describes. For a library
built differently off Windows, declare how with `portacom.use_abi()` before
importing them. See the
[project README](https://github.com/mkomaiha/portacom#readme), which also says
what a `.winmd` for your own IDL needs.
