"""
Nova Lumora Voice Agent — Self-Hosted
======================================
Real, working outbound-calling voice agent. Twilio handles the phone call,
this server bridges the live audio to OpenAI's Realtime API, which listens,
reasons, and speaks back in real time.

This is YOUR code, running on YOUR infrastructure — not a third-party SaaS.

Architecture:
  1. You trigger an outbound call via POST /call (give it a phone number + goal)
  2. Twilio dials the number and connects the call audio to this server over a WebSocket
  3. This server streams that audio to OpenAI's Realtime API
  4. OpenAI's response audio streams back through this server to Twilio, to the phone

Deploy this to Railway, Render, or Fly.io (see DEPLOY.md) — GitHub alone
can host the CODE (version control), but a live phone call needs an
always-running server, which GitHub itself does not provide.
"""

import os
import json
import base64
import asyncio
from datetime import datetime

import websockets
import httpx
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import PlainTextResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv

load_dotenv()

# ---- Real config, pulled from environment variables (set these on your host) ----
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")  # your existing Nova Lumora Twilio number
TRIGGER_PHONE_NUMBER = os.getenv("TRIGGER_PHONE_NUMBER")  # the dormant number people text to request a call
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")  # used to parse freeform SMS requests
PUBLIC_SERVER_URL = os.getenv("PUBLIC_SERVER_URL")  # e.g. https://your-app.up.railway.app
CONTACT_PHONE = os.getenv("CONTACT_PHONE", "")  # James's real callback number for reservations
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "")  # James's real email if a booking needs one
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")  # for business name -> phone number lookup
VEHICLE_INFO = os.getenv("VEHICLE_INFO", "")  # James's truck - year/make/model/mileage for service appointments
VOX_API_KEY = os.getenv("VOX_API_KEY", "")
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN", "")  # PAT with data.records read+write on the VOX Pilot base
VOX_BASE_ID = os.getenv("VOX_BASE_ID", "appXXZGZjyj9PjBZ2")
VOX_USERS_TABLE = os.getenv("VOX_USERS_TABLE", "Users")  # if set, /call requires X-Vox-Key header - blocks strangers from placing calls on our Twilio

VOICE = "alloy"
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime")  # set to "gpt-realtime-mini" to cut audio cost ~3x

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
app = FastAPI()

# In-memory store of the "goal" + who requested it for each active call.
# Real note: resets if the server restarts - fine for single calls; if you want
# history across restarts, log everything to Airtable (see log_call_result).
active_call_goals = {}
active_call_requesters = {}
completed_calls = set()  # finish_call runs at most once per call (stop event + disconnect can both fire)



# ---- DTMF: real touch-tone synthesis so Echo can navigate phone menus ----
import math

DTMF_FREQS = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477),
}

def _lin2ulaw(sample: int) -> int:
    """Standard G.711 mu-law encoder (audioop was removed in Python 3.13)."""
    BIAS, CLIP = 0x84, 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    if sample > CLIP:
        sample = CLIP
    sample += BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF

def dtmf_ulaw_frames(digits: str, tone_ms: int = 250, gap_ms: int = 120):
    """Yield base64-encoded 20ms mu-law frames playing each digit's dual tone."""
    rate = 8000
    for d in digits:
        if d not in DTMF_FREQS:
            continue
        f1, f2 = DTMF_FREQS[d]
        n_tone = int(rate * tone_ms / 1000)
        n_gap = int(rate * gap_ms / 1000)
        pcm = bytearray()
        for i in range(n_tone):
            t = i / rate
            s = int(0.35 * 32767 * (math.sin(2 * math.pi * f1 * t) + math.sin(2 * math.pi * f2 * t)) / 2)
            pcm.append(_lin2ulaw(s))
        pcm.extend(_lin2ulaw(0) for _ in range(n_gap))
        for off in range(0, len(pcm), 160):  # 160 bytes = 20ms at 8kHz
            yield base64.b64encode(bytes(pcm[off:off + 160])).decode()


