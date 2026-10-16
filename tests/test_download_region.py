"""结果包下载的区域判定（修高危①）。

背景
----
生产打包写在 ``data\\packages\\task_<id>.zip``（``app._package_one`` →
``artifacts.default_package_path``），而下载端点原来拿 ``guards.resolve(raw,
task_id, write=False)`` 去校验。那个函数认的是 **agent 的四个区域**
（work / uploads / outputs / inputs），packages 不在里面——于是
``guards.resolve()`` 对每一个真实产物路径都抛 Violation，下载端点恒返回 409：

    任务跑完 → zip 生成 → 点下载永远是「结果包不在允许的目录里」。

整个平台的核心交付物拿不到，而 391 个测试全绿——因为
``tests/test_app.py`` 的 ``make_package()`` 把假 zip 写进了 ``data\\work\\<id>\\``，
一个生产中根本不存在的布局。**夹具替生产撒谎，测试就测不到生产的洞**，所以这个
文件里的每一条都按生产布局造包，并显式断言"造包的地方就是 artifacts 说的地方"。

收紧的另一半
------------
原来 ``write=False`` 也放行 work / uploads / outputs / **inputs** 里的任意文件，
而 inputs 放的是别的任务的输入结构文件。所以最终判定是**只认本任务自己的那一个
文件**：``<data_root>\\packages\\task_<task_id>.zip``（父目录必须正好是 packages，
文件名必须正好是本任务那一个）。既不漏真实包，也不放行别处的文件。
"""
from __future__ import annotations

import zipfile

import pytest
from fastapi.testclient import TestClient

import app as svc
import artifacts
import auth
import db
import guards
import store

GOOD = "password-123"


def is_chinese(detail) -> bool:
    return isinstance(detail, str) and any("一" <= ch <= "鿿" for ch in detail)


# ---------------------------------------------------------------- 夹具 / 帮手

@pytest.fixture
def client():
    """用户面板（8473 那个 app）的客户端。库指向 conftest 的 tmp_path。

    直接构造、不进 `with`：create_public_app() 里没有 lifespan（后台线程由 main()
    起），而 TestClient 的上下文管理器会顺手去跑 lifespan。httpx 0.28 的
    ASGITransport 是异步专用的，所以这里用 starlette 封好的 TestClient。
    """
    return TestClient(svc.create_public_app())


def login(client) -> dict:
    auth.create_user("user1", GOOD, role="user")
    r = client.post("/api/auth/login",
                    json={"username": "user1", "password": GOOD})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def done_task(client, headers, title="分析 1ubq") -> dict:
    r = client.post("/api/tasks", json={"title": title, "brief": "跑一遍"},
                    headers=headers)
    assert r.status_code == 201, r.text
    task = r.json()["task"]
    store.set_status(task["id"], "done", actor="system")
    return task


def write_zip(path, text="算完了") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("summary.txt", text)


def write_package(task_id, name=None):
    """造结果包。**默认落在 artifacts.default_package_path 指的地方**——
    也就是 _package_one 真正写的位置；`name` 只给"故意放错地方"的用例用。"""
    root = db.data_root() / "packages"
    root.mkdir(parents=True, exist_ok=True)
    path = (artifacts.default_package_path(int(task_id)) if name is None
            else root / name)
    write_zip(path)
    return path


def point_at(task_id, path) -> dict:
    store.update_task(task_id, package_path=str(path))
    return store.get_task(task_id, is_admin=True)


# ---------------------------------------------------------------- 生产布局能下

def test_production_layout_package_downloads(client):
    """真实布局必须能下：这条在修之前恒 409（夹具写 work 区，全绿）。"""
    headers = login(client)
    task = done_task(client, headers)

    path = write_package(task["id"])
    # 先钉住"造包的地方就是生产写的地方"：这两句挂了说明夹具又替生产撒谎了
    assert path == db.data_root() / "packages" / f"task_{task['id']}.zip"
    assert path == artifacts.default_package_path(task["id"])

    point_at(task["id"], path)
    r = client.get(f"/api/tasks/{task['id']}/package", headers=headers)
    assert r.status_code == 200, r.text
    assert r.content == path.read_bytes()
    assert "attachment" in r.headers.get("content-disposition", "")


