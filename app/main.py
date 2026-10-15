"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StringConstraints
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, OperationFailure

load_dotenv()  # never overrides variables that are already set in the environment

log = logging.getLogger("attendance")


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"environment variable {name} is required")
    return value


# tz_aware: by default PyMongo returns naive datetimes, which Python would treat as local time.
# A short server-selection timeout lets /health answer 503 quickly instead of hanging for 30 s.
client = MongoClient(_env("MONGO_URI"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[_env("MONGO_DB")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
ABSENT_STATUSES = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_HOURS = Decimal("4.50")  # R5

# Epoch milliseconds only. Anything below 1e11 (March 1973) is a seconds value sent by mistake.
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 9_999_999_999_999

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
MONTH_RE = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except ConnectionFailure:
        log.warning("MongoDB unreachable at startup; indexes will be created once it answers /health")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when an identical index already exists."""
    global _indexes_ready
    if _indexes_ready:
        return
    specs = [
        # Uniqueness is what makes concurrent creates and punch-ins safe.
        (db.employees, [("emp_code", 1)], {"unique": True, "name": "uniq_emp_code"}),
        (db.employees, [("department", 1), ("emp_code", 1)], {"name": "department_emp_code"}),
        (db.employees, [("joined_on", 1)], {"name": "joined_on"}),
        (db.attendance_logs, [("emp_code", 1), ("date", 1)], {"unique": True, "name": "uniq_emp_code_date"}),
        (db.attendance_logs, [("date", -1), ("emp_code", 1)], {"name": "date_emp_code"}),
        (db.attendance_logs, [("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp_code"}),
    ]
    for collection, keys, options in specs:
        try:
            collection.create_index(keys, **options)
        except OperationFailure as exc:  # e.g. duplicates in seeded data; serve anyway, but say so
            log.error("could not create index %s: %s", options["name"], exc)
    _indexes_ready = True


@app.exception_handler(ConnectionFailure)
async def database_unavailable(_: Request, exc: ConnectionFailure):
    log.error("MongoDB unavailable: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Helpers: time and business rules
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> epoch ms. Treats naive values as UTC, which is what MongoDB stores."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def round_half_up(value, places: int = 2) -> float:
    """R4/R8: decimal half-up rounding (Python's round() is banker's rounding on binary floats)."""
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def to_decimal(value) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value or 0))


def ratio(numerator, denominator, places: int) -> float:
    denominator = to_decimal(denominator)
    if not denominator:
        return 0.0
    return round_half_up(to_decimal(numerator) / denominator, places)


def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def shift_of(emp: dict) -> tuple[str, str]:
    return emp.get("shift_start") or "09:30", emp.get("shift_end") or "18:30"


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: the IST date of punch_in, except that on an overnight shift a punch-in
    before shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_bounds(day: date, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end instants of the shift that starts on `day` (IST); overnight shifts end the next day."""
    start = datetime.combine(day, parse_hhmm(shift_start), IST)
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    return start, datetime.combine(end_day, parse_hhmm(shift_end), IST)


def compute_late_minutes(punch_in: datetime, shift_start_at: datetime) -> int:
    """R2: late only when strictly more than 10:00 after shift start; minutes counted from shift start."""
    seconds = (punch_in - shift_start_at).total_seconds()
    return int(seconds // 60) if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: elapsed seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / 3600).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end_at: datetime) -> int:
    """R3: whole minutes after shift end, counted only if at least 30."""
    minutes = int((punch_out - shift_end_at).total_seconds() // 60)
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: decided on the rounded work_hours; false until punch-out."""
    return work_hours is not None and Decimal(str(work_hours)) < HALF_DAY_HOURS


# --------------------------------------------------------------------------- #
# Helpers: request parsing and response shaping
# --------------------------------------------------------------------------- #
def parse_day(value: str, name: str) -> date:
    if not DATE_RE.fullmatch(value or ""):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid date")


def parse_month(value: str) -> tuple[date, date]:
    match = MONTH_RE.fullmatch(value or "")
    if not match:
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = int(match[1]), int(match[2])
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def parse_status(value: Optional[str]) -> Optional[str]:
    if value is not None and value not in PRESENT_STATUSES + ABSENT_STATUSES:
        raise HTTPException(422, "status must be one of " + ", ".join(PRESENT_STATUSES + ABSENT_STATUSES))
    return value


def weekdays_between(start: date, end: date) -> list[str]:
    """R7: Monday-Friday dates in [start, end]; there is no holiday calendar."""
    days = (start + timedelta(days=i) for i in range((end - start).days + 1))
    return [d.isoformat() for d in days if d.weekday() < 5]


def instants_to_ms(value):
    """Convert every BSON datetime in a stored value to epoch ms and drop internal _id fields."""
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: instants_to_ms(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [instants_to_ms(v) for v in value]
    return value


def employee_out(doc: dict) -> dict:
    return instants_to_ms(doc)


def attendance_out(doc: dict) -> dict:
    out = instants_to_ms(doc)
    # Seeded records may omit these (DATA_MODEL): treat as empty / false.
    out["history"] = out.get("history") or []
    out["half_day"] = bool(out.get("half_day"))
    out.setdefault("punch_out", None)
    out.setdefault("work_hours", None)
    out["late_minutes"] = out.get("late_minutes") or 0
    out["overtime_minutes"] = out.get("overtime_minutes") or 0
    return out


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, f"employee {emp_code} not found")
    return emp


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def _real_date(value: str) -> str:
    date.fromisoformat(value)  # rejects 2026-02-30
    return value


Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)]
EmpCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[^\s/]{1,64}$")]
Email = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, max_length=254,
                                         pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
HHMM = Annotated[str, StringConstraints(strict=True, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
DateStr = Annotated[str, StringConstraints(strict=True, pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_real_date)]
# strict: floats, numeric strings and booleans are rejected with 422; the range rejects seconds.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]


class EmployeeIn(BaseModel):
    emp_code: EmpCode
    name: Text
    email: Email
    department: Text
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr


class PunchInIn(BaseModel):
    emp_code: EmpCode
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: EmpCode
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Text
    corrected_by: Text


# --------------------------------------------------------------------------- #
# Health and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()  # no-op once done; catches up if MongoDB was down at startup
    except ConnectionFailure:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides races, not a prior find_one
    except DuplicateKeyError:
        raise HTTPException(409, f"emp_code {body.emp_code} already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    shift_start, shift_end = shift_of(emp)
    at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(at, shift_start, shift_end)
    shift_start_at, _ = shift_bounds(day, shift_start, shift_end)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(at, shift_start_at),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, f"{body.emp_code} already has a record for {doc['date']}")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    shift_start, shift_end = shift_of(emp)
    at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(at, shift_start, shift_end)
    # A shift that runs past midnight is closed on the next calendar day, so the latest
    # record is either today's or yesterday's.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "date": {"$in": [day.isoformat(), (day - timedelta(days=1)).isoformat()]}},
        sort=[("date", -1)],
    )
    if rec is None:
        raise HTTPException(404, f"no punch-in found for {body.emp_code}")
    if rec.get("punch_in") is None:
        raise HTTPException(409, f"cannot punch out of a {rec['status']} record")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, f"{body.emp_code} already punched out for {rec['date']}")
    if at <= rec["punch_in"]:
        raise HTTPException(422, "punch-out must be after punch-in")

    _, shift_end_at = shift_bounds(date.fromisoformat(rec["date"]), shift_start, shift_end)
    work_hours = compute_work_hours(rec["punch_in"], at)
    updated = db.attendance_logs.find_one_and_update(
        # Conditional on the state we read: of two concurrent punch-outs only one matches.
        {"_id": rec["_id"], "punch_in": rec["punch_in"], "punch_out": None},
        {"$set": {
            "punch_out": at,
            "work_hours": work_hours,
            "overtime_minutes": compute_overtime(at, shift_end_at),
            "half_day": compute_half_day(work_hours),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, f"{body.emp_code} already punched out for {rec['date']}")
    return attendance_out(updated)


TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def corrected_values(rec: dict, body: RegularizeIn, emp: dict, day: date) -> dict:
    """Apply a correction to a stored record and recompute the derived fields (R1-R5)."""
    sent = body.model_fields_set
    if "status" in sent and body.status is None:
        raise HTTPException(422, "status cannot be null")
    status = body.status if "status" in sent else rec["status"]
    punch_in_at = rec.get("punch_in")
    punch_out_at = rec.get("punch_out")
    if "punch_in" in sent:
        punch_in_at = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    if "punch_out" in sent:
        punch_out_at = ms_to_dt(body.punch_out) if body.punch_out is not None else None

    shift_start, shift_end = shift_of(emp)
    if status in ABSENT_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"a {status} record has no punch times")
        punch_in_at = punch_out_at = None
    else:
        if punch_in_at is None:
            raise HTTPException(422, f"punch_in is required for status {status}")
        if "punch_in" in sent and attendance_date(punch_in_at, shift_start, shift_end) != day:
            raise HTTPException(422, f"punch_in does not fall on the shift of {day.isoformat()}")
        if punch_out_at is not None and punch_out_at <= punch_in_at:
            raise HTTPException(422, "punch_out must be after punch_in")

    shift_start_at, shift_end_at = shift_bounds(day, shift_start, shift_end)
    work_hours = compute_work_hours(punch_in_at, punch_out_at) if punch_out_at else None
    return {
        "status": status,
        "punch_in": punch_in_at,
        "punch_out": punch_out_at,
        "work_hours": work_hours,
        "late_minutes": compute_late_minutes(punch_in_at, shift_start_at) if punch_in_at else 0,
        "overtime_minutes": compute_overtime(punch_out_at, shift_end_at) if punch_out_at else 0,
        "half_day": compute_half_day(work_hours),
    }


@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code: str, day: str, body: RegularizeIn):
    the_day = parse_day(day, "date")
    emp = get_employee_or_404(emp_code)
    # Optimistic concurrency: the update only applies if the record is still exactly what we
    # read (same fields, same number of history entries). If another correction got in first,
    # re-read and recompute so neither correction's history entry is lost.
    for _ in range(10):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
        if rec is None:
            raise HTTPException(404, f"no attendance record for {emp_code} on {day}")
        new = corrected_values(rec, body, emp, the_day)
        old = {f: rec.get(f) for f in TRACKED_FIELDS}
        old["half_day"] = bool(old["half_day"])
        old["late_minutes"] = old["late_minutes"] or 0
        old["overtime_minutes"] = old["overtime_minutes"] or 0
        entry = {
            "at": now_utc(),
            "by": body.corrected_by,
            "reason": body.reason,
            "changes": {f: {"before": old[f], "after": new[f]} for f in TRACKED_FIELDS if old[f] != new[f]},
        }
        history = rec.get("history")
        update = {"$set": new}
        if isinstance(history, list):
            update["$push"] = {"history": entry}
        else:
            update["$set"] = {**new, "history": [entry]}
        guard = {
            "_id": rec["_id"],
            **{f: rec.get(f) for f in TRACKED_FIELDS},
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(history or [])]},
        }
        updated = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return attendance_out(updated)
    raise HTTPException(409, "the record is being changed concurrently; retry")


ATTENDANCE_SORT = {"date": -1, "emp_code": 1}


def attendance_list_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = parse_day(date_from, "date_from").isoformat()
        if date_to is not None:
            q["date"]["$lte"] = parse_day(date_to, "date_to").isoformat()
    if parse_status(status) is not None:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_list_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (db.attendance_logs.find(q).sort(list(ATTENDANCE_SORT.items()))
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [attendance_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def _field(prefix: str):
    return lambda name: f"{prefix}{name}"


def _is_weekday(f) -> dict:
    day = {"$dateFromString": {"dateString": f("date"), "format": "%Y-%m-%d"}}
    return {"$lte": [{"$isoDayOfWeek": day}, 5]}


def _present_value(f) -> dict:
    """R5/R6: a present status counts 1, or 0.5 on a half day; ABSENT and LEAVE count 0."""
    return {"$cond": [{"$in": [f("status"), list(PRESENT_STATUSES)]},
                      {"$cond": [{"$eq": [f("half_day"), True]}, 0.5, 1]}, 0]}


def _work_hours_decimal(f) -> dict:
    # Summed as Decimal128 so averages are exact before the half-up rounding (R8).
    return {"$cond": [{"$isNumber": f("work_hours")}, {"$toDecimal": f("work_hours")}, 0]}


def employee_monthly_pipeline(emp_code: str, start: date, end: date) -> list:
    f = _field("$")
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "records": {"$sum": 1},
            "present_days": {"$sum": {"$cond": [_is_weekday(f), _present_value(f), 0]}},  # R7
            "absent_days": {"$sum": {"$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "half_days": {"$sum": {"$cond": [{"$eq": ["$half_day", True]}, 1, 0]}},
            "late_days": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            "work_hours_total": {"$sum": _work_hours_decimal(f)},
            "work_hours_records": {"$sum": {"$cond": [{"$isNumber": "$work_hours"}, 1, 0]}},
        }},
    ]


def department_summary_pipeline(start: date, end: date) -> list:
    """Starts from employees so that people with no logs still count towards headcount (R9)."""
    l = _field("$$l.")
    return [
        {"$match": {"joined_on": {"$lte": end.isoformat()}}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                              "late_minutes": 1, "overtime_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "_id": 0,
            "department": 1,
            # R7: Mon-Fri from joined_on for mid-month joiners
            "working_days": {"$size": {"$filter": {
                "input": weekdays_between(start, end), "as": "d", "cond": {"$gte": ["$$d", "$joined_on"]}}}},
            "records": {"$size": "$logs"},
            "present_days": {"$sum": {"$map": {
                "input": "$logs", "as": "l", "in": {"$cond": [_is_weekday(l), _present_value(l), 0]}}}},
            "late_minutes": {"$sum": "$logs.late_minutes"},
            "overtime_minutes": {"$sum": "$logs.overtime_minutes"},
            "work_hours_total": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": _work_hours_decimal(l)}}},
            "work_hours_records": {"$size": {"$filter": {
                "input": "$logs", "as": "l", "cond": {"$isNumber": "$$l.work_hours"}}}},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "working_days": {"$sum": "$working_days"},
            "records": {"$sum": "$records"},
            "present_days": {"$sum": "$present_days"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            "work_hours_total": {"$sum": "$work_hours_total"},
            "work_hours_records": {"$sum": "$work_hours_records"},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(start: date, end: date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_days": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                     "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}], "as": "emp"}},
        {"$unwind": {"path": "$emp", "preserveNullAndEmptyArrays": True}},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank gives ties the same rank and skips the next one (1, 2, 2, 4); the limit is applied
        # only after ranking, and tied rows are ordered by emp_code.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$sort": {"rank": 1, "_id": 1}},
        {"$limit": limit},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_days": 1}},
    ]
    return pipeline


def department_trend_pipeline(department: str, start: date, end: date, window: int) -> list:
    f = _field("$")
    days = (end - start).days + 1
    start_at = datetime.combine(start, time(), timezone.utc)
    calendar_days = {"$map": {"input": {"$range": [0, days]}, "as": "i", "in": {"date": {"$dateToString": {
        "format": "%Y-%m-%d", "date": {"$dateAdd": {"startDate": start_at, "unit": "day", "amount": "$$i"}}}}}}}
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$unwind": "$logs"},
        {"$replaceWith": "$logs"},
        # One empty row per calendar day, so weekends and days without records still get a row.
        {"$unionWith": {"pipeline": [{"$documents": calendar_days}]}},
        {"$group": {
            "_id": "$date",
            "records": {"$sum": {"$cond": [{"$ifNull": ["$status", False]}, 1, 0]}},
            "present": {"$sum": _present_value(f)},
            "late": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        # Trailing window; the first rows simply have fewer documents to average.
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {
            "moving_avg": {"$avg": "$present", "window": {"documents": [1 - window, 0]}}}}},
        {"$sort": {"_id": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    emp = get_employee_or_404(emp_code)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, start, end)))
    r = rows[0] if rows else {}
    joined = date.fromisoformat(emp["joined_on"]) if emp.get("joined_on") else start
    working_days = len(weekdays_between(max(start, joined), end)) if joined <= end else 0
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(r.get("present_days", 0)),
        "absent_days": r.get("absent_days", 0),
        "leave_days": r.get("leave_days", 0),
        "half_days": r.get("half_days", 0),
        "late_days": r.get("late_days", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "total_work_hours": round_half_up(r.get("work_hours_total", 0)),
        "avg_work_hours": ratio(r.get("work_hours_total", 0), r.get("work_hours_records", 0), 2),
        "attendance_rate": ratio(r.get("present_days", 0), working_days, 4),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str):
    start, end = parse_month(month)
    rows = db.employees.aggregate(department_summary_pipeline(start, end))
    return {
        "month": month,
        "departments": [
            {
                "department": r["_id"],
                "headcount": r["headcount"],
                "present_days": round_half_up(r["present_days"]),
                "working_days": r["working_days"],
                "attendance_rate": ratio(r["present_days"], r["working_days"], 4),
                "total_late_minutes": r["total_late_minutes"],
                "total_overtime_minutes": r["total_overtime_minutes"],
                # over records, not an average of per-employee averages
                "avg_work_hours": ratio(r["work_hours_total"], r["work_hours_records"], 2),
            }
            for r in rows
        ],
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=100), department: Optional[str] = None):
    start, end = parse_month(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(start, end, limit, department)))
    return {"month": month, "items": items}


MAX_TREND_DAYS = 366


def trend_range(date_from: str, date_to: str) -> tuple[date, date]:
    start, end = parse_day(date_from, "date_from"), parse_day(date_to, "date_to")
    if end < start:
        raise HTTPException(422, "date_to must not be before date_from")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"the range can cover at most {MAX_TREND_DAYS} days")
    return start, end


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str, date_to: str, window: int = Query(7, ge=1, le=90)):
    start, end = trend_range(date_from, date_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, f"department {department} not found")
    rows = db.employees.aggregate(department_trend_pipeline(department, start, end, window))
    return {
        "department": department,
        "date_from": date_from,
        "date_to": date_to,
        "window": window,
        "items": [
            {"date": r["_id"], "records": r["records"], "present": round_half_up(r["present"]),
             "late": r["late"], "moving_avg": round_half_up(r["moving_avg"])}
            for r in rows
        ],
    }


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def _required(value: Optional[str], name: str) -> str:
    if value is None:
        raise HTTPException(422, f"{name} is required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    month: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
    department: Optional[str] = None,
    window: int = Query(7, ge=1, le=90),
):
    """executionStats explain of the endpoint's main query, built exactly as the endpoint builds it."""
    if endpoint == "attendance_list":
        command = {
            "find": "attendance_logs",
            "filter": attendance_list_filter(emp_code, date_from, date_to, status),
            "sort": ATTENDANCE_SORT,
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        start, end = parse_month(_required(month, "month"))
        command = {"aggregate": "attendance_logs", "cursor": {},
                   "pipeline": employee_monthly_pipeline(_required(emp_code, "emp_code"), start, end)}
    elif endpoint == "department_summary":
        start, end = parse_month(_required(month, "month"))
        command = {"aggregate": "employees", "cursor": {}, "pipeline": department_summary_pipeline(start, end)}
    elif endpoint == "late_leaderboard":
        start, end = parse_month(_required(month, "month"))
        command = {"aggregate": "attendance_logs", "cursor": {},
                   "pipeline": late_leaderboard_pipeline(start, end, limit, department)}
    elif endpoint == "department_trend":
        start, end = trend_range(_required(date_from, "date_from"), _required(date_to, "date_to"))
        command = {"aggregate": "employees", "cursor": {},
                   "pipeline": department_trend_pipeline(_required(department, "department"), start, end, window)}
    else:
        raise HTTPException(404, f"unknown endpoint {endpoint}")
    result = db.command("explain", command, verbosity="executionStats")
    return JSONResponse(content=json.loads(json_util.dumps(result)))
