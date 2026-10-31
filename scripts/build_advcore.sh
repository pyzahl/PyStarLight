#!/bin/sh
# Build Adv2's native library (ADVCore, MPL 2.0) for this machine and put it into the venv's Adv2.
# Needed on Apple Silicon: the Adv2 1.2.0 wheel ships an x86_64 libAdvCore.dylib only.
#
#   sh scripts/build_advcore.sh              # uses .venv/bin/python (uv sync --extra adv first)
#   PYTHON=/path/to/python sh scripts/build_advcore.sh
#
# Needs git and a C++ compiler (macOS: xcode-select --install). Re-run after reinstalling Adv2.
set -eu

PYTHON=${PYTHON:-.venv/bin/python}
REPO=https://github.com/AstroDigitalVideo/ADVCore.git
COMMIT=bbcda85            # 2020-03-14, matches the API Adv2 1.2.0 calls

# Adv2/Adv2DLLlibs without importing Adv2 (the import loads the broken library)
LIBDIR=$("$PYTHON" -c "import importlib.util as u, pathlib; \
print(pathlib.Path(u.find_spec('Adv2').submodule_search_locations[0]) / 'Adv2DLLlibs')")
case $(uname -s) in
    Darwin) LIB=libAdvCore.dylib; LDFLAGS="-dynamiclib -install_name @rpath/libAdvCore.dylib" ;;
    Linux)  LIB=libAdvCore64.so;  LDFLAGS="-shared" ;;
    *) echo "unsupported system $(uname -s)"; exit 1 ;;
esac

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git clone -q "$REPO" "$WORK/ADVCore"
git -C "$WORK/ADVCore" checkout -q "$COMMIT"

SRC="$WORK/ADVCore/src"
CXX=${CXX:-c++}
CC=${CC:-cc}
"$CC" -O2 -fPIC -c "$SRC/quicklz.c" -o "$WORK/quicklz.o"
"$CXX" -std=c++11 -O2 -fPIC -w -D_FILE_OFFSET_BITS=64 $LDFLAGS -o "$WORK/$LIB" \
    $(ls "$SRC"/*.cpp | grep -v -e dllmain.cpp -e stdafx.cpp) "$WORK/quicklz.o"

[ -f "$LIBDIR/$LIB.orig" ] || cp "$LIBDIR/$LIB" "$LIBDIR/$LIB.orig"
cp "$WORK/$LIB" "$LIBDIR/$LIB"
file "$LIBDIR/$LIB" 2>/dev/null || true
"$PYTHON" -c "from Adv2 import AdvLib; print('Adv2 native library loads:', AdvLib.GetLibraryPlatformId())"
