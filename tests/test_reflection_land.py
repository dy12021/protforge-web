"""落经验那条写路径的围栏：**判为经验 → 写进经验**。

**这条围栏是被真机抓出来的**：值班台上「判为经验 → 写进经验」跑完，状态变成
「已落地」、全局经验文件里也真的有那两条，**看着一切正常**；但技能目录下的
``references/lessons.md`` 根本没写，而界面上**一个字都没提示**。

原因是一串叠在一起的：

1. 落地只认**模型正文里标的**技能。模型不认识真实的技能名，它写「通用」，
   于是「知识待在离工具最近的地方」这一层几乎永远落不了地。
2. 记录里那个技能（分类时自动带的，也是模型猜的）从头到尾**没人读**。
3. ``_land_lesson`` 算出来的落地报告（写了多少条、其中多少条进了技能目录、
   有没有只落了一半的说明）被端点直接丢掉，界面无从显示。

所以这里钉死一套归属语义：

* **管理员在落地那一步明确指定的技能 > 模型正文里标的。**
* 模型正文标「通用」= 这条经验是**跨技能的通用做法**，只留全局经验文件。
* **记录里那个技能不参与归属**：它是模型猜的，拿它去覆盖模型标的「通用」，
  等于把一条跨技能的经验按猜测塞进某个技能。
* 管理员指定了一个**不存在的**技能 → 报错，**不悄悄退回模型标的**。
* 落地必须回报告；重复落地被拒。

**技能目录必须隔离。** ``conftest.py`` 只把数据库指向 tmp_path，没有动
``PROTFORGE_WEB_SKILLS_DIR``——用默认值的话，跑一次测试就在真实的
``../protforge\\protforge-plugin`` 里留下 ``references/lessons.md``。
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

    真实技能目录绝不能被这些测试碰到：落地经验会往里写文件。
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


def test_the_admin_can_put_a_generic_lesson_into_a_named_skill(
        client, admin_headers, owner_id, skills):
    """模型标的技能是「通用」时，**管理员在落地那一步指定的技能**要顶上。

    模型不认识真实的技能名，它写「通用」；真正知道技能目录的是管理员。
    落地时那个输入框是唯一能让「知识待在离工具最近的地方」这一层落地的口子，
    认错了就等于这一层永远不存在——而界面上不报错、不提示。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 先跑一条野生型对照再批量跑")
    _classify(client, admin_headers, str(rec["id"]))

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers,
                    json={"skill": SKILL})
    assert r.status_code == 200, f"落地失败：{r.status_code} {r.text}"

    skill_ref = _skill_lesson(skills, SKILL)
    assert skill_ref.is_file(), \
        "技能目录里没有这条经验：管理员明确指定的技能没人认"
    assert "先跑一条野生型对照再批量跑" in skill_ref.read_text(encoding="utf-8")
    # 全局经验文件是那一步的主体，它必须照样写到。
    assert "先跑一条野生型对照再批量跑" in reflection.lessons_path().read_text(encoding="utf-8")


def test_a_generic_lesson_stays_in_the_global_file_when_nobody_names_a_skill(
        client, admin_headers, owner_id, skills):
    """没人指定技能、模型标的又是「通用」→ **只留全局经验文件**。

    「通用」是模型在说「这条经验跨技能都适用」。把它塞进某一个技能的
    references 反而是污染：那个技能没被加载时它不在上下文里，而全局那份
    每个任务都读。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 一条跨技能的通用做法")
    _classify(client, admin_headers, str(rec["id"]))

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers)
    assert r.status_code == 200, f"落地失败：{r.status_code} {r.text}"
    out = r.json()

    assert "一条跨技能的通用做法" in reflection.lessons_path().read_text(encoding="utf-8")
    assert not _skill_lesson(skills, SKILL).exists(), \
        "「通用」那条被塞进了技能目录"
    assert not _skill_lesson(skills, OTHER).exists(), \
        "「通用」那条被塞进了技能目录"
    assert out.get("landed_into_skill") == 0, out
    assert out.get("note"), \
        "只落了一半却什么都不说：管理员以为技能目录里也有那一条"


def test_the_skill_the_model_guessed_does_not_decide_the_attribution(
        client, admin_headers, owner_id, skills):
    """**记录里那个技能不参与归属。**

    分类时自动带的技能是模型猜的（界面上是自动填的，不是人挑的）。拿它去
    覆盖模型自己标的「通用」，等于把一条跨技能的经验按猜测塞进某个技能——
    而且**没任何人提示过**这件事。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 又是一条通用的")
    # 分类时把「模型猜的技能」存进记录里（界面上它会自动出现）。
    _classify(client, admin_headers, str(rec["id"]), skill=SKILL)

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers)
    assert r.status_code == 200, f"落地失败：{r.status_code} {r.text}"

    assert not _skill_lesson(skills, SKILL).exists(), \
        "记录里那个模型猜的技能被当成了管理员的指定"


