# Phase 10 — Security Review

**Scope:** Repository-level review of Django authentication/access control, dashboards, exports, check-in link configuration, uploads/downloads, sessions, audit behavior, secrets, and deployment configuration.  
**Review:** Read-only security review followed by remediation and regression testing.  
**Outcome:** Four code-level findings were identified and fixed. The latest 112-test suite passes with regression coverage; STK-04 assignment controls now also enforce incubatee-level permissions and retain ended assignment history.

## Verification run

- Focused regression modules for account security/configuration, incubatee access, and reporting exports: **61 tests passed**.
- `python manage.py check --deploy --settings=config.settings.production`: **passed with no warnings** using temporary synthetic settings and an HTTPS public base URL. This is a settings-level check only, not a production deployment audit.
- Production URL validation regression confirms HTTP and credential-bearing URLs are rejected.
- Targeted STK-04/INC-06 integration tests: **13 passed**; test coverage includes rejecting inaccessible Restricted incubatees during staff-profile assignment.
- Targeted auth/retention/STK-04/INC-06 tests: **43 passed**, including direct model deletion and the shared permanent-delete helper denying Super Admin hard deletes.
- Full suite recorded in the acceptance report: **112 tests run, 111 passed, 1 opt-in PostgreSQL test skipped**.

## Findings and disposition

| Finding | Severity | Status | Remediation | Verification |
|---|---|---|---|---|
| System Admin could grant Super Admin privileges through invitation or role-edit forms, and manage Super Admin accounts. | High | Fixed | Super Admin role assignment is limited to Super Admin actors; target Super Admin/superuser accounts cannot be edited or deactivated by System Admin. Enforcement is server-side and the form limits offered roles. | `accounts/tests/test_auth_access.py::test_system_admin_cannot_escalate_users_to_super_admin` passed. |
| Board-only incubatee list aggregates included Restricted records and exposed unsuppressed small counts. | Medium | Fixed | Aggregate population excludes Restricted records; total and status cells below the existing technical minimum of five are suppressed. | `incubation/tests/test_incubatees.py::test_board_aggregate_list_excludes_restricted_records_and_suppresses_small_counts` passed. |
| XLSX exports could interpret user-controlled text as spreadsheet formulas. | Medium | Fixed | The shared workbook writer explicitly serializes text cells as string values. | `reporting/tests/test_reporting.py::test_xlsx_export_writes_untrusted_formula_values_as_text` passed. |
| Check-in links could be configured with an externally reachable HTTP public base URL in production. | Medium | Fixed | Production settings now require `DJANGO_PUBLIC_BASE_URL` to be HTTPS and reject credentials, query strings, and fragments. HTTP remains available for local development settings. | `accounts/tests/test_configuration.py::test_production_public_base_url_requires_https_and_rejects_credentials` passed; production settings check passed with HTTPS and correctly rejected HTTP. |

## Areas not verifiable from repository code

- Actual TLS termination, reverse-proxy trust configuration, hostnames, and production environment variables.
- PostgreSQL encryption, network restrictions, privileges, backup schedule, and restore success.
- Private media filesystem/object-storage permissions, encryption, and backup/restore behavior.
- Redis authentication/TLS/network restrictions and live worker/Beat supervision.
- SMTP sender/recipient authorization, live delivery, and failure/bounce handling.
- Browser-level CSRF behavior, accessibility, mobile layout, and end-user workflow.
- Database-owner tampering with audit events. Application model/queryset APIs enforce append-only behavior, but database administrators can modify stored rows unless operational/database controls prevent it.

These limitations are deployment gates, not verified protections. See `DEPLOYMENT_CHECKLIST.md`. Do not deploy publicly or populate the system with real CFERI data without authorization.

## Retention safeguard

Following the user's selected interim direction, permanent deletion is now denied for all roles, including Super Admin, by both the common access helper and the access-controlled model deletion method. Soft deletion and the 90-day recovery period remain unchanged. This preserves business history, audit records, and evidence while CFERI develops and approves a retention schedule. The SRS permanent-delete privilege must not be re-enabled until that schedule and legal-hold policy are approved.
