# AI Trend Tracker

An open-source, serverless-friendly tracker for new AI products and technical
signals. It collects public data, matches it against private natural-language
Tracks, and writes a personalized daily digest to a private Supabase database.

The default deployment uses GitHub Actions + Supabase. There is no always-on
server, and the public repository does not need to contain personal data.

## What is included

- Product Hunt RSS, Hacker News, GitHub, Hugging Face, arXiv, and custom RSS
- Idempotent Supabase upserts and cross-source fingerprint deduplication
- Private natural-language Tracks stored in Supabase
- Invite-only design-partner users with separate goals, ranking, email, and digest
- Signed email feedback actions: 有用、不相关、继续深挖
- Track-scoped soft preference learning from prior feedback
- Optional multilingual embedding retrieval
- Provider-neutral, OpenAI-compatible LLM reranking and analysis
- Up to three deeper analyses per Track and day
- Lightweight Chinese digest: 30-second conclusion, three highlights, trend radar,
  and a compact list of additional related signals
- Private digest storage, optional Resend email, and opt-in artifacts
- Automatic retention cleanup for the Supabase free database
- No third-party Python runtime dependencies

## Architecture

```text
GitHub Actions collect (every 4 hours)
  -> public APIs/RSS
  -> normalize + deduplicate
  -> private Supabase items

GitHub Actions digest (daily)
  -> recent items + each invitee's private natural-language Tracks
  -> compile raw goals into a stable matching brief
  -> optional embeddings + candidate retrieval
  -> compatible Chat API relevance reranking + prior feedback
  -> top-item analysis + trend summary
  -> private Supabase matches/analyses/per-user digest
  -> one private email per invitee
  -> signed feedback page + Supabase Edge Function
  -> retention cleanup
```

## Model roles

The tracker has three **roles**, not necessarily three different LLMs:

1. `EMBEDDING_MODEL` converts Tracks and items into vectors. It is an embedding
   model, not a chat LLM.
2. `RANKING_MODEL` scores candidate relevance and explains the score.
3. `ANALYSIS_MODEL` writes deeper analyses and trend syntheses.

The ranking and analysis roles may use the same model. Embeddings can come from
a different provider. For example, DeepSeek can handle ranking/analysis while
GLM Embedding-3 handles multilingual retrieval.

Without an embedding provider, the tracker still runs with lexical candidate
retrieval followed by LLM reranking, but Chinese Track-to-English-item recall
will usually be weaker.

## 1. Create or upgrade the Supabase schema

For a new project, open **SQL Editor** and run:

`supabase/migrations/001_initial.sql`

If `001_initial.sql` was already run before provider-neutral support was added,
also run:

`supabase/migrations/002_public_provider_compat.sql`

The second migration clears old 768-dimensional embeddings, changes the schema
to 512 dimensions, creates the private `digests` table, and updates cleanup.
Embeddings are regenerated automatically on the next digest run.

To enable the invite-only beta, run this migration after the first two:

`supabase/migrations/003_design_partner_beta.sql`

It is additive: existing items, Tracks, matches, analyses, digests, and personal
delivery data are preserved. It adds beta users, user-scoped Tracks/digests,
structured goals, feedback events, and digest exposure records. Once this code
is deployed, migration 003 is required even if personal mode is retained,
because the application reads the new Track columns.

All application tables use RLS without anonymous policies. GitHub Actions uses
the server-side Supabase secret key; never expose that key in frontend code.

## 2. Configure a public GitHub repository safely

The code can live in a public repository. Store the following under
**Settings -> Secrets and variables -> Actions**.

### Repository secrets

