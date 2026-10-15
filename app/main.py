"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""

import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient
from pymongo.errors import PyMongoError, DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def ensure_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    h, m = map(int, shift_start.split(":"))
    h2, m2 = map(int, shift_end.split(":"))
    p = punch_in.astimezone(IST).replace(microsecond=0)
    start = p.replace(hour=h, minute=m, second=0, microsecond=0)
    end = p.replace(hour=h2, minute=m2, second=0, microsecond=0)
    overnight = shift_end < shift_start
    if overnight and p.time() < end.time():
        start = start - timedelta(days=1)
    secs = int((p - start).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    sec = int((punch_out - punch_in).total_seconds())
    return ((200 * sec + 3600) // 7200) / 100


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    p = punch_out.astimezone(IST).replace(microsecond=0)
    eh, em = map(int, shift_end.split(":"))
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    end0 = day.replace(hour=eh, minute=em)
    end1 = end0 + timedelta(days=1)
    end = min([end0, end1], key=lambda x: abs((p - x).total_seconds()))
    secs = int((p - end).total_seconds())
    return secs // 60 if secs >= 1800 else 0


def ist_date(ts: datetime, shift_start: str, shift_end: str) -> str:
    t = ts.astimezone(IST)
    end = datetime.strptime(shift_end, "%H:%M").time()
    if shift_end < shift_start and t.time() < end:
        return (t.date() - timedelta(days=1)).isoformat()
    return t.date().isoformat()


def epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: int | None = Field(default=None, ge=100000000000, le=4102444800000)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: int | None = Field(default=None, ge=100000000000, le=4102444800000)


class AttendancePatchIn(BaseModel):
    status: str | None = Field(default=None, pattern=r"^(PRESENT|WFH|ON_DUTY|ABSENT|LEAVE)$")
    punch_in: int | None = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: int | None = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/ping")
def ping():
    return {"status": "ok"}


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(department: str | None = None, page: int = 1, page_size: int = 20):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "invalid paging")
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    for it in items:
        if isinstance(it.get("created_at"), datetime):
            it["created_at"] = epoch_ms(it["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc).replace(microsecond=0)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    d = ist_date(ts, emp["shift_start"], emp["shift_end"])
    out = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(out)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    out["punch_in"] = epoch_ms(ts)
    return out


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "invalid paging")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from after date_to")
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
    items = []
    for d in (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    ):
        d["id"] = str(d.pop("_id"))
        d.setdefault("history", [])
        d.setdefault("half_day", False)
        for k in ("punch_in", "punch_out"):
            if d.get(k):
                d[k] = epoch_ms(d[k])
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


def serialize_attendance(d: dict) -> dict:
    d["id"] = str(d.pop("_id"))
    d.setdefault("history", [])
    d.setdefault("half_day", False)
    for k in ("punch_in", "punch_out"):
        if d.get(k):
            d[k] = epoch_ms(d[k])
    return d


def month_bounds(month: str) -> tuple[str, str]:
    return f"{month}-01", f"{month}-31"


def working_days_between(start: str, end: str) -> int:
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    if e < s:
        return 0
    days = 0
    cur = s
    while cur <= e:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


PRESENT_DAYS_EXPR = {
    "$sum": {
        "$cond": [
            {"$in": ["$status", list(PRESENCE)]},
            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
            0,
        ]
    }
}


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc).replace(microsecond=0)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    d = ist_date(ts, emp["shift_start"], emp["shift_end"])
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d})
    if not rec:
        raise HTTPException(404, "no attendance record for this date")
    if rec.get("punch_out"):
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"]:
        raise HTTPException(422, "punch_out must be after punch_in")
    wh = compute_work_hours(rec["punch_in"], ts)
    ot = compute_overtime(ts, emp["shift_end"], d)
    db.attendance_logs.update_one(
        {"_id": rec["_id"]},
        {"$set": {"punch_out": ts, "work_hours": wh, "overtime_minutes": ot, "half_day": wh < 4.5}},
    )
    out = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_attendance(out)


