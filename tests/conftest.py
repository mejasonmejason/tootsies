"""Stub env vars so importing config/db/claude_client doesn't blow up under pytest."""

import os
import sys
from pathlib import Path

# Add repo root to path so `import bot` works from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Tests run without real secrets, force-set to stubs so config.from_env() doesn't blow up.
# We overwrite (not setdefault) because CI/dev shells may have empty values set.
for k, v in {
    "DISCORD_TOKEN": "test-token",
    "ANTHROPIC_API_KEY": "test-key",
    "GITHUB_TOKEN": "test-gh-token",
    "GITHUB_REPO": "test/repo",
    "DATABASE_URL": "postgres://test:test@localhost:5432/test",
    "BOT_LOGS_VERBOSITY": "milestones",
}.items():
    if not os.environ.get(k):
        os.environ[k] = v
