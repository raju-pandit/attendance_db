"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import json
import os
from collections import Counter
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017")
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def ensure_mongodb_collections():
    db.employees.create_index(
        [("emp_code", 1)], unique=True, name="uniq_employee_emp_code"
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uniq_attendance_employee_date",
    )
    db.attendance_logs.create_index([("date", 1)], name="attendance_date")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    # Naive punch times in this API represent local IST; normalize aware ones too.
    punch_out_ist = (
        punch_out.replace(tzinfo=IST)
        if punch_out.tzinfo is None
        else punch_out.astimezone(IST)
    )
    return max(0, int((punch_out_ist - end).total_seconds() / 60))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(IST).replace(tzinfo=None)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = to_local(body.punched_at)
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    page_docs = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("_id", -1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Shared helpers for the endpoints below
# --------------------------------------------------------------------------- #
HALF_DAY_HOURS = 4.5          # worked less than this -> half day
MAX_SHIFT = timedelta(hours=24)


def to_local(ms: Optional[int]) -> datetime:
    """Epoch millis -> naive IST datetime (the convention used for stored punch times)."""
    if ms is None:
        return datetime.now(IST).replace(tzinfo=None)
    return datetime.fromtimestamp(ms / 1000, IST).replace(tzinfo=None)


def parse_date(value: str, field: str = "date") -> str:
    try:
        return date_cls.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    return emp


def serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


def derived_fields(punch_in: datetime, punch_out: Optional[datetime], date_str: str, emp: dict) -> dict[str, Any]:
    out: dict[str, Any] = {
        "late_minutes": compute_late_minutes(punch_in, emp["shift_start"]),
    }
    if punch_out is None:
        out["work_hours"] = None
        out["overtime_minutes"] = 0
        out["half_day"] = False
    else:
        hours = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = hours
        out["overtime_minutes"] = compute_overtime(punch_out, emp["shift_end"], date_str)
        out["half_day"] = hours < HALF_DAY_HOURS
    return out


def resolve_range(month: Optional[str], date_from: Optional[str], date_to: Optional[str],
                  default_days: Optional[int] = None) -> tuple[str, str]:
    """Returns an inclusive (from, to) pair of ISO date strings."""
    if month:
        first = date_cls.fromisoformat(f"{month}-01")
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return first.isoformat(), last.isoformat()
    today = datetime.now(IST).date()
    if date_from or date_to:
        start = parse_date(date_from, "date_from") if date_from else "0000-01-01"
        end = parse_date(date_to, "date_to") if date_to else today.isoformat()
        if start > end:
            raise HTTPException(422, "date_from must be on or before date_to")
        return start, end
    if default_days:
        return (today - timedelta(days=default_days - 1)).isoformat(), today.isoformat()
    first = today.replace(day=1)
    return first.isoformat(), today.isoformat()


def dept_emp_codes(department: str) -> list[str]:
    return [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})]


MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
PRESENT = {"$cond": [{"$ifNull": ["$punch_in", False]}, 1, 0]}
LATE = {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}


# --------------------------------------------------------------------------- #
# Aggregation pipelines (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "days_recorded": {"$sum": 1},
            "days_present": {"$sum": PRESENT},
            "half_days": {"$sum": {"$cond": ["$half_day", 1, 0]}},
            "late_days": {"$sum": LATE},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            "total_work_hours": {"$sum": "$work_hours"},
            "avg_work_hours": {"$avg": "$work_hours"},
            "statuses": {"$push": "$status"},
        }},
    ]


