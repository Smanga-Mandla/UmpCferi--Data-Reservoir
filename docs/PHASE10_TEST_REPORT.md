# Phase 10 — Integration and SRS Acceptance Test Report

**Project:** UMP CFERI Data Reservoir  
**Baseline:** System Requirements Specification dated 20 October 2026  
**Latest revalidation:** 22 October 2026  
**Execution environment:** Django test settings with isolated in-memory SQLite and test email backend. Tests use synthetic records.  
**Acceptance result:** The updated application tests pass, including STK-04 assignment controls and the local INC-06 email/link/submission flow. This is not production acceptance: live PostgreSQL, SMTP, Redis/Celery Beat, browser viewport behavior, and backup restoration were not exercised.

## Automated verification

| Command | Result |
|---|---|
| `python manage.py test --settings=config.settings.test --verbosity 1` | **Passed:** 112 tests run: 111 passed and 1 skipped. The skipped test is the opt-in live PostgreSQL connectivity probe. |
| `python manage.py test accounts.tests.test_auth_access accounts.tests.test_configuration incubation.tests.test_incubatees reporting.tests.test_reporting --settings=config.settings.test --verbosity 1` | **Passed:** 61 focused regression tests; covers privilege escalation, production URL validation, Board aggregate privacy, and formula-safe XLSX output. |
| `python manage.py test stakeholders.tests.test_directory incubation.tests.test_phase7 --settings=config.settings.test --verbosity 1` | **Passed:** 13 tests; includes staff-profile assignment lifecycle/access and local check-in task-email-link-submission integration. |
| `python manage.py test accounts.tests.test_auth_access stakeholders.tests.test_directory incubation.tests.test_phase7 --settings=config.settings.test --verbosity 1` | **Passed:** 43 tests; includes denied permanent deletion for Super Admin, soft-delete recovery, staff-incubatee assignment access/history, and local check-in integration. |
| `python manage.py check --settings=config.settings.test` | **Passed:** no issues identified. |
| `python manage.py check --deploy --settings=config.settings.production` | **Passed:** no deployment security warnings with temporary synthetic environment values, including an HTTPS public base URL. This does not verify real production configuration. |
| `python manage.py makemigrations --check --dry-run --settings=config.settings.test` | **Passed:** no model changes detected. |

The Django test runner creates a test database and applies project migrations as part of the suite. The above does not prove migration behavior against a production PostgreSQL instance.

## Acceptance checklist