@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(emp_code: str, date: str, body: AttendancePatchIn):
    if not db.employees.find_one({"emp_code": emp_code}):
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "attendance record not found")
    emp = db.employees.find_one({"emp_code": emp_code})
    new_status = body.status if body.status is not None else rec["status"]
    new_in = rec.get("punch_in")
    new_out = rec.get("punch_out")
    if body.punch_in is not None:
        new_in = datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc).replace(microsecond=0)
    if body.punch_out is not None:
        new_out = datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc).replace(microsecond=0)
    absent = new_status in ("ABSENT", "LEAVE")
    if absent and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "absent/leave records cannot carry punches")
    if absent:
        new_in = None
        new_out = None
    else:
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if ist_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record's date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "span exceeds 24 hours")
    changed = {}
    for k, old, new in (("status", rec["status"], new_status), ("punch_in", rec.get("punch_in"), new_in), ("punch_out", rec.get("punch_out"), new_out)):
        if old != new:
            changed[k] = {"from": old, "to": new}
    if not changed:
        raise HTTPException(422, "no-op request")
    sets = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    if absent:
        sets.update({"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False})
    else:
        sets["late_minutes"] = compute_late_minutes(new_in, emp["shift_start"], emp["shift_end"])
        if new_out is not None:
            wh = compute_work_hours(new_in, new_out)
            sets.update({
                "work_hours": wh,
                "overtime_minutes": compute_overtime(new_out, emp["shift_end"], date),
                "half_day": wh < 4.5,
            })
        else:
            sets.update({"work_hours": None, "overtime_minutes": 0, "half_day": False})
    entry = {"at": epoch_ms(datetime.now(timezone.utc)), "by": body.regularized_by, "reason": body.reason, "changes": changed}
    old_history = rec.get("history", [])
    hist_filter = {"history": old_history} if "history" in rec else {"history": {"$exists": False}}
    res = db.attendance_logs.update_one({"_id": rec["_id"], **hist_filter}, {"$set": {**sets, "history": old_history + [entry]}})
    if res.modified_count == 0:
        raise HTTPException(409, "concurrent update detected, retry")
    out = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_attendance(out)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    import re as _re
    if not _re.match(MONTH_RE, month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    lo, hi = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": lo, "$lte": hi}}},
        {"$group": {
            "_id": None,
            "present_days": PRESENT_DAYS_EXPR,
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_ot": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    agg = list(db.attendance_logs.aggregate(pipeline))
    r = agg[0] if agg else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late": 0, "total_ot": 0}
    start = max(emp["joined_on"], lo)
    working = working_days_between(start, f"{month}-28") if month.endswith(("02",)) else working_days_between(start, f"{month}-31")
    pct = round(r["present_days"] / working * 100, 2) if working > 0 else None
    return {
        "emp_code": emp_code, "month": month,
        "working_days": working,
        "present_days": round(r["present_days"], 2),
        "leave_days": r["leave_days"],
        "late_count": r["late_count"],
        "total_late_minutes": r["total_late"],
        "total_overtime_minutes": r["total_ot"],
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    import re as _re
    if not _re.match(MONTH_RE, month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    lo, hi = month_bounds(month)
    match = {"date": {"$gte": lo, "$lte": hi}}
    pipeline = [
        {"$match": {"department": department} if department else {}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$ec"]}, {"$gte": ["$date", lo]}, {"$lte": ["$date", hi]}]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$ifNull": ["$logs._id", False]}, PRESENT_DAYS_EXPR["$sum"]["$cond"][0:1][0] if False else {"$cond": [{"$in": [{"$ifNull": ["$logs.status", ""]}, list(PRESENCE)]}, {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}, 0]}},
            "whs": {"$push": {"$cond": [
                {"$and": [{"$in": [{"$ifNull": ["$logs.status", ""]}, list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]},
                "$logs.work_hours", None,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "department": "$_id",
            "headcount": {"$size": "$emps"},
            "present_days": {"$round": ["$present_days", 2]},
            "avg_work_hours": {"$round": [{"$avg": "$whs"}, 2]},
            "late_count": 1,
            "total_late_minutes": "$total_late",
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]
    return list(db.employees.aggregate(pipeline))


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: str | None = None):
    import re as _re
    if not _re.match(MONTH_RE, month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    if limit < 1 or limit > 50:
        raise HTTPException(422, "limit must be 1-50")
    lo, hi = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": lo, "$lte": hi}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$group": {
            "_id": "$emp_code",
            "department": {"$first": "$emp.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
        }},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1, "_id": 1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "department": 1, "total_late_minutes": 1, "rank": 1}},
    ]
    return list(db.attendance_logs.aggregate(pipeline))


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
):
    import re as _re
    for v in (from_, to):
        if not v or not _re.match(DATE_RE, v):
            raise HTTPException(422, "from/to must be YYYY-MM-DD")
    d1 = datetime.strptime(from_, "%Y-%m-%d").date()
    d2 = datetime.strptime(to, "%Y-%m-%d").date()
    if d2 < d1:
        raise HTTPException(422, "to before from")
    if (d2 - d1).days > 92:
        raise HTTPException(422, "range over 92 days")
    codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
    if not codes:
        raise HTTPException(404, "unknown department")
    headcount = len(codes)
    start = datetime(d1.year, d1.month, d1.day)
    end_excl = datetime(d2.year, d2.month, d2.day) + timedelta(days=1)
    pipeline = [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_, "$lte": to}}},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", list(PRESENCE)]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                        0,
                    ]
                }
            },
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$addFields": {"ts": {"$toDate": "$_id"}}},
        {"$densify": {"field": "ts", "range": {"step": 1, "unit": "day", "bounds": [start, end_excl]}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$ts"}},
            "headcount": headcount,
        }},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "dow": {"$dayOfWeek": "$ts"},
        }},
        {"$set": {"is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]}}},
        {"$set": {
            "attendance_rate": {
                "$cond": [
                    {"$and": [{"$eq": ["$is_working_day", True]}, {"$gt": ["$headcount", 0]}]},
                    {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                    None,
                ]
            }
        }},
        {"$sort": {"date": 1}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$set": {"moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}},
        {"$project": {"_id": 0, "ts": 0, "dow": 0}},
    ]
    return list(db.attendance_logs.aggregate(pipeline))


EXPLAIN_ENDPOINTS = ("attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend")


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str,
    month: str | None = None,
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    limit: int = 10,
):
    if endpoint not in EXPLAIN_ENDPOINTS:
        raise HTTPException(404, "unknown endpoint")
    if endpoint == "attendance_list":
        if not date_from or not date_to:
            raise HTTPException(422, "date_from and date_to are required")
        cmd = {"explain": {
            "find": "attendance_logs",
            "filter": {"date": {"$gte": date_from, "$lte": date_to}},
            "sort": {"date": -1, "emp_code": 1},
            "limit": 100,
        }, "verbosity": "executionStats"}
        coll = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not month or not emp_code:
            raise HTTPException(422, "month and emp_code are required")
        lo, hi = month_bounds(month)
        cmd = {"explain": {
            "aggregate": "attendance_logs",
            "pipeline": [
                {"$match": {"emp_code": emp_code, "date": {"$gte": lo, "$lte": hi}}},
                {"$group": {"_id": None, "present_days": PRESENT_DAYS_EXPR}},
            ],
            "cursor": {},
        }, "verbosity": "executionStats"}
        coll = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        lo, hi = month_bounds(month)
        cmd = {"explain": {
            "aggregate": "employees",
            "pipeline": [
                {"$lookup": {
                    "from": "attendance_logs",
                    "let": {"ec": "$emp_code"},
                    "pipeline": [{"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$ec"]},
                        {"$gte": ["$date", lo]}, {"$lte": ["$date", hi]},
                    ]}}}],
                    "as": "logs",
                }},
                {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
                {"$group": {"_id": "$department", "emps": {"$addToSet": "$emp_code"}}},
            ],
            "cursor": {},
        }, "verbosity": "executionStats"}
        coll = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        lo, hi = month_bounds(month)
        cmd = {"explain": {
            "aggregate": "attendance_logs",
            "pipeline": [
                {"$match": {"date": {"$gte": lo, "$lte": hi}, "late_minutes": {"$gt": 0}}},
                {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}},
                {"$sort": {"total": -1, "_id": 1}},
                {"$setWindowFields": {"sortBy": {"total": -1, "_id": 1}, "output": {"rank": {"$rank": {}}}}},
            ],
            "cursor": {},
        }, "verbosity": "executionStats"}
        coll = "attendance_logs"
    else:
        if not from_ or not to:
            raise HTTPException(422, "from and to are required")
        d1 = datetime.strptime(from_, "%Y-%m-%d").date()
        d2 = datetime.strptime(to, "%Y-%m-%d").date()
        start = datetime(d1.year, d1.month, d1.day)
        end_excl = datetime(d2.year, d2.month, d2.day) + timedelta(days=1)
        cmd = {"explain": {
            "aggregate": "attendance_logs",
            "pipeline": [
                {"$match": {"date": {"$gte": from_, "$lte": to}}},
                {"$group": {"_id": "$date", "present_count": PRESENT_DAYS_EXPR}},
                {"$addFields": {"ts": {"$toDate": "$_id"}}},
                {"$densify": {"field": "ts", "range": {"step": 1, "unit": "day", "bounds": [start, end_excl]}}},
            ],
            "cursor": {},
        }, "verbosity": "executionStats"}
        coll = "attendance_logs"
    result = db.command(cmd)
    return {"endpoint": endpoint, "collection": coll, "explain": result}
