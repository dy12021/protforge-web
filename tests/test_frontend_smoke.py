"""把前端脚本真正跑一遍的围栏（``tests/frontend_smoke.mjs`` 的 pytest 外壳）。

这个项目的前端被同一类缺陷咬了五次，每一次都骗过了全部既有检查：

1. ``apiBase()`` 多拼一次 ``/api`` —— 接口全 404
2. 列表响应没拆信封就 ``.map`` —— 一张卡片都画不出来
3. 详情信封当任务对象用 —— 抽屉一片空白
4. ``Object.assign(window, {…, render, …})`` 里的 ``render`` 根本不存在 ——
   顶层 ReferenceError 把脚本掐死，**整个值班台一个接口都不发**
5. ``fmtSize`` 里 ``const n`` 却在循环里 ``n /= 1024`` ——
   任何大小非零的待确认产物都让它抛错，``load()`` 半路夭折

共同形状是**加载时不报错、界面上却是死的**，而且症状与真因隔了两三层。
静态扫描看不出第 4、5 条（语法都完全正确），后端测试更看不出（不碰前端）。

所以唯一的可靠办法是让它跑起来。``.mjs`` 那边用 vm + 一个极简 DOM 桩，
喂**真接口抓下来的返回体**（``tests/_real_fixtures.json``），然后断言：
① 顶层与异步都不抛；② boot() 真的把该拉的接口都拉到了（它自己的 catch 会
吞掉半路的错，只看"抛没抛"会漏）；③ 没有元素被当成文本渲染成 ``[object …]``。

Node 不在 PATH 上时整组跳过——这台机器上它是有的（agent 的守卫测试就靠它）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SMOKE = REPO / "tests" / "frontend_smoke.mjs"
FIXTURES = REPO / "tests" / "_real_fixtures.json"
TARGETS = [REPO / "web" / "app.js", REPO / "web" / "workflow.js",
           REPO / "admin-ui" / "app.js"]

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="没装 node，跑不了前端脚本")


def test_fixtures_are_present_and_look_like_the_real_api():
    """夹具是从跑着的服务抓下来的，先确认它没烂掉。

    夹具一空，冒烟就退化成"什么都不取"，于是恒绿——那比没有围栏更糟。
    """
    assert SMOKE.exists(), "冒烟脚本不见了"
    assert FIXTURES.exists(), (
        "夹具不见了。抓法：\n"
        "  python -c \"import json,urllib.request,pathlib;"
        "pathlib.Path(r'tests/_real_fixtures.json').write_text(json.dumps({"
        "k: json.loads(urllib.request.urlopen('http://127.0.0.1:8474/api/admin'+k"
        "+('?limit=50' if k=='/audit' else '')).read().decode()) "
        "for k in ['/me','/tasks','/users','/audit']}, ensure_ascii=False),"
        "encoding='utf-8')\"")
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    for k in ("/me", "/tasks", "/users", "/audit"):
        assert k in data, f"夹具缺 {k}"
    assert data["/tasks"]["tasks"], "任务列表是空的，冒烟会退化成什么都不渲染"
    assert data["/users"]["users"], "用户列表是空的"
    assert data["/me"]["role"] == "admin", "夹具里的身份得是管理员"


@pytest.mark.parametrize("target", TARGETS, ids=lambda p: p.parent.name)
def test_frontend_script_loads_and_renders(target):
    r = subprocess.run(
        ["node", str(SMOKE), str(target.relative_to(REPO))],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, (
        f"{target} 在桩环境里跑挂了：\n{r.stdout.strip()}\n{r.stderr.strip()}")
