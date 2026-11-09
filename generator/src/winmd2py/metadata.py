"""A small, lazy reader for ECMA-335 metadata, the format of .winmd files.

Only what the generator needs is here: the PE headers that lead to the
metadata, the four heaps, and the tables as rows of plain integers.  Rows are
unpacked on demand with one precompiled struct per table, so opening the 25 MB
Windows.Win32.winmd takes well under a second (dnfile parses every table
eagerly and takes minutes on the same file).

References are to ECMA-335, 6th edition, partition II.
"""

import struct

# Coded index kinds (II.24.2.6): (number of tag bits, tables by tag)
CODED = {
    "TypeDefOrRef": (2, ["TypeDef", "TypeRef", "TypeSpec"]),
    "HasConstant": (2, ["Field", "Param", "Property"]),
    "HasCustomAttribute": (
        5,
        [
            "MethodDef",
            "Field",
            "TypeRef",
            "TypeDef",
            "Param",
            "InterfaceImpl",
            "MemberRef",
            "Module",
            "DeclSecurity",
            "Property",
            "Event",
            "StandAloneSig",
            "ModuleRef",
            "TypeSpec",
            "Assembly",
            "AssemblyRef",
            "File",
            "ExportedType",
            "ManifestResource",
            "GenericParam",
            "GenericParamConstraint",
            "MethodSpec",
        ],
    ),
    "HasFieldMarshal": (1, ["Field", "Param"]),
    "HasDeclSecurity": (2, ["TypeDef", "MethodDef", "Assembly"]),
    "MemberRefParent": (3, ["TypeDef", "TypeRef", "ModuleRef", "MethodDef", "TypeSpec"]),
    "HasSemantics": (1, ["Event", "Property"]),
    "MethodDefOrRef": (1, ["MethodDef", "MemberRef"]),
    "MemberForwarded": (1, ["Field", "MethodDef"]),
    "Implementation": (2, ["File", "AssemblyRef", "ExportedType"]),
    "CustomAttributeType": (3, [None, None, "MethodDef", "MemberRef", None]),
    "ResolutionScope": (2, ["Module", "ModuleRef", "AssemblyRef", "TypeRef"]),
    "TypeOrMethodDef": (1, ["TypeDef", "MethodDef"]),
}

