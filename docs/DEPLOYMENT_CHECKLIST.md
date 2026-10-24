# UMP CFERI Data Reservoir Deployment Checklist

This checklist is operational guidance, not authorization to deploy. Do not expose the system publicly or load real CFERI data without explicit authorization.

## Environment and application

- [ ] Select a supported Python and Django version from `requirements.txt`; install dependencies in a locked, isolated environment.
- [ ] Set `DJANGO_SETTINGS_MODULE=config.settings.production`.
- [ ] Provide a unique, high-entropy `DJANGO_SECRET_KEY` through the deployment secret manager.
- [ ] Set `DJANGO_ALLOWED_HOSTS` to the exact production hostnames; do not use a wildcard.
- [ ] Provide PostgreSQL connection settings through a secret `DATABASE_URL` (Neon pooled URL supported; SSL is required and persistent connections default to off) or the `POSTGRES_*` variables; use a dedicated least-privilege application account.
- [ ] Set and securely back up `MFA_ENCRYPTION_KEY`; it must remain stable during rotations and restores.
- [ ] Configure SMTP credentials/sender and approved recipients for document/MOU alerts and scheduled summaries.
- [ ] Configure `REDIS_URL`; restrict Redis to trusted application hosts/network segments and require authentication/TLS where supported.
- [ ] Confirm production settings enable HTTPS, secure session/CSRF cookies, HSTS, and secure proxy handling appropriate to the actual reverse-proxy topology.
- [ ] Review logging destinations, access, retention, and personal-data handling; ensure logs do not contain secrets or unnecessary personal data.

## Database, migrations, and backup/recovery

- [ ] Provision a supported PostgreSQL database with encryption, network restrictions, monitoring, and automated backups.
- [ ] Confirm backup retention and point-in-time recovery objectives with the data owner.
- [ ] Take a verified backup before migrations or application upgrades.
- [ ] Apply migrations with `python manage.py migrate --settings=config.settings.production`.
- [ ] Run `python manage.py check --deploy --settings=config.settings.production` and review every warning.
- [ ] Run and record a restore rehearsal in an isolated environment; confirm schema, users, and application connectivity.
- [ ] Document database restore and recovery ownership. Example operator commands (use interactive authentication, not passwords embedded in shell history):

  ```powershell
  pg_dump -U <backup-user> -h <database-host> -Fc -f ump_cferi.dump ump_cferi
  createdb -U <restore-user> -h <restore-host> ump_cferi_restore
  pg_restore -U <restore-user> -h <restore-host> -d ump_cferi_restore --no-owner ump_cferi.dump
  ```

- [ ] Back up private uploaded media separately from public/static assets; test restoration and verify private permissions after restore.
- [ ] Keep static assets and private uploads in distinct storage locations. Never publish `PRIVATE_MEDIA_ROOT`.
- [ ] Verify any external/private storage backend's encryption, access policies, retention, and backup behavior before use.

## Accounts and authorization

- [ ] Create the first Super Admin only through an approved, interactive process; no default credentials are provided.
- [ ] Enroll and verify mandatory-role MFA, then confirm the approved lost-device/account-recovery procedure.
- [ ] Assign roles using least privilege and review the seeded resource/action/scope matrix.
- [ ] Confirm policy decisions in `OPEN_DECISIONS.md` before enabling affected access, reporting, retention, or email workflows.
- [ ] Test record-level sharing, sensitivity restrictions, expiry/deactivation, and soft-delete recovery using synthetic accounts.
- [ ] Verify no test or demonstration account contains real credentials or CFERI records.

## Files, exports, and privacy

- [ ] Verify private downloads require authentication and record-level permission checks.
- [ ] Verify allowed upload types, content validation, file size limits, and storage paths.
- [ ] Review access to founder identity/demographic data, stakeholder contact details, confidential documents, and Board reports.
- [ ] Confirm export permissions, output fields, filters, small-cell suppression, and audit events using synthetic data.
- [ ] Confirm access to backups and exports is restricted and retention/disposal is defined.

## Background work and email

- [ ] Start and monitor Celery workers and Celery Beat using supervised production services (not interactive terminals).
- [ ] Verify Redis connectivity, task retries/failure alerts, idempotency, clock/timezone, and scheduled-task registration.
- [ ] Test SMTP delivery and bounce/error visibility with approved non-personal test recipients.
- [ ] Confirm expiry/check-in/monthly-summary recipients and schedules with CFERI; technical defaults are not policy.
- [ ] Confirm tasks do not disclose sensitive record details to unauthorized recipients.

## Static assets, operations, and final acceptance

- [ ] Run `python manage.py collectstatic --noinput --settings=config.settings.production`.
- [ ] Configure the web server to serve static assets and never to serve private media directly.
- [ ] Configure TLS, security headers, request-size limits, timeouts, and rate limiting at the approved ingress.
- [ ] Configure monitoring for application errors, database/storage capacity, task queue backlog, email failures, and backup health.
- [ ] Complete the full test suite and acceptance checklist in `PHASE10_TEST_REPORT.md`.
- [ ] Review `PHASE10_SECURITY_REVIEW.md` and resolve findings before any public deployment.
- [ ] Obtain explicit CFERI authorization before public exposure or importing real CFERI personal/business data.
