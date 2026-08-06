# Nova Lumora Voice Agent — Deployment Guide

Real, honest instructions to get this actually live. This is YOUR code, on YOUR
infrastructure — GitHub stores the code, but a live phone call needs an
always-running server, which is a separate, real hosting step.

## What you actually need (real accounts, ~10 minutes total)

1. **A GitHub account** (you likely have one) — to store this code
2. **A Railway account** (railway.app) — real, honest recommendation: easiest
   platform for exactly this kind of always-on WebSocket server, genuine free
   tier to start, no credit card required for initial testing
3. **An OpenAI API key with Realtime API access** (platform.openai.com) —
   this is a REAL, separate cost from your Claude usage - Realtime API is
   priced per-minute of audio, roughly $0.06/min input + $0.24/min output
   as of this writing - a 3-minute reservation call is real money, but small
   (a few cents), not a meaningful monthly cost at low call volume
4. **Your existing Twilio account** — already have this, no new signup

## Real, step-by-step deployment

### 1. Push this code to a real GitHub repo
```bash
cd voice-agent
git init
git add .
git commit -m "Nova Lumora voice agent - initial build"
# Create a new repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/nova-lumora-voice-agent.git
git push -u origin main
```
**Real, important note:** the `.env` file is intentionally NOT included (only
`.env.example` is) - never commit real API keys to GitHub, even a private repo.

### 2. Deploy to Railway
- Go to railway.app, sign in with GitHub
- "New Project" → "Deploy from GitHub repo" → select this repo
- Railway auto-detects the Procfile and starts building - this is the real,
  actual hosting step that makes this genuinely live

### 3. Set your real environment variables in Railway
In Railway's dashboard, under your project → Variables tab, add every value
from `.env.example` with your REAL credentials (Twilio SID/token/number,
OpenAI key). Leave `PUBLIC_SERVER_URL` blank for now.

### 4. Get your real live URL
After the first deploy, Railway gives you a real URL like
`https://nova-lumora-voice-agent-production.up.railway.app`
Copy this, go back to Variables, and set `PUBLIC_SERVER_URL` to this exact
value. Redeploy (Railway does this automatically when you save a variable).

### 5. Real test call
```bash
curl -X POST https://your-real-railway-url.up.railway.app/call \
  -H "Content-Type: application/json" \
  -d '{
    "to": "+16292541340",
    "goal": "This is a test call. Just say hello and confirm the connection works, then say goodbye."
  }'
```
Call your OWN phone first to hear it actually work before pointing it at a
real restaurant.

### 6. Real setup for the "text to trigger a call" feature
This is the part you specifically asked for: text a dormant number, it places
the call, then texts you back what actually happened.

1. Pick one of your dormant Twilio numbers (from the account audit you already
   have on file) and set it as `TRIGGER_PHONE_NUMBER` in Railway's Variables
2. In the Twilio Console, open that number's settings
3. Under "Messaging" -> "A message comes in", set the webhook to:
   `https://your-real-railway-url.up.railway.app/sms-trigger`
   (Method: HTTP POST)
4. Real test: text that number something like
   `"Call Bob's Pizza at 615-555-1234, book a table for 2 at 7pm tonight under James"`
   You should get an immediate "On it" reply, then a real status text once
   the call actually finishes - a real summary of what happened, not just
   "call completed."

**Real, honest note:** if you don't give a phone number in the text, the
agent will text back asking for it rather than guessing - it won't look up
a business's number on its own in this version.

## Real, honest limitations of this v1

- **No live monitoring dashboard** - you'll see call logs in Railway's own
  log viewer, not yet in your Business Brain (the `log_call_result` function
  in main.py is stubbed in, ready to wire to Airtable the same way STRIDE
  and TALON already log everything - genuinely a next step, not done yet)
- **No retry logic** - if a call fails to connect, it just fails; a real
  production version would retry or alert you
- **One call at a time** - this v1 handles one active call; real concurrent-call
  support is a genuine future upgrade, not needed for your actual use case yet

## Real cost estimate, all-in

- Railway hosting: free tier covers this easily at low usage, ~$5/month if you
  outgrow it
- OpenAI Realtime API: pay-per-minute, genuinely a few cents per call at your
  volume
- Twilio: you already pay for this, small additional per-minute voice cost
  on top of what you already have

**Total real, honest additional monthly cost: likely under $10/month** at the
volume of "call a restaurant sometimes," not the $95-160/month quoted for a
full third-party platform like Vapi - because you're running the actual
infrastructure yourself now.
