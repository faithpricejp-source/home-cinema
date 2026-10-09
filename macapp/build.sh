#!/bin/zsh
# 编译 Home Cinema.app 到 build/，再拷到 /Applications
set -e
here=${0:A:h}
out=$here/../build
app="$out/影院.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
# 不指定 -target 时 swiftc 会按 SDK 版本定最低系统，可能比本机系统还新，导致打不开
# mpv：头文件在 /opt/homebrew/include/mpv/，库是 /opt/homebrew/lib/libmpv.dylib（Homebrew mpv 0.41）
swiftc -O -target arm64-apple-macos13.0 \
  -import-objc-header "$here/mpv-bridge.h" \
  -I/opt/homebrew/include -L/opt/homebrew/lib -lmpv -framework OpenGL \
  -o "$app/Contents/MacOS/HomeCinema" "$here/main.swift" "$here/MPVPlayerView.swift"
cp "$here/Info.plist" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :HCProjectRoot ${here:h}" "$app/Contents/Info.plist"
iconset=$out/AppIcon.iconset
mkdir -p $iconset
# 图标：繁体单字 + 色线
swift "$here/make_char_icon.swift" 影 B7791F $out/icon-1024.png
for s in 16 32 128 256 512; do
  sips -z $s $s $out/icon-1024.png --out $iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) $out/icon-1024.png --out $iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns $iconset -o "$app/Contents/Resources/AppIcon.icns"
codesign --force --sign - "$app"
# 内嵌播放器依赖 libmpv：链不上就当场失败，别等运行时才发现
otool -L "$app/Contents/MacOS/HomeCinema" | grep -q libmpv || {
  echo "ERROR: 二进制没有依赖 libmpv" >&2
  exit 1
}
otool -L "$app/Contents/MacOS/HomeCinema"
ditto "$app" "/Applications/影院.app"
echo "built: /Applications/影院.app"
