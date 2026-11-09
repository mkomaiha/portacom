# Linux test environment for portacom, mirroring .github/workflows/ci.yml.
# Keep the ARGs in sync with ci.yml's env. From the repository root:
#   docker build -t portacom-linux-tests tests/linux
#   docker run --rm -v "$PWD:/src:ro" portacom-linux-tests
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake g++ make libgtest-dev pybind11-dev curl ca-certificates binutils \
    && rm -rf /var/lib/apt/lists/*
ARG MINICOM_REPO=https://github.com/mkomaiha/MiniCOM
ARG MINICOM_REF=249b3a0a03964cc1e9e17b20d90fdd9116d5e598
RUN git clone -q "$MINICOM_REPO" /opt/minicom && git -C /opt/minicom checkout -q "$MINICOM_REF" \
    && cmake -S /opt/minicom -B /opt/minicom/build -DCMAKE_BUILD_TYPE=Release -DPython_EXECUTABLE="$(command -v python)" \
    && cmake --build /opt/minicom/build --target ExampleLib -j 4 \
    && ln -s "$(find /opt/minicom/build -name 'libExampleLib.so' | head -1)" /opt/libExampleLib.so \
    && nm -D /opt/libExampleLib.so | grep -c CoCreateInstance
ENV MINICOM_LIBRARY=/opt/libExampleLib.so
ARG DXC_RELEASE=v1.9.2609
ARG DXC_LINUX=linux_dxc_2026_09_28.x86_x64.tar.gz
RUN mkdir -p /opt/dxc && curl -sSfL "https://github.com/microsoft/DirectXShaderCompiler/releases/download/$DXC_RELEASE/$DXC_LINUX" | tar -xz -C /opt/dxc
ENV DXC_LIBRARY=/opt/dxc/lib/libdxcompiler.so
ARG WIN32METADATA_VERSION=71.0.45-preview
RUN mkdir -p /opt/winmd && curl -sSfL -o /tmp/metadata.zip "https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.win32metadata/$WIN32METADATA_VERSION/microsoft.windows.sdk.win32metadata.$WIN32METADATA_VERSION.nupkg" \
    && python -c "import zipfile; z = zipfile.ZipFile('/tmp/metadata.zip'); z.extract('Windows.Win32.winmd', '/opt/winmd'); z.extract('Microsoft.Direct3D.D3D12.winmd', '/opt/winmd')" && rm /tmp/metadata.zip
ENV WIN32_WINMD=/opt/winmd/Windows.Win32.winmd
ENV D3D12_WINMD=/opt/winmd/Microsoft.Direct3D.D3D12.winmd
ENV PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install -q pytest ruff
WORKDIR /src
# mount a checkout at /src (read-only is fine): the packages are installed from a copy at run time
CMD ["sh", "-c", "cp -r runtime generator dxc /tmp && pip install -q /tmp/runtime /tmp/generator /tmp/dxc && python -B -m pytest -q -rs -p no:cacheprovider tests"]