| Secret | Required | Meaning |
|---|---|---|
| `SUPABASE_URL` | yes | Supabase project URL |
| `SUPABASE_SECRET_KEY` | yes | Server-only `sb_secret_...` key |
| `SUPABASE_SERVICE_ROLE_KEY` | no | Legacy Supabase fallback |
| `CHAT_API_KEY` | recommended | DeepSeek, GLM, or another compatible Chat API key |
| `EMBEDDING_API_KEY` | recommended | Compatible embedding API key; may equal the chat key |
| `TRACKS_JSON` | recommended | Private JSON array of initial Tracks |
| `BETA_USERS_JSON` | beta | Private invite list, email addresses, and natural-language Tracks |
| `FEEDBACK_SIGNING_SECRET` | beta feedback | Random secret also configured in the Supabase Function |
| `SMTP_USERNAME` | delivery | QQ mailbox used to send the digest |
| `SMTP_PASSWORD` | delivery | QQ Mail SMTP authorization code, not the login password |
| `RESEND_API_KEY` | optional | Resend fallback when SMTP is not configured |
| `DIGEST_FROM` | delivery | Sender address; for QQ SMTP this can equal `SMTP_USERNAME` |
| `DIGEST_TO` | delivery | Comma/semicolon-separated recipients, such as QQ and work email |
| `SERVERCHAN_SENDKEY` | optional | ServerChan SendKey when WeChat delivery is desired |
| `OPENAI_API_KEY` | no | Legacy OpenAI-only fallback |

### Repository variables

Variables are not secrets; they only select endpoints and models.

| Variable | Example |
|---|---|
| `CHAT_BASE_URL` | `https://api.deepseek.com` |
| `RANKING_MODEL` | `deepseek-v4-flash` |
| `ANALYSIS_MODEL` | `deepseek-v4-pro` |
| `EMBEDDING_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4` |
| `EMBEDDING_MODEL` | `embedding-3` |
| `EMBEDDING_DIMENSIONS` | `512` |
| `SMTP_HOST` | `smtp.qq.com` (also the code default) |
| `SMTP_PORT` | `465` (also the code default) |
| `UPLOAD_REPORT_ARTIFACT` | leave unset or `false` in public repositories |
| `OUTPUT_LANGUAGE` | `zh-CN` |
| `REPORT_TIMEZONE` | `Asia/Shanghai` |
| `REPORT_HIGHLIGHT_ITEMS` | `3` detailed highlights per Track |
| `REPORT_QUICK_ITEMS` | `12` one-line related signals per Track |
| `REPORT_RELEVANCE_THRESHOLD` | `50` |
| `REPORT_SHOW_SCORES` | `false`; use reader-friendly relevance labels |
| `FORCE_DIGEST` | leave `false`; manual recovery only |
| `BETA_MAX_USERS` | `20`; intentional safety/cost cap |
| `BETA_MAX_TRACKS_PER_USER` | `3`; per-user cost cap |
| `FEEDBACK_PAGE_URL` | GitHub Pages URL ending in `/feedback.html` |
| `FEEDBACK_API_URL` | Supabase URL ending in `/functions/v1/feedback` |

Example private `TRACKS_JSON`:

```json
[{"name":"AI 工作流","goal":"关注能够改善知识工作和软件工作流、现在即可测试的 AI 产品；排除缺少差异化能力的简单套壳产品。"}]
```

### Invite the first design partners

Put this in the `BETA_USERS_JSON` Actions secret. It is the owner-managed source
of truth during the concierge beta. Editing a natural-language `goal` and
running Digest updates that user's goal and recompiles its matching brief.

```json
[
  {
    "email": "you@example.com",
    "display_name": "Jerry",
    "timezone": "Asia/Shanghai",
    "wechat_enabled": true,
    "tracks": [
      {
        "name": "AI 产品与工作流",
        "goal": "关注能帮助 AI 产品经理发现创业机会、改善研究和产品工作流，并且两周内可低成本验证的新 AI 产品；排除单纯模型套壳、没有真实用户证据的项目。"
      }
    ]
  },
  {
    "email": "friend@example.com",
    "display_name": "种子用户 A",
    "timezone": "Asia/Shanghai",
    "tracks": [
      {
        "name": "AI Coding",
        "goal": "关注能显著改善中小团队软件交付效率的新 coding agent、评测方法和开发者工具，优先可立即试用的产品。"
      }
    ]
  }
]
```

