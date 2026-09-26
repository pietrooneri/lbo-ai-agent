#!/bin/zsh
# Build the standalone macOS app: packaging/dist/LBO Agent.app
# Usage (from the project folder):  packaging/build_app.sh [--install]
#   --install  also copies it to /Applications (replacing an older build)
set -euo pipefail
cd "$(dirname "$0")/.."
source "$HOME/.local/bin/env" 2>/dev/null || true

NICEGUI_DIR=$(uv run python -c "import nicegui, os; print(os.path.dirname(nicegui.__file__))")
[[ -f packaging/icon.icns ]] || uv run python packaging/make_icon.py

uv run pyinstaller app.py \
  --name "LBO Agent" \
  --windowed --noconfirm --clean \
  --icon "$PWD/packaging/icon.icns" \
  --osx-bundle-identifier com.pietrooneri.lboagent \
  --add-data "$NICEGUI_DIR:nicegui" \
  --add-data "$PWD/data/sector_benchmarks.json:data" \
  --collect-submodules keyring \
  --distpath packaging/dist --workpath packaging/build --specpath packaging/build

APP="packaging/dist/LBO Agent.app"
echo "Built $APP ($(du -sh "$APP" | cut -f1))"
if [[ "${1:-}" == "--install" ]]; then
  rm -rf "/Applications/LBO Agent.app"
  cp -R "$APP" /Applications/
  echo "Installed /Applications/LBO Agent.app"
fi
