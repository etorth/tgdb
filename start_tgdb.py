"""Launch tgdb from this source checkout without changing directories."""

import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parent
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)

    from tgdb.__main__ import main as tgdb_main

    tgdb_main()


if __name__ == "__main__":
    main()