def test_guard_accepts_the_path_artifacts_writes(client, tmp_path, monkeypatch):
    """跨模块契约：写包的（artifacts）与放行下载的（guards）必须算出同一个路径。

    这一对曾经各说各的：artifacts 写 packages 区，guards 认四区，于是真实包
    永远下不到。这条把"两个函数对同一个 task_id 达成一致"钉住。
    """
    root = tmp_path / "data"
    (root / "packages").mkdir(parents=True)
    monkeypatch.setenv("PROTFORGE_WEB_DATA_ROOT", str(root))

    path = artifacts.default_package_path(5)
    assert guards.resolve_package(5, str(path)) == path.resolve()


def test_packages_is_not_an_agent_region(client):
    """packages **不进 regions()**：那四个区是 agent 的文件边界。

    加进去等于给每个 agent 开一条"读别人任务几十 GB 结果包"的路；下载要的只是
    "本任务自己的那一个文件"，用 resolve_package() 单独管就够。
    """
    assert set(guards.regions(5)) == {"work", "uploads", "outputs", "inputs"}
    with pytest.raises(guards.Violation):
        guards.resolve(str(guards.package_file(5)), 5)


# ---------------------------------------------------------------- 越界仍 409

def test_out_of_region_is_409_and_hides_the_path(client):
    """越界不外泄文件系统布局：一律当"没有包"，且不能把绝对路径吐回去。"""
    headers = login(client)
    task = done_task(client, headers)

    outside = db.data_root() / "escape.zip"
    outside.write_bytes(b"nope")
    point_at(task["id"], outside)

    r = client.get(f"/api/tasks/{task['id']}/package", headers=headers)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert is_chinese(detail), detail
    assert str(db.data_root()) not in detail
    assert "escape.zip" not in detail


@pytest.mark.parametrize("zone", ["work", "uploads", "outputs", "inputs"])
def test_package_path_in_any_other_region_is_rejected(client, zone, tmp_path,
                                                       monkeypatch):
    """verifier 原始担心的那一侧：write=False 原来放行四区里的任意文件。

    inputs 放的是**别的任务的输入结构文件**，能被 package_path 指到就等于让一个
    任务下载到另一个任务的东西。
    """
    headers = login(client)
    task = done_task(client, headers)

    # outputs/inputs 在 ../protforge/data 下（生产真实路径），测试绝不能往那儿写东西，
    # 所以把 PROTORGE_DATA 指到 tmp_path。
    pf = tmp_path / "oih"
    pf.mkdir(parents=True, exist_ok=True)
    (pf / "outputs").mkdir(parents=True, exist_ok=True)
    (pf / "inputs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROTFORGE_DATA", str(pf))

    if zone in ("outputs", "inputs"):
        base = pf / zone
    else:
        base = db.data_root() / zone / str(task["id"])
    planted = base / f"task_{task['id']}.zip"
    write_zip(planted, "别的任务的东西")
    point_at(task["id"], planted)

    r = client.get(f"/api/tasks/{task['id']}/package", headers=headers)
    assert r.status_code == 409, (zone, r.status_code, r.text)
    assert is_chinese(r.json()["detail"])
    assert r.content != planted.read_bytes()


