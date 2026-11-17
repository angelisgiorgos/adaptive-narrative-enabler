# Standalone ANE AI runtimes

`ane-runtime-<platform>.zip` (windows-x64, linux-x64, macos-arm64, macos-x64) built by
`packaging/build_runtime.py` (CI: `.github/workflows/runtime.yml`; releases attach them). The API
puts an engine package next to one to export a standalone ANE AI (`POST /v1/exports` with
`platform`). A runtime is offered only when its code version is the API's own. In a deployment,
download them from the release that matches the image into this folder, or point
`ANE_RUNTIMES_DIR` at a folder that holds them.
