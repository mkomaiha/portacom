"""Real OLE on Windows, through bindings for the whole Windows.Win32.System.Com namespace."""

import ctypes
import sys

import portacom
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs OLE32")

CLSID_StdComponentCategoriesMgr = "0002E005-0000-0000-C000-000000000046"


@pytest.fixture(scope="module")
def com(win32):
    return win32["Com"]


def test_co_get_malloc(com):
    malloc = com.CoGetMalloc(1)
    assert type(malloc) is com.IMalloc
    p = malloc.Alloc(100)
    assert p and malloc.GetSize(p) == 100
    malloc.Free(p)


def test_create_and_enumerate(com):
    portacom.co_initialize()
    # the generated CoCreateInstance: riid is the interface class, ppv comes back typed
    cat = com.CoCreateInstance(CLSID_StdComponentCategoriesMgr, None, com.CLSCTX_INPROC_SERVER, com.ICatInformation)
    assert type(cat) is com.ICatInformation
    enum = cat.EnumCategories(0x409)
    infos, fetched = enum.Next(3)  # an [out] array of structs, sized by an [in] count
    assert fetched == 3 and len(infos) == 3
    assert all(isinstance(i.catid, portacom.GUID) for i in infos)
    assert all(i.szDescription for i in infos)

    # the runtime's own helper does the same
    cat2 = portacom.co_create_instance(CLSID_StdComponentCategoriesMgr, com.ICatInformation)
    assert cat2.query(com.IMalloc) is None


def test_clsid_from_progid():
    assert portacom.clsid_from_progid("Scripting.Dictionary") == portacom.GUID("EE09B103-97E0-11CF-978F-00A02463E06F")
    with pytest.raises(portacom.COMError) as e:
        portacom.clsid_from_progid("portacom.NoSuchClass")
    assert e.value.hr == 0x800401F3  # CO_E_CLASSSTRING


def test_struct_layouts_match_windows(com):
    import win32_bindings.Windows.Win32.System.Variant as Variant

    assert Variant.VARIANT is portacom.VARIANT
    assert ctypes.sizeof(Variant.VARIANT) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
    assert ctypes.sizeof(com.STGMEDIUM) == 3 * ctypes.sizeof(ctypes.c_void_p)
