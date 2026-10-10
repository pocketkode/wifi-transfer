#!/usr/bin/env bash
# © 2026 PocketKode. All rights reserved.
# Builds WiFiTransfer.muxapp (the installable muOS package) from this source folder.
#
# Usage:   ./build.sh            build dist/WiFiTransfer.muxapp
#          ./build.sh --fresh    re-download the bundled pieces
#
# Needs: bash, curl, unzip, zip, shasum and python3. The package holds the app's Python source as it is, plus
# PySDL2, the segno QR code library and fonts (DejaVu Sans; Noto Sans JP, SC and KR for the other languages, checked
# against their SHA-256). The console already has Python 3, SDL2, SDL2_ttf and SDL2_image.
set -euo pipefail
cd "$(dirname "$0")"
SRC="$PWD"
DIST="${DIST:-$SRC/dist}"
CACHE="$DIST/cache"
APP_NAME="WiFiTransfer"
STAGE="$DIST/stage/$APP_NAME"
DEJAVU_URL="https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip"
NOTO_BASE="https://github.com/notofonts/noto-cjk/releases/download/Sans2.004"
# zip on the Noto CJK release · font file · its SHA-256
FONTS=(
	"16_NotoSansJP.zip NotoSansJP-Regular.otf dff723ba59d57d136764a04b9b2d03205544f7cd785a711442d6d2d085ac5073"
	"18_NotoSansSC.zip NotoSansSC-Regular.otf faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9"
	"17_NotoSansKR.zip NotoSansKR-Regular.otf 69975a0ac8472717870aefeab0a4d52739308d90856b9955313b2ad5e0148d68"
)

for tool in curl unzip zip shasum python3; do
	command -v "$tool" >/dev/null || { echo "Missing tool: $tool"; exit 1; }
done
[ "${1:-}" = "--fresh" ] && rm -rf "$CACHE"
mkdir -p "$CACHE"

# fetch_wheel PACKAGE DIR : unpack the pure-Python package DIR from a pip wheel into the cache
fetch_wheel() {
	if [ ! -d "$CACHE/$2" ]; then
		echo "  downloading $1"
		rm -rf "$CACHE/$1-tmp" && mkdir -p "$CACHE/$1-tmp"
		python3 -m pip download --quiet --no-deps --only-binary=:all: "$1" -d "$CACHE/$1-tmp"
		unzip -q "$CACHE/$1-tmp"/*.whl -d "$CACHE/$1-tmp/whl"
		mv "$CACHE/$1-tmp/whl/$2" "$CACHE/$2"
		rm -rf "$CACHE/$1-tmp"
	else
		echo "  cached      $1"
	fi
}

echo "==> Downloading dependencies"
if [ ! -s "$CACHE/dejavu.zip" ]; then
	echo "  downloading dejavu.zip"
	curl -fL --retry 3 -o "$CACHE/dejavu.zip.part" "$DEJAVU_URL"
	mv "$CACHE/dejavu.zip.part" "$CACHE/dejavu.zip"
else
	echo "  cached      dejavu.zip"
fi
fetch_wheel pysdl2 sdl2
rm -rf "$CACHE/sdl2/test" "$CACHE/sdl2/examples"
fetch_wheel segno segno

echo "==> Fonts (Noto Sans JP, SC and KR)"
for entry in "${FONTS[@]}"; do
	read -r zipname file sum <<<"$entry"
	base="${file%-Regular.otf}"
	if [ ! -f "$CACHE/$file" ]; then
		echo "  downloading $zipname"
		curl -fsSL -o "$CACHE/$zipname" "$NOTO_BASE/$zipname"
		rm -rf "$CACHE/unz" && unzip -q -o "$CACHE/$zipname" "$file" LICENSE -d "$CACHE/unz"
		mv "$CACHE/unz/$file" "$CACHE/$file"
		mv "$CACHE/unz/LICENSE" "$CACHE/$base-OFL.txt"
		rm -rf "$CACHE/unz" "$CACHE/$zipname"
	fi
	echo "$sum  $CACHE/$file" | shasum -a 256 -c --quiet - || { echo "Font checksum mismatch: $file"; exit 1; }
done

echo "==> Assembling app folder"
rm -rf "$DIST/stage"
mkdir -p "$STAGE/deps" "$STAGE/fonts" "$STAGE/glyph" "$STAGE/lang" "$STAGE/licenses" "$STAGE/web" "$STAGE/wtransfer"
cp "$SRC/main.py" "$SRC/mux_launch.sh" "$SRC/mux_lang.ini" "$SRC/config.ini" "$SRC/README.md" "$SRC/LICENSE" \
	"$SRC/THIRD-PARTY-NOTICES.md" "$STAGE/"
cp "$SRC"/wtransfer/*.py "$STAGE/wtransfer/"
cp "$SRC"/lang/*.json "$STAGE/lang/"
cp "$SRC"/licenses/* "$STAGE/licenses/"
cp "$SRC"/glyph/* "$STAGE/glyph/"
cp "$SRC"/web/* "$STAGE/web/"
cp -R "$CACHE/sdl2" "$STAGE/deps/sdl2"
cp -R "$CACHE/segno" "$STAGE/deps/segno"
unzip -q -j -o "$CACHE/dejavu.zip" "*/ttf/DejaVuSans.ttf" "*/LICENSE" -d "$CACHE/dejavu"
cp "$CACHE/dejavu/DejaVuSans.ttf" "$STAGE/fonts/"
cp "$CACHE/dejavu/LICENSE" "$STAGE/licenses/DejaVu-Fonts.txt"
for entry in "${FONTS[@]}"; do
	read -r _zip file _sum <<<"$entry"
	cp "$CACHE/$file" "$STAGE/fonts/"
	cp "$CACHE/${file%-Regular.otf}-OFL.txt" "$STAGE/licenses/"
done
chmod +x "$STAGE/mux_launch.sh"
find "$STAGE" \( -name "__pycache__" -o -name ".DS_Store" \) -prune -exec rm -rf {} +

echo "==> Checking Python syntax and translations"
python3 - "$STAGE" <<'EOF'
import ast, json, pathlib, sys
app = pathlib.Path(sys.argv[1])
for p in list(app.glob("*.py")) + list(app.glob("wtransfer/*.py")):
    ast.parse(p.read_text(), str(p))
langs = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(app.glob("lang/*.json"))}
every = set().union(*(d["texts"] for d in langs.values()))
for code, d in langs.items():
    missing = every - set(d["texts"])
    if missing:
        print(f"  {code}: {len(missing)} texts not translated yet (they show in English)")
print(f"  ok ({len(langs)} translations)")
EOF

if [ "${NO_PACKAGE:-}" = "1" ]; then
	echo "Staged: $STAGE"
	exit 0
fi
echo "==> Packaging"
rm -f "$DIST/$APP_NAME.muxapp"
(cd "$DIST/stage" && zip -qr -X "$DIST/$APP_NAME.muxapp" "$APP_NAME")
echo "Done: $DIST/$APP_NAME.muxapp ($(du -h "$DIST/$APP_NAME.muxapp" | cut -f1))"
echo "Copy it to the ARCHIVE folder on SD card 1, then install via Applications > Archive Manager."
