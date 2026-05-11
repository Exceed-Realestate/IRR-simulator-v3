# Exceed IRR Simulator — AI Worker

Cloudflare Worker that holds the Anthropic API key and serves two endpoints used by the simulator:

- `POST /thesis` — generates a 180–220 word investment thesis for the loaded property
- `POST /chat` — answers an agent's client-question, grounded in the loaded property + area data

The simulator is a static GitHub Pages site. It can't hold an API key in the browser without leaking it. This Worker is the secure middleman.

## One-time setup (~10 minutes)

You'll need:
- A free Cloudflare account
- An Anthropic API key from https://console.anthropic.com/settings/keys

```bash
# 1. Install wrangler (Cloudflare's CLI) — one-time
npm install -g wrangler

# 2. Authenticate
wrangler login    # opens a browser

# 3. From this folder, paste the Anthropic API key as a Worker secret
cd cloudflare-worker
wrangler secret put ANTHROPIC_API_KEY
# (it prompts; paste sk-ant-... and press Enter — the value is hidden from logs)

# 4. Deploy
wrangler deploy
# It will print a public URL like:
#   https://exceed-irr-ai.<your-subdomain>.workers.dev
```

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
