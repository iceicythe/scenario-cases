"""场景库：单文件 SQLite，登记场景元数据、真值事件与文件内容；sha256 内容指纹去重。

设计取舍（面试可讲）：
- 单文件 SQLite：零依赖、零服务进程，一个文件拷走就是整个库。
  不上 Postgres/MySQL 的理由：单机单人工具、要能一键拷走。
- 文件内容随元数据一起入库（BLOB）：库自带数据，源目录删了也能原样导出，
  闭环回归直接读库就能跑，不依赖外部路径。
- 没有来源声明（source.json）的场景不予入库——评测数据的可信度一半在声明上。
- 指纹 = 六个文件的内容哈希：内容相同即重复，与放在哪个目录、叫什么名字无关。
- 常用检索轴（profile / v0 / decel / duration / at）建成列，完整参数另存 JSON：
  范围查询走列，信息不丢。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LIBRARY_VERSION = "scenario-library-0.1.0"
SCHEMA_VERSION = 2  # v2 增加 suites 表（回归集快照）

DATA_FILES = ("vehicle_state.csv", "object_state.csv", "planning_state.csv", "control_state.csv")
TRUTH_FILE = "truth.json"
SOURCE_FILE = "source.json"
SCENARIO_FILES = DATA_FILES + (TRUTH_FILE, SOURCE_FILE)
PARAM_COLUMNS = ("v0", "decel", "duration", "at")

SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS scenarios (
        scenario_id TEXT PRIMARY KEY,
        profile TEXT NOT NULL,
        params TEXT NOT NULL,
        v0 REAL, decel REAL, duration REAL, at REAL,
        rows INTEGER NOT NULL,
        event_count INTEGER NOT NULL,
        fingerprint TEXT NOT NULL UNIQUE,
        source_dir TEXT,
        notes TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        scenario_id TEXT NOT NULL REFERENCES scenarios(scenario_id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        start REAL NOT NULL,
        end REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)",
    "CREATE INDEX IF NOT EXISTS idx_scenarios_profile ON scenarios(profile)",
    """CREATE TABLE IF NOT EXISTS files (
        scenario_id TEXT NOT NULL REFERENCES scenarios(scenario_id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        content BLOB NOT NULL,
        PRIMARY KEY (scenario_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS suites (
        suite_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        query TEXT NOT NULL,
        scenario_ids TEXT NOT NULL,
        event_total INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )""",
)


class ScenarioError(Exception):
    """场景目录不合法：缺文件、JSON 损坏、结构不符、id 冲突等。"""


class SuiteError(Exception):
    """回归集操作失败：名字重复、不存在等。"""


@dataclass(frozen=True)
class SuiteRecord:
    suite_id: int
    name: str
    query: dict          # 创建时的查询条件（provenance，不参与成员判定）
    scenario_ids: tuple  # 成员快照
    event_total: int
    created_at: str

    def as_dict(self) -> dict:
        return {
            "suite_id": self.suite_id,
            "name": self.name,
            "query": self.query,
            "scenario_ids": list(self.scenario_ids),
            "scenario_count": len(self.scenario_ids),
            "event_total": self.event_total,
            "created_at": self.created_at,
        }


def _suite_record(row) -> SuiteRecord:
    return SuiteRecord(
        suite_id=row[0], name=row[1], query=json.loads(row[2]),
        scenario_ids=tuple(json.loads(row[3])), event_total=row[4], created_at=row[5])


@dataclass(frozen=True)
class AddResult:
    scenario_id: str
    status: str  # "added" | "duplicate"
    detail: str = ""


@dataclass(frozen=True)
class ScenarioRecord:
    scenario_id: str
    profile: str
    params: dict
    rows: int
    event_count: int
    fingerprint: str
    source_dir: str | None
    notes: list
    created_at: str
    events: tuple  # [(kind, start, end), ...]

    def as_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "profile": self.profile,
            "params": self.params,
            "rows": self.rows,
            "event_count": self.event_count,
            "fingerprint": self.fingerprint,
            "source_dir": self.source_dir,
            "notes": self.notes,
            "created_at": self.created_at,
            "events": [{"kind": kind, "start": start, "end": end}
                       for kind, start, end in self.events],
        }


def fingerprint_of(blobs: dict) -> str:
    """内容指纹：逐项混入 (文件名, 文件哈希) 后再取总和，与目录位置无关。"""
    digest = hashlib.sha256()
    for name in sorted(blobs):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(blobs[name]).digest())
    return digest.hexdigest()


def _number(value):
    """只把真正的数字搬进检索列；布尔与字符串一律按缺失处理。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count_rows(vehicle_csv: bytes) -> int:
    return len(vehicle_csv.decode("utf-8").splitlines()) - 1  # 去掉表头


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_truth(truth, directory) -> None:
    if not isinstance(truth, dict):
        raise ScenarioError(f"truth.json 顶层必须是对象：{directory}")
    spec = truth.get("spec")
    if not isinstance(spec, dict):
        raise ScenarioError(f"truth.json 缺少 spec 对象：{directory}")
    if not isinstance(spec.get("profile"), str) or not spec["profile"]:
        raise ScenarioError(f"truth.json 的 spec.profile 必须是非空字符串：{directory}")
    if not isinstance(spec.get("params"), dict):
        raise ScenarioError(f"truth.json 的 spec.params 必须是对象：{directory}")
    events = truth.get("events")
    if not isinstance(events, list):
        raise ScenarioError(f"truth.json 的 events 必须是数组：{directory}")
    for event in events:
        if not isinstance(event, dict):
            raise ScenarioError(f"events 里的条目必须是对象：{event!r}")
        start, end = event.get("start"), event.get("end")
        for value in (start, end):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ScenarioError(f"事件的 start/end 必须是数字：{event!r}")
        if not 0 <= start <= end:
            raise ScenarioError(f"事件区间不合法（要求 0 <= start <= end）：{event!r}")
        if not isinstance(event.get("kind", "deceleration"), str):
            raise ScenarioError(f"事件的 kind 必须是字符串：{event!r}")
    if not isinstance(truth.get("notes", []), list):
        raise ScenarioError(f"truth.json 的 notes 必须是数组：{directory}")


def read_scenario(directory) -> tuple[dict, dict, str]:
    """读取一个场景目录 → (truth, {文件名: 字节}, 指纹)。不合法抛 ScenarioError。"""
    directory = Path(directory)
    if not directory.is_dir():
        raise ScenarioError(f"目录不存在：{directory}")
    blobs = {}
    for name in SCENARIO_FILES:
        path = directory / name
        if not path.is_file():
            raise ScenarioError(f"缺少 {name}：{directory}")
        blobs[name] = path.read_bytes()
    try:
        truth = json.loads(blobs[TRUTH_FILE].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScenarioError(f"truth.json 解析失败：{exc}") from exc
    _validate_truth(truth, directory)
    return truth, blobs, fingerprint_of(blobs)


class ScenarioLibrary:
    """场景库：SQLite 单文件，内容寻址去重，文件内容随元数据一起入库。"""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            # 旧库打开时幂等补建新表并升版本；user_version 已是新版则不动
            if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            for statement in SCHEMA_STATEMENTS:
                db.execute(statement)
            yield db
            db.commit()
        finally:
            db.close()

    def add_scenario(self, directory) -> AddResult:
        truth, blobs, fingerprint = read_scenario(directory)
        spec = truth["spec"]
        params = spec["params"]
        events = truth.get("events", [])
        notes = truth.get("notes", [])
        scenario_id = str(truth.get("case_id") or Path(directory).name)
        rows = _count_rows(blobs["vehicle_state.csv"])
        with self._connect() as db:
            hit = db.execute("SELECT scenario_id FROM scenarios WHERE fingerprint = ?",
                             (fingerprint,)).fetchone()
            if hit:
                return AddResult(hit[0], "duplicate", f"内容指纹已登记为 {hit[0]}")
            if db.execute("SELECT 1 FROM scenarios WHERE scenario_id = ?",
                          (scenario_id,)).fetchone():
                raise ScenarioError(
                    f"场景 id {scenario_id!r} 已存在且内容不同；换内容请同时换 case_id")
            db.execute(
                "INSERT INTO scenarios (scenario_id, profile, params, v0, decel, duration, at,"
                " rows, event_count, fingerprint, source_dir, notes, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scenario_id, spec["profile"],
                 json.dumps(params, ensure_ascii=False, sort_keys=True),
                 *[_number(params.get(column)) for column in PARAM_COLUMNS],
                 rows, len(events), fingerprint, str(Path(directory)),
                 json.dumps(notes, ensure_ascii=False), _now()))
            for event in events:
                db.execute("INSERT INTO events (scenario_id, kind, start, end) VALUES (?,?,?,?)",
                           (scenario_id, event.get("kind", "deceleration"),
                            float(event["start"]), float(event["end"])))
            for name, content in blobs.items():
                db.execute("INSERT INTO files (scenario_id, name, content) VALUES (?,?,?)",
                           (scenario_id, name, content))
        return AddResult(scenario_id, "added", f"{rows} 行 × {len(blobs)} 个文件已入库")

    def add_many(self, directories) -> dict:
        summary = {"added": [], "duplicate": [], "failed": []}
        for directory in directories:
            try:
                result = self.add_scenario(directory)
            except ScenarioError as exc:
                summary["failed"].append((str(directory), str(exc)))
            else:
                summary[result.status].append(result)
        return summary

    def _record(self, db, row) -> ScenarioRecord:
        events = db.execute(
            "SELECT kind, start, end FROM events WHERE scenario_id = ? ORDER BY start, end",
            (row[0],)).fetchall()
        return ScenarioRecord(
            scenario_id=row[0], profile=row[1], params=json.loads(row[2]), rows=row[3],
            event_count=row[4], fingerprint=row[5], source_dir=row[6],
            notes=json.loads(row[7]), created_at=row[8],
            events=tuple((kind, start, end) for kind, start, end in events))

    _COLUMNS = ("SELECT scenario_id, profile, params, rows, event_count, fingerprint,"
                " source_dir, notes, created_at FROM scenarios")

    def query(self, *, profile=None, event_kind=None, min_decel=None, max_decel=None,
              min_v0=None, max_v0=None, with_events=None) -> list:
        """按条件检索。profile 接受单个模板或模板列表（对应 --profile a,b 的组筛选，
        如一次选出整个对抗组）。"""
        clauses, params = [], []
        if profile is not None:
            names = [profile] if isinstance(profile, str) else list(profile)
            if not names:
                raise ValueError("profile 列表不能为空")
            clauses.append(f"profile IN ({','.join('?' * len(names))})")
            params.extend(names)
        if event_kind is not None:
            clauses.append("EXISTS (SELECT 1 FROM events e"
                           " WHERE e.scenario_id = scenarios.scenario_id AND e.kind = ?)")
            params.append(event_kind)
        if min_decel is not None:
            clauses.append("decel >= ?")
            params.append(float(min_decel))
        if max_decel is not None:
            clauses.append("decel <= ?")
            params.append(float(max_decel))
        if min_v0 is not None:
            clauses.append("v0 >= ?")
            params.append(float(min_v0))
        if max_v0 is not None:
            clauses.append("v0 <= ?")
            params.append(float(max_v0))
        if with_events is True:
            clauses.append("event_count > 0")
        elif with_events is False:
            clauses.append("event_count = 0")
        sql = self._COLUMNS + (" WHERE " + " AND ".join(clauses) if clauses else "") \
            + " ORDER BY scenario_id"
        with self._connect() as db:
            return [self._record(db, row) for row in db.execute(sql, params).fetchall()]

    def get(self, scenario_id) -> ScenarioRecord | None:
        with self._connect() as db:
            row = db.execute(self._COLUMNS + " WHERE scenario_id = ?",
                             (scenario_id,)).fetchone()
            return self._record(db, row) if row else None

    def export(self, scenario_id, directory) -> list:
        with self._connect() as db:
            rows = db.execute("SELECT name, content FROM files WHERE scenario_id = ?"
                              " ORDER BY name", (scenario_id,)).fetchall()
        if not rows:
            return []
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        written = []
        for name, content in rows:
            path = directory / name
            path.write_bytes(content)
            written.append(path)
        return written

    def stats(self) -> dict:
        with self._connect() as db:
            total = db.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0]
            by_profile = dict(db.execute(
                "SELECT profile, COUNT(*) FROM scenarios"
                " GROUP BY profile ORDER BY profile").fetchall())
            with_events = db.execute(
                "SELECT COUNT(*) FROM scenarios WHERE event_count > 0").fetchone()[0]
            rows_total = db.execute(
                "SELECT COALESCE(SUM(rows), 0) FROM scenarios").fetchone()[0]
        return {"total": total, "by_profile": by_profile, "with_events": with_events,
                "without_events": total - with_events, "rows_total": rows_total}

    # ---- 回归集（suite）：把一次查询结果固化成命名快照 ----
    #
    # 设计取舍（面试可讲）：suite 存**快照 id 列表**而不是只存查询条件。
    # 只存条件的话，库里每新增一条场景，回归集的成员就悄悄变了——
    # 两次评测跑的其实不是同一套卷子，指标不可比。条件只作为 provenance
    # 记录在案（"这个集当初是怎么挑的"），成员以快照为准。

    def create_suite(self, name: str, query: dict, records: list) -> SuiteRecord:
        """把一批查询结果固化为命名回归集。同名抛 SuiteError。"""
        scenario_ids = sorted(record.scenario_id for record in records)
        event_total = sum(record.event_count for record in records)
        with self._connect() as db:
            if db.execute("SELECT 1 FROM suites WHERE name = ?", (name,)).fetchone():
                raise SuiteError(f"回归集 {name!r} 已存在；换名字或先删除")
            cursor = db.execute(
                "INSERT INTO suites (name, query, scenario_ids, event_total, created_at)"
                " VALUES (?,?,?,?,?)",
                (name, json.dumps(query, ensure_ascii=False, sort_keys=True),
                 json.dumps(scenario_ids, ensure_ascii=False), event_total, _now()))
            row = db.execute("SELECT suite_id, name, query, scenario_ids, event_total,"
                             " created_at FROM suites WHERE suite_id = ?",
                             (cursor.lastrowid,)).fetchone()
        return _suite_record(row)

    def list_suites(self) -> list:
        with self._connect() as db:
            rows = db.execute("SELECT suite_id, name, query, scenario_ids, event_total,"
                              " created_at FROM suites ORDER BY suite_id").fetchall()
        return [_suite_record(row) for row in rows]

    def get_suite(self, name: str) -> SuiteRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT suite_id, name, query, scenario_ids, event_total,"
                             " created_at FROM suites WHERE name = ?", (name,)).fetchone()
        return _suite_record(row) if row else None

    def suite_members(self, name: str) -> list:
        """回归集当前的成员记录（库里被删过的场景不出现在结果里，保持集合完整语义）。"""
        suite = self.get_suite(name)
        if suite is None:
            raise SuiteError(f"回归集 {name!r} 不存在")
        records = []
        for scenario_id in suite.scenario_ids:
            record = self.get(scenario_id)
            if record is not None:
                records.append(record)
        return records

    def export_suite(self, name: str, directory) -> Path:
        """导出整个回归集：每个场景一个子目录（字节级一致）+ 顶层 manifest.json。

        manifest 是"带标准答案的卷子"的可交付形态：suite 元信息、逐场景
        真值事件与参数摘要、内容指纹。下游工具拿 manifest 即可对答案，
        不必自己解析每份 truth.json。
        """
        suite = self.get_suite(name)
        if suite is None:
            raise SuiteError(f"回归集 {name!r} 不存在")
        records = self.suite_members(name)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        scenarios = []
        for record in records:
            target = directory / record.scenario_id
            self.export(record.scenario_id, target)
            scenarios.append({
                "scenario_id": record.scenario_id,
                "profile": record.profile,
                "params": record.params,
                "rows": record.rows,
                "events": [{"kind": kind, "start": start, "end": end}
                           for kind, start, end in record.events],
                "fingerprint": record.fingerprint,
            })
        manifest = {
            "manifest_version": 1,
            "suite": suite.as_dict(),
            "scenarios": scenarios,
            "counts": {
                "scenarios": len(scenarios),
                "events": sum(record.event_count for record in records),
                "with_events": sum(1 for record in records if record.event_count),
                "without_events": sum(1 for record in records if not record.event_count),
            },
        }
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        return manifest_path
