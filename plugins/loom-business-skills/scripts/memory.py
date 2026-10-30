"""Run directly from an installed plugin directory, without a project checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from loom_business_skills.cli import main

if __name__ == "__main__":
    main()
