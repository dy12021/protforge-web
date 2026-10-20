"""skills_index 的行为测试。

三条主线：
1. 真 YAML 解析 frontmatter（ProtForge 仓库自带 test_frontmatter.py 守的就是这个坑）
2. shell_kind 判定顺序（.sh > WSL 正文 > .py > host 兜底）
3. 超时表

「坏技能不许拖垮扫描」和「frontmatter 解析失败不许静默吞」是两件不同的事，
分别由 test_malformed_frontmatter_raises_instead_of_being_dropped 与
test_unresolvable_skill_degrades_to_host_and_default_timeout 守住。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import skills_index
from skills_index import SkillIndexError, scan, timeout_for


def _real_skills_dir() -> Path:
    """找到**真机上的技能目录**。

    顺序：环境变量 → 仓库里的 ``.env``（本机写的，那份不入版本库）→
    代码里的默认。

    技能仓是**另一个仓库**（github.com/dy12021/protforge），不在这个仓里，
    所以路径不能写死——写死了公开出去的仓库就带着作者本机的布局，
    而换台机器 clone 下来也指着一个不存在的目录。
    """
    env = os.environ.get("PROTFORGE_WEB_SKILLS_DIR")
    if env:
        return Path(env)
    envfile = Path(__file__).resolve().parent.parent / ".env"
    if envfile.is_file():
        for line in envfile.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("PROTFORGE_WEB_SKILLS_DIR="):
                return Path(line.split("=", 1)[1].strip().strip('"'))
    return Path(skills_index.PROTFORGE_SKILLS_DIR)


def _scan_real() -> dict:
    """扫真机上的技能目录；**目录不在就跳过**，不是红。

    这组用例测的是「扫到的技能对不对」——没有技能仓可扫时它无从谈起，
    而把「没装技能仓」报成「代码坏了」会把排查引到错的方向。
    """
    d = _real_skills_dir()
    if not d.is_dir():
        pytest.skip(f"技能目录不在：{d}（装上技能仓，或设 PROTFORGE_WEB_SKILLS_DIR）")
    return scan(str(d))


@pytest.fixture(scope="module")
def idx():
    return _scan_real()


# --- 扫描覆盖面 ---------------------------------------------------------


def test_scan_finds_all_23_skills():
    idx = _scan_real()
    assert len(idx) >= 23
    assert "protforge-freesasa" in idx


def test_no_skill_is_silently_dropped(idx):
    """任何一个 SKILL.md 解析失败都会让技能从字典里消失——这里逐个兜底。

    ProtForge 的 description 是长自由文本（含 'Trigger: When ...'），
    裸标量写法会让 PyYAML 抛 ScannerError，技能就被静默丢掉了。
    """
    empty = [n for n, s in idx.items() if not s.description]
    assert empty == [], f"这些技能被静默丢弃或描述为空：{empty}"


# --- frontmatter --------------------------------------------------------


def test_frontmatter_parsed_with_real_yaml(idx):
    """description 是长自由文本，含 'Trigger: When ...'。

    裸标量里的 ': ' 会让 PyYAML 抛 ScannerError，解析失败的技能会被静默丢弃——
    protforge 仓库自带 test_frontmatter.py 就是这个坑。必须用真 YAML 解析器。
    """
    assert idx["protforge-freesasa"].description       # 不为空
    assert "SASA" in idx["protforge-freesasa"].description


def test_frontmatter_name_and_description_are_strings(idx):
    s = idx["protforge-freesasa"]
    assert isinstance(s.description, str)
    # YAML 把没加引号的纯数字/布尔会解析成非 str，description 必须强制成 str
    assert all(isinstance(v.description, str) for v in idx.values())


def test_malformed_frontmatter_raises_instead_of_being_dropped(tmp_path):
    """坏 frontmatter 必须**炸**，不能 try/except 吞掉后假装技能不存在。"""
    d = tmp_path / "protforge-broken"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: protforge-broken\ndescription: bare scalar: with colon\n---\n\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillIndexError) as ei:
        scan(tmp_path)
    assert "protforge-broken" in str(ei.value)


# --- shell_kind ---------------------------------------------------------


def test_shell_kinds_detected(idx):
    assert idx["protforge-freesasa"].shell_kind == "gitbash"
    assert idx["protforge-opendde"].shell_kind == "wsl"
    assert idx["protforge-alphafold-db"].shell_kind == "host"


def test_alphafold3_sh_runner_beats_wsl_mention(idx):
    """protforge-alphafold3 同时有 .sh runner 和正文里的 wsl.exe -d。

    计划把它归在 17 个 bash runner 里，所以 .sh 优先于 WSL 正文。
    """
    assert idx["protforge-alphafold3"].shell_kind == "gitbash"
    assert idx["protforge-alphafold3"].has_runner is True


def test_wsl_skills_have_no_bash_runner(idx):
    for name in ("protforge-esmfold2", "protforge-opendde", "protforge-af3-msa"):
        assert idx[name].shell_kind == "wsl"
        assert idx[name].runner_path is None      # 不经过 bash runner


def test_unresolvable_skill_degrades_to_host_and_default_timeout(tmp_path):
    """判定不出 shell_kind 时归 host + 默认超时，不许抛异常中断整个扫描。"""
    d = tmp_path / "protforge-mystery"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: protforge-mystery\n---\n\n正文没有任何调用方式\n", encoding="utf-8")

    got = scan(tmp_path)
    assert got["protforge-mystery"].shell_kind == "host"
    assert got["protforge-mystery"].timeout_s == 14400
    assert got["protforge-mystery"].has_runner is False
    assert got["protforge-mystery"].runner_path is None


def test_one_broken_skill_does_not_kill_the_others(tmp_path):
    """一个技能的 SKILL.md 完全缺失时，其余技能仍要扫出来（描述留空，不静默消失）。"""
    good = tmp_path / "protforge-freesasa"
    good.mkdir()
    (good / "SKILL.md").write_text(
        "---\nname: protforge-freesasa\ndescription: SASA analysis\n---\n\nbody\n", encoding="utf-8"
    )
    bare = tmp_path / "protforge-bare"
    bare.mkdir()

    got = scan(tmp_path)
    assert set(got) == {"protforge-freesasa", "protforge-bare"}
    assert got["protforge-bare"].description == ""


# --- 超时表 -------------------------------------------------------------


def test_timeout_table_applied(idx):
    assert idx["protforge-freesasa"].timeout_s == 600
    assert idx["protforge-rfdiffusion"].timeout_s == 28800


@pytest.mark.parametrize(
    "skill,expected",
    [
        # 600s
        ("freesasa", 600), ("chemprop", 600), ("discotope3", 600),
        ("fpocket", 600), ("p2rank", 600),
        # 7200s
        ("vina-gpu", 7200), ("gnina", 7200), ("autodock-gpu", 7200), ("diffdock", 7200),
        # 28800s
        ("rfdiffusion", 28800), ("bindcraft", 28800), ("gromacs", 28800), ("proteinmpnn", 28800),
        # 默认
        ("esmfold2", 14400), ("opendde", 14400), ("af3-msa", 14400), ("overview", 14400),
    ],
)
def test_timeout_for_accepts_bare_and_prefixed_names(skill, expected):
    assert timeout_for(skill) == expected
    assert timeout_for(f"protforge-{skill}") == expected


def test_unknown_skill_gets_default_timeout():
    assert timeout_for("protforge-some-unknown") == 14400
    assert timeout_for("完全不存在的技能") == 14400


def test_pesto_and_esm_timeouts_are_measured():
    """pesto / esm 已于 2026-10-16 实测（pesto 16.6 s / esm 36.9 s），档位归 MED。

    它们**必须**退出 UNVERIFIED_TIMEOUTS：那个集合的用途是让上层标出"别把这个
    当实测值引用"，实测完了还留着，比一开始就没写清楚还糟——后来人会以为没人管。
    """
    assert skills_index.UNVERIFIED_TIMEOUTS == frozenset()
    assert timeout_for("pesto") == skills_index.TIMEOUT_MED_S
    assert timeout_for("esm") == skills_index.TIMEOUT_MED_S
    # 也不能比实测小太多：那会把大输入上的合法运行腰斩
    assert timeout_for("esm") >= 600


# --- 其它字段 -----------------------------------------------------------


def test_reads_workspace_flag(idx):
    """读外部输入目录的技能才需要任务工作区。

    protforge-overview 是反面教材：它正文里写了 `$PROTFORGE_DATA/inputs/`，
    但那只是**描述**目录约定（路由文档），自己不执行，所以不能算 True。
    """
    assert idx["protforge-freesasa"].reads_workspace is True   # 正文引用 data/inputs
    assert idx["protforge-overview"].reads_workspace is False
    assert idx["protforge-overview"].has_runner is False


def test_skills_dir_default_is_portable():
    """默认技能目录**跟着仓库走**（旁边那个技能仓的 skills/），不写死盘符。

    写死作者本机的路径，公开出去的仓库就带着他的磁盘布局；换台机器 clone
    下来也指着一个不存在的目录。真实位置用 PROTFORGE_WEB_SKILLS_DIR 指
    （本机的写法在仓库里的 .env，那份不入版本库）。
    """
    default = Path(skills_index.PROTFORGE_SKILLS_DIR)
    repo = Path(skills_index.__file__).resolve().parent.parent
    assert default == repo.parent / "protforge" / "skills"


def test_scan_is_repeatable(idx):
    """同一目录扫两次结果一致（幂等），便于上层缓存。"""
    assert _scan_real().keys() == idx.keys()