# Table schemas (II.22), by table number. A column is a fixed size ("u1", "u2",
# "u4"), a heap ("str", "guid", "blob"), a table name, or a coded index kind.
SCHEMA = {
    0x00: (
        "Module",
        [("Generation", "u2"), ("Name", "str"), ("Mvid", "guid"), ("EncId", "guid"), ("EncBaseId", "guid")],
    ),
    0x01: ("TypeRef", [("ResolutionScope", "ResolutionScope"), ("TypeName", "str"), ("TypeNamespace", "str")]),
    0x02: (
        "TypeDef",
        [
            ("Flags", "u4"),
            ("TypeName", "str"),
            ("TypeNamespace", "str"),
            ("Extends", "TypeDefOrRef"),
            ("FieldList", "Field"),
            ("MethodList", "MethodDef"),
        ],
    ),
    0x03: ("FieldPtr", [("Field", "Field")]),
    0x04: ("Field", [("Flags", "u2"), ("Name", "str"), ("Signature", "blob")]),
    0x05: ("MethodPtr", [("Method", "MethodDef")]),
    0x06: (
        "MethodDef",
        [
            ("RVA", "u4"),
            ("ImplFlags", "u2"),
            ("Flags", "u2"),
            ("Name", "str"),
            ("Signature", "blob"),
            ("ParamList", "Param"),
        ],
    ),
    0x07: ("ParamPtr", [("Param", "Param")]),
    0x08: ("Param", [("Flags", "u2"), ("Sequence", "u2"), ("Name", "str")]),
    0x09: ("InterfaceImpl", [("Class", "TypeDef"), ("Interface", "TypeDefOrRef")]),
    0x0A: ("MemberRef", [("Class", "MemberRefParent"), ("Name", "str"), ("Signature", "blob")]),
    0x0B: ("Constant", [("Type", "u1"), ("Padding", "u1"), ("Parent", "HasConstant"), ("Value", "blob")]),
    0x0C: ("CustomAttribute", [("Parent", "HasCustomAttribute"), ("Type", "CustomAttributeType"), ("Value", "blob")]),
    0x0D: ("FieldMarshal", [("Parent", "HasFieldMarshal"), ("NativeType", "blob")]),
    0x0E: ("DeclSecurity", [("Action", "u2"), ("Parent", "HasDeclSecurity"), ("PermissionSet", "blob")]),
    0x0F: ("ClassLayout", [("PackingSize", "u2"), ("ClassSize", "u4"), ("Parent", "TypeDef")]),
    0x10: ("FieldLayout", [("Offset", "u4"), ("Field", "Field")]),
    0x11: ("StandAloneSig", [("Signature", "blob")]),
    0x12: ("EventMap", [("Parent", "TypeDef"), ("EventList", "Event")]),
    0x13: ("EventPtr", [("Event", "Event")]),
    0x14: ("Event", [("EventFlags", "u2"), ("Name", "str"), ("EventType", "TypeDefOrRef")]),
    0x15: ("PropertyMap", [("Parent", "TypeDef"), ("PropertyList", "Property")]),
    0x16: ("PropertyPtr", [("Property", "Property")]),
    0x17: ("Property", [("Flags", "u2"), ("Name", "str"), ("Type", "blob")]),
    0x18: ("MethodSemantics", [("Semantics", "u2"), ("Method", "MethodDef"), ("Association", "HasSemantics")]),
    0x19: (
        "MethodImpl",
        [("Class", "TypeDef"), ("MethodBody", "MethodDefOrRef"), ("MethodDeclaration", "MethodDefOrRef")],
    ),
    0x1A: ("ModuleRef", [("Name", "str")]),
    0x1B: ("TypeSpec", [("Signature", "blob")]),
    0x1C: (
        "ImplMap",
        [
            ("MappingFlags", "u2"),
            ("MemberForwarded", "MemberForwarded"),
            ("ImportName", "str"),
            ("ImportScope", "ModuleRef"),
        ],
    ),
    0x1D: ("FieldRVA", [("RVA", "u4"), ("Field", "Field")]),
    0x1E: ("EncLog", [("Token", "u4"), ("FuncCode", "u4")]),
    0x1F: ("EncMap", [("Token", "u4")]),
    0x20: (
        "Assembly",
        [
            ("HashAlgId", "u4"),
            ("MajorVersion", "u2"),
            ("MinorVersion", "u2"),
            ("BuildNumber", "u2"),
            ("RevisionNumber", "u2"),
            ("Flags", "u4"),
            ("PublicKey", "blob"),
            ("Name", "str"),
            ("Culture", "str"),
        ],
    ),
    0x21: ("AssemblyProcessor", [("Processor", "u4")]),
    0x22: ("AssemblyOS", [("OSPlatformID", "u4"), ("OSMajorVersion", "u4"), ("OSMinorVersion", "u4")]),
    0x23: (
        "AssemblyRef",
        [
            ("MajorVersion", "u2"),
            ("MinorVersion", "u2"),
            ("BuildNumber", "u2"),
            ("RevisionNumber", "u2"),
            ("Flags", "u4"),
            ("PublicKeyOrToken", "blob"),
            ("Name", "str"),
            ("Culture", "str"),
            ("HashValue", "blob"),
        ],
    ),
    0x24: ("AssemblyRefProcessor", [("Processor", "u4"), ("AssemblyRef", "AssemblyRef")]),
    0x25: (
        "AssemblyRefOS",
        [("OSPlatformId", "u4"), ("OSMajorVersion", "u4"), ("OSMinorVersion", "u4"), ("AssemblyRef", "AssemblyRef")],
    ),
    0x26: ("File", [("Flags", "u4"), ("Name", "str"), ("HashValue", "blob")]),
    0x27: (
        "ExportedType",
        [
            ("Flags", "u4"),
            ("TypeDefId", "u4"),
            ("TypeName", "str"),
            ("TypeNamespace", "str"),
            ("Implementation", "Implementation"),
        ],
    ),
    0x28: (
        "ManifestResource",
        [("Offset", "u4"), ("Flags", "u4"), ("Name", "str"), ("Implementation", "Implementation")],
    ),
    0x29: ("NestedClass", [("NestedClass", "TypeDef"), ("EnclosingClass", "TypeDef")]),
    0x2A: ("GenericParam", [("Number", "u2"), ("Flags", "u2"), ("Owner", "TypeOrMethodDef"), ("Name", "str")]),
    0x2B: ("MethodSpec", [("Method", "MethodDefOrRef"), ("Instantiation", "blob")]),
    0x2C: ("GenericParamConstraint", [("Owner", "GenericParam"), ("Constraint", "TypeDefOrRef")]),
}
TABLE_NUMBER = {name: number for number, (name, _) in SCHEMA.items()}

