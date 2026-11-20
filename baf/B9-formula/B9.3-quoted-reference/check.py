"""Check the final workspace against expectations kept outside the fixture."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from artifact_check import check_artifact
from check_support import run

TASK = Path(__file__).resolve().parent

if __name__ == "__main__":
    run(lambda work: check_artifact(TASK, work))
