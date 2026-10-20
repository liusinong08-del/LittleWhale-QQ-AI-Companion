#!/data/data/com.termux/files/usr/bin/bash
# Export only public package archives, not any application data or credentials.
set -eu
exec > /sdcard/LittleWhale-source/runtime-export.log 2>&1
mkdir -p /sdcard/LittleWhale-packages
cd /sdcard/LittleWhale-packages
apt-get download libc++ liblz4 pcre2 zlib zstd openssl ca-certificates libandroid-support libbz2 liblzma readline
cp /data/data/com.termux/cache/apt/archives/*.deb /sdcard/LittleWhale-packages/
apt-cache dumpavail > /sdcard/LittleWhale-packages/Packages
printf 'PUBLIC_PACKAGE_EXPORT_COMPLETE\n'
