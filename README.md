# HROne Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Trainee assignment. The API follows `openapi.yaml`; business rules and document shapes are described in `DATA_MODEL.md`.

## Requirements

- Python 3.10+
- MongoDB 6.0+ (MongoDB 7.0 recommended)
- Dependencies in `requirements.txt`

## Run locally (Windows PowerShell)

python -m venv .venv .\\.venv\Scripts\Activate.ps1 pip install -r requirements.txt

Create a local `.env` file in the project root (do not commit it):

MONGO_URI=mongodb://127.0.0.1:27017 MONGO_DB=attendance_db

Make sure the MongoDB service is running, then seed the sample data and start the API:

python sample_seed.py python -m uvicorn app.main:app --port 8000 --reload

Open Swagger UI at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs). The application creates its indexes at startup. `sample_seed.py` resets the two sample collections before loading data, so use it only when resetting local sample data is acceptable.

## Endpoints

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

## Test checklist

Run every endpoint in Swagger and test both valid and invalid cases: duplicate employee/attendance, unknown employee, invalid timestamp/date, duplicate punch-out, no-op correction, pagination bounds, and date-range validation. Check response values as well as HTTP status codes. Use `/admin/explain/{endpoint}` with representative filters and inspect `executionStats` for index use.

The happy-path API checks reported during development returned no errors. That is not a substitute for validating analytics against edge cases or confirming query plans on the grader's larger dataset. See `REVIEW.md` for review notes.

## GitHub submission safety

Commit the source and documentation only. Do **not** commit `.env`, database credentials, tokens, virtual environments, or local data dumps. The repository must be public for evaluation and remain public through 31 October 2026, per the assignment instructions.
