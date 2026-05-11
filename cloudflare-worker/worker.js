/**
 * Exceed IRR Simulator — AI Assistant Cloudflare Worker
 *
 * Holds the ANTHROPIC_API_KEY secret and proxies requests from the simulator
 * to the Claude API. Two endpoints:
 *
 *   POST /thesis    — generates a 200-word investment thesis for the loaded
 *                     property + area context.
 *   POST /chat      — multi-turn Q&A about the loaded property (used by the
 *                     in-simulator client-question chat box).
 *
 * Deploy:
 *   1. wrangler login
 *   2. wrangler secret put ANTHROPIC_API_KEY    # paste your sk-ant-...
 *   3. wrangler deploy
 *
 * After deploy, paste the worker URL (e.g. https://exceed-irr-ai.your-subdomain.workers.dev)
 * into the simulator settings — the simulator stores it in localStorage and
 * starts calling your Worker for AI features.
 */

const ALLOWED_ORIGINS = [
  "https://exceed-realestate.github.io",
  "http://localhost",
  "http://127.0.0.1"
];

const MODEL = "claude-opus-4-7";  // latest as of session date
const MAX_TOKENS = 1024;

function corsHeaders(origin) {
  const ok = origin && ALLOWED_ORIGINS.some(a => origin.startsWith(a));
  return {
    "Access-Control-Allow-Origin": ok ? origin : ALLOWED_ORIGINS[0],
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400"
  };
}

async function callClaude(env, systemPrompt, messages) {
  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": env.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01"
    },
    body: JSON.stringify({
      model: MODEL,
      max_tokens: MAX_TOKENS,
      system: systemPrompt,
      messages
    })
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Claude API ${resp.status}: ${text}`);
  }
  const data = await resp.json();
  return data?.content?.[0]?.text ?? "";
}

function buildContext(payload) {
  const p = payload || {};
  const u = p.unit || {};
  const a = p.area || {};
  const r = p.result || {};
  const lang = p.lang === "ja" ? "Japanese" : "English";
  const parts = [];
  parts.push(`Language: respond in ${lang}.`);
  if (u.name) parts.push(`Property: ${u.name}`);
  if (u.price) parts.push(`Price: AED ${(u.price/1_000_000).toFixed(2)}M`);
  if (u.sqft) parts.push(`Size: ${u.sqft.toLocaleString()} sqft (AED ${Math.round(u.price/u.sqft).toLocaleString()}/sqft)`);
  if (u.propertyType) parts.push(`Type: ${u.propertyType}`);
  if (u.purchaseMode) parts.push(`Mode: ${u.purchaseMode}`);
  if (u.holdYears != null) parts.push(`Holding period: ${u.holdYears} years`);
  if (u.useMortgage && u.ltv) parts.push(`Mortgage: ${u.ltv}% LTV at ${u.mortgageRate}%`);
  if (r.irr != null) parts.push(`Projected IRR: ${(r.irr*100).toFixed(1)}%`);
  if (r.totalReturn != null) parts.push(`Projected total return: ${(r.totalReturn*100).toFixed(0)}%`);
  if (r.netSaleProceeds != null) parts.push(`Net at exit: AED ${Math.round(r.netSaleProceeds/1_000_000)}M`);
  if (r.cashOnCashPct != null) parts.push(`Cash-on-cash: ${r.cashOnCashPct.toFixed(1)}%`);
  if (a.nameEN) parts.push(`Area: ${a.nameEN}${a.tier ? ` (${a.tier})` : ""}`);
  if (a.avgPriceSqftAed) parts.push(`Area avg AED/sqft: apt ${a.avgPriceSqftAed.apartment || "n/a"}, villa ${a.avgPriceSqftAed.villa || "n/a"}`);
  if (a.priceSqftYoYPct) parts.push(`Area YoY: ${a.priceSqftYoYPct}%, 5-yr: ${a.priceSqft5yrPct}%`);
  if (a.avgRentalYieldPct) parts.push(`Area yield: apt ${a.avgRentalYieldPct.apartment || "n/a"}%, villa ${a.avgRentalYieldPct.villa || "n/a"}%`);
  if (a.tenantProfile) parts.push(`Tenant profile: ${a.tenantProfile.en || ""}`);
  if (Array.isArray(a.comparableProjects)) {
    parts.push(`Comparables: ${a.comparableProjects.map(c => c.name).join(", ")}`);
  }
  return parts.join("\n");
}

const THESIS_SYSTEM = `You are an investment-grade Dubai real estate analyst writing for an Exceed Real Estate agent who will read your output aloud to a prospective HNW buyer.
Write a 180–220 word investment thesis for the property in the user message.
Structure:
1. One-sentence opening that names the property and its strongest single positioning point.
2. Two or three sentences on market context — the area's transaction depth, price trajectory, yield band, tenant profile. Reference the supplied numbers; do NOT invent.
3. One sentence on the unit's specific value-vs-area positioning.
4. One sentence on the projected return (IRR/multiple/cash-on-cash), framed as a base case.
5. One sentence on the key risk + how it is mitigated.
6. Closing sentence: who this is right for.
Tone: factual, confident, not salesy. No hype. No emojis. No bullet points — flowing prose only.
If a number isn't provided, never invent — say "verify on DXB Interact" instead.`;

