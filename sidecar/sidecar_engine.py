"""Compatibility entrypoint for the consolidated engine adapter."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sidecar.sidecar import main

if __name__ == "__main__":
    main()
