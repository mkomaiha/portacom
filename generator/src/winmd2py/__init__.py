"""winmd2py: generate portacom ctypes bindings from a .winmd file, in pure Python.

winmd2py.generate("Windows.Win32.winmd", "bindings", ["Windows.Win32.Graphics.Direct3D.Dxc"])
writes the bindings of that namespace, and of what it uses, as the package "bindings".
"""

__version__ = "0.1.0"

from .emit import Generator, generate  # noqa: E402  (emit reads __version__)

__all__ = ["generate", "Generator", "__version__"]
