"""Cross-checks winmd2py's reader against the `winmd` package (ynkdir/py-winmd), a
pure-Python port of Microsoft's C++ winmd reader that is tested against it.

Both read Windows.Win32.winmd, and every TypeDef (with its base, nesting, layout,
attributes, methods, parameters, fields, constants and ImplMap) is reduced to
the same plain record by each; where the two records differ, one of the readers
has a parsing bug.  `winmd` is a test dependency only (pip install winmd, Python
3.11+); without it, or without the .winmd, the test is skipped.  Reading the
whole file twice takes half a minute: correctness is the point here.
"""

import struct
import sys
import uuid

import pytest
from conftest import win32_winmd

winmd = pytest.importorskip("winmd", reason="the winmd package (the reference reader) is not installed")
if sys.version_info < (3, 11):
    pytest.skip("the winmd package needs Python 3.11", allow_module_level=True)

from winmd.reader import ElementType, HasConstant, MemberForwarded, TypeSemantics, cache, find  # noqa: E402
from winmd.reader import get_type_namespace_and_name as ref_names  # noqa: E402
from winmd2py.metadata import Blob  # noqa: E402
from winmd2py.model import PRIMITIVE, WinMD, constant_value  # noqa: E402

ARCH, GUID, UFP = ATTRIBUTES = ("SupportedArchitectureAttribute", "GuidAttribute", "UnmanagedFunctionPointerAttribute")
# winmd's category of each winmd2py kind: interface (also anything with a GUID), class, enum, struct, delegate
CATEGORY = {"interface": 0, "apis": 1, "other": 1, "attribute": 1, "enum": 2, "delegate": 4}
BASE_CATEGORY = {("System", "Enum"): 2, ("System", "ValueType"): 3, ("System", "MulticastDelegate"): 4}


def arch_of(blob):
    return blob and struct.unpack_from("<i", blob, 2)[0]


def guid_of(blob):
    return blob and str(uuid.UUID(bytes_le=bytes(blob[2:18]))).upper()


# -- winmd2py ----------------------------------------------------------------------


def our_type(w, tree):
    kind = tree[0]
    if kind == "ptr":
        return "*" + our_type(w, tree[1])
    if kind == "array":
        return f"{our_type(w, tree[1])}[{tree[2]}]"
    if kind == "ref":
        key = tree[1]
        ns, name = (key[1], key[2]) if key[0] == "ext" else (w.entity(key[1]).row.TypeNamespace, w.entity(key[1]).name)
        return f"{ns}.{name}"
    return tree[1] if kind == "prim" else kind  # "void", "unsupported"


def our_records(path):
    w = WinMD(path)
    md = w.md

    def name_of(coded):
        if not coded:
            return None
        row = md.row(coded)
        return (row.TypeNamespace, row.TypeName)

    attrs = {}  # (table, row) -> {attribute name: blob}, for the attributes compared
    for coded, found in w.attributes.items():
        for name, blob in found:
            if name in ATTRIBUTES:
                attrs.setdefault((coded.table, coded.index), {})[name] = bytes(blob)
    constants = {coded.index: c for coded, c in w.constants.items() if coded.table == "Field"}
    methods_table, params_table, fields_table = md.table("MethodDef"), md.table("Param"), md.table("Field")
    records = []
    for row in md.table("TypeDef"):
        i = row.index
        e = w.entity(i)
        methods = []
        for m in row.MethodList:
            mrow = methods_table[m]
            parsed = w.method(mrow)
            params = [(p.Sequence, p.Name, p.Flags) for p in (params_table[k] for k in mrow.ParamList)]
            types = [our_type(w, p.type) for p in parsed.params]
            arch = arch_of(attrs.get(("MethodDef", m), {}).get(ARCH))
            methods.append((mrow.Name, our_type(w, parsed.ret), types, params, w.implmap.get(m), arch))
        fields = []
        for f in row.FieldList:
            frow = fields_table[f]
            b = Blob(frow.Signature)
            b.byte()  # FIELD
            c = constants.get(f)
            const = None if c is None else (c[0], constant_value(*c))
            guid = guid_of(attrs.get(("Field", f), {}).get(GUID))
            fields.append((frow.Name, frow.Flags, our_type(w, w.read_type(b)), const, guid))
        own = attrs.get(("TypeDef", i), {})
        records.append(
            {
                "type": (row.TypeNamespace, row.TypeName, row.Flags, name_of(row.Extends), w.enclosing.get(i)),
                "category": 0 if e.kind == "interface" or e.guid else CATEGORY.get(e.kind, 3),
                "bases": [name_of(b) for b in w.bases.get(i, [])],
                "layout": w.layouts.get(i),
                "arch": w.architecture("TypeDef", i),
                "guid": w.guid("TypeDef", i),
                "cdecl": w.is_cdecl(e),
                "attrs": own,
                "methods": methods,
                "fields": fields,
            }
        )
    return records


# -- winmd ---------------------------------------------------------------------------