When `BETA_USERS_JSON` is non-empty, beta mode takes precedence over
`TRACKS_JSON`. Include your own address in the beta list if you also want a
personal copy. Removing a person from the JSON does not delete historical data;
the next run sets that person inactive and pauses delivery.

At most one person may set `wechat_enabled` to `true`; that preserves the
owner's existing ServerChan delivery without sending invitees' private digests
to the owner's WeChat.

This phase deliberately has no public signup, password, billing, or user-facing
goal editor. The owner interviews and onboards a small number of users, then
updates the secret. This keeps support and privacy manageable while validating
digest quality and retention.

### Enable one-click feedback

The email shows feedback on detailed highlights and deliberately has no
“已经知道” action. `有用` is a positive signal, `不相关` is negative, and
`继续深挖` is a strong positive preference plus a product-demand signal.
Feedback never silently rewrites the explicit goal. The next ranking run sends
examples from the same Track to the model only as soft evidence.

1. Generate a random value, for example:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. Add it as GitHub Actions secret `FEEDBACK_SIGNING_SECRET`.
3. Deploy the function with the same value:

   ```bash
   supabase login
   supabase link --project-ref YOUR_PROJECT_REF
   supabase secrets set FEEDBACK_SIGNING_SECRET=YOUR_RANDOM_VALUE
   supabase functions deploy feedback
   ```

   The function uses the signed, expiring token as authentication. Invitees do
   not need Supabase accounts, and the function verifies Track ownership.
   To avoid Terminal, use Supabase **Edge Functions → Deploy a new function →
   Via Editor**, name it `feedback`, and paste
   `supabase/functions/feedback/index.ts`. Turn off the function's built-in
   **Verify JWT** setting, then add `FEEDBACK_SIGNING_SECRET` under Edge Function
   Secrets. The repository's `supabase/config.toml` applies the same setting
   when deploying through the CLI.
4. In GitHub repository **Settings → Pages**, publish the `/docs` folder from
   the `main` branch.
5. Add repository variable `FEEDBACK_PAGE_URL`, for example
   `https://YOUR_NAME.github.io/ai-trend-tracker/feedback.html`.
6. Add repository variable `FEEDBACK_API_URL`, for example
   `https://YOUR_PROJECT.supabase.co/functions/v1/feedback`.

The token is placed after `#` in the email link, so email security scanners do
not submit feedback merely by checking the URL.

### Provider examples

DeepSeek chat + GLM embeddings:

```text
CHAT_BASE_URL=https://api.deepseek.com
RANKING_MODEL=deepseek-v4-flash
ANALYSIS_MODEL=deepseek-v4-pro
EMBEDDING_BASE_URL=https://open.bigmodel.cn/api/paas/v4
EMBEDDING_MODEL=embedding-3
EMBEDDING_DIMENSIONS=512
```

GLM for all three roles:

```text
CHAT_BASE_URL=https://open.bigmodel.cn/api/paas/v4
RANKING_MODEL=glm-4.7-flash
ANALYSIS_MODEL=glm-5-turbo
EMBEDDING_BASE_URL=https://open.bigmodel.cn/api/paas/v4
EMBEDDING_MODEL=embedding-3
EMBEDDING_DIMENSIONS=512
```

Set both `CHAT_API_KEY` and `EMBEDDING_API_KEY` to the GLM key in the second
example. Model availability changes over time, so select currently available
model IDs in the provider console rather than editing source code.

## 3. Enable workflows

- `.github/workflows/collect.yml`: every four hours and manual dispatch
- `.github/workflows/digest.yml`: primary run at 06:53 and fallback run at 07:53
  Asia/Shanghai, plus manual dispatch. GitHub schedules are not exact-time
  guarantees, so the second run retries only missing channels. Supabase delivery
  metadata prevents duplicate daily email and WeChat sends.

Run **Collect AI signals** once before **Generate AI trend digest**.

