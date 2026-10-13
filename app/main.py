"""Employee Attendance & Analytics API  (FastAPI + MongoDB).  Everything lives in this one file.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (real environment variables win over a local .env)

Layout of this file
  1. config / database / indexes
  2. time + business-rule helpers (R1-R5)
  3. request models and serialisation
  4. endpoints: health, employees, attendance
  5. analytics pipelines (builders are shared with the explain endpoint)
  6. analytics endpoints + /admin/explain
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, OperationFailure

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------------------
# 1. Config, database, indexes
# --------------------------------------------------------------------------------------
load_dotenv()  # override=False: real environment variables take priority over .env

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes, never naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

# Index names are fixed so queries can hint them (guarantees an IXSCAN, never a COLLSCAN).
IX_EMP_CODE = "emp_code_1"                          # employees: unique business key, list sort, $lookup target
IX_EMP_DEPT_JOINED = "department_1_joined_on_1"     # employees: department filter + headcount (R9)
IX_EMP_JOINED = "joined_on_1"                       # employees: headcount without a department filter
IX_LOG_EMP_DATE = "emp_code_1_date_1"               # logs: unique (emp_code, date); per-employee ranges
IX_LOG_DATE_EMP = "date_-1_emp_code_1"              # logs: date ranges + the list sort order
IX_LOG_STATUS_DATE = "status_1_date_-1_emp_code_1"  # logs: status filter + list sort order
IX_LOG_EMP_PUNCH = "emp_code_1_punch_in_-1"         # logs: "latest record at or before punched_at"

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the index already exists."""
    global _indexes_ready
    employees.create_index([("emp_code", 1)], unique=True, name=IX_EMP_CODE)
    employees.create_index([("department", 1), ("joined_on", 1)], name=IX_EMP_DEPT_JOINED)
    employees.create_index([("joined_on", 1)], name=IX_EMP_JOINED)
    logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name=IX_LOG_EMP_DATE)
    logs.create_index([("date", -1), ("emp_code", 1)], name=IX_LOG_DATE_EMP)
    logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name=IX_LOG_STATUS_DATE)
    logs.create_index([("emp_code", 1), ("punch_in", -1)], name=IX_LOG_EMP_PUNCH)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # keep the process up; /health will report 503 until Mongo answers
        log.warning("index creation deferred: %s", exc)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
async def _mongo_down(_: Request, exc: ConnectionFailure):
    return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})


# --------------------------------------------------------------------------------------
# 2. Time and business-rule helpers
# --------------------------------------------------------------------------------------
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
DAY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]

