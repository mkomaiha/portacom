"""The types of a .winmd file, as the generator sees them.

Win32-metadata conventions (classic COM, not WinRT):

- an interface is a TypeDef with the Interface flag; its base is in InterfaceImpl
- an enum extends System.Enum; its underlying type is the value__ field
- a struct extends System.ValueType; ExplicitLayout makes it a union
- a struct with NativeTypedefAttribute is a typedef of its one field (BOOL, PWSTR)
- a struct with a GUID and no fields is a coclass (MIDL-generated .winmd)
- the "Apis" class of a namespace holds its functions (with ImplMap rows) and
  constants (Constant rows, or GuidAttribute for GUIDs)
- a delegate (function pointer type) extends System.MulticastDelegate; its
  Invoke method is the signature, and UnmanagedFunctionPointerAttribute
  gives the calling convention
- a nested type (a union inside a struct) is in NestedClass
- a parameter of an interface type is CLASS IFoo, meaning IFoo*
- a type or function that differs by architecture is declared once per
  variant, each with SupportedArchitectureAttribute
- a type from another .winmd is a TypeRef whose scope is an AssemblyRef
  named after that file (Microsoft.Direct3D.D3D12.winmd refers to
  Windows.Win32.winmd)

A WinMD reads one file and the reference files given with it.  Their
TypeDefs are numbered in one space (INDEX_BITS), so a type key or an
entity's index says which file it is in.
"""

import bisect
import functools
import os
import struct
import uuid

from .metadata import Blob, Coded, Metadata

# ECMA-335 II.23.1.16 element types
E_VOID, E_BOOLEAN, E_CHAR = 0x01, 0x02, 0x03
E_PTR, E_BYREF, E_VALUETYPE, E_CLASS, E_ARRAY = 0x0F, 0x10, 0x11, 0x12, 0x14
E_I, E_U, E_OBJECT, E_SZARRAY, E_CMOD_REQD, E_CMOD_OPT, E_STRING = 0x18, 0x19, 0x1C, 0x1D, 0x1F, 0x20, 0x0E

PRIMITIVE = {
    E_BOOLEAN: "c_bool",
    E_CHAR: "c_wchar",
    0x04: "c_byte",
    0x05: "c_ubyte",
    0x06: "c_int16",
    0x07: "c_uint16",
    0x08: "c_int32",
    0x09: "c_uint32",
    0x0A: "c_int64",
    0x0B: "c_uint64",
    0x0C: "c_float",
    0x0D: "c_double",
    E_I: "c_ssize_t",
    E_U: "c_size_t",
}

# Typedefs that a .winmd may refer to without declaring them (a MIDL-generated
# .winmd refers to Windows.Win32.Foundation.BOOL and friends): name -> default ctype
KNOWN_TYPEDEFS = {
    "BOOL": "c_int32",
    "BOOLEAN": "c_ubyte",
    "HRESULT": "c_int32",
    "NTSTATUS": "c_int32",
    "BSTR": "c_void_p",
    "PWSTR": "c_wchar_p",
    "PCWSTR": "c_wchar_p",
    "PSTR": "c_char_p",
    "PCSTR": "c_char_p",
    "VARIANT_BOOL": "c_int16",
    "CHAR": "c_char",
    "HANDLE": "c_void_p",
    "HWND": "c_void_p",
    "HINSTANCE": "c_void_p",
    "HMODULE": "c_void_p",
    "HDC": "c_void_p",
    "HGLOBAL": "c_void_p",
    "PSID": "c_void_p",
    "LPARAM": "c_ssize_t",
    "WPARAM": "c_size_t",
    "LRESULT": "c_ssize_t",
    "FARPROC": "c_void_p",
}

# SupportedArchitectureAttribute bits, in the order the bindings list them
ARCHES = (("x64", 2), ("arm64", 4), ("x86", 1))
ALL_ARCHES = 7
ARCH_X64 = 2

