"""app.py 的测试（Task 5）。

三个设计要点各自的钉死项：

1. **双端口物理隔离**——这里测的是"路由不共存"：public app 上没有
   /api/internal/*，internal app 上没有 /api/tasks。真隔离靠端口只绑
   loopback（由 create_public_app/create_internal_app 各自起一个 uvicorn
   兑现），路由不共存是它的第二道闸；两者缺一，反代就可能把内部通道
   暴露出去。
2. **内部通道两套令牌**——test_agent_token_cannot_touch_other_task 是
   agent_token 唯一的作用域保证：任务 A 的令牌打任务 B 必须 403。
3. **detail 恒为中文字符串**——包括 pydantic 框架层错误（422）、
   路由不存在（404）、方法不对（405）。

测试走真实 HTTP：临时端口 + uvicorn 后台线程 + httpx，库指向 conftest 的
tmp_path，绝不碰 data\\web.db。
"""
from __future__ import annotations

import socket
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

import auth
import artifacts
import db
import reflection
import store
import app as svc

GOOD = "password-123"


# ---------------------------------------------------------------- 夹具

@contextmanager
def _serve(application):
    """在回环地址的临时端口上真起一个 uvicorn。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=port, log_level="error"))

    def _run():
        try:
            server.run()
        except OSError:
            # 关停期的 WinError 1005（socket 已被关闭）是 Windows 上的竞态，
            # 无害：启动阶段的 OSError（端口被占）反而必须暴露，所以判 started。
            # 不吞的话它会被 pytest 记成 teardown ERROR，与被测代码无关。
            if not server.started:
                raise

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        if not th.is_alive():
            break
        time.sleep(0.01)
    assert server.started, "uvicorn 没能启动"
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        server.should_exit = True
        th.join(timeout=15)


@pytest.fixture
def public():
    with _serve(svc.create_public_app()) as base:
        with httpx.Client(base_url=base, timeout=30) as c:
            yield c


@pytest.fixture
def internal():
    with _serve(svc.create_internal_app()) as base:
        with httpx.Client(base_url=base, timeout=30) as c:
            yield c


def login(client, username="user1", password=GOOD):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def make_user(username="user1", password=GOOD, role="user"):
    return auth.create_user(username, password, role=role)


def create_task(client, token, title="分析 1ubq", brief="分析 1ubq 的折叠稳定性"):
    r = client.post("/api/tasks", json={"title": title, "brief": brief},
                    headers=bearer(token))
    assert r.status_code == 201, r.text
    return r.json()["task"]


def make_package(task_id, name=None):
    """造一个真 zip，并写回 tasks.package_path。

    **位置由 artifacts.default_package_path 说了算**（`data\\packages\\task_<id>.zip`），
    夹具不自己拼路径。原来这里写的是 `data\\work\\<id>\\task-1.zip`——生产里没有
    任何一行代码那样写，于是"真实包点下载恒 409"这个高危缺陷全绿通过。
    **夹具替生产撒谎，比没夹具更糟**：它把下载测成了一个生产中不存在的布局。

    `name` 只给不关心生产命名的用例用（比如"删任务后磁盘文件还在"）。
    """
    root = db.data_root() / "packages"
    root.mkdir(parents=True, exist_ok=True)
    path = (artifacts.default_package_path(int(task_id)) if name is None
            else root / name)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("summary.txt", "算完了")
    store.update_task(task_id, package_path=str(path))
    return path


def is_chinese(detail) -> bool:
    return isinstance(detail, str) and any("\u4e00" <= ch <= "\u9fff" for ch in detail)


# ---------------------------------------------------------------- 健康 / 隔离

def test_health_needs_no_token(public):
    r = public.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_public_app_has_no_internal_routes(public):
    """内部通道绝不能挂在公网那个 app 上——反代会把它一起转发出去。"""
    r = public.post("/api/internal/agent/1/step", json={"text": "x"},
                    headers={"X-Agent-Token": "whatever"})
    assert r.status_code == 404 and is_chinese(r.json()["detail"])


def test_internal_app_has_no_user_routes(internal):
    r = internal.get("/api/tasks")
    assert r.status_code == 404 and is_chinese(r.json()["detail"])
    # 编辑/删除同理：它们只属于用户 API
    assert internal.patch("/api/tasks/1", json={"title": "t"}).status_code == 404
    assert internal.delete("/api/tasks/1").status_code == 404


def test_internal_app_rejects_user_api_login(internal):
    """用户 API 只在 8473，8475 上没有它——不是靠登录拦，是根本没有这条路由。"""
    r = internal.post("/api/auth/login", json={"username": "user1", "password": GOOD})
    assert r.status_code == 404


def test_unknown_route_detail_is_chinese(public):
    r = public.get("/api/nope")
    assert r.status_code == 404 and is_chinese(r.json()["detail"])


def test_method_not_allowed_detail_is_chinese(public):
    r = public.get("/api/auth/login")
    assert r.status_code == 405 and is_chinese(r.json()["detail"])


# ---------------------------------------------------------------- 登录 / me

def test_login_returns_token_and_user(public):
    make_user()
    r = public.post("/api/auth/login", json={"username": "user1", "password": GOOD})
    assert r.status_code == 200
    body = r.json()
    assert body["token"] and body["user"]["username"] == "user1"
    assert "password_hash" not in r.text


def test_login_wrong_password_is_401(public):
    make_user()
    r = public.post("/api/auth/login", json={"username": "user1", "password": "wrong-wrong-1"})
    assert r.status_code == 401 and is_chinese(r.json()["detail"])


def test_login_missing_field_is_422_with_string_detail(public):
    r = public.post("/api/auth/login", json={"username": "user1"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str) and is_chinese(r.json()["detail"])


def test_me_requires_token(public):
    r = public.get("/api/me")
    assert r.status_code == 401 and is_chinese(r.json()["detail"])


def test_me_returns_current_user(public):
    make_user()
    r = public.get("/api/me", headers=bearer(login(public)))
    assert r.status_code == 200 and r.json()["username"] == "user1"


def test_me_rejects_expired_token(public):
    u = make_user()
    r = public.get("/api/me", headers=bearer(auth.issue_token(u["id"], hours=-1)))
    assert r.status_code == 401


# ---------------------------------------------------------------- 任务

def test_login_then_create_task(public):
    make_user()
    tok = login(public)
    r = public.post("/api/tasks", json={"title": "t", "brief": "分析 1ubq"},
                    headers=bearer(tok))
    assert r.status_code == 201
    assert r.json()["task"]["status"] == "queued"
    assert r.json()["task"]["owner_id"]


def test_create_task_writes_created_event(public):
    """store.create_task 不写 created 事件（已知行为），POST handler 必须自己补。"""
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    events = store.list_events(task["id"])
    assert [e["kind"] for e in events] == ["created"]


def test_create_task_requires_token(public):
    r = public.post("/api/tasks", json={"title": "t", "brief": "b"})
    assert r.status_code == 401


def test_oversize_brief_returns_422_with_chinese_detail(public):
    """关键断言：detail 是**字符串**而非 pydantic 的数组（否则前端拿到 [object Object]）。"""
    make_user()
    tok = login(public)
    r = public.post("/api/tasks", json={"title": "t", "brief": "a" * 20001},
                    headers=bearer(tok))
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert is_chinese(r.json()["detail"])


def test_oversize_title_returns_422(public):
    make_user()
    tok = login(public)
    r = public.post("/api/tasks", json={"title": "标" * 501, "brief": "b"},
                    headers=bearer(tok))
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_empty_title_returns_422(public):
    make_user()
    tok = login(public)
    r = public.post("/api/tasks", json={"title": "   ", "brief": "b"},
                    headers=bearer(tok))
    assert r.status_code == 422 and is_chinese(r.json()["detail"])


def test_broken_json_is_422_with_chinese_detail(public):
    make_user()
    tok = login(public)
    r = public.post("/api/tasks", content=b"{not json",
                    headers={**bearer(tok), "Content-Type": "application/json"})
    assert r.status_code == 422 and is_chinese(r.json()["detail"])


def test_list_tasks_only_returns_own(public):
    u1 = make_user("user1")
    make_user("user2")
    t1 = login(public, "user1")
    task = create_task(public, t1)
    other = store.create_task("别人的任务", "x", owner_id=auth.create_user(
        "user3", GOOD)["id"])
    r = public.get("/api/tasks", headers=bearer(t1))
    assert r.status_code == 200
    ids = [x["id"] for x in r.json()["tasks"]]
    assert task["id"] in ids and other["id"] not in ids


def test_list_tasks_bad_status_is_422(public):
    make_user()
    r = public.get("/api/tasks?status=nope", headers=bearer(login(public)))
    assert r.status_code == 422 and is_chinese(r.json()["detail"])


def test_get_task_includes_events_and_artifacts(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    store.artifacts.add(task["id"], "outputs/a.txt", "a.txt", "file", 3, "agent")
    r = public.get("/api/tasks/%d" % task["id"], headers=bearer(tok))
    assert r.status_code == 200
    body = r.json()
    assert body["task"]["id"] == task["id"]
    assert [e["kind"] for e in body["events"]] == ["created"]
    assert [a["name"] for a in body["artifacts"]] == ["a.txt"]


def test_other_owner_task_is_404_not_403(public):
    """返 403 等于承认"这个 id 存在"，越权探测因此能枚举出别人的任务。"""
    make_user("user1")
    other = store.create_task("别人的任务", "x",
                              owner_id=make_user("user2")["id"])
    tok = login(public, "user1")
    for method, url in (("get", "/api/tasks/%d" % other["id"]),
                        ("post", "/api/tasks/%d/abort" % other["id"]),
                        ("get", "/api/tasks/%d/package" % other["id"])):
        r = getattr(public, method)(url, headers=bearer(tok))
        assert r.status_code == 404, (url, r.status_code)
        assert is_chinese(r.json()["detail"])


def test_missing_task_is_404(public):
    make_user()
    r = public.get("/api/tasks/99999", headers=bearer(login(public)))
    assert r.status_code == 404 and is_chinese(r.json()["detail"])


def test_no_advance_endpoint(public):
    """没有 /advance：状态推进全由系统触发，用户能推就等于绕过完成判定。"""
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    r = public.post("/api/tasks/%d/advance" % task["id"],
                    json={"status": "running"}, headers=bearer(tok))
    assert r.status_code == 404
    assert store.get_task(task["id"])["status"] == "queued"


# ---------------------------------------------------------------- 中止 / 下载

def test_abort_queued_task_marks_failed(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    r = public.post("/api/tasks/%d/abort" % task["id"], headers=bearer(tok))
    assert r.status_code == 200 and r.json()["task"]["status"] == "failed"
    kinds = [e["kind"] for e in store.list_events(task["id"])]
    assert "status" in kinds


def test_abort_running_task_marks_failed(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    store.set_status(task["id"], "running", actor="system")
    r = public.post("/api/tasks/%d/abort" % task["id"], headers=bearer(tok))
    assert r.status_code == 200 and r.json()["task"]["status"] == "failed"


def test_abort_done_task_is_422(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    store.set_status(task["id"], "done", actor="system")
    r = public.post("/api/tasks/%d/abort" % task["id"], headers=bearer(tok))
    assert r.status_code == 422 and is_chinese(r.json()["detail"])


def test_package_before_done_is_409(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    make_package(task["id"])
    r = public.get("/api/tasks/%d/package" % task["id"], headers=bearer(tok))
    assert r.status_code == 409 and is_chinese(r.json()["detail"])


def test_package_downloads_zip(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    path = make_package(task["id"])
    store.set_status(task["id"], "done", actor="system")
    r = public.get("/api/tasks/%d/package" % task["id"], headers=bearer(tok))
    assert r.status_code == 200
    assert r.content == Path(path).read_bytes()
    assert "attachment" in r.headers.get("content-disposition", "")


def test_package_missing_file_is_409(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    store.update_task(task["id"], package_path=str(db.data_root() / "work" / "9" / "x.zip"))
    store.set_status(task["id"], "done", actor="system")
    r = public.get("/api/tasks/%d/package" % task["id"], headers=bearer(tok))
    assert r.status_code == 409


def test_package_outside_allowed_region_is_409(public):
    """package_path 越界不外泄文件系统信息，当作"没有包"。"""
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    outside = db.data_root() / "escape.zip"
    outside.write_bytes(b"nope")
    store.update_task(task["id"], package_path=str(outside))
    store.set_status(task["id"], "done", actor="system")
    r = public.get("/api/tasks/%d/package" % task["id"], headers=bearer(tok))
    assert r.status_code == 409


# ---------------------------------------------------------------- 编辑 / 删除

def test_patch_task_updates_title_and_brief(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    r = public.patch("/api/tasks/%d" % task["id"],
                     json={"title": "1ubq 折叠稳定性", "brief": "改用 FastFold"},
                     headers=bearer(tok))
    assert r.status_code == 200
    got = r.json()["task"]
    assert got["title"] == "1ubq 折叠稳定性" and got["brief"] == "改用 FastFold"


def test_patch_writes_edited_event_with_raw_field_names(public):
    """message 里放**原样的英文字段名**：前端显示层（FIELD_CN）负责翻成
    「改了标题 / 需求」。服务端 message 是 API 契约，不能把中文结构名写死进去。"""
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    public.patch("/api/tasks/%d" % task["id"],
                 json={"title": "新标题", "brief": "新正文"}, headers=bearer(tok))
    edited = [e for e in store.list_events(task["id"]) if e["kind"] == "edited"]
    assert len(edited) == 1
    assert edited[0]["message"] == "修改了：title、brief"
    assert edited[0]["actor"] == "human"


def test_patch_only_touches_given_fields(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    r = public.patch("/api/tasks/%d" % task["id"], json={"title": "只改标题"},
                     headers=bearer(tok))
    assert r.status_code == 200
    got = r.json()["task"]
    assert got["title"] == "只改标题"
    assert got["brief"] == task["brief"]
    edited = [e for e in store.list_events(task["id"]) if e["kind"] == "edited"]
    assert edited[0]["message"] == "修改了：title"


def test_patch_cannot_change_status(public):
    """用户侧没有状态推进权：多传的 status 被忽略，不是 400 也不是静默生效。"""
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    r = public.patch("/api/tasks/%d" % task["id"],
                     json={"title": "新标题", "status": "done"}, headers=bearer(tok))
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "queued"
    assert store.get_task(task["id"])["status"] == "queued"


def test_patch_empty_title_is_422(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    r = public.patch("/api/tasks/%d" % task["id"], json={"title": "   "},
                     headers=bearer(tok))
    assert r.status_code == 422 and is_chinese(r.json()["detail"])
    assert store.get_task(task["id"])["title"] == task["title"]


def test_patch_oversize_brief_is_422(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    r = public.patch("/api/tasks/%d" % task["id"], json={"brief": "a" * 20001},
                     headers=bearer(tok))
    assert r.status_code == 422 and is_chinese(r.json()["detail"])
    assert store.get_task(task["id"])["brief"] == task["brief"]


def test_patch_no_change_is_422(public):
    """内容没变时不写 edited 事件，时间线不该被无意义的保存灌满。"""
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    r = public.patch("/api/tasks/%d" % task["id"],
                     json={"title": task["title"]}, headers=bearer(tok))
    assert r.status_code == 422 and is_chinese(r.json()["detail"])
    assert [e for e in store.list_events(task["id"]) if e["kind"] == "edited"] == []


def test_patch_empty_body_is_422(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    r = public.patch("/api/tasks/%d" % task["id"], json={}, headers=bearer(tok))
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_patch_requires_token(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    r = public.patch("/api/tasks/%d" % task["id"], json={"title": "x"})
    assert r.status_code == 401


def test_patch_other_owner_is_404(public):
    make_user("user1")
    other = store.create_task("别人的任务", "x",
                              owner_id=make_user("user2")["id"])
    r = public.patch("/api/tasks/%d" % other["id"], json={"title": "x"},
                     headers=bearer(login(public, "user1")))
    assert r.status_code == 404 and is_chinese(r.json()["detail"])


def test_delete_task_cascades_to_events_and_artifacts(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    keep = create_task(public, tok)
    store.artifacts.add(task["id"], "outputs/a.txt", "a.txt", "file", 3, "agent")
    store.artifacts.add(keep["id"], "outputs/b.txt", "b.txt", "file", 3, "agent")
    store.add_event(task["id"], "log", "agent", "跑过一步")

    r = public.delete("/api/tasks/%d" % task["id"], headers=bearer(tok))
    assert r.status_code == 200 and r.json() == {"ok": True}

    c = db.connect()
    try:
        assert c.execute("SELECT 1 FROM tasks WHERE id = ?", (task["id"],)).fetchone() is None
        assert c.execute("SELECT 1 FROM artifacts WHERE task_id = ?",
                         (task["id"],)).fetchone() is None
        assert c.execute("SELECT 1 FROM events WHERE task_id = ?",
                         (task["id"],)).fetchone() is None
        # 级联只清自己那一条，别人的行必须还在
        assert c.execute("SELECT 1 FROM tasks WHERE id = ?", (keep["id"],)).fetchone()
        assert c.execute("SELECT 1 FROM artifacts WHERE task_id = ?",
                         (keep["id"],)).fetchone()
    finally:
        c.close()


def test_delete_keeps_task_on_disk(public):
    """磁盘清理由 janitor 负责（7/14/30 天保留）：数据库删了不等于目录该删。"""
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    path = make_package(task["id"], "leftover.zip")
    r = public.delete("/api/tasks/%d" % task["id"], headers=bearer(tok))
    assert r.status_code == 200
    assert path.exists(), "delete 不该碰磁盘，磁盘残留交给 janitor"


def test_delete_task_removes_its_reflection_body(public):
    """**删任务要把反思正文一起删掉。**

    删任务会级联删掉 reflections 里那一条，但正文是**文件**——级联管不到它。
    于是库里那条没了，盘上的 task-N.md 还在：没人再找得到它，也没人会清它
    （janitor 只扫 work/uploads/logs/packages）。

    留着不只是浪费盘：同一条任务**重跑**时新正文会写同一个路径，旧正文
    混在里面时谁也说不清哪份是哪份。
    """
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    body = reflection.body_path(task["id"])
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text("# 反思 · 任务 #%d\n" % task["id"], encoding="utf-8")
    store.create_reflection(task["id"], str(body))
    assert body.exists(), "夹具本身没把正文写出来"

    r = public.delete("/api/tasks/%d" % task["id"], headers=bearer(tok))
    assert r.status_code == 200
    assert not body.exists(), (
        "反思正文留在盘上了：库里那条已经没了，正文成了没人认领的残留")


def test_delete_task_keeps_another_tasks_reflection_body(public):
    """**只删自己那一条的正文。** 别的任务的正文一个字都不许动。"""
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    keep = create_task(public, tok)
    body = reflection.body_path(task["id"])
    keep_body = reflection.body_path(keep["id"])
    for p in (body, keep_body):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# 反思\n", encoding="utf-8")
    store.create_reflection(task["id"], str(body))
    store.create_reflection(keep["id"], str(keep_body))

    r = public.delete("/api/tasks/%d" % task["id"], headers=bearer(tok))
    assert r.status_code == 200
    assert keep_body.exists(), "删一条任务把别人的反思正文也删了"


def test_delete_other_owner_is_404(public):
    make_user("user1")
    other = store.create_task("别人的任务", "x",
                              owner_id=make_user("user2")["id"])
    r = public.delete("/api/tasks/%d" % other["id"], headers=bearer(login(public, "user1")))
    assert r.status_code == 404 and is_chinese(r.json()["detail"])
    assert store.get_task(other["id"])["id"] == other["id"]


def test_delete_missing_task_is_404(public):
    make_user()
    r = public.delete("/api/tasks/99999", headers=bearer(login(public)))
    assert r.status_code == 404


def test_delete_requires_token(public):
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    assert public.delete("/api/tasks/%d" % task["id"]).status_code == 401


def test_detail_returns_full_event_timeline_ascending(public):
    """首屏就要拿到**完整**时间线，SSE 只做增量追加——终态任务没有 SSE 也得能读历史。"""
    make_user()
    tok = login(public)
    task = create_task(public, tok)
    store.set_status(task["id"], "running", actor="system", message="接走了")
    store.add_event(task["id"], "log", "agent", "算完了")
    store.set_status(task["id"], "done", actor="system", message="收口")

    r = public.get("/api/tasks/%d" % task["id"], headers=bearer(tok))
    assert r.status_code == 200
    events = r.json()["events"]
    ids = [e["id"] for e in events]
    assert ids == sorted(ids), "事件必须按 id 升序"
    # store.set_step 只改 step 列、不写事件（事件由 API 层写），所以这里没有 step
    assert [e["kind"] for e in events] == ["created", "status", "log", "status"]


# ---------------------------------------------------------------- 内部：agent 通道

def test_agent_token_cannot_touch_other_task(internal):
    """agent_token 唯一的作用域保证：任务 A 的令牌打任务 B 必须 403。"""
    tok_a = svc.issue_agent_token(1)
    r = internal.post("/api/internal/agent/2/step", json={"text": "x"},
                      headers={"X-Agent-Token": tok_a})
    assert r.status_code == 403 and is_chinese(r.json()["detail"])


def test_agent_token_without_header_is_401(internal):
    r = internal.post("/api/internal/agent/1/step", json={"text": "x"})
    assert r.status_code == 401 and is_chinese(r.json()["detail"])


def test_agent_token_garbage_is_401(internal):
    r = internal.post("/api/internal/agent/1/step", json={"text": "x"},
                      headers={"X-Agent-Token": "deadbeef"})
    assert r.status_code == 401


def test_agent_token_cannot_use_admin_route(internal):
    """两套令牌互不通用：agent_token 不是管理员通行证。"""
    tok = svc.issue_agent_token(1)
    r = internal.post("/api/internal/admin/tokens/1", headers={"X-Agent-Token": tok})
    assert r.status_code in (401, 403)


def test_user_bearer_cannot_use_agent_route(internal):
    """用户令牌也不能冒充 agent：agent 通道只认 X-Agent-Token。"""
    u = make_user()
    tok = auth.issue_token(u["id"])
    r = internal.post("/api/internal/agent/1/step", json={"text": "x"},
                      headers=bearer(tok))
    assert r.status_code == 401


def test_agent_step_updates_step_and_truncates(internal, public):
    make_user()
    task = create_task(public, login(public))
    tok = svc.issue_agent_token(task["id"])
    r = internal.post("/api/internal/agent/%d/step" % task["id"],
                      json={"text": "正在跑 " + "长" * 200},
                      headers={"X-Agent-Token": tok})
    assert r.status_code == 200
    assert len(r.json()["step"]) <= store.STEP_MAX
    assert store.get_task(task["id"])["step"] == r.json()["step"]
    kinds = [e["kind"] for e in store.list_events(task["id"])]
    assert "step" in kinds


def test_agent_step_missing_text_is_422(internal, public):
    make_user()
    task = create_task(public, login(public))
    tok = svc.issue_agent_token(task["id"])
    r = internal.post("/api/internal/agent/%d/step" % task["id"], json={},
                      headers={"X-Agent-Token": tok})
    assert r.status_code == 422 and is_chinese(r.json()["detail"])


def test_agent_step_unknown_task_is_404(internal):
    tok = svc.issue_agent_token(4242)
    r = internal.post("/api/internal/agent/4242/step", json={"text": "x"},
                      headers={"X-Agent-Token": tok})
    assert r.status_code == 404


def test_agent_event_writes_timeline(internal, public):
    make_user()
    task = create_task(public, login(public))
    tok = svc.issue_agent_token(task["id"])
    r = internal.post("/api/internal/agent/%d/event" % task["id"],
                      json={"kind": "log", "message": "调用了 rfdiffusion"},
                      headers={"X-Agent-Token": tok})
    assert r.status_code == 200
    msgs = [e["message"] for e in store.list_events(task["id"]) if e["kind"] == "log"]
    assert "调用了 rfdiffusion" in msgs


def test_agent_event_bad_kind_is_422(internal, public):
    make_user()
    task = create_task(public, login(public))
    tok = svc.issue_agent_token(task["id"])
    r = internal.post("/api/internal/agent/%d/event" % task["id"],
                      json={"kind": "created", "message": "x"},
                      headers={"X-Agent-Token": tok})
    assert r.status_code == 422 and is_chinese(r.json()["detail"])


def test_agent_finish_records_marker_without_moving_status(internal, public):
    """finish **不改状态**——算完不等于交付完。

    状态由调度器推进：进程退出 -> packaging -> 打包器 -> done。原先 finish 直接
    置 done，调度器随后又改回 packaging，界面上流转轨会**倒退一格**再前进。
    """
    make_user()
    task = create_task(public, login(public))
    tok = svc.issue_agent_token(task["id"])
    store.set_status(task["id"], "running", actor="system")
    r = internal.post("/api/internal/agent/%d/finish" % task["id"],
                      json={"summary": "算完了，RMSD 1.2"},
                      headers={"X-Agent-Token": tok})
    assert r.status_code == 200
    got = store.get_task(task["id"])
    assert got["status"] == "running", "finish 不该推状态，状态归调度器"
    assert got["summary"] == "算完了，RMSD 1.2"
    assert store.has_finished(task["id"])


def test_agent_finish_twice_is_refused(internal, public):
    """状态不再由 finish 决定，所以重复收口的闸门必须看 finished 标记。"""
    make_user()
    task = create_task(public, login(public))
    tok = svc.issue_agent_token(task["id"])
    store.set_status(task["id"], "running", actor="system")
    first = internal.post("/api/internal/agent/%d/finish" % task["id"],
                          json={"summary": "算完了"}, headers={"X-Agent-Token": tok})
    assert first.status_code == 200
    again = internal.post("/api/internal/agent/%d/finish" % task["id"],
                          json={"summary": "再算一次"}, headers={"X-Agent-Token": tok})
    assert again.status_code == 422 and is_chinese(again.json()["detail"])


def test_agent_finish_with_error_marks_failed(internal, public):
    make_user()
    task = create_task(public, login(public))
    tok = svc.issue_agent_token(task["id"])
    store.set_status(task["id"], "running", actor="system")
    r = internal.post("/api/internal/agent/%d/finish" % task["id"],
                      json={"error": "Docker 没起来"},
                      headers={"X-Agent-Token": tok})
    assert r.status_code == 200
    assert store.get_task(task["id"])["status"] == "failed"


def test_agent_finish_refuses_after_terminal(internal, public):
    make_user()
    task = create_task(public, login(public))
    tok = svc.issue_agent_token(task["id"])
    store.set_status(task["id"], "done", actor="system")
    r = internal.post("/api/internal/agent/%d/finish" % task["id"], json={},
                      headers={"X-Agent-Token": tok})
    assert r.status_code == 422 and is_chinese(r.json()["detail"])


def test_revoked_agent_token_stops_working(internal, public):
    make_user()
    task = create_task(public, login(public))
    tok = svc.issue_agent_token(task["id"])
    svc.revoke_agent_token(task["id"])
    r = internal.post("/api/internal/agent/%d/step" % task["id"], json={"text": "x"},
                      headers={"X-Agent-Token": tok})
    assert r.status_code == 401


# ---------------------------------------------------------------- 内部：admin 通道

def test_admin_route_needs_admin_role(internal, public):
    make_user("user1")
    task = create_task(public, login(public))
    admin = make_user("adm", role="admin")
    tok = auth.issue_token(admin["id"])
    r = internal.post("/api/internal/admin/tokens/%d" % task["id"], headers=bearer(tok))
    assert r.status_code == 200 and r.json()["token"]

    plain = auth.issue_token(make_user("user2")["id"])
    r = internal.post("/api/internal/admin/tokens/%d" % task["id"], headers=bearer(plain))
    assert r.status_code == 403 and is_chinese(r.json()["detail"])


def test_admin_route_without_token_is_401(internal):
    r = internal.post("/api/internal/admin/tokens/1")
    assert r.status_code == 401 and is_chinese(r.json()["detail"])


def test_admin_issued_token_works_on_its_own_task(internal, public):
    make_user("user1")
    admin = auth.issue_token(make_user("adm", role="admin")["id"])
    task = create_task(public, login(public))
    r = internal.post("/api/internal/admin/tokens/%d" % task["id"],
                      headers=bearer(admin))
    tok = r.json()["token"]
    r = internal.post("/api/internal/agent/%d/step" % task["id"],
                      json={"text": "开始计算"},
                      headers={"X-Agent-Token": tok})
    assert r.status_code == 200
    r = internal.post("/api/internal/agent/%d/step" % (task["id"] + 1),
                      json={"text": "越权"},
                      headers={"X-Agent-Token": tok})
    assert r.status_code == 403