Personal mode requires `DIGEST_TO` and one email provider. Beta mode uses each
email in `BETA_USERS_JSON`, so `DIGEST_TO` may be empty. The free default is QQ
SMTP through `SMTP_USERNAME` and `SMTP_PASSWORD`; Resend remains an optional
fallback. `SERVERCHAN_SENDKEY` is optional and never used for ordinary beta
invitees. Put QQ and work addresses
together in personal-mode `DIGEST_TO`, for example
`name@qq.com,name@company.com`. In beta mode, ServerChan is used only for the
single owner marked with `wechat_enabled: true`. The workflow fails clearly when a delivery
secret is missing instead of silently generating a database-only report.
Email and WeChat delivery are independent and each is retried up to three times.
If a manual recovery must deliberately send a second digest on the same day,
run the workflow with the `force` input enabled.

Reports are saved in the private Supabase `digests` table. View the latest one:

```sql
select generated_at, content
from public.digests
order by generated_at desc
limit 1;
```

Inspect beta delivery and feedback privately:

```sql
select u.email, d.generated_at, d.metadata->'delivery' as delivery
from public.digests d
join public.beta_users u on u.id = d.beta_user_id
order by d.generated_at desc;

select u.email, t.name, f.value, i.title, f.updated_at
from public.feedback f
join public.beta_users u on u.id = f.beta_user_id
join public.tracks t on t.id = f.track_id
join public.items i on i.id = f.item_id
order by f.updated_at desc;

select u.email, t.name, t.goal, t.compiled_goal, t.goal_spec
from public.tracks t
join public.beta_users u on u.id = t.beta_user_id
where u.active and t.active
order by u.created_at, t.created_at;
```

Artifact upload is disabled unless the repository variable
`UPLOAD_REPORT_ARTIFACT` is explicitly set to `true`. Do not enable it for a
public repository when Track definitions or reports are private.

## 4. Run locally

Python 3.9+ is supported.

```bash
cp .env.example .env
PYTHONPATH=src python3 -m trend_tracker seed-tracks
PYTHONPATH=src python3 -m trend_tracker collect
PYTHONPATH=src python3 -m trend_tracker digest
```

Inspect collectors without writing to Supabase:

```bash
PYTHONPATH=src python3 -m trend_tracker collect --dry-run
```

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Public/private boundary

Safe to publish:

- source code, workflows, migrations, prompt templates, source configuration
- `.env.example`, documentation, and tests

Keep private:

- API keys and Supabase secret keys
- `TRACKS_JSON`, `BETA_USERS_JSON`, `FEEDBACK_SIGNING_SECRET`
- email addresses, feedback, collected data, and digests
- Actions artifacts containing personalized reports

Never commit `.env`. If a credential reaches Git history, revoke and replace it;
deleting the file in a later commit is not sufficient.

## Source configuration

Edit `config/sources.json`. Built-in types are `rss`, `hackernews`, `github`,
`huggingface`, and `arxiv`. Product Hunt uses its official RSS feed. GitHub and
Hugging Face queries can be narrowed after observing the first reports.

## Data retention

The daily cleanup keeps:

- fetch logs for 30 days
- low-score matches for 60 days
- private digests for 90 days
- analytics events for 180 days
- embeddings without a high-quality match for 90 days
- unreferenced low-value items for 180 days
- high-quality matches, analyses, Tracks, and feedback

Check storage:

```sql
select public.trend_tracker_storage_status();
```

## Current MVP boundaries

- Trend summaries synthesize collected signals; they are not guaranteed forecasts.
- Product merging is fingerprint-based, not full entity resolution.
- Deep analysis currently uses collected metadata and links; it does not yet
  perform separate website research.
- Goal editing is owner-managed through `BETA_USERS_JSON`; a self-service
  authenticated frontend is deferred until the design-partner loop validates
  repeat usage.
- X, Reddit, and LinkedIn are excluded due to access restrictions or cost.

## License

MIT. See `LICENSE`.