def test_other_tasks_package_is_rejected(client):
    """task A 不能下 task B 的包——哪怕那个包文件真的存在。"""
    headers = login(client)
    a = done_task(client, headers, title="任务 A")
    b = done_task(client, headers, title="任务 B")

    b_pkg = write_package(b["id"])
    assert b_pkg.is_file()
    point_at(a["id"], b_pkg)

    r = client.get(f"/api/tasks/{a['id']}/package", headers=headers)
    assert r.status_code == 409, r.text
    assert is_chinese(r.json()["detail"])

    # 同一个文件在它自己的任务上就是能下的：被拒的是"跨任务绑定"，不是文件本身
    point_at(b["id"], b_pkg)
    ok = client.get(f"/api/tasks/{b['id']}/package", headers=headers)
    assert ok.status_code == 200 and ok.content == b_pkg.read_bytes()


def test_leftover_package_in_packages_dir_is_rejected(client):
    """packages 目录下的**别的**文件同样不行：区域放行到目录一级就够了，
    放行到"本任务那个文件"才是要的。"""
    headers = login(client)
    task = done_task(client, headers)

    stray = write_package(task["id"], name="leftover.zip")
    point_at(task["id"], stray)

    r = client.get(f"/api/tasks/{task['id']}/package", headers=headers)
    assert r.status_code == 409, r.text


def test_missing_production_package_is_409(client):
    """路径合法但文件不在：与越界分开说，但都还是 409（没有包可下）。"""
    headers = login(client)
    task = done_task(client, headers)
    point_at(task["id"], artifacts.default_package_path(task["id"]))

    r = client.get(f"/api/tasks/{task['id']}/package", headers=headers)
    assert r.status_code == 409
    assert is_chinese(r.json()["detail"])


# ---------------------------------------------------------------- 守卫层细节

def test_package_escape_forms_rejected(tmp_path, monkeypatch):
    """形态花招在 resolve_package 这里一样过不去。"""
    root = tmp_path / "data"
    (root / "packages").mkdir(parents=True)
    monkeypatch.setenv("PROTFORGE_WEB_DATA_ROOT", str(root))

    for bad in (r"\\?\C:\Windows\x.zip", r"C:x\y.zip", r"\Windows\x.zip",
                r"\\server\share\x.zip", "", "   ",
                r"..\..\..\..\Windows\x.zip"):
        with pytest.raises(guards.Violation):
            guards.resolve_package(5, bad)


def test_package_subdirectory_is_rejected(tmp_path, monkeypatch):
    """packages\\sub\\task_5.zip 不是生产布局：父目录必须正好是 packages。"""
    root = tmp_path / "data"
    (root / "packages" / "sub").mkdir(parents=True)
    monkeypatch.setenv("PROTFORGE_WEB_DATA_ROOT", str(root))

    with pytest.raises(guards.Violation):
        guards.resolve_package(5, str(root / "packages" / "sub" / "task_5.zip"))


def test_symlinked_package_is_rejected(tmp_path, monkeypatch):
    """packages\\task_5.zip 是指向别处的软链时也要拒：比较发生在 resolve 之后，
    跟过去看到的是别人的文件，名字与父目录都对不上。"""
    root = tmp_path / "data"
    (root / "packages").mkdir(parents=True)
    monkeypatch.setenv("PROTFORGE_WEB_DATA_ROOT", str(root))

    secret = tmp_path / "secret.zip"
    secret.write_bytes(b"x")
    link = root / "packages" / "task_5.zip"
    try:
        link.symlink_to(secret)
    except OSError:                       # 没权限建软链就跳过，不假装测过
        pytest.skip("该环境不允许创建符号链接")
    with pytest.raises(guards.Violation):
        guards.resolve_package(5, str(link))


def test_illegal_task_id_rejected(tmp_path, monkeypatch):
    """task_id 自己就是一条穿越路径：非纯数字不许进路径拼接。"""
    root = tmp_path / "data"
    monkeypatch.setenv("PROTFORGE_WEB_DATA_ROOT", str(root))
    for bad in ("../../evil", "5/6", "", True):
        with pytest.raises(guards.Violation):
            guards.resolve_package(bad, str(root / "packages" / "task_5.zip"))
