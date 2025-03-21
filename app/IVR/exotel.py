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
                            print(f"Received from Exotel: {data['event']}")
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
                                print("Exotel connected")  # Log, but no action needed
                            elif data['event'] == 'dtmf':
                                print(f"DTMF: {data['dtmf']['digit']}") #Log
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
                    nonlocal session_initialized  # Correctly reference nonlocal variable
                    try:
                        async for msg in rtmt_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                response = json.loads(msg.data)
                                print(f"Received from RTMiddleTier: {response}")

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

                                elif response.get("type") == "response.audio.delta":
                                    audio_payload = response.get("data")
                                    if audio_payload:
                                        audio_bytes = base64.b64decode(audio_payload)
                                        # Send audio to Exotel (already chunked correctly)
                                        await websocket.send_text(json.dumps({
                                            "event": "media",
                                            "stream_sid": stream_sid,  # Use the stored stream_sid
                                            "media": {"payload": base64.b64encode(audio_bytes).decode("ascii")}
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