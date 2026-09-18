"""End-to-end sanity checks run against the real FastAPI app (in-process)."""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tmp
os.environ["DRILLS_DIR"] = str(Path(tmp) / "drills")
os.environ["STATIC_DIR"] = str(Path(__file__).resolve().parents[1] / "frontend")
os.environ["HOST_KEY"] = "test-key"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from httpx import ASGITransport, AsyncClient  # noqa: E402

Path(os.environ["DRILLS_DIR"]).mkdir(parents=True, exist_ok=True)
from app.config import DB_PATH  # noqa: E402
from app.db import Database  # noqa: E402
from app.definitions import load_file  # noqa: E402
from app.engine import Engine, EngineError  # noqa: E402
from app.main import app, db as app_db, engine as app_engine, timers  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


SHORT_YAML = """
id: short
name: 短演练
roles:
  - {id: a, name: 甲, invite: AAA}
  - {id: b, name: 乙, invite: BBB}
stages:
  - id: s1
    title: 阶段一
    public: 公开1
    duration: 2
    default_next: s2
    views: {a: 甲的秘密}
    options:
      a:
        - {id: x, text: 去二, next: s2}
        - {id: y, text: 去三, next: s3}
      b:
        - {id: x, text: 乙选项}
  - id: s2
    title: 阶段二
    public: 公开2
    duration: null
    default_next: null
    views: {b: 乙的秘密}
    options:
      b:
        - {id: k, text: 结束}
  - id: s3
    title: 阶段三
    public: 公开3
    duration: null
    options:
      a:
        - {id: k, text: 完事}
"""


async def main():
    failures = []

    # ---- definition validation ----
    print("[1] YAML 校验")
    d = load_file(Path(__file__).resolve().parents[2] / "drills" / "chem-plant.yaml")
    check("示例剧本可解析", d.id == "chem-plant")
    check("3 个角色", len(d.roles) == 3)
    check("8 个阶段", len(d.stages) == 8)

    from app.definitions import DrillError, parse_definition
    import yaml
    bad = yaml.safe_load(SHORT_YAML)
    parse_definition(bad)  # valid baseline
    bad2 = {**bad, "roles": [dict(r) for r in bad["roles"]] + [{"id": "c", "name": "丙", "invite": "AAA"}]}
    try:
        parse_definition(bad2); check("重复邀请码被拒", False)
    except DrillError: check("重复邀请码被拒", True)
    bad3 = {**bad, "stages": [{**dict(bad["stages"][0]), "default_next": "nope"}]}
    try:
        parse_definition(bad3); check("悬空 next 被拒", False)
    except DrillError: check("悬空 next 被拒", True)

    # ---- engine level tests with a fresh DB ----
    print("[2] 引擎核心")
    test_db = Database(str(Path(tmp) / "t.db"))
    await test_db.connect()
    eng = Engine(test_db)
    await eng.upsert_drill(parse_definition(bad))
    sid = await eng.create_session("short")
    ja = await eng.join(sid, "AAA", "张三")
    jb = await eng.join(sid, "b", "")  # wrong code -> uses invite BBB below
    check("错误邀请码应被拒", False) if jb else None


async def run():
    pass


if __name__ == "__main__":
    # simpler: drive via HTTP
    asyncio.run(main())
