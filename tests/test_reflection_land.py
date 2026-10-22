"""落地那条写路径的围栏：**它已经废了，而且要废得干净**。

以前这里是「判为经验 → 写进经验」：管理员点一下落地，那条经验就进全局经验
文件（每个任务开工都要读）与技能的 references。

现在**经验的��手写入者是干活的计算进程**：它收工前用 write / edit 工具直接
写进长期记忆（``server/memory.py``）。「管理员分类 → 点落地」那道关口没有了。

所以这个文件现在钉的是**废掉的边界**：

* 落地**什么都不写**——不写长期记忆、不写旧经验文件、不写技能目录；
* 落地**不抛异常**——管理端还有路由挂在它上面，抛异常会让那些路由 500；
* 落地**仍然校验**那些本来就该校验的东西（技能存不存在、能不能重复落地）。

**技能目录必须隔离。** ``conftest.py`` 只隔离了数据根，技能目录它没动——
用默认值的话，跑一次测试就在真实的 ``<技能仓>`` 里留下 ``references/lessons.md``。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
ADMIN_DIR = REPO / "admin"
if str(ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADMIN_DIR))

import app_admin as admin_svc       # noqa: E402
import auth                         # noqa: E402
import memory                      # noqa: E402
import reflection                   # noqa: E402
import store                        # noqa: E402

SKILL = "protforge-demo"
OTHER = "protforge-other"

BODY = """\
# 反思 · 任务 #{tid} · 演示任务

- 终态：done
- 收工时间：2026-10-19T12:00:00.000
- 模型：stub
- 生成时间：2026-10-19T12:00:00.000

## 这次实际做了什么
演示。

## 顺利的地方
（无）

## 卡住的地方
（无）

## 经验
{lessons}

## 工具意见
（无）

## 建议分类
lesson
"""


@pytest.fixture()
def client():
    return TestClient(admin_svc.create_admin_app())


@pytest.fixture()
def admin_headers():
    auth.create_user("land-root", "pw-for-tests", role="admin")
    row = auth.get_user("land-root")
    return {"Authorization": "Bearer " + auth.issue_token(int(row["id"]))}


@pytest.fixture()
def owner_id():
    auth.create_user("land-owner", "pw-for-tests", role="user")
    return int(auth.get_user("land-owner")["id"])


@pytest.fixture()
def skills(tmp_path, monkeypatch):
    """一个**临时**技能目录（两个技能），指给 PROTFORGE_WEB_SKILLS_DIR。

    真实技能目录绝不能被这些测试碰到。
    """
    root = tmp_path / "plugin"
    for name in (SKILL, OTHER):
        sk = root / "skills" / name
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: 演示技能\n---\n\n## 用法\n跑一下。\n",
            encoding="utf-8")
    monkeypatch.setenv("PROTFORGE_WEB_SKILLS_DIR", str(root / "skills"))
    return root


def _terminal_task(owner: int) -> int:
    t = store.create_task("演示任务", "brief", owner_id=owner)
    tid = int(t["id"])
    store.set_status(tid, "running", actor="system")
    store.set_status(tid, "done", actor="system", message="收工")
    return tid


def _lesson_reflection(tid: int, lessons: str) -> dict:
    """一条「经验」反思，连同它那份正文（模型标的技能就在正文里）。"""
    p = reflection.body_path(tid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(BODY.format(tid=tid, lessons=lessons), encoding="utf-8")
    return store.create_reflection(tid, str(p))


def _classify(client, headers, rec_id: str, skill: str = "") -> None:
    r = client.post(f"/api/admin/reflections/{rec_id}/classify", headers=headers,
                    json={"kind": "lesson", "skill": skill})
    assert r.status_code == 200, f"分类失败：{r.status_code} {r.text}"


def _skill_lesson(skills, name: str) -> Path:
    return skills / "skills" / name / "references" / "lessons.md"


def test_landing_no_longer_writes_into_a_named_skill(
        client, admin_headers, owner_id, skills):
    """落地**不再往技能目录写**。

    经验的归属（哪个技能）这件事现在由写经验的那个人自己决定——
    计算进程写进长期记忆时就把技能标上了，管理端不再代劳。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 先跑一条野生型对照再批量跑")
    _classify(client, admin_headers, str(rec["id"]))

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers,
                    json={"skill": SKILL})
    assert r.status_code == 200, f"落地失败：{r.status_code} {r.text}"

    assert not _skill_lesson(skills, SKILL).exists(), \
        "落地还在往技能目录里写：经验的归属该由写经验的人决定"
    assert not _skill_lesson(skills, OTHER).exists()