async def get_pilot_user(phone: str):
    """Look up a pilot user by the number they texted from. Returns the Airtable
    record dict or None. If AIRTABLE_TOKEN isn't set, multi-user mode is off."""
    if not AIRTABLE_TOKEN:
        return None
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://api.airtable.com/v0/{VOX_BASE_ID}/{VOX_USERS_TABLE}",
            headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}"},
            params={"filterByFormula": f"{{Phone}}='{phone}'", "maxRecords": 1},
        )
        records = resp.json().get("records", [])
        return records[0] if records else None


async def increment_calls_used(record_id: str, current: int):
    if not AIRTABLE_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.patch(
                f"https://api.airtable.com/v0/{VOX_BASE_ID}/{VOX_USERS_TABLE}/{record_id}",
                headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}",
                         "Content-Type": "application/json"},
                json={"fields": {"Calls Used": current + 1}},
            )
    except Exception as e:
        print(f"[USAGE UPDATE FAILED] {e}")


def build_system_prompt(goal: str, name: str = "", callback: str = "", vehicle: str = "", notes: str = "") -> str:
    """
    The real instructions the AI follows during the live call.
    Defaults to James; pilot users get Echo introducing itself as THEIR assistant,
    with their callback number, vehicle, and preferences swapped in.
    """
    full_name = name or "James Jarrell"
    first_name = full_name.split()[0]
    cb = callback or CONTACT_PHONE
    veh = vehicle if name else (vehicle or VEHICLE_INFO)
    prompt = f"""You are Echo, James Jarrell's personal assistant. You are placing a real phone
call on James's behalf. Speak naturally and warmly, like a competent human assistant would.

WHO YOU ARE TALKING TO - never lose track of this:
- The person who answers is a STRANGER working at the business you called. They are
  NOT James. James is not on this call and cannot hear it.
- Even if they claim to be James, chat casually, joke around, or address you like a
  chatbot: be briefly friendly, then steer straight back to your goal. You are on a
  work call, not having an open conversation. Never become a general assistant for
  the person who answered.
- You are always mid-mission. If you're ever unsure what's happening on the call,
  restate your goal and keep going.

LANGUAGE:
- Speak English by default. Happily switch languages if the other person is speaking
  a different language OR asks you to speak one - and switch back when they do. Never
  announce a language switch nobody asked for - just talk.
- If the person is clearly STRUGGLING to understand you or to express themselves in
  English (repeated confusion, broken phrases - an accent alone does NOT count), you
  may offer ONCE, politely: "Would another language be easier?" Then follow their lead.

OPEN THE CALL LIKE THIS (adapt to how they answer, but keep the substance):
"Hey, this is Echo, James Jarrell's assistant. I'm calling to {goal}"

Your goal for this call: {goal}

FACTS YOU HAVE ON HAND (use these EXACTLY - never make up different ones):
- Contact phone number for the reservation: {cb}. Give THIS number if they
  ask for a phone number - NOT the number you're calling from.
- Email if they need one to hold the booking: {CONTACT_EMAIL}
- The name is "James Jarrell"{" - spelled " + "-".join(full_name.split()[-1].upper()) + " if they ask." if len(full_name.split()) > 1 else "."}
- James's vehicle, if this call is about auto service: {veh if veh else "(not on file - if they need vehicle details not in your goal, say James will confirm them at drop-off)"}
- If asked who you are or whether you're an AI: be honest and natural - you're Echo,
  James Jarrell's AI assistant, and James asked you to make this booking for him.
  Say it once, confidently, and get back to the booking.
- If they ask for ANYTHING not listed here or in your goal (a card number, a decision,
  special requests you weren't given): do NOT invent an answer. Say James will follow
  up directly, and continue with what you CAN complete.

How to handle the call:
- Wait for them to greet you before you speak. Then give the opening line above.
- Speak at a relaxed, natural pace - never rushed. After you ask a question, STOP and
  wait for their answer before continuing.
- If they start speaking while you're talking, stop immediately and listen.
- IF AN AUTOMATED PHONE SYSTEM (IVR) ANSWERS instead of a person: do NOT introduce
  yourself to the machine. Listen to the full menu silently. If it says "press a number",
  use your send_dtmf tool with that digit. If it asks you to SPEAK a choice, answer with
  only the short keyword ("carryout", "representative").
- IVR LANGUAGE RULE: always take the ENGLISH menu path first. Never press the option
  for another language ("para espanol...") just because it was the clearest thing you
  heard - wait through the full menu for the English options. A non-English path is a
  LAST resort, only after the English path has failed. Always prefer any path to a live
  person - pressing 0 or saying "representative" often works. Once a HUMAN answers, then
  give your normal opening line. If after several attempts you cannot reach a person or
  place the order, say nothing more and end the call - the summary must honestly say the
  order was not placed.
- IF THIS IS A FOOD ORDER: state the order clearly (items, sizes, quantities), say it's for
  pickup and give the pickup time and the name. Payment will be handled at pickup - if they
  require payment over the phone, say James will call back to pay and confirm what you CAN.
  Before hanging up, get the total price and the ready time, then repeat the full order back
  AS A QUESTION and get a clear yes. Only after they confirm do you say goodbye - never
  stack the confirmation and the goodbye into one breath.
- IF THIS IS A RESERVATION OR AN APPOINTMENT (restaurant, auto service, or similar):
  have the details ready and give them clearly when asked - date, time, the name, and
  what's needed (for restaurants: party size; for auto service: the vehicle and every
  service listed in your goal, including any concerns to have checked).
  If the requested time isn't available, ask for the closest available times and accept the
  nearest reasonable option within about an hour of the request. Say what you booked.
  Before hanging up, repeat the confirmation back in one sentence: date, time, party size,
  and the name. Get a clear yes.
- If they need something you don't have (an email, a card to hold the table, a decision
  outside your goal), say you'll have James follow up directly and get the best next step.
- If asked whether you're an AI or a robot, be honest: you're an AI assistant calling
  on behalf of James Jarrell. Don't pretend to be human.
- Keep every turn short - one or two sentences. It's a phone call, not a speech.
- When it's done, thank them, say a natural goodbye, and stop talking.
"""
    # Personalization pass: swap James for the pilot user everywhere in one shot.
    if full_name != "James Jarrell":
        prompt = prompt.replace("James Jarrell", full_name).replace("James", first_name)
    if notes:
        prompt += f"\nEXTRA PREFERENCES from {first_name} (respect these):\n{notes}\n"
    return prompt


