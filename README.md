# Farm Management Operational Service

Independent synchronous FastAPI/PostgreSQL service for non-monetary daily farm operations. It owns authentication, users, employees, ABK Web App access and ABK daily-record submissions awaiting approval, livestock groups, the layer (laying hen) vaccination and medicine program with the ABK reminders, daily records, feed and egg inventory, Lele sampling, and the Lele biomass read model. It intentionally contains no cashflow, revenue, customer, payment, loan, cash-account, statement, report, or proof-upload feature. The one money value it handles is the price per kg a Superadmin enters when approving an ABK egg outflow: it is kept only to deliver that sale to finance-service, which owns the revenue (see [ABK egg outflows](#abk-egg-outflows-and-finance-service)).

## License

Copyright (c) 2026 Bagus Pribadi. This project is open source and available under the [MIT License](LICENSE).

## Runtime and setup

- Python 3.14
- PostgreSQL
- FastAPI, SQLAlchemy, Alembic, Pydantic, JWT, and Argon2 using the same pinned versions as `backend`

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

Use an independent operational database. Share `JWT_SECRET`, `JWT_ISSUER`, and `JWT_AUDIENCE` with `finance-service`; generate `INTERNAL_API_KEY` separately and never expose it to browser code.

- Swagger: <http://127.0.0.1:8001/docs>
- OpenAPI: <http://127.0.0.1:8001/openapi.json>
- Liveness: <http://127.0.0.1:8001/health/live>
- Readiness: <http://127.0.0.1:8001/health/ready>

The database starts empty. Create the first Superadmin with an Argon2 hash generated locally:

```bash
python -c "from app.core.security import hash_password; print(hash_password(input('Initial password: ')))"
```

```sql
INSERT INTO users
    (name, email, password_hash, role, is_active, must_change_password, token_version, created_at, updated_at)
VALUES
    ('Initial Superadmin', 'superadmin@example.com', '$argon2id$REPLACE_WITH_GENERATED_HASH',
     'SUPERADMIN', TRUE, TRUE, 0, NOW(), NOW());
```

The frontend logs in here with email/password and uses the returned Bearer JWT directly against both operational-service and finance-service. Human routes require only that Bearer token. This service is the identity authority; finance calls the private authorization endpoint on every request to enforce current user state and token version.

## ABK Web App

ABK (field crew) submit their daily operational report from the ABK Web App **without logging in**. Every ABK request carries two headers, checked on every call; there is no session and no token:

```
X-ABK-Username: budi
X-ABK-PIN: 123456
```

ABK are employees, not `users`: they never receive a JWT, cannot call any other endpoint, and finance-service never sees them. Wrong, unknown, and inactive credentials all return `401 INVALID_ABK_CREDENTIALS`. Five consecutive wrong PINs lock the account for 15 minutes (`429 ABK_PIN_LOCKED`); a correct PIN resets the counter and a PIN reset unlocks it. Add the ABK Web App origin to `CORS_ALLOWED_ORIGINS`.

| Who | Endpoint | Purpose |
| --- | --- | --- |
| ABK | `GET /api/v1/abk/form-options` | Check the username/PIN and load the form: employee, scopes, active livestock groups and feed types of those scopes |
| ABK | `POST /api/v1/abk/daily-reports` | Submit a daily report; it is stored as `DRAFT` |
| ABK | `GET /api/v1/abk/daily-reports`, `GET /api/v1/abk/daily-reports/{id}` | The ABK's own reports and their review outcome (filters: `status`, `species`, `from`, `to`) |
| ABK | `GET /api/v1/abk/customers?q=` | Customer picker for the egg outflows (chicken scope; active customers from finance-service, id and name only) |
| ABK | `GET /api/v1/abk/layer-health-reminders?days=7` | Vaccinations and medicines to give today and on the next days, up to 7, prepared by a nightly job (chicken scope; see [Layer health program](#layer-health-program-vaccinations-medicines-and-abk-reminders)) |
| ABK | `POST /api/v1/abk/layer-health-activities`, `GET /api/v1/abk/layer-health-activities`, `GET .../{id}` | Record a reminder task as done or skipped (with a reason); the ABK's own activities |
| Admin/Superadmin | `GET/POST /api/v1/abk-accounts`, `GET/PATCH /api/v1/abk-accounts/{id}` | Give an active employee ABK access (username, scopes), rename, change scopes, activate/deactivate; list filter `scope=AYAM\|LELE\|NONE` |
| Admin/Superadmin | `POST /api/v1/abk-accounts/{id}/reset-pin` | Issue a new PIN and unlock the account |
| Admin/Superadmin | `GET /api/v1/abk-daily-reports?status=DRAFT`, `GET /api/v1/abk-daily-reports/{id}` | Review queue (filters: `status`, `species`, `group_id`, `employee_id`, `from`, `to`) |
| Admin/Superadmin | `POST /api/v1/abk-daily-reports/{id}/approve` | Create the daily record from the report (Superadmin with prices if it has egg outflows) |
| Admin/Superadmin | `POST /api/v1/abk-daily-reports/{id}/reject` | Reject with `{"reason": "..."}` |
| Admin/Superadmin | `GET /api/v1/abk-egg-sales?finance_status=FAILED`, `GET /api/v1/abk-egg-sales/{id}` | Approved egg sales (one per customer) and their delivery to finance-service |
| Superadmin | `POST /api/v1/abk-egg-sales/{id}/resend` | Send again now, optionally with a corrected customer/lines/prices (new revision) |

The PIN (6 digits) is generated by the service and returned only by the create and reset-PIN responses; only its Argon2 hash is stored.

Each account has **scopes**: `AYAM` (chicken, including egg sales) and/or `LELE` (fish), or none (`"scopes": []`). `scopes` is required when creating an account and optional on `PATCH` (omitted: unchanged). The form options list only the groups and feed types of the ABK's scopes, and `can_report_egg_sales` tells whether to show the egg-outflow section. A report for a group outside the scopes, or the customer picker without the chicken scope, is refused with `403 ABK_SCOPE_FORBIDDEN` (the body includes the ABK's `scopes`). An ABK without any scope can still check the PIN but cannot submit anything; show them that they need an assignment. Scope changes apply to the next request; a report submitted while in scope stays reviewable.

The report body is the same as one row of `POST /api/v1/daily-records/bulk`, without `species` (it comes from the group):

```json
{
  "group_id": 1,
  "record_date": "2026-11-17",
  "mortality": 2,
  "notes": null,
  "feeds": [{"feed_type_id": 3, "morning_kg": "3.500", "noon_kg": "2.250", "evening_kg": "0"}],
  "eggs": {
    "UTUH": {"weight_kg": "5.000", "quantity_pieces": 80},
    "PECAH": {"weight_kg": "0.500", "quantity_pieces": 8},
    "REJECT": {"weight_kg": "0", "quantity_pieces": 0}
  },
  "egg_outflows": [
    {"customer_id": 12, "lines": [{"egg_category": "UTUH", "quantity_pieces": 200, "weight_kg": "12.500"}]},
    {"customer_id": 13, "lines": [{"egg_category": "REJECT", "quantity_pieces": 30, "weight_kg": "1.500"}]}
  ]
}
```

An ABK only ever sees the reports they submitted: another ABK's report answers `404` like a missing one. The ABK view (also returned on submit) shows what the ABK entered plus `status`, `reviewed_at`, and `rejection_reason`; it never includes the approved egg sales (Superadmin corrections and prices), their delivery to finance-service, the reviewer, or internal ids. Reports stay visible after a scope is removed.

A `DRAFT` report affects no feed/egg inventory, population, or Lele biomass figure. Only one report per group and date can wait for review, and none is accepted for a date that already has a daily record. Approval runs the normal daily-record rules against the current data and creates the daily record (owned by the approver), from which inventory, population, and biomass follow as usual; if a rule fails (for example the group was closed or a record was entered meanwhile) nothing changes and the report stays `DRAFT`. A rejected report keeps its reason, and the ABK can submit a corrected report for the same group and date. Corrections after approval use the existing daily-record edit.

## ABK egg outflows and finance-service

On an Ayam report the ABK may add `egg_outflows`: the eggs that left the farm that day, one entry per customer (picked from `GET /api/v1/abk/customers`, up to 20), each with the pieces (butir) and kg for one to three egg categories. A customer appears once per report (all of its categories in one entry); the same category may go to several customers. Leave it out, or send `[]`, when no eggs left. It changes nothing until approval.

Only a Superadmin can approve a report with egg outflows (`403 SUPERADMIN_REQUIRED` for Admins). The approval body confirms one sale per customer: pre-fill them from the report's `egg_outflows`, correct customers, pieces, or kg, add or remove customers if needed, and add `price_per_kg` (rupiah) to every line:

```json
{"egg_sales": [
  {"customer_id": 12, "lines": [{"egg_category": "UTUH", "quantity_pieces": 198, "weight_kg": "12.400", "price_per_kg": "28000"}]},
  {"customer_id": 13, "lines": [{"egg_category": "REJECT", "quantity_pieces": 30, "weight_kg": "1.500", "price_per_kg": "9000"}]}
]}
```

The report keeps what the ABK entered (`egg_outflows`) next to what was approved (`egg_sales`). The approval stores each sale as `PENDING` in the same transaction as the daily record. Each sale is a separate revenue in finance-service (one per customer) and is delivered, retried, and corrected on its own, so one customer's problem never holds back the others: right after the response, then by the worker with an exponential back-off (1 minute doubling to 1 hour) while finance-service is unavailable. A rejection by finance-service marks that sale `FAILED` with the error; a Superadmin corrects it with `resend`. Once `SENT`, corrections are made in finance-service.

Approving does **not** change the egg inventory. finance-service reports the sale back through the internal egg-sales inventory API below, like any other egg revenue, so egg stock leaves with a sale through a single path.

The contract finance-service must implement (customers and the egg-sale revenue endpoint) is [`docs/finance-internal-api.yaml`](docs/finance-internal-api.yaml); the tests validate every request this service sends against it. Configure `FINANCE_BASE_URL` and `FINANCE_INTERNAL_API_KEY` (the `X-FM-Internal-Key` finance-service accepts). Reports without an egg outflow never call finance-service.

```bash
python -m app.scripts.finance_egg_sale_worker          # keep retrying undelivered sales
python -m app.scripts.finance_egg_sale_worker --once   # from cron
```

## Layer health program (vaccinations, medicines, and ABK reminders)

There is one farm-wide vaccination schedule and medicine program for the laying hens (Ayam groups). It applies to every group and can be changed at any time. ABK see what it asks them to do today and on the next days.

### Hen age

Everything is timed by the hens' **age**, never by the calendar. An Ayam group has `arrival_age_week`: the hens' age in whole weeks on the group's `start_date`.

- On `POST /api/v1/livestock-groups` it is optional and defaults to 13. `PATCH` changes it; leave it out to keep it.
- Ayam groups that existed before this field were set to 13 by the migration.
- Lele groups have none (`422` if sent).

From there the age goes up one day per day. It is shown as age week and day of the week (1-7):

| Date | Age (arrived at 13 weeks on 2026-03-02) |
| --- | --- |
| 2026-03-02 (start date) | week 13, day 1 |
| 2026-03-08 | week 13, day 7 |
| 2026-03-09 | week 14, day 1 |

The group responses (`GET /api/v1/livestock-groups...`) and the ABK form options include `arrival_age_week` and `current_age`, e.g. `{"week": 24, "day": 3}`. A closed group's age stops on its closing date. Changing `arrival_age_week` moves the group's whole program.

### Setup (Admin/Superadmin)

| Endpoint | Purpose |
| --- | --- |
| `GET/POST /api/v1/layer-vaccinations`, `GET/PATCH /api/v1/layer-vaccinations/{id}` | Vaccination schedule, youngest age first (filters: `is_active`, `age_week`, `q` on vaccine name or target disease) |
| `GET/POST /api/v1/layer-medicine-programs`, `GET/PATCH /api/v1/layer-medicine-programs/{id}` | Medicine program, by the age of the first course (filters: `is_active`, `q` on medicine name or purpose) |
| `GET /api/v1/layer-vaccinations/template`, `POST /api/v1/layer-vaccinations/import` | Excel template of the vaccination schedule, and upload of the filled-in template (replaces the whole schedule; see below) |
| `GET /api/v1/layer-health-reminders?from=&to=&group_id=` | The stored reminders per date for any period up to 92 days (default: today and the next 7 days): the coming week and the earlier days as they were reminded |

A vaccination is due during its **whole `age_week`**: it can be given on any of those 7 days.

```json
{"age_week": 14, "vaccine_name": "ND-IB Live", "target_disease": "ND, IB", "method": "EYE_DROP",
 "dosage": "1 tetes/ekor", "notes": "Air minum tanpa klorin"}
```

A medicine program says when its first course starts and how it repeats:

- **Start:** day `start_day` (1-7, default 1) of age week `start_age_week`.
- **Course:** `course_days` consecutive days (default 1).
- **Repeat:** a new course starts every `interval_value` `interval_unit`, counted from the start of the previous course. The unit is `DAY`, `WEEK`, or `MONTH`. A month is 4 age weeks (28 days), so a course always starts on the same day of the age week.

For example, 3 days from day 3 of week 13, every 2 weeks:

```json
{"medicine_name": "Vita Stress", "purpose": "Vitamin", "method": "DRINKING_WATER", "dosage": "1 g / 2 L air minum",
 "start_age_week": 13, "start_day": 3, "course_days": 3, "interval_value": 2, "interval_unit": "WEEK", "notes": null}
```

- `method` is one of `EYE_DROP`, `NASAL_DROP`, `ORAL`, `DRINKING_WATER`, `FEED`, `SPRAY`, `INTRAMUSCULAR`, `SUBCUTANEOUS`, `WING_WEB`, `OTHER` (explain `OTHER` in `notes`). `dosage` is free text.
- Ages go from 0 to 200 weeks. Intervals go up to 365 days, 52 weeks, or 12 months. A course is 1 to 30 days and never longer than its interval.
- The same vaccine can appear only once per age (`409 LAYER_VACCINATION_EXISTS`); giving it again at another age is a booster. A medicine has one program (`409 LAYER_MEDICINE_EXISTS`). Names are compared case-insensitively, and the conflict body includes `existing_id`.
- `PATCH` replaces the whole item, including `is_active`. Items are never deleted: deactivate one that no longer applies. An inactive item keeps its name reserved and can be reactivated.
- Every create and update is recorded in the audit events.

#### Vaccination schedule from Excel

The whole vaccination schedule can be replaced from an Excel sheet. The frontend only uploads the file; the backend reads and validates it.

1. **Download** `GET /api/v1/layer-vaccinations/template` (an `.xlsx` attachment, `layer-vaccination-schedule.xlsx`).
   - The `Vaccinations` sheet has the schedule's columns: `age_week`, `vaccine_name`, `target_disease`, `method`, `dosage`, `notes`. It has a drop-down for `method` and a 0-200 check on `age_week`.
   - The `Instructions` sheet explains the columns and methods.
   - The sheet is prefilled with the current active schedule so it can be edited and uploaded back. `include_current=false` gives an empty sheet.
2. **Upload** `POST /api/v1/layer-vaccinations/import` as `multipart/form-data` with the file in the `file` field (`.xlsx`, up to 2 MB and 500 vaccinations).
   - Add `dry_run=true` to check the file and see what would change without saving anything, e.g. to show a confirmation.

**The upload replaces the whole schedule.** In one transaction, the file becomes the active schedule:

- A row with the age week and vaccine name (case-insensitive) of an existing vaccination, active or not, updates that vaccination and keeps its id. What the ABK already recorded for it this week therefore stays recorded and snoozed.
- Other rows are created.
- Every active vaccination that is not in the file is deactivated. It is no longer valid and is not deleted: recorded activities still refer to it.
- Reminders follow the new schedule from the next nightly run. Run the job by hand to apply it at once.
- Every change is in the audit events, with `import:<file name>` as the reason.

```json
{"data": {"dry_run": false,
  "summary": {"rows": 4, "created": 1, "updated": 2, "unchanged": 1, "deactivated": 1},
  "schedule": [{"id": 1, "age_week": 4, "vaccine_name": "ND-IB Live", "target_disease": "ND, IB", "method": "EYE_DROP",
                "dosage": "2 tetes/ekor", "notes": null, "change": "UPDATED"}, "..."],
  "deactivated": [{"id": 3, "age_week": 18, "vaccine_name": "EDS"}]}}
```

Reading the file:

- The header row names the columns. Any order is accepted, and `target_disease`, `dosage`, and `notes` may be left out. The sheet named `Vaccinations` is read, or the first sheet.
- Blank rows are ignored. Method cells may be typed loosely (`eye drop` becomes `EYE_DROP`).
- Every row follows the rules of `POST /api/v1/layer-vaccinations`, and a vaccine appears only once per age week.
- **Any invalid row rejects the whole file.** Nothing changes, and the `422` answer lists every error, keyed by Excel row number and column:

```json
{"code": "VALIDATION_ERROR", "detail": "The file has invalid rows; nothing was imported.",
 "errors": {"rows.4.method": ["Input should be 'EYE_DROP', 'NASAL_DROP', ..."],
            "rows.7.vaccine_name": ["The same vaccine and age week as row 6."]}}
```

- A file that cannot be used at all gives `422 INVALID_FILE` with `errors.file`. This covers a file that is not an `.xlsx`, unknown or missing columns, no vaccination at all (an empty schedule cannot be imported), or more than 500 rows. A file over 2 MB gives `413 FILE_TOO_LARGE`.
- CORS exposes `Content-Disposition`, so the frontend can read the template's file name.

### Reminders

| Who | Endpoint | Purpose |
| --- | --- | --- |
| ABK (chicken scope) | `GET /api/v1/abk/layer-health-reminders?days=7&group_id=&include_snoozed=false` | What to do today and on the next `days` days (0-7, default 7), for every active Ayam group or one group |

The reminders are **not computed when they are requested**. A nightly job computes them and stores them in two tables: `layer_health_daily_reminders` has one row per active Ayam group and date, with the hens' age. `layer_health_daily_reminder_tasks` holds that day's vaccinations and medicines, with the program details copied in. Both endpoints only read these tables.

Only active Ayam groups are included, and only from their start date. Two rules follow from timing by age:

- A vaccination at a younger age week than the arrival age was given before arrival and is never shown.
- A medicine that started before arrival keeps its rhythm. The group joins it at the next course, or at the remaining days of a course in progress.

Every date of the period is listed. Each date lists every active group with its age, including groups without any task, so the ABK can see the coming days and prepare (buy the vaccine, stop chlorinating the water, ...). Groups are ordered by name, and within a group vaccinations come before medicines:

```json
{"today": "2026-03-02", "from": "2026-03-02", "to": "2026-03-09", "days": [
  {"date": "2026-03-04", "days_from_today": 2, "groups": [
    {"group": {"id": 1, "code": "KANDANG-1-BLOK-A", "name": "Kandang 1", "location": "Blok A"},
     "age": {"week": 13, "day": 3}, "computed_at": "2026-03-01T17:05:00+00:00",
     "tasks": [
       {"key": "VACCINATION-2-1-2026-03-04", "type": "VACCINATION", "source_id": 2, "name": "Coryza",
        "target_disease": "Snot", "purpose": null, "method": "INTRAMUSCULAR", "dosage": "0,5 ml/ekor", "notes": null,
        "window": {"from": "2026-03-02", "to": "2026-03-08", "day": 3, "days": 7}, "course": null},
       {"key": "MEDICINE-3-1-2026-03-04", "type": "MEDICINE", "source_id": 3, "name": "Vita Stress",
        "target_disease": null, "purpose": "Vitamin", "method": "DRINKING_WATER", "dosage": "1 g / 2 L air minum",
        "notes": null, "window": null, "course": {"day": 1, "days": 3}}
     ]},
    {"group": {"id": 4, "code": "KANDANG-3-BLOK-C", "name": "Kandang 3", "location": "Blok C"},
     "age": {"week": 2, "day": 3}, "computed_at": "2026-03-01T17:05:00+00:00", "tasks": []}
  ]}
]}
```

- A vaccination is listed on each day of its age week. `window` gives that week (`from`..`to`) and which of the 7 days this is, so it is given once, on any of them.
- A medicine has a task on each day of its course. `course` says which day it is.
- `key` identifies one task: the source, its id, the group, and the date. Record the task with it (see [Activities](#activities-done-or-skipped)).
- Snoozed tasks are left out. With `include_snoozed=true` (also on the staff view) they are listed with `snoozed_at`; `snoozed_at` is `null` for the others.
- An ABK without the chicken scope gets `403 ABK_SCOPE_FORBIDDEN`.

#### Snoozes

A task stops being reminded when its **occurrence** is snoozed. An occurrence is what is done once:

- For a vaccination it is its whole age week, so snoozing hides the vaccination on all 7 days.
- For a medicine it is one course day: each day of a course is a dose of its own.

Each task carries its `occurrence_key`. Snoozes are kept in `layer_health_reminder_snoozes`, apart from the rows the nightly job rewrites, so they also hold for the tasks of later runs. A snooze records only what caused it (`source`, `source_id`). Recording an activity is one cause. The reminder side knows nothing about activities, so reminders can later serve other contexts than the health program.

#### Nightly reminder job

```bash
python -m app.scripts.layer_health_reminder_job                    # today (APP_TIMEZONE) and the next 7 days
python -m app.scripts.layer_health_reminder_job --date 2026-03-02  # as if today were that date
```

Schedule it every night **just after midnight in `APP_TIMEZONE`**, so "today" is the new day. Also run it once after deploying: the tables start empty. Example crontab line, with the server clock in Asia/Jakarta:

```
5 0 * * *  cd /srv/operational-service && .venv/bin/python -m app.scripts.layer_health_reminder_job
```

Each run does the following:

- It replaces the dates from today to 7 days later, in one transaction, with the program, group ages, and groups of that moment. Readers see either the previous or the new reminders, never a mix, and two runs never overlap.
- The coming week is therefore refreshed every day. Changes to the program or to a group's arrival age, new groups, and closed groups show up from the next run. Run the job by hand when a change must show sooner.
- The ABK endpoint leaves out a group as soon as it is closed, even before the next run. The staff view keeps showing the stored rows until then.
- Earlier dates are not touched. They stay as they were last computed, which is what the ABK were reminded of on that day.
- If a run is missed, the rows from the previous runs still cover the coming days, with the program as it was then.

### Activities (done or skipped)

| Who | Endpoint | Purpose |
| --- | --- | --- |
| ABK (chicken scope) | `POST /api/v1/abk/layer-health-activities` | Record a reminder task as `DONE`, or `SKIPPED` with `skip_reason` |
| ABK | `GET /api/v1/abk/layer-health-activities`, `GET /api/v1/abk/layer-health-activities/{id}` | The activities this ABK recorded (filters: `from`, `to`, `status`, `type`, `group_id`) |
| Admin/Superadmin | `GET /api/v1/layer-health-activities`, `GET /api/v1/layer-health-activities/{id}` | Every activity, with who recorded it (filters: `from`, `to`, `status`, `type`, `group_id`, `employee_id`) |

A reminder is an activity the ABK should do, unless they say why not:

```json
{"task_key": "VACCINATION-2-1-2026-03-04", "status": "DONE", "notes": "Semua ayam sudah"}
{"task_key": "MEDICINE-3-1-2026-03-04", "status": "SKIPPED", "skip_reason": "Obat habis, menunggu kiriman"}
```

Recording does two things in one transaction:

1. **The activity.** It copies every detail of the reminder task into `layer_health_daily_activities`: group, date (`activity_date`), age, type and source id, name, target disease or purpose, method, dosage, the program's notes (`program_notes`), and the vaccination week (`window`) or course day (`course`). It also stores the ABK's `status`, `skip_reason`, `notes`, who recorded it, and when. An activity never refers to the reminder tables: it stays exactly as recorded when the program changes and the reminders are rebuilt. `reminder_task_key` only tells which reminder task it answered.
2. **The snooze.** It snoozes the task's occurrence. A vaccination disappears for the rest of its week, a medicine for that course day (the next course day is still reminded). This applies to `DONE` and to `SKIPPED`.

Rules:

- Only a task dated today or earlier can be recorded (`422`). A late recording keeps the task's date as `activity_date`.
- The task must still be in the stored reminders (`404`), and its group must still be active (`409 LAYER_HEALTH_GROUP_NOT_ACTIVE`).
- An occurrence is recorded once, by any ABK (`409 LAYER_HEALTH_ACTIVITY_EXISTS` with its `status` and `activity_date`). A vaccination done on Monday cannot be recorded again on Wednesday. An occurrence snoozed for another reason gives `409 LAYER_HEALTH_REMINDER_SNOOZED`.
- `skip_reason` is required for `SKIPPED` and not allowed for `DONE`. Activities are not edited.
- An ABK sees only their own activities; another ABK's activity answers `404`.

## Internal inventory API

Machine calls use only `X-FM-Internal-Key`. `INTERNAL_API_PREVIOUS_KEY` supports zero-downtime rotation. A source is addressed by service, type, and path ID. Revisions must increase; an exact retry is a no-op, a stale revision is rejected, and reusing a revision with different content is rejected.

Feed receipt:

```bash
curl -X PUT http://127.0.0.1:8001/api/v1/internal/inventory/feed-receipts/cashflow-42 \
  -H 'X-FM-Internal-Key: replace-with-internal-secret' \
  -H 'Content-Type: application/json' \
  -d '{
    "source_service":"finance-service",
    "source_revision":1,
    "status":"ACTIVE",
    "received_date":"2026-11-17",
    "lines":[
      {"source_line_id":"line-1","feed_type_code":"AYAM-BR-1","quantity_kg":"100.000"}
    ]
  }'
```

Egg sale:

```bash
curl -X PUT http://127.0.0.1:8001/api/v1/internal/inventory/egg-sales/revenue-84 \
  -H 'X-FM-Internal-Key: replace-with-internal-secret' \
  -H 'Content-Type: application/json' \
  -d '{
    "source_service":"finance-service",
    "source_revision":1,
    "status":"ACTIVE",
    "transaction_date":"2026-11-17",
    "lines":[
      {"source_line_id":"line-1","egg_category":"UTUH","weight_kg":"12.500","quantity_pieces":200}
    ]
  }'
```

Send the complete current line set on every higher revision. Use `status: "VOIDED"` with an empty `lines` array to remove the source's inventory effect. The service accepts no monetary, price, customer, approval, or payment fields.

Feed types expose an immutable generated `code`; integrations must use it instead of a database ID. Renaming a feed type does not change its code.

## Lele biomass jobs

Writes enqueue biomass rebuilds transactionally and normally drain them after the response. A dedicated worker is available as a reliability fallback:

```bash
python -m app.scripts.lele_biomass_worker
python -m app.scripts.lele_biomass_worker --once
```

Recompute all sampling results and biomass snapshots after direct data repairs or configuration changes:

```bash
python -m app.scripts.backfill_lele_sampling_results --dry-run
python -m app.scripts.backfill_lele_sampling_results
```

## Checks

Tests require a PostgreSQL database whose name ends in `_test`.

```bash
python -m pytest
ruff check app tests
mypy app
```
