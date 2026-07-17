# Security policy

Please do not report exposed credentials or other sensitive data in a public
issue. Revoke the affected credential first, then contact the repository owner
privately through the security advisory feature.

## Deployment boundaries

- Keep API keys, Supabase secret keys, Track definitions, email addresses, and
  other personal configuration in GitHub Actions Secrets.
- Never commit `.env`; it is excluded by `.gitignore`.
- Reports are stored in the private Supabase `digests` table by default.
- Do not enable `UPLOAD_REPORT_ARTIFACT` in a public repository unless the
  report content is intentionally public.
- Review workflow changes before merging contributions because workflows can
  access repository secrets on trusted scheduled or manually dispatched runs.
