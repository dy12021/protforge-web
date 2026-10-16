import pytest, store, db

def test_create_task_defaults_queued():
    t = store.create_task("跑 freesasa", "分析 1ubq 的表面暴露", owner_id=1, actor="human")
    assert t["status"] == "queued" and t["step"] == "" and t["brief"].startswith("分析")

def test_brief_rejected_when_too_long():
    with pytest.raises(store.Invalid):
        store.create_task("x", "a" * 20001, owner_id=1, actor="human")

def test_step_truncated_by_codepoint_not_byte():
    t = store.create_task("t", "b", owner_id=1, actor="human")
    out = store.set_step(t["id"], "蛋白设计" * 40)   # 160 码点
    assert len(out) <= 81 and out.endswith("…")
    assert not out.encode("utf-8").endswith(b"\xe2")  # 没被切断的多字节

def test_cross_user_get_returns_notfound():
    a = store.create_task("a", "b", owner_id=1, actor="human")
    with pytest.raises(store.NotFound):
        store.get_task(a["id"], owner_id=2)

def test_admin_sees_any_task():
    a = store.create_task("a", "b", owner_id=1, actor="human")
    assert store.get_task(a["id"], owner_id=2, is_admin=True)["id"] == a["id"]

def test_finished_marker_distinguishes_outcomes():
    t = store.create_task("a", "b", owner_id=1, actor="human")
    assert store.has_finished(t["id"]) is False
    store.record_finished(t["id"])
    assert store.has_finished(t["id"]) is True

def test_audit_log_separate_from_events():
    t = store.create_task("a", "b", owner_id=1, actor="human")
    store.add_event(t["id"], "status", "admin", "改了状态")
    store.audit.log(t["id"], "bash", "rm -rf x", 1, 12)
    assert len(store.list_events(t["id"])) == 1          # 审计不进时间线
    assert len(store.audit.list(t["id"])) == 1