@app.get("/")
async def health():
    return {"status": "Nova Lumora Voice Agent - running", "time": datetime.utcnow().isoformat()}


@app.post("/call")
async def place_call(request: Request):
    """
    Real trigger endpoint. Call this (from Make, curl, or anywhere) to place
    an outbound call:

    POST /call
    {
        "to": "+16155551234",
        "goal": "Book a table for 2 at 7:30pm tonight under the name James"
    }
    """
    if VOX_API_KEY and request.headers.get("x-vox-key") != VOX_API_KEY:
        return {"error": "unauthorized"}
    body = await request.json()
    to_number = body["to"]
    goal = body.get("goal", "Confirm you've reached the business and ask how you can help.")

    call = twilio_client.calls.create(
        to=to_number,
        from_=TWILIO_PHONE_NUMBER,
        url=f"{PUBLIC_SERVER_URL}/twiml?goal={base64.urlsafe_b64encode(goal.encode()).decode()}",
    )

    active_call_goals[call.sid] = goal
    return {"status": "calling", "call_sid": call.sid, "to": to_number, "goal": goal}


async def parse_sms_request(text: str, default_name: str = "James Jarrell") -> dict:
    """
    Real AI parsing step: turns a freeform text like
    "call Bob's Pizza at 615-555-1234, book a table for 4 at 7pm tonight under James"
    into structured { "to": "+16155551234", "goal": "..." }.

    Uses Claude (same model this whole system was built with) since it's
    genuinely better at flexible, freeform parsing than rigid regex rules.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 300,
                "messages": [{
                    "role": "user",
                    "content": f"""A text message came in requesting a phone call be placed on someone's