def test_landing_no_longer_writes_the_global_file(
        client, admin_headers, owner_id, skills):
    """落地**不再写全局经验文件**（旧的 ``lessons.md``）。

    新的长期记忆由计算进程自己写；这条旧路要是在旁边偷偷写一份，
    两份记忆会各说各话，而注入提示词的那一份没人知道是哪份。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 一条跨技能的通用做法")
    _classify(client, admin_headers, str(rec["id"]))

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers)
    assert r.status_code == 200, f"落地失败：{r.status_code} {r.text}"

    assert not reflection.lessons_path().exists(), "落地还在写旧经验文件"
    assert not memory.long_term_path().exists(), \
        "落地还在写长期记忆：这条路的权限不该被留下来"


def test_the_skill_the_model_guessed_does_not_decide_the_attribution(
        client, admin_headers, owner_id, skills):
    """归属这件事**已经不存在了**：不管记里是什么技能，都没有文件被写。

    以前这条钉的是「记录里那个模型猜的技能不参与归属」；现在归属归零了，
    钉法也要跟着换——否则这条围栏会变成一句没人能验证的话。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 又是一条通用的")
    # 分类时把「模型猜的技能」存进记录里（界面上它会自动出现）。
    _classify(client, admin_headers, str(rec["id"]), skill=SKILL)

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers)
    assert r.status_code == 200, f"落地失败：{r.status_code} {r.text}"

    assert not _skill_lesson(skills, SKILL).exists()
    assert not memory.long_term_path().exists()


def test_the_skill_given_at_landing_time_is_not_written_anywhere(
        client, admin_headers, owner_id, skills):
    """管理员在落地时指定的技能**不再被写进任何地方**。

    那个输入框还在（删端点是主线的事），但它已经不起作用了：填什么
    都不会有文件被写出来。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 一条经验")
    _classify(client, admin_headers, str(rec["id"]), skill=SKILL)

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers,
                    json={"skill": OTHER})
    assert r.status_code == 200, f"落地失败：{r.status_code} {r.text}"

    assert not _skill_lesson(skills, OTHER).exists()
    assert not _skill_lesson(skills, SKILL).exists()
    assert not memory.long_term_path().exists()


def test_naming_a_skill_that_does_not_exist_is_refused(
        client, admin_headers, owner_id, skills):
    """管理员填了一个不存在的技能 → **仍然报错**。

    这条校验跟落地那条路无关：它是「别把东西写进不存在的目录」这道闸。
    路废了，闸还在——哪天有人把这条路接回去，这道闸是拦得住的。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 会被拒掉的一条")
    _classify(client, admin_headers, str(rec["id"]))

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers,
                    json={"skill": "protforge-压根不存在"})
    assert r.status_code == 422, \
        f"不存在的技能被接受了：{r.status_code} {r.text}"

    assert not memory.long_term_path().exists(), "拒了却写进了长期记忆"
    assert store.get_reflection(rec["id"])["status"] == "reviewed", \
        "拒了却把状态推到了已落地"


def test_landing_still_reports_but_writes_nothing(
        client, admin_headers, owner_id, skills):
    """落地报告仍然回来，但**一个字都没写**。

    这条钉的是一个**已知的残留**：管理端那个端点在主线删掉它之前，
    还会返回「已落地 N 条」而实际上什么都没落地。留着这条围栏是为了
    提醒：别把那个返回体当成「已经沉淀了」。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(
        tid, "- [技能 protforge-demo] 落得下的一条\n- [技能 通用] 落不下的一条")
    _classify(client, admin_headers, str(rec["id"]))

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers)
    assert r.status_code == 200, f"落地失败：{r.status_code} {r.text}"
    out = r.json()
    assert out.get("landed") == 2, f"落地报告说写了 {out.get('landed')} 条"

    # 报告说写了，实际上一个字节都没落。
    assert not memory.long_term_path().exists()
    assert not _skill_lesson(skills, SKILL).exists()
    assert not reflection.lessons_path().exists()


def test_landing_the_same_reflection_twice_is_refused(
        client, admin_headers, owner_id, skills):
    """同一条不许落地两次。

    重复落地在旧流程里会把同一条经验写两遍，而经验文件是**每个任务开工
    都要读**的。现在它不写任何东西，但「重复落地被拒」这道闸还在。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 同一条经验")
    _classify(client, admin_headers, str(rec["id"]))

    first = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers)
    assert first.status_code == 200, f"第一次落地失败：{first.status_code} {first.text}"
    again = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers)
    assert again.status_code == 422, \
        f"同一条落地了两次：{again.status_code} {again.text}"
