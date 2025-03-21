
import asyncio
import json
import logging
from enum import Enum
from typing import Any, Callable, Optional

import aiohttp
from aiohttp import web
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

logger = logging.getLogger("voicerag")

class ToolResultDirection(Enum):
    TO_SERVER = 1
    TO_CLIENT = 2

class ToolResult:
    text: str
    destination: ToolResultDirection

    def __init__(self, text: str, destination: ToolResultDirection):
        self.text = text
        self.destination = destination

    def to_text(self) -> str:
        if self.text is None:
            return ""
        return self.text if type(self.text) == str else json.dumps(self.text)

class Tool:
    target: Callable[..., ToolResult]
    schema: Any

    def __init__(self, target: Any, schema: Any):
        self.target = target
        self.schema = schema

class RTToolCall:
    tool_call_id: str
    previous_id: str

    def __init__(self, tool_call_id: str, previous_id: str):
        self.tool_call_id = tool_call_id
        self.previous_id = previous_id

class RTMiddleTier:
    endpoint: str
    deployment: str
    key: Optional[str] = None
    
    # Tools are server-side only for now, though the case could be made for client-side tools
    # in addition to server-side tools that are invisible to the client
    tools: dict[str, Tool] = {}

    # Server-enforced configuration, if set, these will override the client's configuration
    # Typically at least the model name and system message will be set by the server
    model: Optional[str] = None
    system_message: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    #disable_audio: Optional[bool] = None
    voice_choice: Optional[str] = None
    api_version: str = "2024-10-01-preview"
    _tools_pending = {}
    _token_provider = None

    def __init__(self, endpoint: str, deployment: str, credentials: AzureKeyCredential | DefaultAzureCredential, voice_choice: Optional[str] = None):
        self.endpoint = endpoint
        self.deployment = deployment
        self.voice_choice = voice_choice
        if voice_choice is not None:
            logger.info("Realtime voice choice set to %s", voice_choice)
        if isinstance(credentials, AzureKeyCredential):
            self.key = credentials.key
            logger.info("Using AzureKeyCredential")
        else:
            self._token_provider = get_bearer_token_provider(credentials, "https://cognitiveservices.azure.com/.default")
            logger.info("Using DefaultAzureCredential.  Attempting initial token fetch...")
            try:
                token = self._token_provider() # Warm up during startup so we have a token cached when the first request arrives
                logger.info(f"Initial token fetch successful: {token[:20]}...")  # Log first 20 chars of token
            except Exception as e:
                logger.error(f"Initial token fetch failed: {e}")
                raise  # Re-raise to prevent startup if token fetch fails
    async def _process_message_to_client(self, msg: str, client_ws: web.WebSocketResponse, server_ws: web.WebSocketResponse) -> Optional[str]:
        message = json.loads(msg.data)
        updated_message = msg.data
        if message is not None:
            match message["type"]:
                case "session.created":
                    session = message["session"]
                    # Hide the instructions, tools and max tokens from clients, if we ever allow client-side 
                    # tools, this will need updating
                    session["instructions"] = ""
                    session["tools"] = []
                    session["voice"] = self.voice_choice
                    session["tool_choice"] = "none"
                    session["max_response_output_tokens"] = None
                    updated_message = json.dumps(message)

                case "response.output_item.added":
                    if "item" in message and message["item"]["type"] == "function_call":
                        updated_message = None
                    elif "item" in message and message["item"]["type"] == "message": # ADD THIS
                        if "content" in message["item"]: # ADD THIS
                            for content_item in message["item"]["content"]: # ADD THIS
                                if content_item["type"] == "audio": # ADD THIS
                                    # Audio is not function call, so we pass it through.
                                    pass  # Keep message as is.

                case "conversation.item.created":
                    if "item" in message and message["item"]["type"] == "function_call":
                        item = message["item"]
                        if item["call_id"] not in self._tools_pending:
                            self._tools_pending[item["call_id"]] = RTToolCall(item["call_id"], message["previous_item_id"])
                        updated_message = None
                    elif "item" in message and message["item"]["type"] == "function_call_output":
                        updated_message = None

                case "response.function_call_arguments.delta":
                    updated_message = None
                
                case "response.function_call_arguments.done":
                    updated_message = None

                case "response.output_item.done":
                    if "item" in message and message["item"]["type"] == "function_call":
                        item = message["item"]
                        tool_call = self._tools_pending[message["item"]["call_id"]]
                        tool = self.tools[item["name"]]
                        args = item["arguments"]
                        result = await tool.target(json.loads(args))
                        await server_ws.send_json({
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output",
                                "call_id": item["call_id"],
                                "output": result.to_text() if result.destination == ToolResultDirection.TO_SERVER else ""
                            }
                        })
                        if result.destination == ToolResultDirection.TO_CLIENT:
                            # TODO: this will break clients that don't know about this extra message, rewrite 
                            # this to be a regular text message with a special marker of some sort
                            await client_ws.send_json({
                                "type": "extension.middle_tier_tool_response",
                                "previous_item_id": tool_call.previous_id,
                                "tool_name": item["name"],
                                "tool_result": result.to_text()
                            })
                        updated_message = None

                case "response.done":
                    if len(self._tools_pending) > 0:
                        self._tools_pending.clear() # Any chance tool calls could be interleaved across different outstanding responses?
                        await server_ws.send_json({
                            "type": "response.create"
                        })
                    if "response" in message:
                        replace = False
                        for i, output in enumerate(reversed(message["response"]["output"])):
                            if output["type"] == "function_call":
                                message["response"]["output"].pop(i)
                                replace = True
                        if replace:
                            updated_message = json.dumps(message)                        
                case "response.content_part.added": # ADD THIS
                    if "part" in message and message["part"]["type"] == "audio": # ADD THIS
                        # Audio content part added, keep it
                        pass # Keep message as is

                case "response.audio_transcript.delta": # ADD THIS
                    # Audio transcript delta,keep it
                    pass # Keep message as is
                case "response.audio.delta": # VERY IMPORTANT - Add handling for audio data
                    # Audio data delta - keep the message
                    pass  # Keep message as is

        return updated_message
    async def _process_message_to_server(self, msg: str, ws: web.WebSocketResponse) -> Optional[str]:
        message = json.loads(msg.data)
        updated_message = msg.data
        if message is not None:
            match message["type"]:
                case "session.update":
                    session = message["session"]
                    if self.system_message is not None:
                        session["instructions"] = self.system_message
                    if self.temperature is not None:
                        session["temperature"] = self.temperature
                    if self.max_tokens is not None:
                        session["max_response_output_tokens"] = self.max_tokens
                    # Remove this line:
                    # if self.disable_audio is not None:
                    #     session["disable_audio"] = self.disable_audio
                    if self.voice_choice is not None:
                        session["voice"] = self.voice_choice
                    session["tool_choice"] = "auto" if len(self.tools) > 0 else "none"
                    session["tools"] = [tool.schema for tool in self.tools.values()]
                    updated_message = json.dumps(message)

        return updated_message

    async def _forward_messages(self, ws: web.WebSocketResponse):
        async with aiohttp.ClientSession(base_url=self.endpoint) as session:
            params = { "api-version": self.api_version, "deployment": self.deployment}
            logger.info(f"Connecting to Azure OpenAI with params: {params}")
            headers = {}
            if "x-ms-client-request-id" in ws.headers:
                headers["x-ms-client-request-id"] = ws.headers["x-ms-client-request-id"]
            if self.key is not None:
                headers = { "api-key": self.key }
                logger.info("Using API Key for authentication.")
            else:
                logger.info("Using AAD token for authentication. Fetching token...")
                try:
                    token = self._token_provider()
                    logger.info(f"Token fetched successfully: {token[:20]}...") # Log first 20 chars
                    headers["Authorization"] = f"Bearer {token}"
                except Exception as e:
                    logger.error(f"Failed to fetch AAD token: {e}")
                    await ws.send_str(f"Error: Failed to authenticate with Azure OpenAI: {e}")  # Inform the client
                    return  # Exit if we can't get a token

            logger.info(f"Request Headers: {headers}")  # Log the headers

            try:
                async with session.ws_connect("/openai/realtime", headers=headers, params=params) as target_ws:
                    logger.info("Connected to Azure OpenAI realtime endpoint.")

                    async def from_client_to_server():
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                new_msg = await self._process_message_to_server(msg, ws)
                                if new_msg is not None:
                                    await target_ws.send_str(new_msg)
                            else:
                                print("Error: unexpected message type:", msg.type)
                        
                        # Means it is gracefully closed by the client then time to close the target_ws
                        if target_ws:
                            print("Closing OpenAI's realtime socket connection.")
                            await target_ws.close()
                            
                    async def from_server_to_client():
                        async for msg in target_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                new_msg = await self._process_message_to_client(msg, ws, target_ws)
                                if new_msg is not None:
                                    await ws.send_str(new_msg)
                            else:
                                print("Error: unexpected message type:", msg.type)

                    try:
                        await asyncio.gather(from_client_to_server(), from_server_to_client())
                    except ConnectionResetError:
                        # Ignore the errors resulting from the client disconnecting the socket
                        pass
                    except aiohttp.ClientResponseError as e:
                        logger.error(f"Azure OpenAI ClientResponseError: {e.status}, {e.message}, {e.headers}")
                        await ws.send_str(f"Error from Azure OpenAI: {e.status} - {e.message}")
                    except Exception as e:
                        logger.exception(f"An unexpected error occurred: {e}")
                        await ws.send_str(f"An unexpected error occurred: {type(e).__name__} - {e}")

            except aiohttp.ClientConnectorError as e:
                logger.error(f"Failed to connect to Azure OpenAI: {e}")
                await ws.send_str(f"Error: Failed to connect to Azure OpenAI: {e}")
            except Exception as e:
                logger.exception(f"An unexpected error occurred during connection: {e}")
                await ws.send_str(f"An unexpected error occurred: {type(e).__name__} - {e}")


    async def _websocket_handler(self, request: web.Request):
        logger.info(f"Received websocket connection request: {request.headers}")  # Log incoming request headers
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await self._forward_messages(ws)
        return ws
    
    def attach_to_app(self, app, path):
        app.router.add_get(path, self._websocket_handler)
