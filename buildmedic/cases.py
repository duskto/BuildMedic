import hashlib
from pathlib import Path

from .benchmark_cases import SYNTHETIC_CASES
from .config import ROOT

CASES = {
    "pagination": {
        "title": "分页边界错误",
        "category": "业务逻辑",
        "description": "分页结果缺少最后一项，检查切片边界及空页行为。",
        "target": "tests/test_pagination.py",
        "fingerprint": "test_pages",
    },
    "imports": {
        "title": "内部导入错误",
        "category": "模块导入",
        "description": "格式化模块移动后，发票模块仍引用旧的内部路径。",
        "target": "tests/test_invoice.py",
        "fingerprint": "ModuleNotFoundError",
    },
    "signature": {
        "title": "调用签名不一致",
        "category": "参数传播",
        "description": "运费接口新增 region 参数，检查所有调用入口。",
        "target": "tests/test_shipping.py",
        "fingerprint": "TypeError",
    },
    "missing-dependency": {
        "title": "依赖环境阻塞",
        "category": "失败路径",
        "description": "登记环境缺少必要依赖，保留证据并停止自动执行。",
        "blocked": True,
    },
    "not-reproduced": {
        "title": "外部服务未复现",
        "category": "失败路径",
        "description": "历史外部服务故障在固定本地检查中未出现。",
        "base_case": "pagination",
        "fingerprint": "ConnectionError: upstream service unavailable",
    },
}
DEMO_CASE_IDS = tuple(CASES)
CASES.update(SYNTHETIC_CASES)

EVALUATION_CASE_IDS = ("pagination", "imports", "signature", *SYNTHETIC_CASES)


def case_dir(case_id):
    case = CASES[case_id]
    return ROOT / "fixtures" / "public_cases" / case.get("base_case", case_id)


def source_hash(root: Path):
    digest = hashlib.sha256()
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            raise ValueError("快照包含符号链接")
        if file.is_file() and file.suffix == ".py" and "__pycache__" not in file.parts:
            digest.update(str(file.relative_to(root)).encode() + b"\0" + file.read_bytes())
    return digest.hexdigest()


def metadata(case_id):
    case = CASES[case_id]
    digest = source_hash(case_dir(case_id)) if not case.get("blocked") else None
    return {
        "case_id": case_id,
        "repo": "local/" + case["repository_group"]
        if case_id in SYNTHETIC_CASES
        else "local/buildmedic-demos",
        "head_sha": None,
        "snapshot_sha256": digest,
        "run_id": None,
        "run_attempt": None,
        "job_id": None,
        "source_type": "demo",
        "title": case["title"],
        "profile_version": "python-pytest-demo-v1",
        "execution_label": "本地真实案例 · 非 GitHub 运行记录",
    }
