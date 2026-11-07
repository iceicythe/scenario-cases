"""Validate fixed inputs and evaluate explicit per-case expectations."""
import json
import math
from pathlib import Path

MEETS = "meets_numeric_rule"
ASSESSMENTS = {MEETS, "below_numeric_rule", "source_unverified"}


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取 {path}：{error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须是 JSON 对象")
    return value


def _interval(item):
    try:
        a, b = (item["start"], item["end"]) if isinstance(item, dict) else item
        a, b = float(a), float(b)
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError(f"无效事件区间：{item!r}") from error
    if not math.isfinite(a) or not math.isfinite(b) or a >= b:
        raise ValueError(f"区间须为有限数且 start < end：{item!r}")
    return a, b


def require_same_ids(expected, actual, description):
    missing, extra = sorted(expected - actual), sorted(actual - expected)
    if missing or extra:
        raise ValueError(f"{description}不一致：缺失 {missing}；多余 {extra}")


def load_fixed_truth(cases_dir, labels_dir):
    """Validate a nonempty one-to-one mapping before any runtime is created."""
    cases_dir, labels_dir = Path(cases_dir), Path(labels_dir)
    if not cases_dir.is_dir() or not labels_dir.is_dir():
        raise ValueError("固定案例模式需要有效的 cases 与 labels 目录")
    case_ids = {p.name for p in cases_dir.iterdir() if p.is_dir()}
    if not case_ids:
        raise ValueError("固定案例目录为空")
    labels = {}
    for path in sorted(labels_dir.glob("*.json")):
        label = _read(path)
        cid = label.get("case_id", path.stem)
        if not isinstance(cid, str) or cid != path.stem:
            raise ValueError(f"标签 case_id 与文件名不一致：{path.name}")
        if cid in labels:
            raise ValueError(f"重复标签：{cid}")
        raw_events = label.get("events", [])
        if not isinstance(raw_events, list):
            raise ValueError(f"{cid} 的 events 必须是列表")
        events = []
        for item in raw_events:
            a, b = _interval(item)
            events.append({"start": a, "end": b,
                           "kind": item.get("kind", "deceleration") if isinstance(item, dict) else "deceleration"})
        decision = label.get("decision", "positive" if events else "negative")
        if not isinstance(decision, str) or decision not in {"positive", "negative", "uncertain"}:
            raise ValueError(f"{cid} 的 decision 无效：{decision}")
        if (decision == "positive" and not events) or (decision == "negative" and events):
            raise ValueError(f"{cid} 的 decision 与 events 矛盾")
        labels[cid] = {"events": events, "decision": decision,
                       "profile": label.get("scene", "external"),
                       "notes": [n for n in [label.get("notes"), label.get("scene")] if n]}
    require_same_ids(case_ids, set(labels), "案例与标签集合")
    return labels


def load_manifest(path, truth):
    """A supplied suite manifest must constrain every member explicitly."""
    data = _read(Path(path))
    members = data.get("members")
    if not isinstance(members, list) or not members:
        raise ValueError("manifest.members 必须为非空列表")
    expected = {}
    allowed = {"candidate_assessments", "reference_events", "allow_numeric_confirmation"}
    for member in members:
        if not isinstance(member, dict) or not isinstance(member.get("case_id"), str):
            raise ValueError("manifest 成员缺少 case_id")
        cid = member["case_id"]
        if cid in expected:
            raise ValueError(f"manifest 重复成员：{cid}")
        checks = member.get("expected")
        if not isinstance(checks, dict) or not checks or set(checks) - allowed:
            raise ValueError(f"{cid} 需要有效的 expected 行为断言")
        if "candidate_assessments" in checks:
            states = checks["candidate_assessments"]
            if not isinstance(states, list) or any(not isinstance(s, str) or s not in ASSESSMENTS for s in states):
                raise ValueError(f"{cid} 的 candidate_assessments 无效")
        if "reference_events" in checks:
            if not isinstance(checks["reference_events"], list):
                raise ValueError(f"{cid} 的 reference_events 必须为列表")
            checks["reference_events"] = [list(_interval(v)) for v in checks["reference_events"]]
        if "allow_numeric_confirmation" in checks and type(checks["allow_numeric_confirmation"]) is not bool:
            raise ValueError(f"{cid} 的 allow_numeric_confirmation 必须为布尔值")
        if cid in truth and "human_decision" in member and member["human_decision"] != truth[cid]["decision"]:
            raise ValueError(f"{cid} 的 manifest 与标签 decision 不一致")
        expected[cid] = checks
    require_same_ids(set(expected), set(truth), "manifest 与案例/标签集合")
    return expected


def check_behavior(cases, expected):
    """Check states/intervals directly; aggregate metric improvements cannot hide drift."""
    results = []
    for cid, checks in expected.items():
        if cid not in cases:
            results.append({"case_id": cid, "field": "present", "expected": True, "actual": False, "ok": False})
            continue
        case = cases[cid]
        for field, wanted in checks.items():
            if field == "candidate_assessments":
                actual = sorted(c["event_assessment"] for c in case["candidates"])
                ok = actual == sorted(wanted)
            elif field == "reference_events":
                actual = sorted(case["tool_reference_events"])
                ordered = sorted(wanted)
                ok = len(actual) == len(ordered) and all(
                    abs(a[0]-b[0]) < 5e-4 and abs(a[1]-b[1]) < 5e-4 for a,b in zip(actual,ordered))
            else:
                actual = any(c["event_assessment"] == MEETS for c in case["candidates"])
                ok = wanted or not actual
            results.append({"case_id": cid, "field": field, "expected": wanted, "actual": actual, "ok": bool(ok)})
    return {"passed": all(r["ok"] for r in results), "checks": results}
