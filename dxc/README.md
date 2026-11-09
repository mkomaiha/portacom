# portacom-dxc

[DirectX Shader Compiler](https://github.com/microsoft/DirectXShaderCompiler)
from Python on Windows, Linux and macOS: the full COM API generated from
`Windows.Win32.winmd` with [winmd2py](https://pypi.org/project/winmd2py/), on
the [portacom](https://pypi.org/project/portacom/) runtime, plus a small
compile API.

```bash
pip install portacom-dxc
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

It does not ship DXC itself. It loads `dxcompiler.dll` / `libdxcompiler.so`
from `$DXC_LIBRARY`, the library search path, `$VULKAN_SDK` or the Windows
SDK, in that order; or call `portacom_dxc.load(path)`. For the full COM API,
use `portacom_dxc.dxc`.

See the [project README](https://github.com/mkomaiha/portacom#readme) for the
rest.

MIT license. The bindings are generated from Microsoft's Win32 metadata (MIT,
Copyright (c) Microsoft Corporation); see
[THIRD_PARTY_NOTICES.md](https://github.com/mkomaiha/portacom/blob/main/dxc/THIRD_PARTY_NOTICES.md).
