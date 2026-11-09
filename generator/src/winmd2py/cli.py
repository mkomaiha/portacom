"""winmd2py FILE.winmd [-n NAMESPACE ...] [--ref OTHER.winmd ...] -o OUTDIR"""

import argparse
import sys
import time

from . import __version__
from .emit import Generator
from .model import WinMD


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="winmd2py",
        description="Generate portacom ctypes bindings for classic COM interfaces from a .winmd file. "
        "The bindings follow the Windows ABI the metadata describes; for a library built differently, "
        "call portacom.use_abi() before importing them.",
    )
    parser.add_argument("winmd", help="the .winmd file (Win32 metadata format)")
    parser.add_argument(
        "-n",
        "--namespace",
        action="append",
        dest="namespaces",
        help="namespace to generate (repeatable; default: all namespaces in the file)",
    )
    parser.add_argument(
        "--ref",
        action="append",
        default=[],
        metavar="OTHER.winmd",
        help="a .winmd whose types the file uses, such as Windows.Win32.winmd for "
        "Microsoft.Direct3D.D3D12.winmd (repeatable); what is used from it is generated too",
    )
    parser.add_argument("-o", "--outdir", help="output package directory")
    parser.add_argument("--list", action="store_true", help="list the namespaces in the file and exit")
    parser.add_argument("-q", "--quiet", action="store_true", help="do not print what was left out or simplified")
    parser.add_argument("--version", action="version", version=f"winmd2py {__version__}")
    args = parser.parse_args(argv)
    if not args.list and not args.outdir:
        parser.error("-o/--outdir is required")

    start = time.time()
    try:
        if args.list:
            for ns in WinMD(args.winmd).namespaces():
                print(ns)
            return 0
        generator = Generator(args.winmd, args.namespaces, refs=args.ref)
        written = generator.generate(args.outdir)
    except (OSError, ValueError) as error:
        print(f"winmd2py: error: {error}", file=sys.stderr)
        return 1
    if not args.quiet:
        for note in generator.notes:
            print(f"winmd2py: {note}", file=sys.stderr)
    print(f"winmd2py: wrote {len(written)} modules to {args.outdir} in {time.time() - start:.1f}s")
    return 0
