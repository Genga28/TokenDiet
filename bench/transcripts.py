"""Where tokens go in real Claude Code sessions. Implementation lives in tokendiet.sessions.

  python -m bench.transcripts [project_dir ...] [--json out.json]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tokendiet.sessions import main  # noqa: E402

if __name__ == "__main__":
    main()
