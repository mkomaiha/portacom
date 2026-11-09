"""Used by generated code: the plumbing that winmd2py's output calls.

Bindings import it as `from portacom import codegen as _cg`.  Code written
by hand needs only the top-level portacom API.

RETVAL, COMOUTPTR and CONST document a parameter as the metadata declares it;
the runtime does not read them.
"""

from __future__ import annotations

from . import _platform, _types
from ._abi import abi_for
from ._interface import Function
from ._loader import library_candidates
from ._marshal import COMOUTPTR, CONST, IN, OPTIONAL, OUT, RETVAL, Param, Unsupported
from ._platform import ARCH, FUNCTYPE, WCHAR_SIZE, by_arch
from ._types import (
    HRESULT,
    VARIANT_BOOL,
    Array,
    Delegate,
    Enum,
    Flags,
    LazyModule,
    Ptr,
    Structure,
    Union,
    bitfields,
    set_fields,
)


class MissingType(TypeError):
    """A struct layout or interface that the bindings could not describe:
    a type it needs was not in the .winmd, or not declared for this
    architecture.  Raised on first use, not on import."""


class _Missing:
    def __init__(self, message):
        self.message = message

    def __call__(self):
        raise MissingType(self.message)


def missing(message):
    """A fields or _signatures_ function that raises MissingType(message)."""
    return _Missing(message)


def not_on_arch(name):
    """The signature of a function the metadata does not declare for this architecture."""
    return Unsupported(f"{name} is not declared for {_platform.ARCH}")


def layout(cls, fields, pack=None, anonymous=(), abi=None):
    """portacom's layout(), for fields that may be missing (None from by_arch:
    not declared for this architecture).  A struct that cannot be laid out,
    or embeds one that cannot, raises MissingType on every use."""
    if fields is None:
        fields = missing(f"{cls.__name__}: layout unknown, not declared for {_platform.ARCH}")

    def checked():
        try:
            result = fields()
            for _, t in result:
                _types._embedded_layouts(t)  # lays out what it embeds first
        except MissingType:
            _types._pending[cls] = spec  # still pending: fail again on the next use
            raise
        return result

    spec = (checked, pack, anonymous, abi)
    _types._pending[cls] = spec


def finish_layouts(*classes):
    """Lay out a module's structs on import.  One that cannot be imports, and
    raises MissingType when used: made, embedded, or passed."""
    for cls in classes:
        try:
            _types.ensure_layout(cls)
        except MissingType as error:
            cls.__init__ = _unusable(str(error))


def _unusable(message):
    def __init__(self, *args, **kwargs):
        raise MissingType(message)

    return __init__


__all__ = [
    "abi_for",
    "Function",
    "Param",
    "Unsupported",
    "IN",
    "OUT",
    "OPTIONAL",
    "RETVAL",
    "COMOUTPTR",
    "CONST",
    "HRESULT",
    "VARIANT_BOOL",
    "Ptr",
    "Array",
    "Structure",
    "Union",
    "Delegate",
    "Enum",
    "Flags",
    "LazyModule",
    "layout",
    "finish_layouts",
    "set_fields",
    "bitfields",
    "by_arch",
    "missing",
    "not_on_arch",
    "MissingType",
    "FUNCTYPE",
    "WCHAR_SIZE",
    "ARCH",
    "library_candidates",
]
