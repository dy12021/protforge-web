"""前端读响应字段的方式，必须与服务端**真实**返回的形状对上。

缺陷是怎么来的
--------------
``POST /api/tasks`` / ``PATCH /api/tasks/{id}`` / ``POST …/abort`` 三个接口返回的都是
**详情信封** ``{"task": …, "events": […], "artifacts": […]}``，任务本体在 ``task`` 里。
前端却把返回值直接当任务对象用：

- ``openDrawer(got.id)`` 打开的是 ``undefined``，于是去请求 ``/api/tasks/undefined``
  —— 真机上表现为「提交完抽屉是空的」，控制台里一条 422；
- 改完标题/需求说明，``state.byId.set(id, got)`` 存进去的是整个信封，
  抽屉里字段全空。

这是**同一个根因的第四次现身**：前端猜服务端长什么样，猜错时不报错。
前三次分别是不带 ``.tasks`` 就 ``.map``、``apiBase()`` 多拼一次 ``/api``、
管理员面板写全路径——全都骗过了全部自动化手段，只有点一次真界面才现形。

所以这里做**双边**钉死：服务端那边拿真实响应断言信封确实存在，前端那边
断言每个消费点都经过 ``detailTask()`` 拆信封。任一边单独改都会红。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import app as svc                      # noqa: E402
import auth                            # noqa: E402

REPO = Path(__file__).resolve().parent.parent
USER_JS = REPO / "web" / "app.js"

GOOD_PW = "password-123"


def _src() -> str:
    return USER_JS.read_text(encoding="utf-8")


def _code() -> str:
    """源码**去掉注释**。

    不去掉的话，本文件顶部那段解释缺陷的注释里就写着 `openDrawer(got.id)`，
    扫描会把它当成一处真实调用——对着自己的说明文档报错。
    """
    s = _src()
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"(?m)//.*$", "", s)


#: 任何会返回详情信封的取数。
#:
#: **GET 与 POST 在集合路径上写出来的字面量完全一样**（都是 `"/api/tasks"`），
#: 区别只在方法：GET 返回 `{"tasks": [...]}`（列表，由 applyItems 拆），
#: POST 返回详情信封。所以这里把方法与路径一起抓出来，再按方法剔掉列表。
#: 子资源（stream / package / events）返回的不是信封，同样不在内。
_DETAIL_FETCH = re.compile(
    r'api\(\s*"(GET|POST|PATCH)"\s*,\s*'
    r'"(/api/tasks(?:/"\s*\+\s*[\w.]+(?:\s*"\s*/abort")?)?)')

#: 集合上的 GET 是列表端点，不返回详情信封。
_LIST_CALL = ("GET", "/api/tasks")


def _detail_fetches() -> list[tuple[int, str]]:
    """返回 (位置, 该处前后 60 字符) —— 逐个核对有没有被 detailTask 包住。"""
    code = _code()
    out = []
    for m in _DETAIL_FETCH.finditer(code):
        if (m.group(1), m.group(2)) == _LIST_CALL:
            continue
        out.append((m.start(), code[max(0, m.start() - 60):m.end() + 90]))
    return out


@pytest.fixture
def panel():
    auth.create_user("shapeuser", GOOD_PW, role="user")
    with TestClient(svc.create_public_app()) as c:
        r = c.post("/api/auth/login",
                   json={"username": "shapeuser", "password": GOOD_PW})
        assert r.status_code == 200, r.text
        c.headers["Authorization"] = "Bearer " + r.json()["token"]
        yield c


@pytest.fixture
def task_id(panel):
    r = panel.post("/api/tasks", json={"title": "信封形状", "brief": "只测响应形状"})
    assert r.status_code == 201, r.text
    return r.json()["task"]["id"]


# ---------------------------------------------------------------- 服务端那一半


def _assert_envelope(body: dict, where: str) -> dict:
    assert isinstance(body, dict), f"{where} 返回的不是对象：{type(body)}"
    assert set(body) >= {"task", "events", "artifacts"}, (
        f"{where} 的响应里没有详情信封，顶层键是 {sorted(body)}")
    task = body["task"]
    assert isinstance(task, dict) and isinstance(task.get("id"), int), (
        f"{where} 的 task 字段不是任务本体：{task!r}")
    return task


def test_create_task_really_returns_the_envelope(panel):
    """POST /api/tasks —— 提交后前端要拿它返回的 id 打开抽屉。"""
    r = panel.post("/api/tasks", json={"title": "信封 A", "brief": "b"})
    assert r.status_code == 201, r.text
    _assert_envelope(r.json(), "POST /api/tasks")


def test_patch_task_really_returns_the_envelope(panel, task_id):
    r = panel.patch(f"/api/tasks/{task_id}", json={"title": "信封 B"})
    assert r.status_code == 200, r.text
    task = _assert_envelope(r.json(), "PATCH /api/tasks/{id}")
    assert task["title"] == "信封 B", task


def test_abort_really_returns_the_envelope(panel, task_id):
    r = panel.post(f"/api/tasks/{task_id}/abort", json={})
    assert r.status_code == 200, r.text
    task = _assert_envelope(r.json(), "POST /api/tasks/{id}/abort")
    assert task["status"] == "failed", task


# ---------------------------------------------------------------- 前端那一半


def test_the_scanner_actually_finds_the_detail_fetches():
    """防恒真：扫到 0 个就等于什么都没检查。"""
    found = _detail_fetches()
    assert len(found) == 7, f"预期 7 个取详情信封的调用点，实际 {len(found)}：{found}"


def test_the_scanner_does_not_swallow_the_list_endpoint():
    """列表端点返回的是 {"tasks": [...]}，不是详情信封，扫进来会误报。"""
    code = _code()
    assert 'api("GET", "/api/tasks")' in code, "列表取数写法变了，扫描器要跟着改"
    hits = [c for _, c in _detail_fetches() if '"GET", "/api/tasks")' in c]
    assert not hits, f"列表端点被当成详情信封扫进来了：{hits}"


def test_every_detail_fetch_is_wrapped_in_detail_task():
    """所有取详情信封的地方都必须经 detailTask() 拆过再用。

    漏掉一处就静默做错事，而且症状各不相同：
      - GET（打开抽屉 / 实时刷新）→ 抽屉标题、编号、状态、时间全空，
        流转轨也永远不前进（拿信封的 status 去查 STATUS_ORDER 当然是 -1）；
      - POST（提交）→ openDrawer 拿到 undefined，去请求 /api/tasks/undefined；
      - PATCH / abort → 存进 state.byId 的是整个信封，抽屉字段全空。
    """
    unwrapped = [ctx for _, ctx in _detail_fetches() if "detailTask(" not in ctx]
    assert not unwrapped, (
        "这些地方拿到 {task, events, artifacts} 后直接当任务对象用了：\n"
        + "\n".join("  …" + c.replace("\n", " ⏎ ") for c in unwrapped))


def test_detail_task_refuses_a_shape_it_does_not_understand():
    """拆信封必须**严格**：不能写成 got.task || got。

    容错会把形状不匹配重新藏起来——而藏形状不匹配正是这个洞的成因。
    """
    fn = re.search(r"function\s+detailTask\s*\(.*?\n\}", _code(), re.S)
    assert fn, "找不到 detailTask()"
    body = fn.group(0)
    assert "|| got" not in body, (
        "detailTask() 写了容错分支，形状不匹配会被重新藏起来")
    assert "throw" in body, "detailTask() 形状不对时不报错，等于静默猜错"


def test_no_call_site_opens_a_drawer_with_a_bare_response_field():
    """openDrawer(...) 的实参不能是响应对象的裸字段——那正是 undefined 的来源。"""
    bad = re.findall(r"openDrawer\((?:await\s+)?api\(", _code())
    assert not bad, "openDrawer 直接吃了 api() 的返回值，没拆信封"
    bad2 = re.findall(r"openDrawer\(\s*(?:got|res|resp|body)\.", _code())
    assert not bad2, f"openDrawer 还在直接读响应对象的裸字段：{bad2}"


def test_detail_task_is_reachable_from_the_console():
    """挂到 window 上，与其它调试出口一致。"""
    assert "detailTask," in _src(), "detailTask 没挂到 window，调试时拿不到"
