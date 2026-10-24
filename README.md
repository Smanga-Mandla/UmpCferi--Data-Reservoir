# UMP CFERI Data Reservoir

The project is implemented in SRS-aligned phases. Phases 1–9 cover the platform foundation, authentication/access control, centre compliance document vault, partner/MOU management, stakeholder directory, incubatee onboarding, check-ins and milestones, interactions, dashboards, reporting, exports, and audit. Phase 10 automated acceptance checks pass under isolated test settings; integration and deployment verification remains outstanding. Known limitations are recorded in the linked documentation below. Some metric formulas and privacy thresholds remain provisional pending CFERI decisions.

## Requirements

- Python 3.10 or newer (the project is tested with Python 3.11)
- PostgreSQL 14 or newer
- Redis 6 or newer for Celery workers

## Local setup (Windows PowerShell)

From the project root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set `DJANGO_SECRET_KEY`. To use Neon, copy the pooled PostgreSQL connection string from the Neon dashboard into `DATABASE_URL` in `.env`; the application enforces SSL and disables persistent connections by default for pooler compatibility. Never commit or paste the connection string into source control or chat. No user accounts or default passwords are created by this project.

For Neon, create a project and database in the Neon console, choose **Connect**, select the project database and role, then copy the pooled connection string (the hostname contains `-pooler`). Keep the connection string private and put it only in your local `.env` or deployment secret manager. Leave the `POSTGRES_*` variables unused when `DATABASE_URL` is set. If using local PostgreSQL instead, leave `DATABASE_URL` blank and configure the `POSTGRES_*` variables below.

For a local PostgreSQL installation, open PowerShell and create the database (enter the PostgreSQL administrator password when prompted):

```powershell
psql -U postgres -h 127.0.0.1 -c "CREATE DATABASE ump_cferi;"
```

Set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, and `POSTGRES_PORT` in `.env` to match the database and a dedicated application database user. Do not use the PostgreSQL superuser as the production application account.

Create the custom email-based user and role tables before the first database migration:

```powershell
python manage.py makemigrations accounts
python manage.py migrate
python manage.py check
python manage.py test
```

The migrations include the custom user model and seven project roles; they create no user accounts.

Start the development server:

```powershell
python manage.py runserver
```

Visit `http://127.0.0.1:8000/`. The database-backed health endpoint is `http://127.0.0.1:8000/health/`; it returns HTTP 503 when PostgreSQL is unavailable. Development email is written to the console unless `EMAIL_HOST` is configured in the ignored local `.env`; with a host configured, Django sends through SMTP.

To deliver password setup or reset links locally, set `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` or `EMAIL_USE_SSL`, and `DEFAULT_FROM_EMAIL` in `.env`. Keep SMTP credentials in `.env` and never share them in chat or commit them. Restart the development server after changing these settings.

To test a real connection to the configured PostgreSQL database, after the database is running and configured in `.env`:

```powershell
$env:RUN_DB_CONNECTIVITY_TESTS = "1"
python manage.py test accounts.tests.test_database
Remove-Item Env:RUN_DB_CONNECTIVITY_TESTS
```

To create an administrator interactively, use `python manage.py createsuperuser`. Django prompts for credentials; no default account or password is installed. The Django superuser has Super Admin access in the project authorization layer. Assign regular accounts through the protected account-management UI.

## Authentication and access control

