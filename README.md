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
  -> recent items + private Tracks
  -> optional embeddings + candidate retrieval
  -> compatible Chat API relevance reranking
  -> top-item analysis + trend summary
  -> private Supabase matches/analyses/digest
  -> optional private email
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

Example private `TRACKS_JSON`:

```json
[{"name":"AI 工作流","goal":"关注能够改善知识工作和软件工作流、现在即可测试的 AI 产品；排除缺少差异化能力的简单套壳产品。"}]
```

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

Scheduled email delivery requires `DIGEST_TO` and one email provider. The free
default is QQ SMTP through `SMTP_USERNAME` and `SMTP_PASSWORD`; Resend remains
an optional fallback. `SERVERCHAN_SENDKEY` is optional and can be omitted when
you do not want public-account-based WeChat delivery. Put the QQ and work
addresses together in `DIGEST_TO`, for example
`name@qq.com,name@company.com`. The workflow fails clearly when a delivery
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
- `TRACKS_JSON`, email addresses, feedback, collected data, and digests
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
- X, Reddit, and LinkedIn are excluded due to access restrictions or cost.

## License

MIT. See `LICENSE`.