def test_the_skill_given_at_landing_time_wins(client, admin_headers, owner_id, skills):
    """落地时管理员填的技能**优先**于记录里那个。

    记录里的技能是模型猜的（分类那一步自动带上的），管理员在确认表里填的
    才是最终决定。两者冲突时听管理员的。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 一条经验")
    _classify(client, admin_headers, str(rec["id"]), skill=SKILL)

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers,
                    json={"skill": OTHER})
    assert r.status_code == 200, f"落地失败：{r.status_code} {r.text}"

    assert _skill_lesson(skills, OTHER).is_file(), \
        "落地时管理员指定的技能没有被采用"
    assert not _skill_lesson(skills, SKILL).exists(), \
        "两个技能都写了：管理员指定的和记录里那个同时生效"


def test_naming_a_skill_that_does_not_exist_is_refused(
        client, admin_headers, owner_id, skills):
    """管理员填了一个不存在的技能 → **报错**，不许悄悄退回模型标的。

    退回去等于「我说了放 A 技能，你放进了 B 技能」，而界面什么也不说。
    而且必须**在写任何东西之前**就拒：写到一半再拒，全局经验文件里已经有
    那一条了，撤不干净。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 会被拒掉的一条")
    _classify(client, admin_headers, str(rec["id"]))

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers,
                    json={"skill": "protforge-压根不存在"})
    assert r.status_code == 422, \
        f"不存在的技能被接受了：{r.status_code} {r.text}"

    assert not reflection.lessons_path().exists() or \
        "会被拒掉的一条" not in reflection.lessons_path().read_text(encoding="utf-8"), \
        "拒了却已经写进全局经验文件：写了一半没法撤"
    assert store.get_reflection(rec["id"])["status"] == "reviewed", \
        "拒了却把状态推到了已落地"


def test_landing_reports_what_it_actually_wrote(client, admin_headers, owner_id, skills):
    """落地报告要回到调用方：写了多少条、其中多少条进了技能目录。

    报告被丢掉的时候，管理员看到的只是「状态变成已落地」——**只落了一半**
    （比如模型标的技能对不上真实技能）这件事一个字都不会说。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(
        tid, "- [技能 protforge-demo] 落得下的一条\n- [技能 通用] 落不下的一条")
    _classify(client, admin_headers, str(rec["id"]))

    r = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers)
    assert r.status_code == 200, f"落地失败：{r.status_code} {r.text}"
    out = r.json()
    assert out.get("landed") == 2, f"落地报告说写了 {out.get('landed')} 条，应该是 2 条"
    assert out.get("landed_into_skill") == 1, \
        f"进技能目录的条数是 {out.get('landed_into_skill')}，应该是 1 条"
    assert out.get("note"), \
        "只落了一半却什么都不说：管理员以为技能目录里也有那一条"


def test_landing_the_same_reflection_twice_is_refused(client, admin_headers, owner_id, skills):
    """同一条不许落地两次。

    全局经验文件是**每个任务开工都要读**的：重复写一次，那条经验就被反复灌进
    上下文。界面上的按钮落地后就消失了，但端点这一层也得自己挡着。
    """
    tid = _terminal_task(owner_id)
    rec = _lesson_reflection(tid, "- [技能 通用] 同一条经验")
    _classify(client, admin_headers, str(rec["id"]))

    first = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers)
    assert first.status_code == 200, f"第一次落地失败：{first.status_code} {first.text}"
    again = client.post(f"/api/admin/reflections/{rec['id']}/land", headers=admin_headers)
    assert again.status_code == 422, \
        f"同一条落地了两次：{again.status_code} {again.text}"

    text = reflection.lessons_path().read_text(encoding="utf-8")
    assert text.count("同一条经验") == 1, \
        "重复落地把同一条经验写了两遍：它会被每个任务都读一遍"