- Argon2 is the preferred password hasher. Email addresses are normalized and authenticated case-insensitively.
- System account email addresses must end in `@gmail.com`, including accounts assigned the External Guest role. Passwords must be at least eight characters and pass Django's configured similarity, common-password, and numeric-password checks when set or reset.
- Super Admin, System Admin, Admin, and Board Member users must enroll a TOTP authenticator and verify a code at login. Keep `DJANGO_SECRET_KEY` and `MFA_ENCRYPTION_KEY` stable across restarts; `MFA_ENCRYPTION_KEY` can be generated with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- For production, set an independent Fernet key in `MFA_ENCRYPTION_KEY`; the development fallback derives an encryption key from `DJANGO_SECRET_KEY`. Back up the independent key securely because TOTP secrets cannot be recovered if it is lost.
- Password reset/invitation links expire after 30 minutes. Five failed password or MFA attempts lock the email for 15 minutes.
- Sessions expire after 30 minutes of inactivity. Freelancer and External Guest accounts require an expiry date and are deactivated on use after expiry and by the scheduled Celery task.
- Super Admin and System Admin can manage other user accounts at `/accounts/manage/`; neither may change their own roles or expiry there. Only Super Admin can edit the role matrix at `/accounts/role-matrix/`.
- The permission matrix is seeded by migration and represented by resource/action/scope grants. Reusable authorization helpers in `accounts.access` must be applied to each record view, queryset, download, export, and background task. Record shares do not bypass restricted data, System Admin confidential-document denial, or Board aggregate-only limits.
- `AccessControlledModel` provides sensitivity, owner/assignee fields, and soft deletion. Subclasses must set `access_resource`. Its active manager hides deleted records; `all_objects` supports controlled recovery. Bulk deletion is disabled. Permanent deletion requires Super Admin. Use `soft_delete_record`, `permanently_delete_record`, and the model's 90-day `restore` method rather than direct deletion in business views.
- Test auth/access changes with `python manage.py test accounts.tests --settings=config.settings.test`. The test settings use an isolated in-memory SQLite database; the default application configuration remains PostgreSQL.
- MFA method/clock-skew/recovery and the conflicting Board confidential-document rule remain open for CFERI confirmation in [OPEN_DECISIONS.md](docs/OPEN_DECISIONS.md).

### Role guide

The seven role grants are the enforceable source of access; a screen or navigation link does not itself grant permission.

- **Super Admin:** system-wide administration and protected role-matrix management. Permanent deletion is currently disabled until an approved retention schedule exists; authorized deletes use soft deletion with 90-day recovery.
- **System Admin:** account administration and permitted audit/system functions; no automatic access to confidential business documents.
- **Admin:** authorized operational and reporting access, including sensitive profile fields where explicitly granted.
- **Board Member:** read-only access according to the seeded matrix; incubatee information is aggregate-only, with personal/free-text fields restricted.
- **CFERI Staff:** operational access to records assigned or shared with them, subject to the resource grant.
- **Freelancer:** limited access to assigned records and permitted interactions; account access requires an expiry date.
- **External Guest:** access only to records explicitly available to them; account access requires an expiry date.

Users can hold multiple roles; grants combine, but higher-priority sensitivity and record rules still apply. Confirm the exact resource/action/scope grants in the seeded matrix and the decisions register before assigning production roles.

## Background tasks

Celery is configured to use `REDIS_URL` (default `redis://127.0.0.1:6379/0`). Start a worker in a second PowerShell window after Redis is available:

```powershell
.\.venv\Scripts\Activate.ps1
celery -A config worker --loglevel=INFO --pool=solo
```

Start Celery Beat in another PowerShell window to run expired-account deactivation and daily document/MOU expiry alert tasks (06:00 and 06:15 in the configured Django timezone):

```powershell
.\.venv\Scripts\Activate.ps1
celery -A config beat --loglevel=INFO
```

## Centre compliance document vault

The vault is available at `/documents/`. It supports private PDF, DOCX, XLSX, and raster-image uploads up to 25 MB, version history, archival, metadata search, access-controlled downloads, audit events, expiry alerts, and a compliance status board. Document files are served only through authorized download views and are not exposed at a public media URL.

Configure `DOCUMENT_EXPIRY_ALERT_RECIPIENTS` as a comma-separated list and provide the SMTP variables in `.env` to enable email delivery. Dashboard alerts are still recorded when recipients are missing; the alert reports the configuration/delivery error. Run the worker and Beat processes for scheduled expiry notices. Expiry boundary and required-category rules remain subject to CFERI confirmation in [OPEN_DECISIONS.md](docs/OPEN_DECISIONS.md).

Migrations and automated tests:

```powershell
python manage.py migrate
python manage.py check --settings=config.settings.test
python manage.py test documents.tests --settings=config.settings.test
python manage.py test --settings=config.settings.test
```

## Partners, stakeholders, and MOUs