behalf (a restaurant reservation, a food order for pickup, or similar). Extract the phone number
to call and a clear, specific goal for that call.
If no name is given for a reservation or pickup, the name is "{default_name}".
Write the goal as a natural phrase that completes the sentence "I'm calling to ..." (e.g. "book a
table for two at 7pm tonight under {default_name}" or "order a large cheese pizza for pickup at
12pm under the name {default_name}").
Include EVERY detail given, whatever the request type: items, sizes, quantities, times,
services requested (e.g. oil change, tire rotation, brake inspection), vehicle info
(year/make/model/mileage), party sizes, special requests, and the name. Details the
caller will need must survive into the goal - never compress them away.

If no phone number is given directly but a business name and location are (e.g. "the Papa John's
in Pleasant View TN"), set "to" to "NEED_LOOKUP" and put the business name + location in
"business_query" (e.g. "Papa John's Pleasant View TN").
If neither a number nor a findable business name+location is given, set "to" to "NEED_NUMBER".
Never guess a phone number.

Reply with ONLY valid JSON, nothing else, in this exact format:
{{"to": "+1XXXXXXXXXX or NEED_LOOKUP or NEED_NUMBER", "business_query": "business name and location, or empty string", "goal": "clear instructions for what the call should accomplish"}}

MESSAGE: {text}"""
                }],
            },
        )
        data = resp.json()
        raw = data["content"][-1]["text"].strip()
        return json.loads(raw)


async def lookup_business_number(business_query: str) -> str:
    """
    Real business lookup: 'Papa John's Pleasant View TN' -> '+16153824444'.
    Uses Google Places Text Search (new API). Returns E.164 number or '' if not found.
    """
    if not GOOGLE_PLACES_API_KEY:
        return ""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": "places.displayName,places.internationalPhoneNumber,places.formattedAddress",
            },
            json={"textQuery": business_query, "maxResultCount": 1},
        )
        data = resp.json()
        places = data.get("places", [])
        if not places:
            print(f"[LOOKUP] No results for: {business_query}")
            return ""
        place = places[0]
        phone = place.get("internationalPhoneNumber", "")
        print(f"[LOOKUP] {business_query} -> {place.get('displayName', {}).get('text', '?')} @ {place.get('formattedAddress', '?')} -> {phone}")
        return phone.replace(" ", "").replace("-", "")


import hmac as _hmac
import hashlib as _hashlib

def check_twilio_signature(request, form) -> bool:
    """
    Validates X-Twilio-Signature (HMAC-SHA1 of URL + sorted form params, keyed by
    the auth token). LOG-ONLY for now: we flag failures in logs without blocking,
    to prove the math matches our proxy setup before enforcing.
    """
    try:
        sig = request.headers.get("x-twilio-signature", "")
        url = f"{PUBLIC_SERVER_URL}{request.url.path}"
        payload = url + "".join(k + form[k] for k in sorted(form.keys()))
        expected = base64.b64encode(
            _hmac.new(TWILIO_AUTH_TOKEN.encode(), payload.encode(), _hashlib.sha1).digest()
        ).decode()
        if not _hmac.compare_digest(expected, sig):
            print(f"[SIG FAIL] {request.url.path} - signature mismatch (log-only, not blocking)")
            return False
        return True
    except Exception as e:
        print(f"[SIG CHECK ERROR] {e}")
        return False


@app.post("/sms-trigger")
async def sms_trigger(request: Request):
    """
    Real, live endpoint: point your dormant Twilio number's SMS webhook here.
    Someone texts a request -> this parses it, places the real call, and -
    once the call finishes - texts back a real status update to whoever asked.
    """
    form = await request.form()
    check_twilio_signature(request, dict(form))
    from_number = form.get("From")
    body = form.get("Body", "")

    # ---- Pilot gate: who is this, are they active, do they have calls left ----
    user_fields, user_record_id = {}, None
    if AIRTABLE_TOKEN:
        user = await get_pilot_user(from_number)
        if not user or not user.get("fields", {}).get("Active"):
            twilio_client.messages.create(
                to=from_number, from_=TRIGGER_PHONE_NUMBER,
                body="VOX is currently invite-only. Reply if you think this is a mistake and we'll get you sorted.",
            )
            return PlainTextResponse("", media_type="application/xml")
        user_fields = user.get("fields", {})
        user_record_id = user["id"]
        used = int(user_fields.get("Calls Used") or 0)
        limit = int(user_fields.get("Call Limit") or 15)
        if used >= limit:
            twilio_client.messages.create(
                to=from_number, from_=TRIGGER_PHONE_NUMBER,
                body=f"You've used all {limit} of your pilot calls. Text James if you need more.",
            )
            return PlainTextResponse("", media_type="application/xml")

    try:
        parsed = await parse_sms_request(body, user_fields.get("Name") or "James Jarrell")
    except Exception:
        twilio_client.messages.create(
            to=from_number, from_=TRIGGER_PHONE_NUMBER,
            body="Couldn't understand that request. Try: 'Call [business] at [phone number] and [what you need].'",
        )
        return PlainTextResponse("", media_type="application/xml")

    if parsed.get("to") == "NEED_LOOKUP":
        found = await lookup_business_number(parsed.get("business_query", ""))
        if found:
            parsed["to"] = found
        elif not GOOGLE_PLACES_API_KEY:
            twilio_client.messages.create(
                to=from_number, from_=TRIGGER_PHONE_NUMBER,
                body="I can't look up business numbers yet - text me the phone number and I'll make the call.",
            )
            return PlainTextResponse("", media_type="application/xml")
        else:
            twilio_client.messages.create(
                to=from_number, from_=TRIGGER_PHONE_NUMBER,
                body=f"Couldn't find a listing for '{parsed.get('business_query', '')}'. Text me the phone number and I'll make the call.",
            )
            return PlainTextResponse("", media_type="application/xml")

    if parsed.get("to") == "NEED_NUMBER":
        twilio_client.messages.create(
            to=from_number, from_=TRIGGER_PHONE_NUMBER,
            body="Got it, but I need the actual phone number to call - text it again with the number included.",
        )
        return PlainTextResponse("", media_type="application/xml")

    persona = {
        "name": user_fields.get("Name") or "",
        "callback": user_fields.get("Callback Number") or (from_number if user_fields else ""),
        "vehicle": user_fields.get("Vehicle") or "",
        "notes": user_fields.get("Notes") or "",
    }
    persona_b64 = base64.urlsafe_b64encode(json.dumps(persona).encode()).decode()
    call = twilio_client.calls.create(
        to=parsed["to"], from_=TWILIO_PHONE_NUMBER,
        url=f"{PUBLIC_SERVER_URL}/twiml?goal={base64.urlsafe_b64encode(parsed['goal'].encode()).decode()}&persona={persona_b64}",
        status_callback=f"{PUBLIC_SERVER_URL}/call-status",
        status_callback_event=["completed"],
    )
    active_call_goals[call.sid] = parsed["goal"]
    active_call_requesters[call.sid] = from_number  # remember who to text the result back to
    if user_record_id:
        await increment_calls_used(user_record_id, int(user_fields.get("Calls Used") or 0))

    twilio_client.messages.create(
        to=from_number, from_=TRIGGER_PHONE_NUMBER,
        body=f"On it. Calling {parsed['to']} now - I'll text you the result as soon as the call ends. - Echo",
    )
    return PlainTextResponse("", media_type="application/xml")


@app.post("/call-status")
async def call_status(request: Request):
    """
    Twilio posts here when a call reaches a final state. Catches the calls that
    NEVER connected (no-answer, busy, failed, canceled) - those never open a
    media stream, so finish_call never runs and the requester would otherwise
    hear nothing. This closes that loop with an honest status text.
    """
    form = await request.form()
    check_twilio_signature(request, dict(form))
    call_sid = form.get("CallSid")
    call_status_value = form.get("CallStatus", "")
    to_number = form.get("To", "")

    if call_status_value in ("no-answer", "busy", "failed", "canceled"):
        requester = active_call_requesters.get(call_sid)
        if requester:
            reasons = {
                "no-answer": f"No answer at {to_number} - they didn't pick up.",
                "busy": f"{to_number} was busy.",
                "failed": f"The call to {to_number} couldn't go through - double-check the number.",
                "canceled": f"The call to {to_number} was canceled before it connected.",
            }
            twilio_client.messages.create(
                to=requester, from_=TRIGGER_PHONE_NUMBER,
                body=f"Echo here. {reasons[call_status_value]} Text me again to retry.",
            )
    return PlainTextResponse("", media_type="application/xml")


@app.post("/twiml")
async def twiml_endpoint(request: Request):
    """Twilio hits this the moment the call connects - tells Twilio to stream
    the live audio to our WebSocket instead of playing a static message."""
    goal_encoded = request.query_params.get("goal", "")
    persona_encoded = request.query_params.get("persona", "")
    response = VoiceResponse()
    connect = Connect()
    stream_url = f"{PUBLIC_SERVER_URL.replace('https://', 'wss://')}/media-stream"
    stream = connect.stream(url=stream_url)
    # Twilio strips query params from Media Stream URLs - custom Parameters are
    # the real, documented way to pass data. They arrive in the 'start' event.
    stream.parameter(name="goal", value=goal_encoded)
    if persona_encoded:
        stream.parameter(name="persona", value=persona_encoded)
    response.append(connect)
    return PlainTextResponse(str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """
    The real, live bridge: Twilio's audio <-> OpenAI's Realtime API <-> back to Twilio.
    This is the actual "phone call brain" - runs for the full duration of the call.
    """
    await websocket.accept()

    stream_sid = None
    call_sid = None
    goal = "Have a helpful conversation."
    persona = {}
    transcript_lines = []  # real, running transcript of what the AI actually said during the call

    # Twilio sends 'connected' then 'start' before any audio. The 'start' event
    # carries our custom Parameters (the goal) - query params get stripped by Twilio,
    # so we MUST wait for start before we know what this call is for.
    while stream_sid is None:
        message = await websocket.receive_text()
        data = json.loads(message)
        if data.get("event") == "start":
            stream_sid = data["start"]["streamSid"]
            call_sid = data["start"].get("callSid")
            goal_encoded = data["start"].get("customParameters", {}).get("goal", "")
            if goal_encoded:
                goal = base64.urlsafe_b64decode(goal_encoded).decode()
            persona_encoded = data["start"].get("customParameters", {}).get("persona", "")
            if persona_encoded:
                try:
                    persona = json.loads(base64.urlsafe_b64decode(persona_encoded).decode())
                except Exception:
                    persona = {}
            print(f"[CALL START] {call_sid} goal: {goal} persona: {persona.get('name') or 'James (default)'}")

    async with websockets.connect(
        f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}",
        extra_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
    ) as openai_ws:

        # Configure the live session with our real goal for this call
        # NOTE: GA Realtime API shape - audio config nested under session.audio, not flat
        await openai_ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                "tools": [{
                    "type": "function",
                    "name": "send_dtmf",
                    "description": "Press phone keypad buttons to navigate an automated menu. Use when the phone system says 'press 1' etc.",
                    "parameters": {
                        "type": "object",
                        "properties": {"digits": {"type": "string", "description": "The digits to press, e.g. '1' or '0'"}},
                        "required": ["digits"],
                    },
                }],
                "tool_choice": "auto",
                "instructions": build_system_prompt(goal, persona.get("name", ""), persona.get("callback", ""), persona.get("vehicle", ""), persona.get("notes", "")),
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "turn_detection": {"type": "server_vad"},
                        "transcription": {"model": "whisper-1"},
                    },
                    "output": {
                        "format": {"type": "audio/pcmu"},
                        "voice": VOICE,
                    },
                },
            }
        }))

        async def twilio_to_openai():
            nonlocal stream_sid, call_sid
            async for message in websocket.iter_text():
                data = json.loads(message)
                if data["event"] == "media":
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": data["media"]["payload"],
                    }))
                elif data["event"] == "stop":
                    await finish_call(call_sid, transcript_lines)
                    return  # call is over - exit so the OpenAI session gets closed, not leaked

        async def openai_to_twilio():
            async for message in openai_ws:
                data = json.loads(message)
                event_type = data.get("type", "")
                # GA event names: response.output_audio.delta / response.output_audio_transcript.delta+done
                if event_type in ("response.audio.delta", "response.output_audio.delta") and stream_sid:
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": data["delta"]},
                    })
                # Real transcript capture - this is what lets us text back an actual
                # summary instead of just "call finished"
                elif event_type in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
                    line = f"Agent: {data.get('transcript', '')}"
                    if not transcript_lines or transcript_lines[-1] != line:
                        transcript_lines.append(line)
                # GA reliably finalizes the spoken text here even when the .done
                # transcript event doesn't fire - belt and suspenders, deduped
                elif event_type == "response.content_part.done":
                    part = data.get("part", {})
                    if part.get("type") == "audio" and part.get("transcript"):
                        line = f"Agent: {part['transcript']}"
                        if not transcript_lines or transcript_lines[-1] != line:
                            transcript_lines.append(line)
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    transcript_lines.append(f"Them: {data.get('transcript', '')}")
                # Echo pressed a keypad button: synthesize real DTMF audio into the call
                elif event_type == "response.function_call_arguments.done":
                    try:
                        args = json.loads(data.get("arguments", "{}"))
                        digits = args.get("digits", "")
                        print(f"[DTMF] pressing: {digits}")
                        if stream_sid:
                            for frame in dtmf_ulaw_frames(digits):
                                await websocket.send_json({
                                    "event": "media", "streamSid": stream_sid,
                                    "media": {"payload": frame},
                                })
                        await openai_ws.send(json.dumps({
                            "type": "conversation.item.create",
                            "item": {"type": "function_call_output",
                                     "call_id": data.get("call_id"),
                                     "output": json.dumps({"status": "pressed", "digits": digits})},
                        }))
                        await openai_ws.send(json.dumps({"type": "response.create"}))
                    except Exception as e:
                        print(f"[DTMF FAILED] {e}")
                # Barge-in: caller started talking while Echo may be mid-sentence.
                # Cancel the in-flight response AND flush Twilio's buffered audio -
                # without the clear, Twilio keeps playing seconds of queued speech.
                elif event_type == "input_audio_buffer.speech_started":
                    await openai_ws.send(json.dumps({"type": "response.cancel"}))
                    if stream_sid:
                        await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                elif event_type == "error":
                    err = json.dumps(data)
                    if "cancel" in err and "no active response" in err.lower():
                        pass  # expected when we cancel with nothing in flight
                    else:
                        print(f"[OPENAI ERROR] {err}")
                elif event_type == "session.updated":
                    print(f"[SESSION CONFIRMED] {json.dumps(data.get('session', {}))}")
                elif event_type not in ("response.done", "response.created", "input_audio_buffer.speech_stopped", "input_audio_buffer.committed", "conversation.item.created", "conversation.item.added", "conversation.item.done", "conversation.item.input_audio_transcription.delta", "response.output_item.added", "response.output_item.done", "response.content_part.added", "response.output_audio.done", "response.output_audio_transcript.delta", "response.function_call_arguments.delta", "rate_limits.updated", "output_audio_buffer.started", "output_audio_buffer.stopped", "output_audio_buffer.cleared"):
                    # Catch-all: log anything unrecognized so silent failures show up in logs instead of dead air
                    print(f"[UNHANDLED EVENT] {event_type}: {json.dumps(data)[:500]}")

        t1 = asyncio.create_task(twilio_to_openai())
        t2 = asyncio.create_task(openai_to_twilio())
        try:
            # First side to finish (call ended / socket dropped) wins; cancel the other
            # so we never hold an idle OpenAI session open until its 60-minute cap.
            done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                    print(f"[BRIDGE ERROR] {exc}")
        finally:
            await finish_call(call_sid, transcript_lines)
            # Fix 2: don't let per-call state accumulate forever
            active_call_goals.pop(call_sid, None)
            active_call_requesters.pop(call_sid, None)


async def finish_call(call_sid: str, transcript_lines: list):
    """
    Real wrap-up when a call ends: summarize what actually happened using the
    real transcript, log it, and text the result back to whoever requested it.
    Hardened: runs at most once per call, ALWAYS sends a text even if the
    summary API fails, and a text-send failure can't crash anything else.
    """
    if call_sid in completed_calls:
        return
    completed_calls.add(call_sid)

    full_transcript = "\n".join(transcript_lines) if transcript_lines else "(no transcript captured)"
    try:
        summary = await summarize_call(full_transcript)
    except Exception as e:
        # The summary is a nice-to-have. The confirmation text is NOT.
        # Fall back to Echo's own last confirmation line from the call.
        print(f"[SUMMARY FAILED] {e} - falling back to raw transcript line")
        agent_lines = [l for l in transcript_lines if l.startswith("Agent:")]
        summary = (agent_lines[-1].replace("Agent: ", "", 1)
                   if agent_lines else "Call finished - I couldn't generate a summary, check Railway logs for the transcript.")

    await log_call_result(call_sid, summary, full_transcript)

    requester = active_call_requesters.get(call_sid)
    if requester:
        try:
            twilio_client.messages.create(
                to=requester, from_=TRIGGER_PHONE_NUMBER,
                body=f"Echo here. {summary}",
            )
        except Exception as e:
            print(f"[RESULT TEXT FAILED] {e}")


async def summarize_call(transcript: str) -> str:
    """Real, honest summary of what actually happened on the call, generated
    from the real captured transcript - not a guess."""
    if transcript == "(no transcript captured)":
        return "Call completed, but no transcript was captured - check Railway logs for details."
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 200,
                "messages": [{
                    "role": "user",
                    "content": f"""Summarize the real outcome of this phone call in 1-2 short sentences,
suitable for a text message. State clearly whether the goal was achieved (e.g. reservation
confirmed, time/date if given) or not (e.g. fully booked, wrong number, no answer).

TRANSCRIPT:
{transcript}"""
                }],
            },
        )
        data = resp.json()
        return data["content"][-1]["text"].strip()


async def log_call_result(call_sid, summary: str, transcript: str = ""):
    """
    Real hook for logging every call to your Business Brain, the same way
    everything else in Nova Lumora gets logged. Wire this to an Airtable API
    call (Decisions Log or a new 'Voice Calls' table) so every reservation
    call - and what actually happened - is on record, same as everything else.
    """
    print(f"[{datetime.utcnow().isoformat()}] Call {call_sid}: {summary}")
    print(f"Transcript:\n{transcript}")
    # TODO: real Airtable write here, matching the pattern used everywhere
    # else in Nova Lumora - e.g. requests.post to an Airtable-connected
    # Make webhook, so this call gets logged in the Decisions Log automatically.


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
