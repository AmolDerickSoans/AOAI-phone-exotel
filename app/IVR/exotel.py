import os
import json
import base64
import asyncio
from fastapi import FastAPI, WebSocket, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.websockets import WebSocketDisconnect
from dotenv import load_dotenv
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import aiohttp
from aiohttp import web
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
import numpy as np  # Import numpy
from scipy import signal  # Import scipy.signal
# Assuming rtmt.py is in a sibling directory named 'backend'
from ..backend.rtmt import RTMiddleTier, Tool, ToolResult, ToolResultDirection

load_dotenv()

# Configuration
PORT = int(os.getenv('PORT', 8765))  # Consistent port
AZURE_OPENAI_ENDPOINT = os.getenv('AZURE_OPENAI_ENDPOINT')
AZURE_OPENAI_DEPLOYMENT = os.getenv('AZURE_OPENAI_REALTIME_DEPLOYMENT')
AZURE_OPENAI_KEY = os.getenv('AZURE_OPENAI_API_KEY')
AZURE_OPENAI_VOICE_CHOICE = os.getenv('AZURE_OPENAI_REALTIME_VOICE_CHOICE', 'alloy')
AZURE_OPENAI_USE_AAD = os.getenv('AZURE_OPENAI_USE_AAD', 'False').lower() == 'true'
# --- Inbound/Outbound Mode ---
INITIATION_MODE = os.getenv("INITIATION_MODE", "inbound").lower()
if INITIATION_MODE not in ("inbound", "outbound"):
    raise ValueError("INITIATION_MODE must be 'inbound' or 'outbound'")
INITIAL_BOT_MESSAGE = "Hello! I'm ready to assist you."

# Constants
SYSTEM_MESSAGE = (
    "Your knowledge cutoff is 2023-10. You are a helpful, witty, and friendly AI. "
    "Act like a human, but remember that you aren't a human..."
)
# --- Audio Processing Constants ---
EXOTEL_SAMPLE_RATE = 8000
OPENAI_SAMPLE_RATE = 24000

# --- FastAPI Setup ---
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Data Models ---
class CallRequest(BaseModel):
    message: str
    system: str

# --- API Endpoints ---
@app.post("/call")
async def update_prompt(data: CallRequest):
    global SYSTEM_MESSAGE
    SYSTEM_MESSAGE = data.system
    return {"status": "ok"}

@app.get("/status")
async def get_status():
    return {"system_prompt": SYSTEM_MESSAGE, "initiation_mode": INITIATION_MODE}

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Exotel Media Stream Server is running!"}

# --- RTMiddleTier Setup ---
if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_DEPLOYMENT:
    raise ValueError("Azure OpenAI endpoint and deployment are required.")

if AZURE_OPENAI_USE_AAD:
    credentials = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(credentials, "https://cognitiveservices.azure.com/.default")
else:
    if not AZURE_OPENAI_KEY:
        raise ValueError("Azure OpenAI key is required if not using AAD.")
    credentials = AzureKeyCredential(AZURE_OPENAI_KEY)

rt_middle_tier = RTMiddleTier(
    endpoint=AZURE_OPENAI_ENDPOINT,
    deployment=AZURE_OPENAI_DEPLOYMENT,
    credentials=credentials,
    voice_choice=AZURE_OPENAI_VOICE_CHOICE
)

# Example tool
async def search_knowledge_base(query: dict) -> ToolResult:
    results = f"Simulated search results for: {query['search_term']}"
    return ToolResult(results, ToolResultDirection.TO_CLIENT)

knowledge_base_tool_schema = {
  "name": "search_knowledge_base",
  "description": "Searches a knowledge base.",
  "parameters": {
      "type": "object",
      "properties": {"search_term": {"type": "string", "description": "The search query"}},
      "required": ["search_term"]
  }
}
knowledge_base_tool = Tool(target=search_knowledge_base, schema=knowledge_base_tool_schema)
rt_middle_tier.tools["search_knowledge_base"] = knowledge_base_tool
rt_middle_tier.system_message = SYSTEM_MESSAGE

# --- Downsampling Function ---
def downsample_audio(audio_data, from_rate=OPENAI_SAMPLE_RATE, to_rate=EXOTEL_SAMPLE_RATE):
    """Downsamples audio data from OpenAI's sample rate to Exotel's sample rate."""
    number_of_samples = round(len(audio_data) * to_rate / from_rate)
    resampled = signal.resample(audio_data, number_of_samples)
    return resampled.astype(np.int16)

