# Packaging the desktop app (macOS)

```bash
packaging/build_app.sh             # builds packaging/dist/LBO Agent.app (~100 MB, PyInstaller)
packaging/build_app.sh --install   # also copies it to /Applications
```

- **Standalone:** the app bundles Python, NiceGUI, pywebview and the calibrated benchmarks;
  it needs neither this folder nor uv to run.
- **Where things live when packaged:**
  - projects and Excel files: `~/Documents/LBO Agent`;
  - language setting: `~/Library/Application Support/LBO Agent`;
  - API key: macOS Keychain, service `lbo-agent`, shared with the source version.
- **First launch after each build:** macOS asks whether "LBO Agent" may read the `lbo-agent`
  key in the Keychain. Click **Always Allow**. Until the prompt is answered, the window stays blank.
- **Unsigned (ad-hoc) build**, for personal use: built on this Mac, so Gatekeeper does not block it.
  To share it, it would need a Developer ID signature and notarisation.
- **Icon:** drawn by `make_icon.py`, which writes `icon.png` and `icon.icns`.
- **Debugging a hang:** `kill -USR1 <pid>` makes the app print every thread's stack.
- **Rebuild after code changes:** the bundle is a snapshot of the code at build time.