const CHAT_SYSTEM = `You are an investment-grade Dubai real estate analyst supporting an Exceed Real Estate agent during a client meeting.
The agent's current property context (loaded into the simulator) is provided below. Answer the agent's client-question concisely (under 150 words), grounded in the supplied numbers.
If a number isn't supplied, say "we'd need to verify that on DXB Interact / DLD" — do NOT invent figures.
If the question is outside Dubai real estate, briefly redirect.
Match the response language to the agent's input language (Japanese if they wrote in JP, English otherwise).`;

const COMPARABLES_SYSTEM = `You are a Dubai real estate analyst. Given the property + area context, return 3–5 comparable Dubai projects MOST RELEVANT to this specific property based on:
- Tier match (ultra-prime / prime / prime-suburban / emerging-prime)
- Product type match (apartment / villa / mansion / branded / standalone)
- Price-per-sqft band match
- Developer / brand tier (Emaar, Damac, Sobha, Nakheel, Omniyat, Aldar, MAF, Select Group, etc.)
- Recency / post-handover transaction relevance

For each comparable, include:
- name: full project name as known in the Dubai market (e.g. "Bulgari Residences", "One at Palm Jumeirah", "Six Senses Residences The Palm")
- context: ONE short sentence (max 90 chars) on why it's a useful reference for THIS property

Return ONLY valid JSON, no markdown, no preamble, no commentary:
{ "comparables": [ { "name": "...", "context": "..." }, ... ] }

Rules:
- 3 to 5 entries, sorted by relevance (most relevant first)
- Real, named Dubai projects only — no invented names, no generic "branded apartment tower"
- If you cannot confidently identify any comparable, return { "comparables": [] }
- Match comparables to the property TIER first, area second — a Palm Jumeirah branded apartment should be compared to other branded apartments in similar tiers, not random Palm villas.`;

async function handleThesis(request, env, origin) {
  const payload = await request.json().catch(() => ({}));
  const ctx = buildContext(payload);
  const text = await callClaude(env, THESIS_SYSTEM, [
    { role: "user", content: `Property + area context:\n${ctx}\n\nWrite the investment thesis now.` }
  ]);
  return new Response(JSON.stringify({ thesis: text, model: MODEL }), {
    headers: { ...corsHeaders(origin), "Content-Type": "application/json" }
  });
}

async function handleComparables(request, env, origin) {
  const payload = await request.json().catch(() => ({}));
  const ctx = buildContext(payload);
  const text = await callClaude(env, COMPARABLES_SYSTEM, [
    { role: "user", content: `Property + area context:\n${ctx}\n\nReturn the JSON now.` }
  ]);
  // Be lenient: strip optional code-fence wrappers
  const cleaned = String(text || "").trim()
    .replace(/^```(?:json)?\s*/i, "").replace(/```\s*$/i, "").trim();
  let parsed = null;
  try { parsed = JSON.parse(cleaned); } catch (e) {}
  if (!parsed || !Array.isArray(parsed.comparables)) {
    return new Response(JSON.stringify({ error: "Invalid AI response", raw: text }), {
      status: 502,
      headers: { ...corsHeaders(origin), "Content-Type": "application/json" }
    });
  }
  // Sanitize to expected shape
  const comparables = parsed.comparables
    .filter(c => c && typeof c.name === "string" && c.name.trim())
    .slice(0, 5)
    .map(c => ({ name: String(c.name).trim(), context: String(c.context || "").trim() }));
  return new Response(JSON.stringify({ comparables, model: MODEL }), {
    headers: { ...corsHeaders(origin), "Content-Type": "application/json" }
  });
}

async function handleChat(request, env, origin) {
  const payload = await request.json().catch(() => ({}));
  const ctx = buildContext(payload);
  const history = Array.isArray(payload.history) ? payload.history : [];
  const question = String(payload.question || "").trim();
  if (!question) {
    return new Response(JSON.stringify({ error: "Missing 'question'" }), {
      status: 400,
      headers: { ...corsHeaders(origin), "Content-Type": "application/json" }
    });
  }
  // Build conversation. First user message carries the property context.
  const messages = [];
  history.forEach(turn => {
    if (turn && turn.role && turn.content) {
      messages.push({ role: turn.role, content: String(turn.content) });
    }
  });
  messages.push({
    role: "user",
    content: `Loaded property + area context:\n${ctx}\n\nClient asks: ${question}`
  });
  const text = await callClaude(env, CHAT_SYSTEM, messages);
  return new Response(JSON.stringify({ reply: text, model: MODEL }), {
    headers: { ...corsHeaders(origin), "Content-Type": "application/json" }
  });
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }
    const url = new URL(request.url);
    try {
      if (request.method === "POST" && url.pathname === "/thesis") {
        return await handleThesis(request, env, origin);
      }
      if (request.method === "POST" && url.pathname === "/chat") {
        return await handleChat(request, env, origin);
      }
      if (request.method === "POST" && url.pathname === "/comparables") {
        return await handleComparables(request, env, origin);
      }
      if (request.method === "GET" && url.pathname === "/") {
        return new Response("Exceed IRR Simulator AI Worker. POST /thesis or /chat.", {
          headers: { ...corsHeaders(origin), "Content-Type": "text/plain" }
        });
      }
    } catch (err) {
      return new Response(JSON.stringify({ error: String(err.message || err) }), {
        status: 500,
        headers: { ...corsHeaders(origin), "Content-Type": "application/json" }
      });
    }
    return new Response("Not found", { status: 404, headers: corsHeaders(origin) });
  }
};