def ref_type(sig, byref=False):
    stars = "*" * (byref + sig.ptr_count() + sig.is_szarray())
    t = sig.Type()
    if isinstance(t, ElementType):
        base = {ElementType.Void: "void", ElementType.String: "c_wchar_p", ElementType.Object: "unsupported"}.get(t)
        base = base or PRIMITIVE[int(t)]
    else:
        td = find(t)  # the definition, as winmd2py names it; a nested TypeRef has no namespace of its own
        ns, name = (td.TypeNamespace(), td.TypeName()) if td else ref_names(t)
        base = f"{ns}.{name}"
        if sig.element_type() == ElementType.Class:  # a reference type (an interface) means a pointer to it
            if td is None or td.Flags().Semantics() == TypeSemantics.Interface:
                stars += "*"
    if sig.is_array():
        one = sig.array_rank() == 1 and len(sig.array_sizes()) == 1
        base = f"{base}[{sig.array_sizes()[0]}]" if one else "unsupported"
    return stars + base


def ref_records(path):
    db = cache([path]).databases()[0]

    def name_of(coded):
        return ref_names(coded) if coded else None

    attrs = {}  # (table, row) -> {attribute name: blob}
    for a in db.CustomAttribute:
        name = a.TypeNamespaceAndName()[1]
        if name in ATTRIBUTES:
            parent = a.Parent()
            attrs.setdefault((parent.type().name, parent.index() + 1), {})[name] = bytes(a._get_blob(2))
    constants = {
        c.Parent().index() + 1: (int(c.Type()), c.Value())
        for c in db.Constant
        if c.Parent().type() is HasConstant.Field
    }
    implmap = {
        row.MemberForwarded().index() + 1: (int(row.MappingFlags()), row.ImportName(), row.ImportScope().Name())
        for row in db.ImplMap
        if row.MemberForwarded().type() is MemberForwarded.MethodDef
    }
    layouts = {row.Parent().index() + 1: (row.PackingSize(), row.ClassSize()) for row in db.ClassLayout}
    enclosing = {row.NestedType().index() + 1: row.EnclosingType().index() + 1 for row in db.NestedClass}
    records = []
    for t in db.TypeDef:
        i = t.index() + 1
        methods = []
        for m in t.MethodList():
            sig = m.Signature()
            ret = ref_type(sig.ReturnType().Type(), sig.ReturnType().ByRef()) if sig.ReturnType() else "void"
            types = [ref_type(p.Type(), p.ByRef()) for p in sig.Params()]
            params = [(p.Sequence(), p.Name(), int(p.Flags())) for p in m.ParamList()]
            arch = arch_of(attrs.get(("MethodDef", m.index() + 1), {}).get(ARCH))
            methods.append((m.Name(), ret, types, params, implmap.get(m.index() + 1), arch))
        fields = []
        for f in t.FieldList():
            guid = guid_of(attrs.get(("Field", f.index() + 1), {}).get(GUID))
            fields.append(
                (f.Name(), int(f.Flags()), ref_type(f.Signature().Type()), constants.get(f.index() + 1), guid)
            )
        own = attrs.get(("TypeDef", i), {})
        interface = t.Flags().Semantics() == TypeSemantics.Interface or GUID in own
        ufp = own.get(UFP)
        records.append(
            {
                "type": (t.TypeNamespace(), t.TypeName(), int(t.Flags()), name_of(t.Extends()), enclosing.get(i)),
                "category": 0 if interface else BASE_CATEGORY.get(name_of(t.Extends()), 1),
                "bases": [name_of(ii.Interface()) for ii in t.InterfaceImpl()],
                "layout": layouts.get(i),
                "arch": arch_of(own.get(ARCH)),
                "guid": guid_of(own.get(GUID)),
                "cdecl": ufp is not None and len(ufp) >= 6 and struct.unpack_from("<i", ufp, 2)[0] == 2,
                "attrs": own,
                "methods": methods,
                "fields": fields,
            }
        )
    return records


# -- the comparison ---------------------------------------------------------------


def first_difference(ours, theirs):
    """(what, ours, theirs) for the first part of the records that differs."""
    for key in ours:
        a, b = ours[key], theirs[key]
        if a == b:
            continue
        if isinstance(a, list):
            for n, (x, y) in enumerate(zip(a, b, strict=False)):
                if x != y:
                    return f"{key}[{n}]", x, y
            return f"len({key})", len(a), len(b)
        return key, a, b
    return None


def test_every_typedef_matches_the_reference_reader():
    path = win32_winmd()
    if not path:
        pytest.skip("Windows.Win32.winmd not found (set WIN32_WINMD)")
    ours, theirs = our_records(path), ref_records(path)
    assert len(ours) == len(theirs)
    differences = []
    for i, (a, b) in enumerate(zip(ours, theirs, strict=True), 1):
        found = first_difference(a, b)
        if found:
            what, x, y = found
            differences.append(
                f"TypeDef[{i}] {a['type'][0]}.{a['type'][1]}: {what}\n  winmd2py: {x!r}\n  winmd:    {y!r}"
            )
    shown = "\n".join(differences[:20])
    assert not differences, (
        f"{len(differences)} of {len(ours)} TypeDefs differ; the first {min(20, len(differences))}:\n{shown}"
    )