INDEX_BITS = 24  # TypeDef row numbers; the file is in the bits above
TD_INTERFACE = 0x20
TD_LAYOUT_MASK, TD_EXPLICIT = 0x18, 0x10
PD_IN, PD_OUT, PD_OPTIONAL = 0x1, 0x2, 0x10
FD_STATIC, FD_LITERAL = 0x10, 0x40


def arch_names(mask):
    """("x64", "arm64", "x86") for a SupportedArchitectureAttribute mask (None: all)."""
    mask = ALL_ARCHES if mask is None else mask
    return tuple(name for name, bit in ARCHES if mask & bit)


class Unsupported(Exception):
    pass


def _per_entity(method):
    """Keep what the method reads for an entity on the entity: the generator asks more than once."""
    attribute = "_" + method.__name__

    @functools.wraps(method)
    def cached(self, entity):
        found = entity.__dict__.get(attribute)
        if found is None:
            found = entity.__dict__[attribute] = method(self, entity)
        return found

    return cached


# Type trees: ("prim", ctype) ("void",) ("ref", key) ("ptr", tree) ("array", tree, n)
# key: ("def", entity index) or ("ext", namespace, name)


class Param:
    def __init__(self, name, type, flags, attrs):
        self.name = name
        self.type = type
        self.flags = flags  # PD_* flags
        self.attrs = attrs  # attribute name -> named arguments


class Method:
    def __init__(self, name, ret, params, row):
        self.name = name
        self.ret = ret
        self.params = params
        self.row = row


class Entity:
    """One TypeDef that the bindings may declare."""

    def __init__(self, winmd, row):
        self.winmd = winmd  # the file that declares it
        self.row = row
        self.local = row.index
        self.index = winmd.base + row.index
        self.name = row.TypeName
        enclosing = winmd.enclosing.get(row.index)
        self.enclosing = None if enclosing is None else winmd.base + enclosing
        top = row
        while top.index in winmd.enclosing:
            top = winmd.md.table("TypeDef")[winmd.enclosing[top.index]]
        self.namespace = top.TypeNamespace
        self.top = top.index
        self.kind = winmd.classify(row)

    @property
    def pyname(self):
        if self.enclosing:
            return self.winmd.entity(self.enclosing).pyname + "_" + self.name
        return self.name

    @property
    def arch(self):
        """The SupportedArchitectureAttribute mask of the top-level type, or None."""
        return self.winmd.architecture("TypeDef", self.top)

    @property
    def guid(self):
        return self.winmd.guid("TypeDef", self.local)

    def has_attr(self, name):
        return self.winmd.has_attr("TypeDef", self.local, name)

    def __repr__(self):
        return f"<{self.kind} {self.namespace}.{self.pyname}>"