# Columns that start a run of rows in another table, which ends where the next row's run starts
LISTS = {("TypeDef", "FieldList"): "Field", ("TypeDef", "MethodList"): "MethodDef", ("MethodDef", "ParamList"): "Param"}


class Coded:
    """A decoded coded index: a table name and a 1-based row number (0 is null)."""

    __slots__ = ("table", "index")

    def __init__(self, table, index):
        self.table = table
        self.index = index

    def __bool__(self):
        return self.index != 0

    def __eq__(self, other):
        return isinstance(other, Coded) and (self.table, self.index) == (other.table, other.index)

    def __hash__(self):
        return hash((self.table, self.index))

    def __repr__(self):
        return f"{self.table}[{self.index}]"


class Table:
    """Rows of one table. Row numbers start at 1, as metadata tokens do."""

    def __init__(self, metadata, name, columns, count, offset, sizes):
        self.md = metadata
        self.name = name
        self.count = count
        self.offset = offset
        self.columns = columns
        fmt = "<" + "".join({1: "B", 2: "H", 4: "I"}[size] for size in sizes)
        self._struct = struct.Struct(fmt)
        self.row_size = self._struct.size
        self._names = [name for name, _ in columns]
        self._kinds = [kind for _, kind in columns]
        self._cache = {}

    def __len__(self):
        return self.count

    def raw(self, index):
        """The row as a tuple of integers, as stored."""
        return self._struct.unpack_from(self.md.data, self.offset + (index - 1) * self.row_size)

    def __getitem__(self, index):
        if not 1 <= index <= self.count:
            raise IndexError(f"{self.name}[{index}] out of range 1..{self.count}")
        row = self._cache.get(index)
        if row is None:
            row = Row(self, index, self.raw(index))
            self._cache[index] = row
        return row

    def __iter__(self):
        for index in range(1, self.count + 1):
            yield self[index]

    def column(self, name):
        return self._names.index(name)


class Row:
    """One row, with its columns as attributes: heap columns as str/bytes,
    table columns as row numbers, coded indexes as Coded, and list columns as
    a range of row numbers."""

    __slots__ = ("table", "index", "_raw", "_values")

    def __init__(self, table, index, raw):
        self.table = table
        self.index = index
        self._raw = raw
        self._values = {}

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            pass
        table = self.table
        try:
            i = table._names.index(name)
        except ValueError:
            raise AttributeError(name) from None
        value = table.md._decode(table, i, self._raw[i], self.index)
        self._values[name] = value
        return value

    def __repr__(self):
        return f"{self.table.name}[{self.index}]"


