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
import audioop
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

VOICE = "alloy"
REALTIME_MODEL = "gpt-4o-realtime-preview-2024-12-17"

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
app = FastAPI()

# In-memory store of the "goal" + who requested it for each active call.
# Real note: resets if the server restarts - fine for single calls; if you want
# history across restarts, log everything to Airtable (see log_call_result).
active_call_goals = {}
active_call_requesters = {}


def build_system_prompt(goal: str) -> str:
    """
    The real instructions the AI follows during the live call.
    Edit this per-call, or make it dynamic based on what you're booking.
    """
    return f"""You are calling on behalf of James and Conor at Nova Lumora, a real person
placing this call, not a script. Speak naturally, like a person would on the phone.

Your goal for this call: {goal}

Real, important rules:
- If asked "is this a robot" or similar, be honest: say you're an AI assistant calling
  on behalf of James, not pretending to be human.
- Get a clear, explicit confirmation before ending the call (a time, a name on the
  reservation, whatever "success" means for this specific goal).
- If the goal can't be completed (fully booked, wrong number, etc.), get the most
  useful real information you can (next available time, correct number, etc.)
  rather than just giving up.
- Keep your responses short and natural, like real phone conversation, not a monologue.
- When the call is complete, say a natural goodbye and stop talking - do not keep
  the line open unnecessarily.
"""


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


async def parse_sms_request(text: str) -> dict:
    """
    Real AI parsing step: turns a freeform text like
    "call Bob's Pizza at 615-555-1234, book a table for 4 at 7pm tonight under James"
    into structured { "to": "+16155551234", "goal": "..." }.

    Uses Claude (same model this whole system was built with) since it's
    genuinely better at flexible, freeform parsing than rigid regex rules.
    """
    async with httpx.AsyncClient() as client:
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
behalf. Extract the real phone number to call and a clear, specific goal for that call.

If no phone number is given directly, reply with "NEED_NUMBER" as the phone field -
do not guess a number.

Reply with ONLY valid JSON, nothing else, in this exact format:
{{"to": "+1XXXXXXXXXX", "goal": "clear instructions for what the call should accomplish"}}

MESSAGE: {text}"""
                }],
            },
        )
        data = resp.json()
        raw = data["content"][-1]["text"].strip()
        return json.loads(raw)


@app.post("/sms-trigger")
async def sms_trigger(request: Request):
    """
    Real, live endpoint: point your dormant Twilio number's SMS webhook here.
    Someone texts a request -> this parses it, places the real call, and -
    once the call finishes - texts back a real status update to whoever asked.
    """
    form = await request.form()
    from_number = form.get("From")
    body = form.get("Body", "")

    try:
        parsed = await parse_sms_request(body)
    except Exception:
        twilio_client.messages.create(
            to=from_number, from_=TRIGGER_PHONE_NUMBER,
            body="Couldn't understand that request. Try: 'Call [business] at [phone number] and [what you need].'",
        )
        return PlainTextResponse("", media_type="application/xml")

    if parsed.get("to") == "NEED_NUMBER":
        twilio_client.messages.create(
            to=from_number, from_=TRIGGER_PHONE_NUMBER,
            body="Got it, but I need the actual phone number to call - text it again with the number included.",
        )
        return PlainTextResponse("", media_type="application/xml")

    call = twilio_client.calls.create(
        to=parsed["to"], from_=TWILIO_PHONE_NUMBER,
        url=f"{PUBLIC_SERVER_URL}/twiml?goal={base64.urlsafe_b64encode(parsed['goal'].encode()).decode()}",
    )
    active_call_goals[call.sid] = parsed["goal"]
    active_call_requesters[call.sid] = from_number  # remember who to text the result back to

    twilio_client.messages.create(
        to=from_number, from_=TRIGGER_PHONE_NUMBER,
        body=f"On it - calling {parsed['to']} now. I'll text you the result when it's done.",
    )
    return PlainTextResponse("", media_type="application/xml")


@app.post("/twiml")
async def twiml_endpoint(request: Request):
    """Twilio hits this the moment the call connects - tells Twilio to stream
    the live audio to our WebSocket instead of playing a static message."""
    goal_encoded = request.query_params.get("goal", "")
    response = VoiceResponse()
    connect = Connect()
    stream_url = f"{PUBLIC_SERVER_URL.replace('https://', 'wss://')}/media-stream?goal={goal_encoded}"
    connect.stream(url=stream_url)
    response.append(connect)
    return PlainTextResponse(str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """
    The real, live bridge: Twilio's audio <-> OpenAI's Realtime API <-> back to Twilio.
    This is the actual "phone call brain" - runs for the full duration of the call.
    """
    await websocket.accept()
    goal_encoded = websocket.query_params.get("goal", "")
    goal = base64.urlsafe_b64decode(goal_encoded).decode() if goal_encoded else "Have a helpful conversation."

    stream_sid = None
    call_sid = None
    transcript_lines = []  # real, running transcript of what the AI actually said during the call

    async with websockets.connect(
        f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}",
        extra_headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"},
    ) as openai_ws:

        # Configure the live session with our real goal for this call
        await openai_ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "voice": VOICE,
                "instructions": build_system_prompt(goal),
                "modalities": ["text", "audio"],
                "temperature": 0.8,
            }
        }))

        async def twilio_to_openai():
            nonlocal stream_sid, call_sid
            async for message in websocket.iter_text():
                data = json.loads(message)
                if data["event"] == "start":
                    stream_sid = data["start"]["streamSid"]
                    call_sid = data["start"].get("callSid")
                elif data["event"] == "media":
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": data["media"]["payload"],
                    }))
                elif data["event"] == "stop":
                    await finish_call(call_sid, transcript_lines)

        async def openai_to_twilio():
            async for message in openai_ws:
                data = json.loads(message)
                if data.get("type") == "response.audio.delta" and stream_sid:
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": data["delta"]},
                    })
                # Real transcript capture - this is what lets us text back an actual
                # summary instead of just "call finished"
                elif data.get("type") == "response.audio_transcript.done":
                    transcript_lines.append(f"Agent: {data.get('transcript', '')}")
                elif data.get("type") == "conversation.item.input_audio_transcription.completed":
                    transcript_lines.append(f"Them: {data.get('transcript', '')}")

        try:
            await asyncio.gather(twilio_to_openai(), openai_to_twilio())
        except WebSocketDisconnect:
            await finish_call(call_sid, transcript_lines)


async def finish_call(call_sid: str, transcript_lines: list):
    """
    Real wrap-up when a call ends: summarize what actually happened using the
    real transcript, log it, and text the result back to whoever requested it.
    """
    full_transcript = "\n".join(transcript_lines) if transcript_lines else "(no transcript captured)"
    summary = await summarize_call(full_transcript)
    await log_call_result(call_sid, summary, full_transcript)

    requester = active_call_requesters.get(call_sid)
    if requester:
        twilio_client.messages.create(
            to=requester, from_=TRIGGER_PHONE_NUMBER,
            body=f"Call result: {summary}",
        )


async def summarize_call(transcript: str) -> str:
    """Real, honest summary of what actually happened on the call, generated
    from the real captured transcript - not a guess."""
    if transcript == "(no transcript captured)":
        return "Call completed, but no transcript was captured - check Railway logs for details."
    async with httpx.AsyncClient() as client:
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
