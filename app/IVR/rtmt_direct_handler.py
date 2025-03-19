import asyncio
import json
import logging
from typing import Dict, Optional

import aiohttp
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

logger = logging.getLogger('rtmt_direct')

class RTMTDirectHandler:
    def __init__(self, endpoint: str, deployment: str, credentials: AzureKeyCredential | DefaultAzureCredential, voice_choice: Optional[str] = None):
        # Log configuration
        logger.info(f"Initializing RTMT Direct Handler with endpoint: {endpoint}, deployment: {deployment}")
        
        self.endpoint = endpoint
        self.deployment = deployment
        self.voice_choice = voice_choice
        self.api_version = "2024-10-01-preview"  # Using a stable API version
        
        # Handle credentials
        if isinstance(credentials, AzureKeyCredential):
            self.key = credentials.key
            self._token_provider = None
            logger.info("Using Azure Key Credential for authentication")
        else:
            self.key = None
            self._token_provider = get_bearer_token_provider(credentials, "https://cognitiveservices.azure.com/.default")
            logger.info("Using Azure Identity Credential for authentication")
            
        # Track connections and sessions
        self.connections = {}  # Store WebSocket connections by call_sid
        self.sessions = {}     # Store aiohttp sessions by call_sid
        self.response_queues = {}  # Store response queues by call_sid
        self.processing_tasks = {}  # Store background tasks by call_sid
        
    async def create_rtmt_connection(self, call_sid: str):
        """Create a new RTMT connection for a specific call"""
        try:
            # Create a session for this call
            logger.info(f"Creating aiohttp session for call {call_sid}")
            session = aiohttp.ClientSession()
            self.sessions[call_sid] = session
            self.response_queues[call_sid] = asyncio.Queue()
            
            # Prepare headers
            headers = {}
            if self.key:
                headers["api-key"] = self.key
            else:
                token = self._token_provider()
                headers["Authorization"] = f"Bearer {token}"
                
            # Connect to the WebSocket
            ws_url = f"{self.endpoint}/openai/realtime"
            params = {"api-version": self.api_version, "deployment": self.deployment}
            logger.info(f"Connecting to WebSocket: {ws_url} with params: {params}")
            
            ws = await session.ws_connect(ws_url, params=params, headers=headers)
            self.connections[call_sid] = ws
            logger.info(f"WebSocket connection established for call {call_sid}")
            
            # Create and store the response processing task
            process_task = asyncio.create_task(self._process_responses(call_sid))
            self.processing_tasks[call_sid] = process_task
            
            # Initialize the session with proper configuration
            logger.info(f"Initializing RTMT session for call {call_sid}")
            init_message = {
                "type": "session.update",
                "session": {
                    "instructions": "You are a helpful assistant.",
                    "temperature": 0.7,
                    "max_response_output_tokens": 500,
                    # "disable_audio": False,
                    "voice": self.voice_choice or "alloy"
                }
            }
            await ws.send_json(init_message)
            logger.info(f"Sent session initialization message for call {call_sid}")
            
            return ws
            
        except Exception as e:
            logger.error(f"Error creating RTMT connection: {str(e)}", exc_info=True)
            # Clean up any resources
            await self.close_connection(call_sid)
            raise
    
    async def _process_responses(self, call_sid: str):
        """Process WebSocket responses in background"""
        try:
            if call_sid not in self.connections:
                logger.error(f"No connection found for call {call_sid} in _process_responses")
                return
                
            ws = self.connections[call_sid]
            logger.info(f"Started response processor for call {call_sid}")
            
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    logger.debug(f"Received message type: {data.get('type')}")
                    
                    # Log any errors from the service
                    if data.get("type") == "error":
                        logger.error(f"Error from RTMT service: {data}")
                        continue
                        
                    # Handle session creation confirmation
                    if data.get("type") == "session.created":
                        logger.info(f"Session successfully created for call {call_sid}")
                        continue
                    
                    # Extract text from response items
                    if data.get("type") == "response.output_item.added":
                        item = data.get("item", {})
                        if item.get("type") == "text":
                            content = item.get("content")
                            if content:
                                logger.info(f"Received text response: {content[:50]}...")
                                await self.response_queues[call_sid].put(content)
                
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.warning(f"WebSocket closed for call {call_sid}")
                    break
                    
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error for call {call_sid}: {ws.exception()}")
                    break
                    
        except asyncio.CancelledError:
            logger.info(f"Response processor task cancelled for call {call_sid}")
        except Exception as e:
            logger.error(f"Error in response processor for call {call_sid}: {str(e)}", exc_info=True)
        finally:
            # Don't close the connection here, as that would cause problems
            # if the connection is still being used
            logger.info(f"Response processor ending for call {call_sid}")
    
    async def process_audio(self, call_sid: str, audio_chunk: bytes) -> Optional[str]:
        """Process audio chunk and return transcription if available"""
        try:
            if call_sid not in self.connections:
                logger.warning(f"No connection found for call {call_sid}")
                return None
                
            ws = self.connections[call_sid]
            
            if ws.closed:
                logger.warning(f"Connection closed for call {call_sid}, attempting to reconnect")
                try:
                    await self.close_connection(call_sid)
                    await self.create_rtmt_connection(call_sid)
                    ws = self.connections[call_sid]
                except Exception as e:
                    logger.error(f"Failed to reconnect for call {call_sid}: {str(e)}")
                    return None
            
            # Create a new response for each audio chunk
            await ws.send_json({"type": "response.create"})
            
            # Send the audio chunk
            await ws.send_bytes(audio_chunk)
            
            # Check if there's a response with a brief timeout
            try:
                return await asyncio.wait_for(self.response_queues[call_sid].get(), 0.5)
            except asyncio.TimeoutError:
                # No response available yet, that's normal
                return None
            
        except Exception as e:
            logger.error(f"Error processing audio for call {call_sid}: {str(e)}", exc_info=True)
            return None
    
    async def close_connection(self, call_sid: str):
        """Close an RTMT connection for a specific call"""
        logger.info(f"Closing connection for call {call_sid}")
        
        # Cancel the processing task if it exists
        if call_sid in self.processing_tasks:
            try:
                self.processing_tasks[call_sid].cancel()
                await asyncio.sleep(0.1)  # Give it a moment to clean up
            except Exception as e:
                logger.error(f"Error cancelling processing task: {str(e)}")
            finally:
                del self.processing_tasks[call_sid]
        
        # Close the WebSocket if it exists
        if call_sid in self.connections:
            try:
                ws = self.connections[call_sid]
                if not ws.closed:
                    await ws.close()
            except Exception as e:
                logger.error(f"Error closing WebSocket: {str(e)}")
            finally:
                del self.connections[call_sid]
        
        # Close the session if it exists
        if call_sid in self.sessions:
            try:
                await self.sessions[call_sid].close()
            except Exception as e:
                logger.error(f"Error closing session: {str(e)}")
            finally:
                del self.sessions[call_sid]
        
        # Clean up the response queue
        if call_sid in self.response_queues:
            del self.response_queues[call_sid]
            
        logger.info(f"Closed RTMT connection for call {call_sid}")