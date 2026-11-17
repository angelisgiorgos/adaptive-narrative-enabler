"""Entry point of the standalone ANE AI (PyInstaller): the runtime's command line.

    ane-ai serve --port 0 --ready-file ready.json --parent-pid <UNP pid>
"""
import multiprocessing
import sys

from narrative_runtime.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # offline training spawns processes; frozen builds need this
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the ready line is UTF-8 JSON on every OS
    main()
