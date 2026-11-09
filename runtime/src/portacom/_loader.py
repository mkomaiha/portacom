"""Finding the shared library behind a DLL name from the metadata.

A .winmd names Windows DLLs ("dxcompiler.dll").  Off Windows the same library
is a shared object with another name, so a list of candidates is tried, the
way Silk.NET does it:

    dxcompiler.dll -> libdxcompiler.so, dxcompiler.so, libdxcompiler.so.<N>,
                      libdxcompiler.dylib, dxcompiler.dylib, ...

register_library() overrides the search for one DLL name, with a path or an
already loaded ctypes.CDLL.
"""

import ctypes
import ctypes.util
import glob
import os
import re
import sys

from ._platform import NATIVE_COM

_registered = {}  # lower-case dll name -> path or CDLL
_loaded = {}  # lower-case dll name -> CDLL
_generation = 0  # bumped by register_library, so that bound functions rebind


def _key(dll_name):
    return os.path.basename(str(dll_name)).lower()


def register_library(dll_name, library):
    """Use library (a path or a loaded ctypes.CDLL) for functions that the
    metadata imports from dll_name.  Functions already called rebind on
    their next call."""
    global _generation
    key = _key(dll_name)
    _loaded.pop(key, None)
    _registered[key] = library
    _generation += 1


def generation() -> int:
    """Changes whenever register_library() does."""
    return _generation


def export(library, name, restype, *argtypes):
    """A private ctypes function for an export of library, so that setting its
    argtypes does not change the library's shared attribute for other code."""
    function = getattr(library, name)
    stdcall = NATIVE_COM and function._flags_ & ctypes._FUNCFLAG_STDCALL
    prototype = (ctypes.WINFUNCTYPE if stdcall else ctypes.CFUNCTYPE)(restype, *argtypes)
    return prototype(ctypes.cast(function, ctypes.c_void_p).value)


def _stem(dll_name):
    name = os.path.basename(str(dll_name))
    if name.lower().endswith(".dll"):
        name = name[:-4]
    return name


def library_candidates(dll_name):
    """File names tried for dll_name, in order.  A path, or a name that is
    already a shared library's (libfoo.so, libfoo.so.1, libfoo.dylib), is
    tried as it is."""
    name = os.fspath(dll_name)
    if NATIVE_COM or os.path.dirname(name) or re.search(r"\.(so(\.\d+)*|dylib)$", name):
        return [name]
    stem = _stem(dll_name)
    names = [stem]
    if stem.lower() != stem:
        names.append(stem.lower())
    so = [f"lib{n}.so" for n in names] + [f"{n}.so" for n in names]
    dylib = [f"lib{n}.dylib" for n in names] + [f"{n}.dylib" for n in names]
    candidates = dylib + so if sys.platform == "darwin" else so + dylib
    # versioned sonames (libfoo.so.3) in the directories the loader is told about
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        for directory in os.environ.get(var, "").split(os.pathsep):
            if directory:
                for n in names:
                    candidates += sorted(glob.glob(os.path.join(directory, f"lib{n}.so.*")))
    for n in names:
        found = ctypes.util.find_library(n)
        if found:
            candidates.append(found)
    seen = []
    for c in candidates:
        if c not in seen:
            seen.append(c)
    return seen


def load_library(dll_name):
    """The ctypes library for a DLL name from the metadata."""
    key = _key(dll_name)
    library = _loaded.get(key)
    if library is not None:
        return library
    registered = _registered.get(key)
    if isinstance(registered, ctypes.CDLL):
        library = registered
    elif registered is not None:
        library = _open(registered)
    else:
        errors = []
        for candidate in library_candidates(dll_name):
            try:
                library = _open(candidate)
                break
            except OSError as e:
                errors.append(f"  {candidate}: {e}")
        else:
            raise OSError(
                f"could not load a library for {dll_name!r}; tried:\n"
                + "\n".join(errors)
                + f"\nUse portacom.register_library({str(dll_name)!r}, '/path/to/library') to point at it."
            )
    _loaded[key] = library
    return library


def _open(path):
    if NATIVE_COM:
        return ctypes.WinDLL(str(path))
    return ctypes.CDLL(str(path))
