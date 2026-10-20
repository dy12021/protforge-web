"""共享 pytest 夹具。

职责：
1. 把 `server/` 加进 sys.path，让测试能 `import db, store, skills_index`。
2. autouse 把数据库指向 tmp_path + 初始化 schema——测试**绝不碰**真实的
   `<数据盘根>\\web.db`（计划 Global Constraints）。

`db` 的 import 放在 fixture 内部而不是模块顶层，这样某个正在改的模块
导入失败时，不会连带让不依赖它的测试无法收集。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    """每个测试一块干净的临时库，跑完即弃。"""
    monkeypatch.setenv("PROTFORGE_WEB_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("PROTFORGE_WEB_DB", str(tmp_path / "data" / "web.db"))

    import db

    db.init_db()
    return tmp_path / "data" / "web.db"
