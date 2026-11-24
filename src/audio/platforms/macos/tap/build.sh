#!/usr/bin/env bash
# Build the universal (arm64 + x86_64) Core Audio tap helper. Requires Xcode command line tools.
set -euo pipefail
cd "$(dirname "$0")"
OUT=build; mkdir -p "$OUT"
for arch in arm64 x86_64; do
  swiftc -O -swift-version 5 -target "$arch-apple-macos14.2" dsaudiotap.swift -o "$OUT/dsaudiotap-$arch"
done
lipo -create "$OUT/dsaudiotap-arm64" "$OUT/dsaudiotap-x86_64" -output "$OUT/dsaudiotap"
rm -f "$OUT/dsaudiotap-arm64" "$OUT/dsaudiotap-x86_64"
lipo -info "$OUT/dsaudiotap"
