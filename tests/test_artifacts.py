"""产物发现与打包（spec §8.2 两条路径都跑 / §5.3 打包走子进程 / §3.05.3 峰值）。

最要紧的一条是 ``test_both_paths_run_even_when_prefix_matched``：agent 完全自主，
系统提示要求 job 名带 ``t{id}_`` 前缀**不构成强制**。多工具接力时第一个守规矩、
第二个不守规矩很常见，若实现成「前缀命中就不扫时间窗」，第二个工具的产物就永久丢失。

时间窗命中项 **verified=0，可见但不可下载**，等管理员确认；并发跑时时间窗可能误收
他人产物，所以「先显示、再确认」比「自动采纳」安全。

``outputs`` 根用 ``PROTFORGE_DATA`` 指向 tmp_path，**测试绝不读真实的
``../protforge/data\\outputs``**（那里有用户既有的计算结果）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import artifacts  # noqa: E402
import db  # noqa: E402
import store  # noqa: E402

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f"


def iso(ts: float) -> str:
    """epoch 秒 → 与 db.now_iso 同格式的本地朴素时间串。"""
    return datetime.fromtimestamp(ts).strftime(_ISO_FMT)


def stamp(path: Path, ts: float) -> None:
    """把 path 及其全部子孙的 mtime 刷成 ts。

    先写文件再刷 mtime 是必须的：改文件 mtime **不会**更新父目录 mtime，
    所以目录自己的时间戳要显式设，否则「目录建于窗口外、文件写于窗口内」
    这个用例会被目录 mtime 掩盖。
    """
    for p in [path] + [q for q in path.rglob("*") if q.is_file()]:
        os.utime(p, (ts, ts))


@pytest.fixture
def outputs(tmp_path, monkeypatch) -> Path:
    """假的 ProtForge outputs 根。"""
    monkeypatch.setenv("PROTFORGE_DATA", str(tmp_path / "pf"))
    root = tmp_path / "pf" / "outputs"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def task() -> dict:
    return store.create_task("设计一个结合蛋白", "要能表达的小分子结合蛋白", owner_id=1)


def make_job(outputs: Path, name: str, ts: float, files: int = 2) -> Path:
    d = outputs / name
    d.mkdir(parents=True)
    for i in range(files):
        (d / f"part{i}.pdb").write_text("ATOM      1  N   MET A   1\n", encoding="utf-8")
    stamp(d, ts)
    return d


# --------------------------------------------------------------- 发现


def test_both_paths_run_even_when_prefix_matched(outputs, task):
    """模拟：第一个工具守规矩叫 t1_a，第二个没守规矩叫 b_random。"""
    now = time.time()
    make_job(outputs, f"t{task['id']}_a", now - 60)
    make_job(outputs, "b_random", now - 30)

    res = artifacts.discover(task_id=task["id"],
                             started_at=iso(now - 3600), ended_at=iso(now + 60))

    assert {r["source"] for r in res} == {"prefix", "timewindow"}
    assert next(r for r in res if r["source"] == "timewindow")["verified"] == 0
    assert {r["name"] for r in res} == {f"t{task['id']}_a", "b_random"}


def test_prefix_hit_is_verified_and_immediately_downloadable(outputs, task):
    make_job(outputs, f"t{task['id']}_rfdiffusion", time.time() - 10)

    res = artifacts.discover(task_id=task["id"])

    hit = next(r for r in res if r["name"] == f"t{task['id']}_rfdiffusion")
    assert (hit["source"], hit["verified"], hit["kind"]) == ("prefix", 1, "dir")
    assert hit["size"] > 0


def test_timewindow_hit_stays_unverified_in_db(outputs, task):
    """落库就必须是 0 —— store.artifacts.add 恒写 verified=1，实现要自己降回来。"""
    now = time.time()
    make_job(outputs, "sneaky_job", now - 10)

    artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                       ended_at=iso(now + 60))

    row = next(r for r in store.artifacts.list(task["id"]) if r["name"] == "sneaky_job")
    assert row["verified"] == 0


def test_outside_window_is_not_discovered(outputs, task):
    now = time.time()
    make_job(outputs, "ancient_job", now - 86400 * 3)

    res = artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                             ended_at=iso(now))

    assert res == []
    assert store.artifacts.list(task["id"]) == []


def test_nested_file_mtime_counts_not_just_dir(outputs, task):
    """目录与老文件都在窗口外、新文件写于窗口内——只看目录 mtime 会漏掉。

    NTFS 只在**增删条目**时更新目录 mtime，所以「跑了几小时、期间只是往已有文件里
    追加」的 job 目录 mtime 停在窗口外。测试用 os.utime 把目录时间戳按回去，
    复现的正是这个形态。
    """
    now = time.time()
    old = now - 86400 * 3
    d = make_job(outputs, "long_running_job", old)
    (d / "final.cif").write_text("data_final\n", encoding="utf-8")
    stamp(d / "final.cif", now - 5)
    os.utime(d, (old, old))  # 新建文件把目录顶到了 now，按回窗口外

    res = artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                             ended_at=iso(now))

    assert [r["name"] for r in res] == ["long_running_job"]
    assert res[0]["source"] == "timewindow"


def test_prefix_dir_is_not_duplicated_by_timewindow(outputs, task):
    """守规矩的目录同时落在时间窗里，去重后只留 prefix 一行（否则下载列表翻倍）。"""
    now = time.time()
    make_job(outputs, f"t{task['id']}_a", now - 60)

    res = artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                             ended_at=iso(now))

    assert len(res) == 1
    assert res[0]["source"] == "prefix"


def test_loose_files_in_outputs_are_ignored(outputs, task):
    """outputs/ 下的散文件是工具的临时残留，不是产物目录。"""
    now = time.time()
    (outputs / "scratch.tmp").write_text("junk", encoding="utf-8")
    stamp(outputs / "scratch.tmp", now - 5)

    res = artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                             ended_at=iso(now))

    assert res == []


def test_discover_is_idempotent(outputs, task):
    """重跑（断点续跑）不能把下载列表翻倍——store.add 已去重，discover 不该再插。"""
    now = time.time()
    make_job(outputs, f"t{task['id']}_a", now - 60)
    make_job(outputs, "b_random", now - 30)

    first = artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                               ended_at=iso(now))
    second = artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                                ended_at=iso(now))

    assert len(first) == len(second) == 2
    assert len(store.artifacts.list(task["id"])) == 2
    assert {r["id"] for r in first} == {r["id"] for r in second}


def test_discover_falls_back_to_task_created_at(outputs, task):
    """不传窗口时用 tasks.created_at 当起点、现在当终点，任务自己的产物仍能捞到。"""
    make_job(outputs, f"t{task['id']}_a", time.time() - 5)

    res = artifacts.discover(task_id=task["id"])

    assert [r["source"] for r in res] == ["prefix"]


def test_discover_ignores_other_tasks_prefix(outputs, task):
    """t2_ 前缀不能被 task 1 认领（目录时间也放到窗口外，只给前缀路径机会）。"""
    now = time.time()
    make_job(outputs, "t2_a", now - 86400 * 3)

    assert artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                              ended_at=iso(now)) == []


def test_discover_missing_task_raises():
    with pytest.raises(store.NotFound):
        artifacts.discover(task_id=999, started_at=iso(time.time() - 60),
                           ended_at=iso(time.time()))


# --------------------------------------------------------------- verify


def test_verify_promotes_timewindow_item(outputs, task):
    now = time.time()
    make_job(outputs, "b_random", now - 30)
    row = artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                             ended_at=iso(now))[0]

    out = artifacts.verify(task["id"], row["id"], ok=True)

    assert out["verified"] == 1
    assert store.artifacts.get(task["id"], row["id"])["verified"] == 1


def test_verify_reject_survives_rediscovery(outputs, task):
    """管理员否决后重跑 discover 不得把它悄悄提回来——store.add 恒写 1。"""
    now = time.time()
    make_job(outputs, "b_random", now - 30)
    row = artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                             ended_at=iso(now))[0]
    artifacts.verify(task["id"], row["id"], ok=False)

    artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                       ended_at=iso(now))

    assert store.artifacts.get(task["id"], row["id"])["verified"] == 0


def test_verify_keeps_admin_confirmation_on_rediscovery(outputs, task):
    now = time.time()
    make_job(outputs, "b_random", now - 30)
    row = artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                             ended_at=iso(now))[0]
    artifacts.verify(task["id"], row["id"], ok=True)

    artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                       ended_at=iso(now))

    assert store.artifacts.get(task["id"], row["id"])["verified"] == 1


def test_verify_unknown_id_raises(task):
    with pytest.raises(store.NotFound):
        artifacts.verify(task["id"], 4242, ok=True)


# --------------------------------------------------------------- 打包


def test_package_zips_verified_only(outputs, task):
    now = time.time()
    make_job(outputs, f"t{task['id']}_a", now - 60)
    make_job(outputs, "b_random", now - 30)
    artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                       ended_at=iso(now))

    out = artifacts.package(task["id"])

    names = zipfile.ZipFile(out).namelist()
    assert f"t{task['id']}_a/part0.pdb" in names
    assert not any(n.startswith("b_random") for n in names)
    assert Path(out) == db.data_root() / "packages" / f"task_{task['id']}.zip"


def test_package_runs_in_subprocess_and_reports_progress(outputs, task):
    make_job(outputs, f"t{task['id']}_a", time.time() - 5, files=4)
    artifacts.discover(task_id=task["id"])

    seen: list[dict] = []
    out = artifacts.package(task["id"], progress_cb=seen.append)

    assert Path(out).exists()
    assert seen, "打包必须回报进度，否则前端进度条永远不动"
    assert {p["event"] for p in seen} >= {"start", "progress", "done"}
    start = next(p for p in seen if p["event"] == "start")
    assert start["pid"] != os.getpid(), "打包必须 fork 子进程，不能在主服务里跑 zipfile"
    last = [p for p in seen if p["event"] == "progress"][-1]
    assert (last["done"], last["total"]) == (start["total"], start["total"])


def test_package_writes_events_by_default(outputs, task):
    """不传 progress_cb 时进度落进 events，前端 SSE 直接能推。"""
    make_job(outputs, f"t{task['id']}_a", time.time() - 5, files=3)
    artifacts.discover(task_id=task["id"])

    artifacts.package(task["id"])

    msgs = [e["message"] for e in store.list_events(task["id"]) if e["kind"] == "package"]
    assert msgs, "打包进度必须可观测"
    assert any("100%" in m for m in msgs)


def test_package_without_verified_artifacts_raises(outputs, task):
    now = time.time()
    make_job(outputs, "b_random", now - 30)
    artifacts.discover(task_id=task["id"], started_at=iso(now - 3600),
                       ended_at=iso(now))

    with pytest.raises(artifacts.PackageError):
        artifacts.package(task["id"])


def test_package_surfaces_child_failure(outputs, task):
    """源目录在打包前被清掉：子进程必须以非 0 退出，主服务抛中文 PackageError。"""
    d = make_job(outputs, f"t{task['id']}_a", time.time() - 5)
    artifacts.discover(task_id=task["id"])
    for f in d.iterdir():
        f.unlink()
    d.rmdir()

    with pytest.raises(artifacts.PackageError):
        artifacts.package(task["id"])


def test_package_custom_out_zip_and_preflight(outputs, task):
    make_job(outputs, f"t{task['id']}_a", time.time() - 5)
    artifacts.discover(task_id=task["id"])
    out = db.data_root() / "custom" / "res.zip"

    pre = artifacts.preflight(task["id"], out)

    assert pre["artifact_bytes"] > 0
    assert pre["peak_bytes"] == pre["artifact_bytes"] * 2  # §3.05.3 产物 + zip 两份
    assert artifacts.package(task["id"], out_zip=out) == str(out)
    assert out.exists()


def test_package_refuses_over_quota_before_spawning(outputs, task):
    """配额按「产物 + zip」合计算，超了要在起子进程**之前**就拒绝。"""
    make_job(outputs, f"t{task['id']}_a", time.time() - 5)
    artifacts.discover(task_id=task["id"])
    out = db.data_root() / "packages" / "res.zip"

    with pytest.raises(artifacts.QuotaExceeded):
        artifacts.package(task["id"], out_zip=out, quota_bytes=1)

    assert not out.exists()


# --------------------------------------------------------------- 打包子进程


def test_pack_worker_entrypoint_is_self_contained(tmp_path):
    """worker 模式必须能用「本文件 + spec」独立跑起来（真跑一次，证明它不是死代码）。"""
    src = tmp_path / "src" / "job_a"
    src.mkdir(parents=True)
    (src / "x.txt").write_text("hello", encoding="utf-8")
    out = tmp_path / "out.zip"
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"out_zip": str(out),
                                "sources": [{"src": str(src), "arcname": "job_a"}]}),
                    encoding="utf-8")

    proc = subprocess.run([sys.executable, artifacts.__file__, "--pack", str(spec)],
                          capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr
    assert zipfile.ZipFile(out).read("job_a/x.txt") == b"hello"
    assert json.loads(proc.stdout.strip().splitlines()[-1])["event"] == "done"
