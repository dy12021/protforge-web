"""stop-services 的「认进程」规则要有围栏。

这条规则为什么要有围栏
----------------------
``stop-services.ps1`` 原来只按**监听端口**杀进程。于是有一种进程杀不掉：
它跑着本仓的服务入口，却已经不占端口了。这种进程留着，下一次启动会
因为「端口已占用」跳过启动，于是**旧版本继续对外服务**——而日志上写的是
「already listening」，看着一切正常。

修法是把「算不算我们的服务进程」这条判断拎成纯函数
（``scripts/select_service_pids.py``）去测。而这条判断一旦放宽就会误杀：
本机同时跑着别的项目（``media-center-pms``、``creative-panel``、MaiBot 等），
还有一堆**看起来很像**的东西——``python app.py``（另一个项目的入口）、
node 进程、``-c`` 脚本里恰好提到路径。

所以这里钉的是**该杀的杀、不该杀的绝对不杀**，两个方向都要有。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import select_service_pids as sel   # noqa: E402

PY = '"C:\\ProgramData\\anaconda3\\python.exe"'
OURS = [
    {"pid": 18240, "name": "python.exe", "cmdline": f"{PY} server\\app.py public"},
    {"pid": 46392, "name": "python.exe", "cmdline": f"{PY} server\\app.py internal"},
    {"pid": 10748, "name": "python.exe", "cmdline": f"{PY} admin\\app_admin.py"},
]


def test_the_three_services_are_recognised():
    """三个服务入口都要认出来——认不出来就等于没修。"""
    assert sel.select_service_pids(OURS) == [18240, 46392, 10748]


def test_a_bare_app_py_from_another_project_is_left_alone():
    """别的项目的 ``python app.py`` 不能杀。

    本机确实有一个这样的进程（监听 8790），它跟本仓毫无关系。
    按「命令行里出现了 app.py 就杀」来认，会把它一起杀掉。
    """
    rows = [{"pid": 42488, "name": "python.exe",
             "cmdline": f"{PY} app.py"}]
    assert sel.select_service_pids(rows) == []


def test_a_similarly_named_path_is_left_alone():
    """``myserver\\app.py`` 不是 ``server\\app.py``——要逐段对上。"""
    rows = [{"pid": 7, "name": "python.exe",
             "cmdline": f"{PY} myserver\\app.py public"}]
    assert sel.select_service_pids(rows) == []


def test_a_non_python_process_is_left_alone():
    """node 进程参数里带同样的路径也不能杀。"""
    rows = [{"pid": 8, "name": "node.exe",
             "cmdline": "node server\\app.js"}]
    assert sel.select_service_pids(rows) == []


def test_a_path_mentioned_inside_a_script_is_left_alone():
    """入口得是**整个参数**。

    ``-c "print('server/app.py')"`` 里提到了入口，但它不是入口——
    杀它就是杀掉一个正在跑的一次性脚本。
    """
    rows = [{"pid": 9, "name": "python.exe",
             "cmdline": f'{PY} -c "print(\'server/app.py\')"'}]
    assert sel.select_service_pids(rows) == []


def test_an_absolute_path_to_the_entry_point_is_recognised():
    """用绝对路径起的也要认——认不出来等于留了孤儿。"""
    rows = [{"pid": 10, "name": "python.exe",
             "cmdline": f'{PY} ".\\server\\app.py" public'}]
    assert sel.select_service_pids(rows) == [10]


def test_the_same_pid_is_only_reported_once():
    """同一个进程命中两个入口（不可能，但别把 pid 报两遍）。"""
    rows = [
        {"pid": 11, "name": "python.exe",
         "cmdline": f"{PY} server\\app.py public admin\\app_admin.py"},
    ]
    assert sel.select_service_pids(rows) == [11]


def test_the_helper_is_actually_wired_into_the_stop_script():
    """规则和脚本必须接在一起。

    纯函数测得再好，脚本不调它就等于没修——所以这里钉住「脚本里确实有
    这次扫描」，免得以后有人把那段删了而测试照绿。
    """
    script = (SCRIPTS / "stop-services.ps1").read_text(encoding="utf-8")
    assert "select_service_pids" in script, \
        "stop-services.ps1 里没有这次进程扫描：不占端口的残留还杀不掉"