def department_summary_pipeline(start: str, end: str) -> list:
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$lookup": {"from": "employees", "localField": "emp_code",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$group": {
            "_id": "$emp.department",
            "active_employees": {"$addToSet": "$emp_code"},
            "records": {"$sum": 1},
            "present_days": {"$sum": PRESENT},
            "late_days": {"$sum": LATE},
            "half_days": {"$sum": {"$cond": ["$half_day", 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            "avg_work_hours": {"$avg": "$work_hours"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id",
            "active_employees": {"$size": "$active_employees"},
            "records": 1, "present_days": 1, "late_days": 1, "half_days": 1,
            "total_late_minutes": 1, "total_overtime_minutes": 1, "avg_work_hours": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(start: str, end: str, limit: int, emp_codes: Optional[list] = None) -> list:
    match = {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}
    if emp_codes is not None:
        match["emp_code"] = {"$in": emp_codes}
    return [
        {"$match": match},
        {"$group": {
            "_id": "$emp_code",
            "late_days": {"$sum": 1},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "max_late_minutes": {"$max": "$late_minutes"},
        }},
        {"$sort": {"total_late_minutes": -1, "late_days": -1, "_id": 1}},
        {"$limit": limit},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": {"path": "$emp", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "_id": 0, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "late_days": 1, "total_late_minutes": 1, "max_late_minutes": 1,
            "avg_late_minutes": {"$round": [{"$divide": ["$total_late_minutes", "$late_days"]}, 1]},
        }},
    ]


def department_trend_pipeline(emp_codes: list, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": "$date",
            "present": {"$sum": PRESENT},
            "late": {"$sum": LATE},
            "half_days": {"$sum": {"$cond": ["$half_day", 1, 0]}},
            "avg_work_hours": {"$avg": "$work_hours"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
        {"$sort": {"_id": 1}},
    ]


def _r(value, digits=2):
    return round(value, digits) if isinstance(value, (int, float)) else value


# --------------------------------------------------------------------------- #
# Attendance: punch-out and corrections
# --------------------------------------------------------------------------- #
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class AttendancePatch(BaseModel):
    punch_in: Optional[int] = None    # epoch millis
    punch_out: Optional[int] = None   # epoch millis; explicit null clears it
    status: Optional[str] = None
    reason: Optional[str] = None
    edited_by: Optional[str] = None


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = to_local(body.punched_at)

    # Latest open record, so a night shift that crosses midnight still closes correctly.
    log = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}, "punch_out": None},
        sort=[("date", -1)],
    )
    if log is None:
        raise HTTPException(404, "No open punch-in found for this employee")
    if ts <= log["punch_in"]:
        raise HTTPException(422, "punch_out must be after punch_in")
    if ts - log["punch_in"] > MAX_SHIFT:
        raise HTTPException(422, "Open punch-in is older than 24 hours; correct it via PATCH")

    update = {"punch_out": ts, **derived_fields(log["punch_in"], ts, log["date"], emp)}
    doc = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"], "punch_out": None},   # guards against a concurrent punch-out
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise HTTPException(409, "Already punched out")
    return serialize(doc)


@app.patch("/attendance/{emp_code}/{date}")
def update_attendance(emp_code: str, date: str, body: AttendancePatch):
    date = parse_date(date)
    emp = get_employee_or_404(emp_code)
    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if log is None:
        raise HTTPException(404, "Attendance record not found")

    changes = body.model_dump(exclude_unset=True, exclude={"reason", "edited_by"})
    if not changes:
        raise HTTPException(422, "Provide at least one of punch_in, punch_out, status")

    new: dict = {}
    if "punch_in" in changes:
        if changes["punch_in"] is None:
            raise HTTPException(422, "punch_in cannot be null")
        new["punch_in"] = to_local(changes["punch_in"])
        if new["punch_in"].date().isoformat() != date:
            raise HTTPException(422, "punch_in must fall on the record's date")
    if "punch_out" in changes:
        new["punch_out"] = to_local(changes["punch_out"]) if changes["punch_out"] is not None else None
    if "status" in changes:
        if not changes["status"]:
            raise HTTPException(422, "status cannot be empty")
        new["status"] = changes["status"]

    pin = new.get("punch_in", log.get("punch_in"))
    pout = new.get("punch_out", log.get("punch_out"))
    if pin is None and pout is not None:
        raise HTTPException(422, "punch_out requires a punch_in")
    if pin and pout:
        if pout <= pin:
            raise HTTPException(422, "punch_out must be after punch_in")
        if pout - pin > MAX_SHIFT:
            raise HTTPException(422, "Shift cannot exceed 24 hours")
    if pin:
        new.update(derived_fields(pin, pout, date, emp))

    diff = {k: {"from": log.get(k), "to": v} for k, v in new.items() if log.get(k) != v}
    if not diff:
        return serialize(log)

    entry = {
        "at": datetime.now(IST).replace(tzinfo=None),
        "edited_by": body.edited_by,
        "reason": body.reason,
        "changes": diff,
    }
    doc = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"]},
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    return serialize(doc)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Optional[str] = Query(None, pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    month = month or datetime.now(IST).strftime("%Y-%m")
    start, end = resolve_range(month, None, None)

    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end)))
    agg = rows[0] if rows else {}
    return {
        "emp_code": emp_code,
        "name": emp.get("name"),
        "department": emp.get("department"),
        "month": month,
        "days_recorded": agg.get("days_recorded", 0),
        "days_present": agg.get("days_present", 0),
        "half_days": agg.get("half_days", 0),
        "late_days": agg.get("late_days", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "total_work_hours": _r(agg.get("total_work_hours", 0)),
        "avg_work_hours": _r(agg.get("avg_work_hours")),
        "status_breakdown": dict(Counter(s for s in agg.get("statuses", []) if s)),
    }


@app.get("/analytics/departments/summary")
def departments_summary(
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    start, end = resolve_range(month, date_from, date_to)
    headcount = {
        r["_id"]: r["n"]
        for r in db.employees.aggregate([{"$group": {"_id": "$department", "n": {"$sum": 1}}}])
    }
    stats = {r["department"]: r for r in db.attendance_logs.aggregate(department_summary_pipeline(start, end))}

    items = []
    for dept in sorted(set(headcount) | set(stats)):
        s = stats.get(dept, {})
        items.append({
            "department": dept,
            "headcount": headcount.get(dept, 0),
            "active_employees": s.get("active_employees", 0),
            "present_days": s.get("present_days", 0),
            "late_days": s.get("late_days", 0),
            "half_days": s.get("half_days", 0),
            "total_late_minutes": s.get("total_late_minutes", 0),
            "total_overtime_minutes": s.get("total_overtime_minutes", 0),
            "avg_work_hours": _r(s.get("avg_work_hours")),
            "late_rate": _r(s["late_days"] / s["present_days"], 3) if s.get("present_days") else 0,
        })
    return {"date_from": start, "date_to": end, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
):
    start, end = resolve_range(month, date_from, date_to)
    codes = dept_emp_codes(department) if department else None
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(start, end, limit, codes)))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return {"date_from": start, "date_to": end, "department": department, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = Query(30, ge=1, le=366),
):
    codes = dept_emp_codes(department)
    if not codes:
        raise HTTPException(404, "Department not found")
    start, end = resolve_range(None, date_from, date_to, default_days=days)
    if start == "0000-01-01":   # only date_to given -> look back `days` from it
        start = (date_cls.fromisoformat(end) - timedelta(days=days - 1)).isoformat()

    by_date = {r["_id"]: r for r in db.attendance_logs.aggregate(department_trend_pipeline(codes, start, end))}
    headcount = len(codes)
    series = []
    d, last = date_cls.fromisoformat(start), date_cls.fromisoformat(end)
    while d <= last:   # fill gaps so charts get a continuous series
        key = d.isoformat()
        r = by_date.get(key, {})
        present = r.get("present", 0)
        series.append({
            "date": key,
            "present": present,
            "late": r.get("late", 0),
            "half_days": r.get("half_days", 0),
            "avg_work_hours": _r(r.get("avg_work_hours")),
            "total_overtime_minutes": r.get("total_overtime_minutes", 0),
            "attendance_rate": _r(present / headcount, 3),
        })
        d += timedelta(days=1)
    return {"department": department, "headcount": headcount,
            "date_from": start, "date_to": end, "series": series}


