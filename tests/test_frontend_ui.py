"""UI behavior and semantic references across all shipped frontend pages."""
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parent.parent

@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required')
def test_frontend_ui_behaviors():
    result = subprocess.run(['node','--test','tests/frontend_ui.mjs'],cwd=REPO,
                            capture_output=True,text=True,encoding='utf-8',
                            errors='replace',timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr

class Semantics(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.refs = set()
        self.dialogs = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            assert attrs['id'] not in self.ids, f"Duplicate id: {attrs['id']}"
            self.ids.add(attrs['id'])
        for name in ('for','aria-labelledby','aria-describedby','aria-controls'):
            self.refs.update((attrs.get(name) or '').split())
        if tag == 'dialog':
            self.dialogs.append(attrs)

@pytest.mark.parametrize('page',['web/index.html','web/workflow.html','admin-ui/index.html'])
def test_accessible_references_exist_and_dialogs_have_names(page):
    semantics=Semantics()
    semantics.feed((REPO/page).read_text(encoding='utf-8'))
    assert not semantics.refs - semantics.ids
    for dialog in semantics.dialogs:
        assert dialog.get('aria-labelledby') or dialog.get('aria-label'), dialog

@pytest.mark.parametrize('path',['/','/index.html','/app.js','/style.css','/workflow.html','/workflow.js'])
def test_public_static_resources_require_cache_revalidation(path):
    from fastapi.testclient import TestClient
    import app
    with TestClient(app.create_public_app()) as client:
        response=client.get(path)
        assert response.status_code == 200
        assert response.headers.get('cache-control') == 'no-cache'
