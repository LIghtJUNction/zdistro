"""Create zdistro's runnable standard-library-only Python archive."""

from pathlib import Path
import shutil
import sys
import tempfile
import zipapp


def main():
    destination = Path(sys.argv[1]).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "zdistro"
    with tempfile.TemporaryDirectory(prefix="zdistro-zipapp-") as temporary:
        root = Path(temporary)
        shutil.copytree(source, root / "zdistro", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        zipapp.create_archive(root, destination, interpreter="/usr/bin/env python3", main="zdistro.cli:main", compressed=True)
    destination.chmod(0o755)


if __name__ == "__main__":
    main()
