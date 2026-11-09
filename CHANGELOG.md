# Changelog

All notable changes to portacom, winmd2py and portacom-dxc. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the packages use
[semantic versioning](https://semver.org/) and are released together.

## [Unreleased]

### Added

- `tests/test_reader_reference.py` cross-checks winmd2py's reader against the
  `winmd` package (py-winmd) on every type of `Windows.Win32.winmd`; `winmd`
  is a test dependency only.

## [0.1.0] - 2026-11-08

Initial release.

- `portacom`: the runtime. Calls classic (IUnknown-based) COM interfaces
  through ctypes on Windows, Linux and macOS, implements them in Python, and
  converts BSTRs, VARIANTs, SAFEARRAYs, GUIDs, enums and callbacks. IDispatch
  automation. A library built for Linux or macOS declares how it differs from
  the Windows ABI with `portacom.use_abi`. Pure Python, no dependencies.
- `winmd2py`: generates portacom bindings from a `.winmd` file, in pure
  Python, including types from other files (`--ref`) and every architecture.
- `portacom-dxc`: DirectX Shader Compiler bindings plus `compile()` and
  `disassemble()`.

[Unreleased]: https://github.com/mkomaiha/portacom/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mkomaiha/portacom/releases/tag/v0.1.0