| # | Area | Verification evidence | Result and remaining limitation |
|---:|---|---|---|
| 1 | Authentication and 2FA | `accounts/tests/test_auth_access.py`, including mandatory-role MFA tests | Automated pass; live SMTP reset delivery and approved MFA recovery procedures remain unverified/open. |
| 2 | Seven-role permissions | `accounts/tests/test_auth_access.py`, `accounts/tests/test_configuration.py`, `accounts/tests/test_dashboard_access.py` | Automated pass for seeded role grants and protected routes. |
| 3 | Multiple-role access | `accounts/tests/test_auth_access.py` | Automated pass for combined role grants; higher-priority sensitivity rules remain enforced. |
| 4 | Sensitive document restrictions | `documents/tests/test_vault.py`, `accounts/tests/test_auth_access.py` | Automated pass for role/sensitivity checks and System Admin restrictions. |
| 5 | Record-level sharing | `accounts/tests/test_auth_access.py`, `documents/tests/test_vault.py` | Automated pass for explicit shares and restrictions that shares cannot override. |
| 6 | Password reset and lockout | `accounts/tests/test_auth_access.py` | Automated pass for expiry, failed attempts, and lock duration. |
| 7 | Session timeout and account expiry | `accounts/tests/test_auth_access.py` | Automated pass for inactivity and expired-account access; real scheduled deactivation was not exercised on Celery Beat. |
| 8 | Centre document uploads and versions | `documents/tests/test_vault.py` | Automated pass for file validation, retrieval, replacement, version history, and access checks. |
| 9 | Compliance document expiry alerts | `documents/tests/test_vault.py` | Task/dashboard/email-backend tests pass; live SMTP and deployed scheduling are unverified. Missing-document status policy remains open. |
| 10 | Partner and MOU management | `partners/tests/test_partners_mous.py` | Automated pass for CRUD, history, representatives, attachments, and access controls. |
| 11 | MOU folders and renewal alerts | `partners/tests/test_partners_mous.py` | Folder creation and 90/60/30-day alert logic are tested; live email delivery is unverified. |
| 12 | Stakeholder records and consent | `stakeholders/tests/test_directory.py` | Automated pass for duplicate warnings, shared records, relationship history, consent history, restricted fields, and STK-04 staff-profile assignment access/history. |
| 13 | Incubatee onboarding | `incubation/tests/test_incubatees.py` | Automated pass for baseline persistence, validation, founder protection, and import behavior. CFERI-required field rules remain open. |
| 14 | Programme, cohort, mentor assignments | `incubation/tests/test_incubatees.py` | Automated pass for programme/cohort, staff owner, and mentor assignments. Operational staff/freelancer assignment policy remains open. |
| 15 | Monthly check-ins and notifications | `incubation/tests/test_phase7.py::test_monthly_secure_link_email_idempotence_submission_and_history` | Celery task wrapper executed locally; test email captured, time-limited link opened, check-in submitted, repeat dispatch idempotent. Live SMTP, Redis, and Beat operation are unverified. |
| 16 | Quarterly metrics and milestones | `incubation/tests/test_phase7.py` | Automated pass for quarterly reviews, observations, milestones, mentoring, evidence access, and duplicate prevention. Some rating/status vocabularies remain open. |
| 17 | Interactions and engagement logs | `interactions/tests/test_interactions.py` | Automated pass for CRUD, participants, permissions, follow-up tasks, and attendance. |
| 18 | Dashboard calculations | `reporting/tests/test_reporting.py` | Automated pass for stored-record metrics, privacy suppression, role dashboards, and data-quality behavior. Provisional formulas/thresholds remain open. |
| 19 | Excel and PDF exports | `reporting/tests/test_reporting.py` | Automated pass for XLSX/PDF exports and access filtering across reporting/list surfaces. Browser download UX and external spreadsheet/PDF readers were not tested. |
| 20 | Audit logging and soft deletes | `reporting/tests/test_reporting.py`, `accounts/tests/test_auth_access.py`, and module tests | Automated pass for application-level append-only audit APIs and recoverable deletion. Database-owner tamper resistance requires production controls. |
| 21 | Data validation and duplicate detection | `accounts/tests/test_auth_access.py`, `documents/tests/test_vault.py`, `incubation/tests/test_incubatees.py`, `stakeholders/tests/test_directory.py` | Automated pass for covered validation and duplicate checks; official controlled vocabularies remain subject to open decisions. |
| 22 | Responsive interface and navigation | `accounts/tests/test_configuration.py`; Django system check | Core templates render and responsive Bootstrap markup is present. No real-browser, mobile viewport, accessibility, or visual-regression test was run. |
| 23 | Database migrations | Full suite applies migrations; `makemigrations --check --dry-run` | Automated SQLite migration application passed; no uncommitted model migration is needed. Live PostgreSQL migration/upgrade verification remains outstanding. |
| 24 | Scheduled background tasks | `documents/tests/test_vault.py`, `partners/tests/test_partners_mous.py`, `incubation/tests/test_phase7.py`, `reporting/tests/test_reporting.py` | Task logic and email paths are tested with mocks/test backend. A live Redis worker/Beat schedule and delivery were not verified. |
| 25 | Backup and recovery | `docs/DEPLOYMENT_CHECKLIST.md` | Instructions/checklist provided; no backup, restore, or recovery rehearsal was performed. |

## Functional scope with known incomplete requirements

See the acceptance matrix in `REQUIREMENTS_TRACEABILITY.md` for each requirement ID, its status, source files, associated test, result, and remaining issues. At acceptance time:

- **Not implemented:** AUTH-09 (SSO) and MOU-08 (incubatee-to-MOU benefit links).
- **Partially implemented:** DOC-09 (missing-document determination) and INC-09 (external follow-up survey delivery).
- **Implemented but not verified against live services:** INC-06 SMTP/Redis/Celery Beat delivery and other integrations requiring live PostgreSQL, SMTP, Redis/Celery Beat, production media storage, or real browser/device testing.
- Open CFERI policy decisions are not silently resolved. They remain in `OPEN_DECISIONS.md`.

## Reproduction commands

Run from the project root in PowerShell:

```powershell
python manage.py test --settings=config.settings.test --verbosity 1
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
```