class Metadata:
    def __init__(self, path_or_bytes):
        if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
            self.data = bytes(path_or_bytes)
        else:
            with open(path_or_bytes, "rb") as f:
                self.data = f.read()
        self._strings = {}
        self._parse()

    # -- PE and metadata root ------------------------------------------------------

    def _parse(self):
        d = self.data
        if d[:2] != b"MZ":
            raise ValueError("not a PE file")
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        if d[pe : pe + 4] != b"PE\0\0":
            raise ValueError("no PE signature")
        coff = pe + 4
        (sections_count,) = struct.unpack_from("<H", d, coff + 2)
        (optional_size,) = struct.unpack_from("<H", d, coff + 16)
        opt = coff + 20
        (magic,) = struct.unpack_from("<H", d, opt)
        directories = opt + (96 if magic == 0x10B else 112)
        cli_rva, _ = struct.unpack_from("<II", d, directories + 14 * 8)
        self._sections = []
        sec = opt + optional_size
        for i in range(sections_count):
            vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", d, sec + i * 40 + 8)
            self._sections.append((va, max(vsize, raw_size), raw_ptr))
        cli = self._offset(cli_rva)
        md_rva, _ = struct.unpack_from("<II", d, cli + 8)
        root = self._offset(md_rva)
        if struct.unpack_from("<I", d, root)[0] != 0x424A5342:
            raise ValueError("no metadata signature")
        (version_length,) = struct.unpack_from("<I", d, root + 12)
        pos = root + 16 + version_length
        _, stream_count = struct.unpack_from("<HH", d, pos)
        pos += 4
        self.streams = {}
        for _ in range(stream_count):
            offset, size = struct.unpack_from("<II", d, pos)
            pos += 8
            end = d.index(b"\0", pos)
            name = d[pos:end].decode("ascii")
            pos = (end + 4) & ~3
            self.streams[name] = (root + offset, size)
        self._parse_tables()

    def _offset(self, rva):
        for va, size, raw in self._sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError(f"RVA 0x{rva:x} is in no section")

    def _parse_tables(self):
        d = self.data
        start, _ = self.streams.get("#~") or self.streams["#-"]
        heap_sizes = d[start + 6]
        self._string_size = 4 if heap_sizes & 1 else 2
        self._guid_size = 4 if heap_sizes & 2 else 2
        self._blob_size = 4 if heap_sizes & 4 else 2
        (valid,) = struct.unpack_from("<Q", d, start + 8)
        pos = start + 24
        counts = {}
        for number in range(64):
            if valid >> number & 1:
                counts[number] = struct.unpack_from("<I", d, pos)[0]
                pos += 4
        self._counts = {SCHEMA[n][0]: c for n, c in counts.items() if n in SCHEMA}
        self.tables = {}
        for number in sorted(counts):
            if number not in SCHEMA:
                raise ValueError(f"unknown metadata table 0x{number:x}")
            name, columns = SCHEMA[number]
            sizes = [self._column_size(kind) for _, kind in columns]
            table = Table(self, name, columns, counts[number], pos, sizes)
            self.tables[name] = table
            pos += table.row_size * table.count

    def _column_size(self, kind):
        if kind == "u1":
            return 1
        if kind == "u2":
            return 2
        if kind == "u4":
            return 4
        if kind == "str":
            return self._string_size
        if kind == "guid":
            return self._guid_size
        if kind == "blob":
            return self._blob_size
        if kind in CODED:
            bits, tables = CODED[kind]
            largest = max(self._counts.get(t, 0) for t in tables if t)
            return 2 if largest < 1 << (16 - bits) else 4
        return 2 if self._counts.get(kind, 0) < 1 << 16 else 4

    def _decode(self, table, column, value, index):
        kind = table._kinds[column]
        if kind == "str":
            return self.string(value)
        if kind == "blob":
            return self.blob(value)
        if kind == "guid":
            return self.guid(value)
        if kind in CODED:
            bits, tables = CODED[kind]
            return Coded(tables[value & ((1 << bits) - 1)], value >> bits)
        target = LISTS.get((table.name, table._names[column]))
        if target:
            end = self.table(target).count + 1
            if index < table.count:
                end = table.raw(index + 1)[column]
            return range(value, end)
        return value

    # -- heaps ----------------------------------------------------------------------

    def string(self, offset):
        s = self._strings.get(offset)
        if s is None:
            start = self.streams["#Strings"][0] + offset
            s = self.data[start : self.data.index(b"\0", start)].decode("utf-8")
            self._strings[offset] = s
        return s

    def blob(self, offset):
        start = self.streams["#Blob"][0] + offset
        length, size = compressed(self.data, start)
        return self.data[start + size : start + size + length]

    def guid(self, index):
        if not index:
            return None
        start = self.streams["#GUID"][0] + (index - 1) * 16
        return self.data[start : start + 16]

    def table(self, name):
        """The table, or an empty one when the file has none."""
        t = self.tables.get(name)
        if t is None:
            number = TABLE_NUMBER[name]
            t = Table(self, name, SCHEMA[number][1], 0, 0, [1] * len(SCHEMA[number][1]))
            self.tables[name] = t
        return t

    def row(self, coded):
        return self.table(coded.table)[coded.index]


def compressed(data, pos):
    """An unsigned integer compressed as in II.23.2, and how many bytes it took."""
    first = data[pos]
    if first & 0x80 == 0:
        return first, 1
    if first & 0xC0 == 0x80:
        return (first & 0x3F) << 8 | data[pos + 1], 2
    return (first & 0x1F) << 24 | data[pos + 1] << 16 | data[pos + 2] << 8 | data[pos + 3], 4


class Blob:
    """Reads a signature or custom attribute blob front to back."""

    def __init__(self, data):
        self.data = bytes(data)
        self.pos = 0

    def byte(self):
        self.pos += 1
        return self.data[self.pos - 1]

    def peek(self):
        return self.data[self.pos]

    def compressed(self):
        value, size = compressed(self.data, self.pos)
        self.pos += size
        return value

    def unpack(self, fmt):
        values = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += struct.calcsize("<" + fmt)
        return values

    def ser_string(self):
        if self.data[self.pos] == 0xFF:
            self.pos += 1
            return None
        length = self.compressed()
        self.pos += length
        return self.data[self.pos - length : self.pos].decode("utf-8")

    def at_end(self):
        return self.pos >= len(self.data)
