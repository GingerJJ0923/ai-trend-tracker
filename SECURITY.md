# Security policy

Please do not report exposed credentials or other sensitive data in a public
issue. Revoke the affected credential first, then contact the repository owner
privately through the security advisory feature.

## Deployment boundaries

- Keep API keys, Supabase secret keys, Track definitions, email addresses, and
  other personal configuration in GitHub Actions Secrets.
- Keep `BETA_USERS_JSON` and `FEEDBACK_SIGNING_SECRET` in Actions Secrets. The
  signing secret must also be stored as a Supabase Function secret, never in
  repository variables or frontend JavaScript.
- Never commit `.env`; it is excluded by `.gitignore`.
- Reports are stored in the private Supabase `digests` table by default.
- Do not enable `UPLOAD_REPORT_ARTIFACT` in a public repository unless the
  report content is intentionally public.
- Review workflow changes before merging contributions because workflows can
  access repository secrets on trusted scheduled or manually dispatched runs.
- Public-repository Actions logs are public. Keep log messages anonymous and do
  not print parsed beta configuration, recipients, goals, or report content.
- The public feedback function must remain scoped by signed, expiring tokens and
  verify that each Track belongs to the token's beta user before writing.
