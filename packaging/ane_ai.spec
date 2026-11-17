# PyInstaller spec of the standalone ANE AI runtime: one folder (onedir) with the program
# `ane-ai` (`ane-ai.exe` on Windows), Python, numpy, PyYAML and the engine code. The engine
# package (engine.json, config/, data/) goes next to the program; nothing of it is built in.
# Build with packaging/build_runtime.py (it also writes the code version and runtime.json).
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
hidden = collect_submodules("core_engine", filter=lambda name: name != "core_engine.augmenter")
hidden += collect_submodules("narrative_runtime")
hidden += ["utils.config_loader", "utils.tag_similarity", "ctypes", "secrets", "hmac", "select", "http.server",
           "urllib.request", "multiprocessing", "concurrent.futures"]
excludes = ["torch", "transformers", "sentence_transformers", "sklearn", "scipy", "matplotlib", "gradio", "fastapi",
            "uvicorn", "pandas", "core_engine.augmenter", "evaluator", "backend", "app", "api", "main", "tkinter",
            "IPython", "pytest"]

analysis = Analysis([os.path.join(SPECPATH, "launcher.py")], pathex=[ROOT], hiddenimports=hidden, excludes=excludes,
                    noarchive=False)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="ane-ai", console=True, upx=False,
          disable_windowed_traceback=False)
COLLECT(exe, analysis.binaries, analysis.datas, upx=False, name="ane-ai")