Partner organisations are available at `/partners/`, agreements at `/partners/mous/`, and shared contacts at `/stakeholders/`. MOU agreement files are stored under private per-agreement folders and can only be downloaded after the viewer passes the MOU access check. Contact records are shared across partners, allow multiple stakeholder relationship types, normalize and uniquely constrain email, and warn on possible phone duplicates. Board/employment profiles and POPIA consent history are access restricted; stakeholder records do not create login accounts.

Configure `MOU_ALERT_RECIPIENTS` as a comma-separated list and set the SMTP environment variables to enable renewal reminder email. Dashboard alerts are retained when recipients are missing or delivery fails. Emails for Confidential and Restricted MOUs are suppressed until CFERI confirms a sensitivity-aware recipient policy. MOU alerts run daily at 06:15; the current reminder thresholds are 90, 60, and 30 days. Confirm recipients, timing, renewal workflow, stakeholder identity rules, and obligation statuses in [OPEN_DECISIONS.md](docs/OPEN_DECISIONS.md). MOU-08 is not implemented; the Incubatee model exists but the benefit relationship and access behavior remain unresolved (OD-22).

Apply all database migrations and run the partner/MOU tests with:

```powershell
python manage.py migrate
python manage.py check --settings=config.settings.test
python manage.py test partners.tests --settings=config.settings.test
python manage.py test stakeholders.tests --settings=config.settings.test
python manage.py test --settings=config.settings.test
```

## Incubatee management and onboarding

Incubatee records are available at `/incubatees/`. Authorized users can create and update company profiles through the eight-step baseline wizard, assign programmes/cohorts/staff owners/mentors, record lifecycle status changes, and upload categorized files to private per-incubatee storage. Founder identity, founder demographics, consent steps, and incubatee document downloads are restricted to Super Admin/Admin pending CFERI approval of a broader field-level policy. Non-admin users see only records permitted by the existing assignment and role grants; Board Members receive aggregate counts only.

Create and manage Programmes and Cohorts through Django Admin. Cohorts must belong to their selected Programme when assigned. The lifecycle UI supports the exact SRS status values; transition rules have not been invented. Authorized administrators can preview and confirm `.xlsx` bulk imports. The import accepts a canonical subset of company/business columns and up to 1,000 data rows; the template, column list, and cap remain subject to CFERI confirmation. Unresolved baseline requiredness, controlled vocabularies, assignment policy, status transitions, and document access/retention decisions are recorded in [OPEN_DECISIONS.md](docs/OPEN_DECISIONS.md).

Apply migrations and run the Phase 6 tests with:

```powershell
python manage.py migrate
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
python manage.py test incubation.tests --settings=config.settings.test
```

The full isolated test suite can be run with:

```powershell
python manage.py test --settings=config.settings.test
```

## Check-ins, metrics, and milestones

Monthly check-ins, quarterly reviews, dated milestones, mentoring sessions, exit records, and six-/twelve-month follow-ups are available through authorized incubatee profiles. Monthly email links are scoped, expiring, single-use tokens; only their digests are stored. Evidence is private and downloaded through permission-checked views. The profile timeline combines the dated records currently implemented; general interaction events will be added with the Interactions phase.

The monthly dispatch task runs daily and uses configurable settings: `INCUBATEE_CHECKIN_SEND_DAY` (default `1`), `INCUBATEE_CHECKIN_DEADLINE_DAY` (default `15`), `INCUBATEE_CHECKIN_STATUSES` (default `active`), and `INCUBATEE_CHECKIN_LINK_MAX_AGE_SECONDS` (default `2592000`). `INCUBATEE_CHECKIN_REMINDER_DAYS` is empty by default, so reminders are disabled until configured. These are technical defaults, not CFERI policy. SMTP configuration is required for live email; the test suite uses an isolated test email backend.

Run Celery worker and Beat in separate PowerShell windows after Redis is available:

```powershell
.\.venv\Scripts\Activate.ps1
celery -A config worker --loglevel=INFO --pool=solo
```

```powershell
.\.venv\Scripts\Activate.ps1
celery -A config beat --loglevel=INFO
```

Check-in timing, reminders, verification, follow-up recipients/delivery, metric formulas, completeness rules, and status/rating vocabularies require CFERI confirmation. See [OPEN_DECISIONS.md](docs/OPEN_DECISIONS.md). General interaction timeline entries are unavailable until INT-01–INT-04 are implemented.

