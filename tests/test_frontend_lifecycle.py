"""Exercise controlled asynchronous response ordering in actual frontend code."""
import shutil
import subprocess
from pathlib import Path
import pytest

@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required')
def test_frontend_lifecycle():
    repo = Path(__file__).resolve().parent.parent
    result = subprocess.run(['node', '--test', 'tests/frontend_lifecycle.mjs'],
                            cwd=repo, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