class WinMD:
    """A .winmd file, and the files given with it whose types it may use."""

    def __init__(self, path, refs=()):
        self._load(path, [], {})
        for ref in refs:
            WinMD.__new__(WinMD)._load(ref, self.files, self.unresolved)

    def _load(self, path, files, unresolved):
        self.path = path
        self.name = os.path.basename(path)
        self.base = len(files) << INDEX_BITS
        self.files = files  # this file first, then the reference files; shared by all of them
        self.unresolved = unresolved  # (namespace, name) -> the assembly a TypeRef names, shared
        files.append(self)
        try:
            self.md = md = Metadata(path)
        except (ValueError, struct.error, IndexError, KeyError) as error:
            raise ValueError(f"{path}: not a .winmd file ({error})") from None
        self._entities = {}

        self.enclosing = {}
        self.nested = {}
        for row in md.table("NestedClass"):
            self.enclosing[row.NestedClass] = row.EnclosingClass
            self.nested.setdefault(row.EnclosingClass, []).append(row.NestedClass)

        # top-level types by (namespace, name), several when they differ by architecture
        self.by_name = {}
        for row in md.table("TypeDef"):
            if row.index in self.enclosing or row.TypeName == "<Module>":
                continue
            self.by_name.setdefault((row.TypeNamespace, row.TypeName), []).append(row.index)

    def namespaces(self):
        return sorted({ns for ns, _ in self.by_name if ns})

    # The indexes below are built on first use: listing namespaces needs none of them.

    @functools.cached_property
    def attributes(self):
        """Custom attributes by what they are on: Coded -> [(attribute name, blob)]."""
        md = self.md
        attributes = {}
        member_refs = md.table("MemberRef")
        type_refs = md.table("TypeRef")
        type_defs = md.table("TypeDef")
        names = {}
        for row in md.table("CustomAttribute"):
            ctor = row.Type
            name = names.get(ctor)
            if name is None:
                name = "?"
                if ctor.table == "MemberRef":
                    parent = member_refs[ctor.index].Class
                    if parent.table == "TypeRef":
                        name = type_refs[parent.index].TypeName
                    elif parent.table == "TypeDef":
                        name = type_defs[parent.index].TypeName
                elif ctor.table == "MethodDef":
                    name = self._method_owner_name(ctor.index)
                names[ctor] = name
            attributes.setdefault(row.Parent, []).append((name, row.Value))
        return attributes

    @functools.cached_property
    def constants(self):
        return {row.Parent: (row.Type, row.Value) for row in self.md.table("Constant")}

    @functools.cached_property
    def bases(self):
        bases = {}
        for row in self.md.table("InterfaceImpl"):
            bases.setdefault(row.Class, []).append(row.Interface)
        return bases

    @functools.cached_property
    def layouts(self):
        return {row.Parent: (row.PackingSize, row.ClassSize) for row in self.md.table("ClassLayout")}

    @functools.cached_property
    def implmap(self):
        implmap = {}
        md = self.md
        for row in md.table("ImplMap"):
            if row.MemberForwarded.table == "MethodDef":
                scope = md.table("ModuleRef")[row.ImportScope].Name if row.ImportScope else ""
                implmap[row.MemberForwarded.index] = (row.MappingFlags, row.ImportName, scope)
        return implmap

    def _method_owner_name(self, method_index):
        """The TypeDef whose MethodList contains the method (an attribute
        declared in the file itself has its constructor there)."""
        if not hasattr(self, "_method_starts"):
            column = self.md.table("TypeDef").column("MethodList")
            table = self.md.table("TypeDef")
            self._method_starts = [table.raw(i)[column] for i in range(1, table.count + 1)]
        position = bisect.bisect_right(self._method_starts, method_index)
        if position == 0:
            return "?"
        return self.md.table("TypeDef")[position].TypeName

    # -- attributes -------------------------------------------------------------------

    def attrs(self, table, index):
        return self.attributes.get(Coded(table, index), [])

    def has_attr(self, table, index, name):
        return any(n == name for n, _ in self.attrs(table, index))

    def attr_blob(self, table, index, name):
        for n, blob in self.attrs(table, index):
            if n == name:
                return blob
        return None

    def guid(self, table, index):
        blob = self.attr_blob(table, index, "GuidAttribute")
        if blob is None or len(blob) < 18:
            return None
        return str(uuid.UUID(bytes_le=bytes(blob[2:18]))).upper()

    def architecture(self, table, index):
        blob = self.attr_blob(table, index, "SupportedArchitectureAttribute")
        if blob is None:
            return None
        return struct.unpack_from("<i", blob, 2)[0]

    @staticmethod
    def named_args(blob):
        """Named arguments of a custom attribute whose constructor takes none."""
        b = Blob(blob)
        if len(b.data) < 4 or b.unpack("H")[0] != 1:
            return {}
        count = b.unpack("H")[0]
        values = {}
        for _ in range(count):
            b.byte()  # FIELD or PROPERTY
            kind = b.byte()
            if kind == 0x55:  # enum: its type name, then (here assumed) an int32
                b.ser_string()
                kind = 0x08
            name = b.ser_string()
            if kind == 0x0E:
                values[name] = b.ser_string()
            else:
                fmt = {
                    0x02: "?",
                    0x03: "H",
                    0x04: "b",
                    0x05: "B",
                    0x06: "h",
                    0x07: "H",
                    0x08: "i",
                    0x09: "I",
                    0x0A: "q",
                    0x0B: "Q",
                    0x0C: "f",
                    0x0D: "d",
                }.get(kind)
                if fmt is None:
                    break
                values[name] = b.unpack(fmt)[0]
        return values

    # -- types ------------------------------------------------------------------------
    # Methods that take an entity read the file that declares it.

    def entity(self, index):
        f = self.files[index >> INDEX_BITS]
        local = index & ((1 << INDEX_BITS) - 1)
        e = f._entities.get(local)
        if e is None:
            e = Entity(f, f.md.table("TypeDef")[local])
            f._entities[local] = e
        return e

    def classify(self, row):
        if row.Flags & TD_INTERFACE:
            return "interface"
        extends = ""
        if row.Extends:
            base = self.md.row(row.Extends)
            extends = base.TypeName
        if extends == "Enum":
            return "enum"
        if extends == "MulticastDelegate":
            return "delegate"
        if extends == "ValueType":
            if self.has_attr("TypeDef", row.index, "NativeTypedefAttribute") or self.has_attr(
                "TypeDef", row.index, "MetadataTypedefAttribute"
            ):
                return "typedef"
            if not len(row.FieldList) and self.guid("TypeDef", row.index):
                return "coclass"
            if row.Flags & TD_LAYOUT_MASK == TD_EXPLICIT:
                return "union"
            return "struct"
        if extends == "Attribute":
            return "attribute"
        if row.TypeName == "Apis":
            return "apis"
        return "other"

    def pick(self, indexes):
        """Of a type's variants by architecture (local row numbers), the x64 one."""
        if len(indexes) == 1:
            return indexes[0]
        for index in indexes:
            arch = self.architecture("TypeDef", index)
            if arch is None or arch & ARCH_X64:
                return index
        return indexes[0]

    def variants(self, entity):
        """Indexes of the declarations of a top-level type, one per architecture variant."""
        if entity.enclosing:
            return [entity.index]
        f = entity.winmd
        return [f.base + i for i in f.by_name[(entity.namespace, entity.name)]]

    def nested_of(self, entity):
        f = entity.winmd
        return [f.base + i for i in f.nested.get(entity.local, ())]

    def layout_of(self, entity):
        """(packing, size) from ClassLayout, 0 where not given."""
        return entity.winmd.layouts.get(entity.local, (0, 0))

    def find(self, namespace, name):
        """The index of a top-level type in this file or a reference file, or None."""
        for f in self.files:
            found = f.by_name.get((namespace, name))
            if found:
                return f.base + f.pick(found)
        return None

    def resolve(self, coded):
        """A key for a TypeDefOrRef: ("def", index) or ("ext", namespace, name)."""
        if coded.table == "TypeDef":
            return ("def", self.base + coded.index)
        if coded.table == "TypeRef":
            row = self.md.table("TypeRef")[coded.index]
            scope = row.ResolutionScope
            if scope and scope.table == "TypeRef":  # a nested type, of any variant of the outer one
                outer = self.resolve(scope)
                if outer[0] == "def":
                    for variant in self.variants(self.entity(outer[1])):
                        for child in self.nested_of(self.entity(variant)):
                            if self.entity(child).name == row.TypeName:
                                return ("def", child)
                return ("ext", "", row.TypeName)
            found = self.find(row.TypeNamespace, row.TypeName)
            if found is not None:
                return ("def", found)
            if scope and scope.table == "AssemblyRef":
                assembly = self.md.table("AssemblyRef")[scope.index].Name
                self.unresolved.setdefault((row.TypeNamespace, row.TypeName), assembly)
            return ("ext", row.TypeNamespace, row.TypeName)
        raise Unsupported("generic type")

    def missing(self, namespace, name):
        """Why a type is not here, and what to do about it."""
        files = " or ".join(f.name for f in self.files)
        text = f"{namespace}.{name} is not in {files}"
        assembly = self.unresolved.get((namespace, name))
        if assembly and assembly.endswith(".winmd"):
            return f"{text}; it is in {assembly}, pass that file with --ref"
        return f"{text}; pass the .winmd that declares it with --ref"

    def key_name(self, key):
        if key[0] == "def":
            e = self.entity(key[1])
            return e.namespace, e.name
        return key[1], key[2]

    def is_interface_key(self, key):
        if key[0] == "def":
            return self.entity(key[1]).kind == "interface"
        return key[2] in ("IUnknown", "IDispatch") or key[2].startswith("I") and key[2][1:2].isupper()

    def read_type(self, b):
        element = b.byte()
        while element in (E_CMOD_REQD, E_CMOD_OPT):
            b.compressed()
            element = b.byte()
        if element == E_VOID:
            return ("void",)
        if element in PRIMITIVE:
            return ("prim", PRIMITIVE[element])
        if element in (E_PTR, E_BYREF):
            return ("ptr", self.read_type(b))
        if element == E_SZARRAY:
            return ("ptr", self.read_type(b))
        if element in (E_VALUETYPE, E_CLASS):
            value = b.compressed()
            table = ["TypeDef", "TypeRef", "TypeSpec"][value & 3]
            key = self.resolve(Coded(table, value >> 2))
            if element == E_CLASS:
                if key[0] == "def" and self.entity(key[1]).kind == "delegate":
                    return ("ref", key)
                if self.is_interface_key(key) or key[0] == "ext":
                    return ("ptr", ("ref", key))  # a reference type: IFoo means IFoo*
            return ("ref", key)
        if element == E_ARRAY:
            child = self.read_type(b)
            rank = b.compressed()
            sizes = [b.compressed() for _ in range(b.compressed())]
            for _ in range(b.compressed()):
                b.compressed()
            if rank != 1 or len(sizes) != 1:
                return ("unsupported", f"array of rank {rank}")
            return ("array", child, sizes[0])
        if element == E_STRING:
            return ("prim", "c_wchar_p")
        if element == E_OBJECT:
            return ("unsupported", "System.Object")
        return ("unsupported", f"element type 0x{element:02x}")

    # -- members ----------------------------------------------------------------------

    def method(self, row):
        """A MethodDef row of this file."""
        b = Blob(row.Signature)
        b.byte()  # calling convention
        count = b.compressed()
        ret = self.read_type(b)
        types = [self.read_type(b) for _ in range(count)]
        params = [None] * count
        ret_attrs = {}
        params_table = self.md.table("Param")
        for index in row.ParamList:
            p = params_table[index]
            attrs = {}
            for name, blob in self.attrs("Param", index):
                attrs[name] = (
                    self.named_args(blob) if name in ("NativeArrayInfoAttribute", "MemorySizeAttribute") else {}
                )
            if p.Sequence == 0:
                ret_attrs = attrs
                continue
            if p.Sequence - 1 < count:
                params[p.Sequence - 1] = Param(p.Name, types[p.Sequence - 1], p.Flags, attrs)
        for i in range(count):
            if params[i] is None:
                params[i] = Param(f"arg{i}", types[i], 0, {})
        m = Method(row.Name, ret, params, row)
        m.ret_attrs = ret_attrs
        return m

    def interface_base(self, entity):
        f = entity.winmd
        bases = f.bases.get(entity.local, [])
        if not bases:
            return None
        keys = [f.resolve(b) for b in bases]
        if len(keys) == 1:
            return keys[0]

        def depth(key):
            if key[0] != "def":
                return 0
            base = self.interface_base(self.entity(key[1]))
            return 1 + (depth(base) if base else 0)

        return max(keys, key=depth)

    @_per_entity
    def methods(self, entity):
        f = entity.winmd
        table = f.md.table("MethodDef")
        return [f.method(table[i]) for i in entity.row.MethodList]

    def invoke(self, entity):
        """A delegate's signature: the Method of its Invoke."""
        for m in self.methods(entity):
            if m.name == "Invoke":
                return m
        raise Unsupported(f"delegate {entity.name} has no Invoke")

    def is_cdecl(self, entity):
        """Whether a delegate is cdecl (UnmanagedFunctionPointerAttribute(CallingConvention.Cdecl));
        otherwise it is Winapi, stdcall on x86."""
        blob = entity.winmd.attr_blob("TypeDef", entity.local, "UnmanagedFunctionPointerAttribute")
        return blob is not None and len(blob) >= 6 and struct.unpack_from("<i", blob, 2)[0] == 2

    @_per_entity
    def fields(self, entity):
        """[(name, type tree, Field row number)] of a struct's instance fields."""
        f = entity.winmd
        table = f.md.table("Field")
        result = []
        for i in entity.row.FieldList:
            row = table[i]
            if row.Flags & FD_STATIC:
                continue
            b = Blob(row.Signature)
            b.byte()  # FIELD
            result.append((row.Name, f.read_type(b), i))
        return result

    def bitfields(self, entity, field_index):
        """[(name, bit offset, bit count)] packed into a field (NativeBitfieldAttribute)."""
        result = []
        for name, blob in entity.winmd.attrs("Field", field_index):
            if name != "NativeBitfieldAttribute":
                continue
            b = Blob(blob)
            b.unpack("H")  # prolog
            bit_name = b.ser_string()
            offset, length = b.unpack("qq")
            result.append((bit_name, offset, length))
        return result

    def enum_values(self, entity):
        f = entity.winmd
        table = f.md.table("Field")
        underlying = "c_int32"
        values = []
        for i in entity.row.FieldList:
            row = table[i]
            if row.Name == "value__":
                b = Blob(row.Signature)
                b.byte()
                t = f.read_type(b)
                underlying = t[1] if t[0] == "prim" else underlying
                continue
            c = f.constants.get(Coded("Field", i))
            if c is not None:
                values.append((row.Name, constant_value(*c)))
        return underlying, values

    def typedef_target(self, entity):
        fields = self.fields(entity)
        return fields[0][1] if fields else ("prim", "c_void_p")

    @_per_entity
    def apis_members(self, entity):
        """(functions, constants) of an Apis class.  functions is
        [(Method, (flags, import name, library), architecture mask or None)],
        with a function declared once per architecture listed once per variant."""
        f = entity.winmd
        functions = []
        methods = f.md.table("MethodDef")
        for i in entity.row.MethodList:
            if i in f.implmap:
                functions.append((f.method(methods[i]), f.implmap[i], f.architecture("MethodDef", i)))
        constants = []
        fields = f.md.table("Field")
        for i in entity.row.FieldList:
            row = fields[i]
            c = f.constants.get(Coded("Field", i))
            b = Blob(row.Signature)
            b.byte()
            t = f.read_type(b)
            if c is not None and c[0] != 0x12:
                constants.append((row.Name, "value", constant_value(*c), t))
                continue
            g = f.guid("Field", i)
            if g:
                constants.append((row.Name, "guid", g, t))
        return functions, constants


def constant_value(kind, blob):
    blob = bytes(blob)
    if kind == 0x0E:
        return blob.decode("utf-16-le")
    fmt = {
        0x02: "?",
        0x03: "H",
        0x04: "b",
        0x05: "B",
        0x06: "h",
        0x07: "H",
        0x08: "i",
        0x09: "I",
        0x0A: "q",
        0x0B: "Q",
        0x0C: "f",
        0x0D: "d",
    }.get(kind)
    if fmt is None:
        return None
    return struct.unpack("<" + fmt, blob)[0]