# --------------------------------------------------------------------------- #
# Admin: query plans
# --------------------------------------------------------------------------- #
ExplainTarget = Literal[
    "attendance", "employee-monthly", "departments-summary", "leaderboard-late", "department-trend"
]


def _plan_summary(node, stages: list, indexes: set, stats: list):
    if isinstance(node, dict):
        if isinstance(node.get("stage"), str) and node["stage"] not in stages:
            stages.append(node["stage"])
        if isinstance(node.get("indexName"), str):
            indexes.add(node["indexName"])
        es = node.get("executionStats")
        if isinstance(es, dict):
            stats.append({k: es.get(k) for k in
                          ("nReturned", "totalKeysExamined", "totalDocsExamined", "executionTimeMillis")})
        for v in node.values():
            _plan_summary(v, stages, indexes, stats)
    elif isinstance(node, list):
        for v in node:
            _plan_summary(v, stages, indexes, stats)


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Runs MongoDB explain (executionStats) on the query behind an endpoint."""
    start, end = resolve_range(month, date_from, date_to)
    sample_emp = emp_code or (db.employees.find_one({}, {"emp_code": 1}) or {}).get("emp_code", "")
    sample_dept = department or (db.employees.find_one({}, {"department": 1}) or {}).get("department", "")

    if endpoint == "attendance":
        q: dict = {"date": {"$gte": start, "$lte": end}}
        if emp_code:
            q["emp_code"] = emp_code
        pipeline = [{"$match": q}, {"$sort": {"date": -1}}, {"$limit": 20}]
    elif endpoint == "employee-monthly":
        pipeline = monthly_pipeline(sample_emp, start, end)
    elif endpoint == "departments-summary":
        pipeline = department_summary_pipeline(start, end)
    elif endpoint == "leaderboard-late":
        pipeline = late_leaderboard_pipeline(start, end, 10, dept_emp_codes(department) if department else None)
    else:
        pipeline = department_trend_pipeline(dept_emp_codes(sample_dept), start, end)

    raw = db.command(
        "explain",
        {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )
    stages: list = []
    indexes: set = set()
    stats: list = []
    _plan_summary(raw, stages, indexes, stats)
    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "pipeline": json.loads(json_util.dumps(pipeline)),
        "summary": {
            "stages": stages,
            "indexes_used": sorted(indexes),
            "collection_scan": "COLLSCAN" in stages,
            "execution_stats": stats,
        },
        "explain": json.loads(json_util.dumps(raw)),
    }