# Strict int: JSON floats (even 1.0), strings and bools are rejected -> 422.
EpochMs = Annotated[int, Field(strict=True, ge=MS_MIN, le=MS_MAX)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_dt() -> datetime:
    return datetime.fromtimestamp(int(time.time()), tz=UTC)


def secs(delta: timedelta) -> int:
    return delta.days * 86400 + delta.seconds


def hhmm_minutes(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def ist_at(day: str, hhmm: str, plus_days: int = 0) -> datetime:
    d = date_cls.fromisoformat(day) + timedelta(days=plus_days)
    h, m = hhmm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=IST)


def is_overnight(emp: dict) -> bool:
    return hhmm_minutes(emp["shift_end"]) <= hhmm_minutes(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; an overnight-shift punch-in before shift_end belongs to the previous day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(emp):
        time_of_day = ist.hour * 3600 + ist.minute * 60 + ist.second
        if time_of_day < hhmm_minutes(emp["shift_end"]) * 60:
            day -= timedelta(days=1)
    return day.isoformat()


def calc_late(punch_in: datetime, day: str, emp: dict) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then floor(minutes since shift_start)."""
    diff = secs(punch_in - ist_at(day, emp["shift_start"]))
    return diff // 60 if diff > GRACE_SECONDS else 0


def calc_close(punch_in: datetime, punch_out: datetime, day: str, emp: dict):
    """R3-R5 -> (work_hours, overtime_minutes, half_day)."""
    total = secs(punch_out - punch_in)
    hours = (Decimal(total) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # R4 half-up
    shift_end = ist_at(day, emp["shift_end"], 1 if is_overnight(emp) else 0)
    over = secs(punch_out - shift_end)
    overtime = over // 60 if over >= MIN_OVERTIME_MINUTES * 60 else 0  # R3
    return float(hours), overtime, hours < Decimal("4.50")  # R5 uses the rounded value


def derived_for(status: str, punch_in, punch_out, day: str, emp: dict):
    """(work_hours, late_minutes, overtime_minutes, half_day) for a final state of a record."""
    if status not in PRESENCE or punch_in is None:
        return None, 0, 0, False
    late = calc_late(punch_in, day, emp)
    if punch_out is None:
        return None, late, 0, False
    hours, overtime, half = calc_close(punch_in, punch_out, day, emp)
    return hours, late, overtime, half


def half_up(value: Any, places: int) -> Decimal:
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def bad_request(loc: list, msg: str, status: int = 422):
    return HTTPException(status_code=status, detail=[{"loc": loc, "msg": msg, "type": "value_error"}])


def parse_day(value: str, loc: list) -> date_cls:
    if not DAY_RE.match(value):
        raise bad_request(loc, "date must be YYYY-MM-DD")
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise bad_request(loc, "not a valid calendar date")


def parse_month(value: str):
    """'YYYY-MM' -> (first_day, last_day)."""
    try:
        year, month = int(value[:4]), int(value[5:7])
        first = date_cls(year, month, 1)
        return first, date_cls(year, month, calendar.monthrange(year, month)[1])
    except (ValueError, IndexError):
        raise bad_request(["query", "month"], "month must be YYYY-MM")


def weekdays_between(a: date_cls, b: date_cls) -> int:
    return sum(1 for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5)


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


# --------------------------------------------------------------------------------------
# 3. Request models and serialisation
# --------------------------------------------------------------------------------------
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_day(cls, v: str) -> str:
        if not DAY_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date_cls.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"], "created_at": dt_to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for field, ch in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):  # BSON dates nested inside changes -> epoch ms
            ch = {"from": dt_to_ms(ch.get("from")), "to": dt_to_ms(ch.get("to"))}
        changes[field] = ch
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Stored document -> API shape. Missing legacy fields default (history [], half_day false, ...)."""
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")), "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------------------
# 4. Endpoints: health, employees, attendance
# --------------------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.fromtimestamp(int(time.time() * 1000) / 1000, tz=UTC)
    try:
        employees.insert_one(doc)  # uniqueness is enforced by the unique emp_code index (race-safe)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    flt = {"department": department} if department is not None else {}
    hint = IX_EMP_DEPT_JOINED if department is not None else IX_EMP_CODE
    total = employees.count_documents(flt, hint=hint) if flt else employees.estimated_document_count()
    cur = employees.find(flt, {"_id": 0}).sort("emp_code", 1)
    if department is not None:
        cur = cur.hint(IX_EMP_DEPT_JOINED)
    cur = cur.skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee(body.emp_code)
    at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    day = attendance_date(at, emp)
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": at, "punch_out": None, "work_hours": None,
        "late_minutes": calc_late(at, day, emp), "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        logs.insert_one(doc)  # the unique (emp_code, date) index decides the race: one 201, the rest 409
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee(body.emp_code)
    at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    # Most recent record whose punch_in <= punched_at (works across midnight: no date arithmetic needed).
    doc = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": at}},
        sort=[("punch_in", -1)], hint=IX_LOG_EMP_PUNCH,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")
    if at <= doc["punch_in"]:
        raise bad_request(["body", "punched_at"], "punched_at must be after punch_in")
    if at - doc["punch_in"] > timedelta(hours=24):
        raise bad_request(["body", "punched_at"], "punch_out is more than 24 hours after punch_in")
    hours, overtime, half = calc_close(doc["punch_in"], at, doc["date"], emp)
    # Compare-and-set on punch_out == null: of two simultaneous punch-outs exactly one matches.
    res = logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None, "punch_in": doc["punch_in"]},
        {"$set": {"punch_out": at, "work_hours": hours, "overtime_minutes": overtime, "half_day": half}},
        return_document=ReturnDocument.AFTER,
    )
    if res is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return record_out(res)


