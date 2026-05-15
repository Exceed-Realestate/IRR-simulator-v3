# Exceed IRR Simulator — AI + DLD Worker

Cloudflare Worker that holds **two sets of secrets** (Anthropic + data.dubai DDA) and serves five endpoints used by the simulator and the daily scraper:

**AI endpoints** (called from the browser):
- `POST /thesis` — 180–220-word investment thesis for the loaded property
- `POST /chat` — multi-turn Q&A grounded in the property context
- `POST /comparables` — property-specific comparable projects
- `POST /suggest-compare` — pick best B-side from a candidate list

**DLD proxy endpoints** (called from GitHub Actions runner):
- `POST /dld-health` — health-check passthrough to data.dubai
- `POST /dld-fetch` — paginated data fetch passthrough (entity + dataset)

The simulator is a static GitHub Pages site and the scraper runs on a US-based GH Actions runner. Neither can call data.dubai directly (the API is UAE-only). This Worker — with **Smart Placement** enabled in `wrangler.toml` — is geo-pinned to the edge nearest data.dubai (Dubai/UAE region), so it can reach the API on behalf of either caller.

## One-time setup (~10 minutes)

You'll need:
- A free Cloudflare account
- An Anthropic API key from https://console.anthropic.com/settings/keys

```bash
# 1. Install wrangler (Cloudflare's CLI) — one-time
npm install -g wrangler

# 2. Authenticate
wrangler login    # opens a browser

# 3. From this folder, paste your secrets:
cd cloudflare-worker

# 3a. Anthropic API key (for AI endpoints)
wrangler secret put ANTHROPIC_API_KEY
# (paste sk-ant-... and press Enter)

# 3b. data.dubai DDA credentials (for DLD proxy endpoints)
wrangler secret put DLD_APP_ID         # x-DDA-SecurityApplicationIdentifier (e.g. Q-gsyXKzaC8...)
wrangler secret put DLD_CLIENT_ID
wrangler secret put DLD_CLIENT_SECRET
wrangler secret put DLD_PROXY_SECRET   # shared secret between GH Action and Worker — invent any long random string

# 3c. (Optional) which environment — STG by default
wrangler secret put DLD_ENV            # "stg" or "prod"

# 4. Deploy
wrangler deploy
# It will print a public URL like:
#   https://exceed-irr-ai.<your-subdomain>.workers.dev
```

After deploying, also add these in the GitHub repo:

- **Repo secrets** (`Settings → Secrets and variables → Actions → Secrets`):
  - `DLD_PROXY_URL` = Worker URL from above
  - `DLD_PROXY_SECRET` = same long random string you set as a Worker secret in step 3b
- **Repo variables** (`Settings → Secrets and variables → Actions → Variables`):
  - `DLD_ENTITY` = the DLD entity name (e.g. `dld`) — get from data.dubai portal
  - `DLD_TX_DATASET` = transactions dataset name (e.g. `dld_transactions-open`)
  - `DLD_RENT_DATASET` = rentals dataset name (e.g. `dld_rental_contracts-open`)

## Wire it to the simulator

1. Open https://exceed-realestate.github.io/IRR-simulator-v3/
2. Click the **✨ AI** button in the header → **Configure Worker URL**
3. Paste the URL from `wrangler deploy`
4. Save. The simulator stores the URL in your browser (localStorage); each agent does this once per device.

## Verify it works

After saving the URL, load any preset and click **✨ Generate Thesis** in the Area Analysis card. You should see a 180–220 word narrative in a few seconds. If it errors, check the Worker logs:

```bash
wrangler tail
```

## Costs

- Cloudflare Workers free tier: 100,000 requests/day — way beyond agent usage
- Anthropic API: ~$0.015 per thesis (Opus 4.7, ~1K input + 1K output tokens). Each chat reply ~$0.01. Daily team usage of 100 calls ≈ $1.50/day.

## CORS / security

The Worker's `ALLOWED_ORIGINS` array restricts which hosts can call it. By default it allows `https://exceed-realestate.github.io` and localhost. To allow other origins, edit `worker.js` and re-deploy.

## Endpoints

### `POST /thesis`

Request body:
```json
{
  "unit":   { "name": "Six Senses Palm 3BR", "price": 32000000, "sqft": 6151, ... },
  "area":   { "nameEN": "Palm Jumeirah", "tier": "ultra-prime", ... },
  "result": { "irr": 0.087, "totalReturn": 0.21, "cashOnCashPct": 6.2, ... },
  "lang":   "ja"
}
```

Response:
```json
{ "thesis": "...180–220 words...", "model": "claude-opus-4-7" }
```

### `POST /chat`

Request body:
```json
{
  "unit":     { ... },
  "area":     { ... },
  "result":   { ... },
  "history":  [ { "role": "user", "content": "..." }, { "role": "assistant", "content": "..." } ],
  "question": "What if the client wants to leave UAE after 3 years?",
  "lang":     "ja"
}
```

Response:
```json
{ "reply": "...under 150 words...", "model": "claude-opus-4-7" }
```
