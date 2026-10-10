"""
HROne Employee Attendance & Analytics API
Run: python -m uvicorn app.main:app --port 8000 --reload
"""
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure
from bson import json_util

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENT_STATUSES | {"ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def startup():
    # Idempotent indexes required by the contract.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True, name="uq_attendance_employee_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_employee_punchin"
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("late_minutes", DESCENDING)],
        name="ix_attendance_date_status_late"
    )


# ------------------------------- validation --------------------------------
def validate_epoch_ms(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be an integer epoch-millisecond timestamp")
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise ValueError("timestamp is outside the allowed epoch-millisecond range")
    return value


def ms_to_dt(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    validate_epoch_ms(value)
    # Truncate to whole seconds before storage/calculation.
    return datetime.fromtimestamp(value // 1000, tz=UTC).replace(tzinfo=None)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def as_utc_aware(value: datetime) -> datetime:
    # PyMongo returns naive UTC datetimes by default.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def rounded(value: float, places: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1." + "0" * places), rounding=ROUND_HALF_UP))


def valid_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Date must be YYYY-MM-DD")


def valid_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def month_working_days(joined_on: str, month_start: date, month_end: date) -> int:
    start = max(month_start, valid_date(joined_on))
    if start > month_end:
        return 0
    return sum(1 for n in range((month_end - start).days + 1)
               if (start + timedelta(days=n)).weekday() < 5)


def ist_dt(value: datetime) -> datetime:
    return as_utc_aware(value).astimezone(IST)


def shift_start_dt(attendance_date: str, shift_start: str) -> datetime:
    d = valid_date(attendance_date)
    h, m = map(int, shift_start.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=IST)


def shift_end_dt(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    d = valid_date(attendance_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start_minutes, end_minutes = sh * 60 + sm, eh * 60 + em
    end_day = d + timedelta(days=1) if end_minutes <= start_minutes else d
    return datetime.combine(end_day, time(eh, em), tzinfo=IST)


def attendance_date_for(punch_in: datetime, employee: dict) -> str:
    local = ist_dt(punch_in)
    start = employee.get("shift_start", "09:30")
    end = employee.get("shift_end", "18:30")
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    if (eh * 60 + em) <= (sh * 60 + sm) and (local.hour * 60 + local.minute) < (eh * 60 + em):
        local = local - timedelta(days=1)
    return local.date().isoformat()


def compute_late_minutes(punch_in: Optional[datetime], shift_start: str, attendance_date: str) -> int:
    if punch_in is None:
        return 0
    actual = ist_dt(punch_in).replace(microsecond=0)
    start = shift_start_dt(attendance_date, shift_start)
    seconds = int((actual - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = int((as_utc_aware(punch_out) - as_utc_aware(punch_in)).total_seconds())
    return rounded(seconds / 3600, 2)


def compute_overtime(punch_out: Optional[datetime], shift_start: str, shift_end: str, attendance_date: str) -> int:
    if punch_out is None:
        return 0
    end = shift_end_dt(attendance_date, shift_start, shift_end).astimezone(UTC)
    seconds = int((as_utc_aware(punch_out) - end).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0


def derive_fields(doc: dict, employee: dict) -> dict:
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    status = doc.get("status")
    if status not in PRESENT_STATUSES:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(pin, employee["shift_start"], doc["date"])
    hours = compute_work_hours(pin, pout)
    overtime = compute_overtime(pout, employee["shift_start"], employee["shift_end"], doc["date"])
    return {
        "late_minutes": late,
        "work_hours": hours,
        "overtime_minutes": overtime,
        "half_day": bool(hours is not None and hours < 4.50),
    }


def serialize_doc(doc: dict, attendance: bool = False) -> dict:
    result = dict(doc)
    result.pop("_id", None)
    if attendance:
        result.pop("id", None)
        result["punch_in"] = dt_to_ms(result.get("punch_in"))
        result["punch_out"] = dt_to_ms(result.get("punch_out"))
        result["history"] = result.get("history") or []
        result["half_day"] = result.get("half_day", False)
        result["late_minutes"] = result.get("late_minutes", 0)
        result["overtime_minutes"] = result.get("overtime_minutes", 0)
        for entry in result["history"]:
            entry["at"] = dt_to_ms(entry.get("at"))
            for field_change in entry.get("changes", {}).values():
                for k in ("from", "to"):
                    if isinstance(field_change.get(k), datetime):
                        field_change[k] = dt_to_ms(field_change[k])
    elif isinstance(result.get("created_at"), datetime):
        result["created_at"] = dt_to_ms(result["created_at"])
    return result


def clean_employee(doc: dict) -> dict:
    return serialize_doc(doc, attendance=False)


def clean_attendance(doc: dict) -> dict:
    return serialize_doc(doc, attendance=True)


def parse_month_query(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    return month


# ---------------------------------- models ----------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value):
        date.fromisoformat(value)
        if len(value) != 10:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        return validate_epoch_ms(value)


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        return validate_epoch_ms(value)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_timestamp(cls, value):
        return validate_epoch_ms(value)


# ---------------------------------- endpoints -------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return clean_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    docs = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [clean_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    ts = ts.replace(microsecond=0)
    day = attendance_date_for(ts, emp)
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return clean_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
    ts = ts.replace(microsecond=0)
    # Find latest open punch-in not later than the requested punch-out, including overnight shifts.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "status": {"$in": list(PRESENT_STATUSES)},
         "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)]
    )
    if not record:
        any_record = db.attendance_logs.find_one({"emp_code": body.emp_code})
        if not any_record:
            raise HTTPException(status_code=404, detail="no punch-in found")
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")
    elapsed = (ts - record["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch-out must be after punch-in and within 24 hours")
    updated = dict(record)
    updated["punch_out"] = ts
    updated.update(derive_fields(updated, emp))
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": updated["work_hours"],
                  "overtime_minutes": updated["overtime_minutes"], "half_day": updated["half_day"]}}
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="record already punched out")
    updated.pop("_id", None)
    return clean_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        valid_date(date_from)
    if date_to:
        valid_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    q: dict[str, Any] = {}
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
    docs = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [clean_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    valid_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="attendance record not found")

    request_data = body.model_dump(exclude_unset=True)
    reason = request_data.pop("reason")
    by = request_data.pop("regularized_by")
    new_status = request_data.get("status", record.get("status"))
    if new_status in {"ABSENT", "LEAVE"} and ("punch_in" in request_data or "punch_out" in request_data):
        raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot include punch times")
    pin = ms_to_dt(request_data["punch_in"]) if "punch_in" in request_data else record.get("punch_in")
    pout = ms_to_dt(request_data["punch_out"]) if "punch_out" in request_data else record.get("punch_out")
    if new_status in {"ABSENT", "LEAVE"}:
        pin = pout = None
    elif pin is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")
    if pin is not None and attendance_date_for(pin, emp) != date:
        raise HTTPException(status_code=422, detail="punch_in must belong to the attendance date")
    if pout is not None:
        if pin is None or pout <= pin or (pout - pin).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    candidate = dict(record)
    candidate.update({"status": new_status, "punch_in": pin, "punch_out": pout})
    candidate.update(derive_fields(candidate, emp))
    tracked = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for field in tracked:
        old = record.get(field)
        new = candidate.get(field)
        if field == "half_day":
            old = bool(old or False)
            new = bool(new or False)
        if old != new:
            changes[field] = {"from": old, "to": new}
    if not changes:
        raise HTTPException(status_code=422, detail="request does not change anything")
    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None),
        "by": by, "reason": reason, "changes": changes,
    }
    # Optimistic concurrency guard: only update if the prior history array still matches.
    old_history = record.get("history", [])
    update_filter = {"_id": record["_id"], "history": old_history}
    update_doc = {
        "status": candidate["status"], "punch_in": candidate["punch_in"], "punch_out": candidate["punch_out"],
        "work_hours": candidate["work_hours"], "late_minutes": candidate["late_minutes"],
        "overtime_minutes": candidate["overtime_minutes"], "half_day": candidate["half_day"],
    }
    result = db.attendance_logs.update_one(update_filter, {"$set": update_doc, "$push": {"history": history_entry}})
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="record changed concurrently; retry")
    candidate["history"] = old_history + [history_entry]
    candidate.pop("_id", None)
    return clean_attendance(candidate)


# ------------------------------- analytics ----------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    first, last = valid_month(month)
    docs = list(db.attendance_logs.aggregate([
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$project": {"date": 1, "status": 1, "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                      "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                      "half_day": {"$ifNull": ["$half_day", False]}}}
    ]))
    present_days = 0.0
    leave_days = late_count = total_late = total_ot = 0
    for d in docs:
        day = valid_date(d["date"])
        if d["status"] == "LEAVE":
            leave_days += 1
        if d["late_minutes"] > 0:
            late_count += 1
            total_late += d["late_minutes"]
        total_ot += d["overtime_minutes"]
        if day.weekday() < 5 and d["status"] in PRESENT_STATUSES:
            present_days += 0.5 if d["half_day"] else 1.0
    working_days = month_working_days(emp["joined_on"], first, last)
    attendance_pct = rounded(present_days / working_days * 100, 2) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days,
            "present_days": rounded(present_days, 2), "leave_days": leave_days, "late_count": late_count,
            "total_late_minutes": total_late, "total_overtime_minutes": total_ot, "attendance_pct": attendance_pct}


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first, last = valid_month(month)
    employee_filter: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_filter["department"] = department
    # Aggregate starts with employees, so headcount includes employees with no attendance logs.
    pipeline = [
        {"$match": employee_filter},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
          "pipeline": [{"$match": {"$expr": {"$and": [
              {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first.isoformat()]},
              {"$lte": ["$date", last.isoformat()]}]}}},
            {"$project": {"status": 1, "date": 1, "work_hours": 1, "half_day": {"$ifNull": ["$half_day", False]},
                          "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                          "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]}}}],
          "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {"_is_present_day": {"$and": [
            {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
            {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "2000-01-01"]}}}}, 7]},
            {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "2000-01-01"]}}}}, 1]}
        ]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$cond": ["$_is_present_day", {"$cond": ["$logs.half_day", 0.5, 1]}, 0]}},
            "avg_work_hours": {"$avg": {"$cond": [{"$and": [
                {"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]},
                "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$sort": {"_id": 1}},
    ]
    raw = list(db.employees.aggregate(pipeline))
    items = []
    for x in raw:
        if x["headcount"] == 0:
            continue
        items.append({"department": x["_id"], "headcount": x["headcount"],
          "present_days": rounded(x["present_days"], 2),
          "avg_work_hours": rounded(x["avg_work_hours"], 2) if x.get("avg_work_hours") is not None else None,
          "late_count": x["late_count"], "total_late_minutes": x["total_late_minutes"],
          "leave_count": x["leave_count"], "on_duty_count": x["on_duty_count"]})
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = valid_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
          "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start, end = valid_date(from_date), valid_date(to_date)
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="invalid date range; maximum range is 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="department not found")
    # Gap-fill dates in Python is deliberately avoided; $densify fills missing daily rows.
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date",
          "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_STATUSES)]},
                                                 {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "date": {"$dateFromString": {"dateString": "$_id"}},
                      "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day",
                      "bounds": [datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)]}}},
        {"$set": {"date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                  "present_count": {"$ifNull": ["$present_count", 0]},
                  "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$sort": {"date": 1}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    # $densify cannot create rows when the pipeline has no input documents.
    # Generate the requested dates as a fallback for a completely empty range.
    if not rows:
        current_day = start
        while current_day <= end:
            rows.append({
                "date_str": current_day.isoformat(),
                "present_count": 0,
                "late_count": 0,
            })
            current_day += timedelta(days=1)
    # Headcount and moving averages use the generated daily rows.
    employees = list(db.employees.find({"department": department}, {"joined_on": 1, "_id": 0}))
    result = []
    rates = []
    for row in rows:
        day = valid_date(row["date_str"])
        headcount = sum(1 for e in employees if e["joined_on"] <= day.isoformat())
        working = day.weekday() < 5
        rate = rounded(row["present_count"] / headcount, 4) if working and headcount > 0 else None
        rates.append(rate)
        valid_rates = [r for r in rates[-7:] if r is not None]
        moving = rounded(sum(valid_rates) / len(valid_rates), 4) if valid_rates else None
        result.append({"date": day.isoformat(), "is_working_day": working, "headcount": headcount,
          "present_count": rounded(row["present_count"], 2), "late_count": row["late_count"],
          "attendance_rate": rate, "moving_avg_7d": moving})
    return {"department": department, "items": result}


# -------------------------------- explain -----------------------------------
def json_safe(value):
    # Mongo explain may contain ObjectIds and datetimes.
    return json_util.loads(json_util.dumps(value))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                valid_date(date_from)
                q["date"]["$gte"] = date_from
            if date_to:
                valid_date(date_to)
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        explanation = db.command("explain", {"find": "attendance_logs", "filter": q,
            "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size},
            verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        first, last = valid_month(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}}]
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                                 verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        first, last = valid_month(month)
        q = {"joined_on": {"$lte": last.isoformat()}}
        if department is not None:
            q["department"] = department
        explanation = db.command("explain", {"find": "employees", "filter": q, "sort": {"department": 1}},
                                 verbosity="executionStats")
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        first, last = valid_month(month)
        pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
                    {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}}]
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                                 verbosity="executionStats")
        collection = "attendance_logs"
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        start, end = valid_date(from_date), valid_date(to_date)
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid date range")
        pipeline = [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                    {"$group": {"_id": "$date", "count": {"$sum": 1}}}]
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                                 verbosity="executionStats")
        collection = "attendance_logs"
    return {"endpoint": endpoint, "collection": collection, "explain": json_safe(explanation)}