Apply migrations and run the Phase 7 tests with:

```powershell
python manage.py migrate
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
python manage.py test incubation.tests.test_phase7 --settings=config.settings.test
python manage.py test --settings=config.settings.test
```

## Interactions and engagement

The interaction log is available at `/interactions/`. It supports meetings, mentoring, workshops, site visits, calls, emails, and events; filters; permission-checked links to incubatees and partners; stakeholder participants and standalone participant names; immutable edit history; soft deletion; event/workshop attendance; and owner-assigned follow-up tasks. Interaction access follows the seeded role matrix: Board Members have read-only access, CFERI Staff are limited to assigned records, Freelancers to their own permitted interactions, and System Admin has no interaction grant. Linked records and follow-up owners are checked against their own permissions.

Interactions require a date/time, type, and nonnegative whole-minute duration; zero minutes is accepted. Event/workshop attendance currently uses Registered, Attended, and Absent states, while tasks use Open, In progress, and Completed. These status and duration handling choices are provisional pending CFERI confirmation in [OPEN_DECISIONS.md](docs/OPEN_DECISIONS.md).

The incubatee timeline includes interactions linked to that incubatee only when the current viewer may read the interaction. Run the focused tests with:

```powershell
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
python manage.py test interactions.tests --settings=config.settings.test
```

## Dashboards, reporting, exports, and audit

Role dashboards are available under `/dashboard/`. The impact dashboard supports date filters and recorded breakdowns by cohort, sector, province, gender, and youth. Authorized XLSX/PDF downloads are available for impact reports, role dashboards, interactions, incubatee/check-in lists, partners/MOUs, stakeholders, centre documents and expiry/compliance lists, MOU renewal alerts, and audit logs. Board-only reports contain aggregates and suppress small cells; Board-only interaction views and exports withhold participant names and free-text topics/outcomes pending CFERI policy confirmation. The read-only JSON endpoint is `/dashboard/analytics.json` and requires an authenticated account with report access.

The audit log is available at `/dashboard/audit/` to Super Admin and System Admin readers. Audit events are captured for normal model create/update/delete operations, successful/failed logins, logout, role/permission changes, sensitive document access, and exports. Model and queryset APIs reject audit event modification/deletion. Django bulk operations bypass model signals and must not be used for audited business changes without explicit audit logging.

The monthly Celery summary runs on the first day of each month at 07:00 (Django timezone). It sends individualized previous-calendar-month summaries to active Admin and Board accounts, each computed using that recipient's permission scope. Configure SMTP, Redis, and worker/Beat processes as described above. These schedule and calculation details are technical defaults pending CFERI approval; see OD-07, OD-12, OD-17, and OD-33–OD-35 in [OPEN_DECISIONS.md](docs/OPEN_DECISIONS.md).

Apply migrations and run Phase 9 verification with:

```powershell
python manage.py migrate
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
python manage.py test reporting.tests documents.tests interactions.tests --settings=config.settings.test
python manage.py test --settings=config.settings.test
```

## Deployment settings

Set `DJANGO_SETTINGS_MODULE=config.settings.production` in the production environment and provide `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, PostgreSQL connection variables, and SMTP settings through environment variables. Production settings require HTTPS and enable secure session/CSRF cookies and HSTS. Run `python manage.py collectstatic --noinput` during deployment and serve collected static assets from `STATIC_ROOT`; uploaded/private files are stored separately under `var/private-media` and deliberately have no public media URL.

## UI libraries and exports

The shared templates use Bootstrap 5 and Chart.js. Excel exports use `openpyxl`; PDF reports use ReportLab.

## Requirements and decisions

- [Phase 10 acceptance report](docs/PHASE10_TEST_REPORT.md)
- [Security review](docs/PHASE10_SECURITY_REVIEW.md)
- [Deployment checklist](docs/DEPLOYMENT_CHECKLIST.md)
- [Requirements traceability](docs/REQUIREMENTS_TRACEABILITY.md)
- [Open decisions](docs/OPEN_DECISIONS.md)

Do not deploy publicly or populate the system with real CFERI data without explicit authorization and completion of the deployment/security checklist.