def attendance_query(emp_code, date_from, date_to, status, page, page_size):
    """Single source of truth for the list query; the explain endpoint reuses it. -> (filter, sort, skip, limit, hint)."""
    flt: dict = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    rng: dict = {}
    if date_from is not None:
        rng["$gte"] = date_from
    if date_to is not None:
        rng["$lte"] = date_to
    if rng:
        flt["date"] = rng
    if status is not None:
        flt["status"] = status
    hint = IX_LOG_EMP_DATE if emp_code is not None else IX_LOG_STATUS_DATE if status is not None else IX_LOG_DATE_EMP
    return flt, [("date", -1), ("emp_code", 1)], (page - 1) * page_size, page_size, hint


def checked_range(date_from: Optional[str], date_to: Optional[str], names=("date_from", "date_to")):
    a = parse_day(date_from, ["query", names[0]]) if date_from is not None else None
    b = parse_day(date_to, ["query", names[1]]) if date_to is not None else None
    if a and b and a > b:
        raise bad_request(["query", names[0]], f"{names[0]} must not be after {names[1]}")
    return a, b


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    checked_range(date_from, date_to)
    flt, sort, skip, limit, hint = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(flt, hint=hint) if flt else logs.estimated_document_count()
    items = logs.find(flt, {"_id": 0}).sort(sort).hint(hint).skip(skip).limit(limit)
    return {"items": [record_out(d) for d in items], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    day = parse_day(date, ["path", "date"]).isoformat()
    emp = get_employee(emp_code)

    # Optimistic concurrency: the write only lands if the record is exactly what we read (same status, punch
    # times and history length). If another edit/punch-out slipped in, re-read and recompute instead of
    # overwriting it; history entries are therefore never lost.
    for _ in range(10):
        doc = logs.find_one({"emp_code": emp_code, "date": day}, hint=IX_LOG_EMP_DATE)
        if doc is None:
            raise HTTPException(status_code=404, detail="no attendance record for that date")

        old_status, old_in, old_out = doc["status"], doc.get("punch_in"), doc.get("punch_out")
        new_status = body.status or old_status
        supplied_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
        supplied_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None

        if new_status in ("ABSENT", "LEAVE"):
            if supplied_in is not None or supplied_out is not None:
                raise bad_request(["body"], "punch times cannot be supplied with status ABSENT or LEAVE")
            new_in = new_out = None  # these statuses clear both punch times
        else:
            new_in = supplied_in if supplied_in is not None else old_in
            new_out = supplied_out if supplied_out is not None else old_out
            if new_in is None:
                raise bad_request(["body", "punch_in"], "a presence status requires a punch_in")
            if supplied_in is not None and attendance_date(new_in, emp) != day:
                raise bad_request(["body", "punch_in"], "punch_in must stay on the record's attendance date")
            if (supplied_in or supplied_out) and new_out is not None:
                if new_out <= new_in:
                    raise bad_request(["body", "punch_out"], "punch_out must be after punch_in")
                if new_out - new_in > timedelta(hours=24):
                    raise bad_request(["body", "punch_out"], "punch_out must be within 24 hours of punch_in")

        if new_status == old_status and new_in == old_in and new_out == old_out:
            raise bad_request(["body"], "request changes nothing")

        work_hours, late, overtime, half = derived_for(new_status, new_in, new_out, day, emp)
        new_vals = {
            "status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": work_hours,
            "late_minutes": late, "overtime_minutes": overtime, "half_day": half,
        }
        old_vals = {
            "status": old_status, "punch_in": old_in, "punch_out": old_out, "work_hours": doc.get("work_hours"),
            "late_minutes": doc.get("late_minutes") or 0, "overtime_minutes": doc.get("overtime_minutes") or 0,
            "half_day": bool(doc.get("half_day", False)),
        }
        changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if old_vals[k] != v}
        entry = {
            "at": datetime.fromtimestamp(int(time.time() * 1000) / 1000, tz=UTC),
            "by": body.regularized_by, "reason": body.reason, "changes": changes,
        }
        history_len = len(doc.get("history") or [])
        res = logs.find_one_and_update(
            {
                "_id": doc["_id"], "status": old_status, "punch_in": old_in, "punch_out": old_out,
                "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
            },
            {"$set": new_vals, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if res is not None:
            return record_out(res)
    raise HTTPException(status_code=409, detail="concurrent modification, please retry")


# --------------------------------------------------------------------------------------
# 5. Analytics: pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------------------
def dec_half_up(expr: Any, places: int) -> dict:
    """Half-up rounding in the database. $round is banker's rounding, so do floor(x*10^p + 0.5)/10^p in Decimal128."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, Decimal128("0.5")]}}, scale]}


def f_is_present(p: str) -> dict:
    return {"$in": [p + "status", PRESENCE]}


def f_weight(p: str) -> dict:  # a half day counts 0.5 (stored value), anything else 1; missing half_day = false
    return {"$cond": [{"$eq": [{"$ifNull": [p + "half_day", False]}, True]}, 0.5, 1]}


def f_is_weekday(p: str) -> dict:  # date strings are IST calendar days; parse as plain dates and take Mon-Fri
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": p + "date", "format": "%Y-%m-%d"}}}, 5]}


def f_present_days(p: str, weekdays_only: bool) -> dict:
    cond = [f_is_present(p)] + ([f_is_weekday(p)] if weekdays_only else [])
    return {"$cond": [{"$and": cond}, f_weight(p), 0]}


def f_is_late(p: str) -> dict:
    return {"$gt": [{"$ifNull": [p + "late_minutes", 0]}, 0]}


def one_if(cond: dict) -> dict:
    return {"$cond": [cond, 1, 0]}


def build_monthly(emp_code: str, month: str):
    first, last = parse_month(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": f_present_days("$", True)},          # R5-R7: half = 0.5, Mon-Fri only
            "leave_days": {"$sum": one_if({"$eq": ["$status", "LEAVE"]})},
            "late_count": {"$sum": one_if(f_is_late("$"))},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},       # stored values, not recomputed
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return "attendance_logs", pipeline, IX_LOG_EMP_DATE


def build_summary(month: str, department: Optional[str]):
    first, last = parse_month(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9: joined on/before the last day of the month
    if department is not None:
        match["department"] = department
    p = "$logs."
    counted_hours = {"$and": [f_is_present(p), {"$isNumber": p + "work_hours"}]}
    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {"emp_code": 1, "department": 1, "logs": 1}},
        # preserveNullAndEmptyArrays keeps employees with zero logs, so they still count in headcount
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": f_present_days(p, True)},
            "wh_sum": {"$sum": {"$cond": [counted_hours, {"$toDecimal": p + "work_hours"}, 0]}},
            "wh_n": {"$sum": one_if(counted_hours)},                    # mean over RECORDS, not per-employee means
            "late_count": {"$sum": one_if(f_is_late(p))},
            "total_late_minutes": {"$sum": {"$ifNull": [p + "late_minutes", 0]}},
            "leave_count": {"$sum": one_if({"$eq": [p + "status", "LEAVE"]})},
            "on_duty_count": {"$sum": one_if({"$eq": [p + "status", "ON_DUTY"]})},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": {"$size": "$emps"}, "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$wh_n", 0]}, {"$toDouble": dec_half_up({"$divide": ["$wh_sum", "$wh_n"]}, 2)}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]
    return "employees", pipeline, IX_EMP_DEPT_JOINED if department is not None else IX_EMP_JOINED


def build_leaderboard(month: str, limit: int, department: Optional[str]):
    first, last = parse_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
        # $rank = standard competition ranking (1,2,2,4): ties share a rank, the next rank is skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff are all kept
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
    ]
    return "attendance_logs", pipeline, IX_LOG_DATE_EMP


def build_trend(department: str, d_from: date_cls, d_to: date_cls):
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC)
    f_s, t_s = d_from.isoformat(), d_to.isoformat()
    p = "$logs."
    as_day = lambda path: {"$dateFromString": {"dateString": path, "format": "%Y-%m-%d"}}
    pipeline = [
        {"$match": {"department": department, "joined_on": {"$lte": t_s}}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f_s, "$lte": t_s}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {"joined_on": 1, "logs": 1}},
        # Two small per-day tallies from one scan of the department: records per day, and joiners per day
        # (joiners before `from` are clamped to `from`, so a running sum later gives headcount per R9).
        {"$facet": {
            "logs": [
                {"$unwind": "$logs"},
                {"$group": {"_id": "$logs.date",
                            "present_count": {"$sum": f_present_days(p, False)},
                            "late_count": {"$sum": one_if(f_is_late(p))}}},
            ],
            "joined": [
                {"$group": {"_id": {"$cond": [{"$lt": ["$joined_on", f_s]}, f_s, "$joined_on"]},
                            "joined": {"$sum": 1}}},
            ],
        }},
        {"$project": {"rows": {"$concatArrays": [
            {"$map": {"input": "$logs", "as": "x", "in": {
                "day": as_day("$$x._id"), "present_count": "$$x.present_count", "late_count": "$$x.late_count"}}},
            {"$map": {"input": "$joined", "as": "x", "in": {"day": as_day("$$x._id"), "joined": "$$x.joined"}}},
            [{"day": lo}, {"day": hi}],  # anchors so gap filling always spans the full range
        ]}}},
        {"$unwind": "$rows"},
        {"$replaceWith": "$rows"},
        {"$group": {"_id": "$day", "present_count": {"$sum": "$present_count"},
                    "late_count": {"$sum": "$late_count"}, "joined": {"$sum": "$joined"}}},
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1, "joined": 1}},
        {"$sort": {"day": 1}},
        # one row per calendar day, weekends and empty days included
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi + timedelta(days=1)]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "working": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {"rate_d": {"$cond": [
            {"$and": ["$working", {"$gt": ["$headcount", 0]}]},
            dec_half_up({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}, 4),
            None]}}},
        # window = current row + up to 6 previous rows INSIDE the range; $avg skips nulls, all-null -> null
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "ma_d": {"$avg": "$rate_d", "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": "$working", "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$toDouble": "$rate_d"},
            "moving_avg_7d": {"$cond": [{"$eq": [{"$ifNull": ["$ma_d", None]}, None]}, None,
                                        {"$toDouble": dec_half_up("$ma_d", 4)}]},
        }},
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline, IX_EMP_DEPT_JOINED


def run(builder_result):
    coll, pipeline, hint = builder_result
    return list(db[coll].aggregate(pipeline, hint=hint))


# --------------------------------------------------------------------------------------
# 6. Analytics endpoints and explain
# --------------------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    first, last = parse_month(month)
    emp = get_employee(emp_code)
    rows = run(build_monthly(emp_code, month))
    agg = rows[0] if rows else {}
    start = max(first, date_cls.fromisoformat(emp["joined_on"]))  # R7: count from joined_on for mid-month joiners
    working = weekdays_between(start, last)
    present = float(agg.get("present_days", 0))
    pct = float(half_up(Decimal(str(present)) * 100 / working, 2)) if working > 0 else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working, "present_days": present,
        "leave_days": agg.get("leave_days", 0), "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    return {"month": month, "items": run(build_summary(month, department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(pattern=MONTH_RE), limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    return {"month": month, "items": run(build_leaderboard(month, limit, department))}


def checked_trend_range(from_: str, to: str):
    a = parse_day(from_, ["query", "from"])
    b = parse_day(to, ["query", "to"])
    if b < a:
        raise bad_request(["query", "to"], "to must not be before from")
    if (b - a).days + 1 > 92:
        raise bad_request(["query", "to"], "range must not exceed 92 days")
    return a, b


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()):
    a, b = checked_trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}, hint=IX_EMP_DEPT_JOINED) is None:
        raise HTTPException(status_code=404, detail="unknown department")
    return {"department": department, "items": run(build_trend(department, a, b))}


def jsonable(doc: Any) -> Any:
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


def require(**params) -> None:
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(status_code=422, detail=[
            {"loc": ["query", k], "msg": "Field required", "type": "missing"} for k in missing])


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1, le=1_000_000),
    page_size: int = Query(20, ge=1, le=100),
):
    """executionStats explain of the endpoint's main query, built by the same builders the endpoint uses."""
    if endpoint == "attendance_list":
        checked_range(date_from, date_to)
        flt, sort, skip, lim, hint = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        inner = {"find": coll, "filter": flt, "sort": dict(sort), "skip": skip, "limit": lim, "hint": hint}
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            built = build_monthly(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            built = build_summary(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            built = build_leaderboard(month, limit, department)
        else:
            require(department=department, **{"from": from_, "to": to})
            a, b = checked_trend_range(from_, to)
            built = build_trend(department, a, b)
        coll, pipeline, hint = built
        inner = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "hint": hint}
    out = db.command({"explain": inner, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": jsonable(out)}