# --- WebSocket Handling ---
@app.websocket("/handle-call")
async def handle_media_stream(websocket: WebSocket):
    print("Exotel client connected")
    await websocket.accept()
    try:
        headers = {}
        if AZURE_OPENAI_USE_AAD:
            headers["Authorization"] = f"Bearer {token_provider()}"
        else:
            headers["api-key"] = AZURE_OPENAI_KEY

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.ws_connect(
                f"ws://localhost:8765/realtime",  # Connect to rtmt.py
            ) as rtmt_ws:

                stream_sid = None  # Initialize stream_sid
                session_initialized = False

                async def receive_from_exotel():
                    nonlocal stream_sid  # Correctly reference nonlocal variable
                    try:
                        while True:
                            message = await websocket.receive_text()
                            data = json.loads(message)
                           
                            if data['event'] == 'media':
                                if rtmt_ws.closed: return
                                audio_payload = data['media']['payload']
                                await rtmt_ws.send_str(json.dumps({
                                     "type": "input_audio_buffer.append",
                                     "audio": audio_payload,
                                }))
                            elif data['event'] == 'start':
                                # Store the stream_sid when received
                                stream_sid = data['stream_sid']
                                print(f"Incoming stream started: {stream_sid}")
                            elif data['event'] == 'connected':
                                print("Exotel connected")
                            elif data['event'] == 'dtmf':
                                print(f"DTMF: {data['dtmf']['digit']}")
                            elif data['event'] == 'stop':
                                print(f"Stream stopped: {data['stop']['reason']}")
                                if not rtmt_ws.closed: await rtmt_ws.close()
                                return
                            else:
                                print(f"Unknown Exotel event: {data['event']}")
                    except WebSocketDisconnect:
                        print("Exotel disconnected.")
                    except Exception as e:
                        print(f"Error in receive_from_exotel: {e}")
                    finally:
                        if not rtmt_ws.closed: await rtmt_ws.close()

                async def send_to_exotel():
                    nonlocal session_initialized
                    try:
                        async for msg in rtmt_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                # --- Robust JSON Parsing ---
                                try:
                                    response = json.loads(msg.data)
                                except json.JSONDecodeError as e:
                                    print(f"JSONDecodeError: {e} - Data: {msg.data}")
                                    continue  # Skip to the next message

                                truncated_response = str(response)[:600] + "..." if len(str(response)) > 100 else str(response)
                                print(f"Received from RTMiddleTier: {truncated_response}")
                                if response.get("type") == "session.created" and not session_initialized:
                                    await rtmt_ws.send_str(json.dumps({
                                        "type": "session.update",
                                        "session": {
                                            "instructions": SYSTEM_MESSAGE,
                                            "voice": AZURE_OPENAI_VOICE_CHOICE,
                                            "turn_detection": {"type": "server_vad"}
                                        }
                                    }))
                                    session_initialized = True

                                    # --- Inbound/Outbound Logic ---
                                    if INITIATION_MODE == "outbound":
                                        await rtmt_ws.send_str(json.dumps({
                                            "type": "conversation.item.create",
                                            "item": {
                                                "type": "text",
                                                "text": INITIAL_BOT_MESSAGE
                                            }
                                        }))
                                        await rtmt_ws.send_str(json.dumps({
                                            "type": "response.create"
                                        }))
                                    elif INITIATION_MODE == "inbound":
                                        # Start listening for audio input immediately
                                        await rtmt_ws.send_str(json.dumps({
                                            "type": "response.create"
                                        }))

                                elif response.get("type") == "response.audio.delta" and response.get("delta"):
                                    # Audio data delta - truncate to 30 chars for logging
                                    truncated_response = response.copy()
                                    truncated_response["delta"] = response["delta"][:30] + "..."
                                    print(f"Received from RTMiddleTier: {response['delta'][:30]}...")
                                    # Keep message as is
                                    pass  # Keep message as is
                                    audio_payload = response.get("delta")
                                    if audio_payload:
                                        # --- DOWNSAMPLING (Corrected) ---
                                        audio_bytes = base64.b64decode(audio_payload)
                                        audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
                                        downsampled_audio = downsample_audio(audio_array)
                                        downsampled_bytes = downsampled_audio.tobytes()

                                        # Send audio to Exotel
                                        await websocket.send_text(json.dumps({
                                            "event": "media",
                                            "stream_sid": stream_sid,
                                            "media": {"payload": base64.b64encode(downsampled_bytes).decode("ascii")}
                                        }))

                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f"RTMiddleTier WebSocket error: {rtmt_ws.exception()}")
                                break  # Exit on error

                    except Exception as e:
                        print(f"Error in send_to_exotel: {e}")
                    finally:
                        if not rtmt_ws.closed: await rtmt_ws.close()

                await asyncio.gather(receive_from_exotel(), send_to_exotel())

    except Exception as e:
        print(f"Error connecting to RTMiddleTier: {e}")
    finally:
        print("Closing WebSocket connection")

# --- Dependency ---
async def get_aiohttp_request(request: Request) -> web.Request:
    return web.Request(request.scope, request.receive, request._send)

# --- Register RTMiddleTier ---
app.add_api_route("/realtime", lambda request: rt_middle_tier._websocket_handler(request=request), methods=["GET"], response_model=None)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)