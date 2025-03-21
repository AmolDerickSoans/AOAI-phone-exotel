import asyncio
import json
import logging
from typing import Dict, Optional
import numpy as np
from scipy import signal
import base64
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
        self.sample_rate = 24000  # Default sample rate for Azure OpenAI
        
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
        self.audio_buffers = {}
        self.current_response_ids = {}  # Track current response IDs
        self.speech_active = {}  # Track if speech is currently active
        
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
            endpoint = self.endpoint.rstrip('/')
            ws_url = f"{endpoint}/openai/realtime"
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
                    "voice": self.voice_choice or "alloy",
                    "input_audio_format": "pcm16",  # Changed from g711_ulaw to pcm16
                    "output_audio_format": "pcm16",  # Changed from g711_ulaw to pcm16
                    "turn_detection": {"type": "server_vad"},
                    "vad": {
                        "speech_activity_threshold": 0.8,        # Higher threshold for more precision
                        "speech_start_threshold_ms": 200,        # Wait 200ms before considering speech started
                        "speech_end_threshold_ms": 1000,         # Wait 1000ms of silence before ending turn
                        "speech_timeout_threshold_ms": 15000     # Max 15s for a single utterance
                    },
                    "sample_rate": self.sample_rate,
                    "modalities": ["text", "audio"]
                }
            }
            await ws.send_json(init_message)

                            # Send initial conversation setup
            init_conversation = {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "system",
                        "content": [
                            {
                                "type": "text",
                                "text": "This is a voice conversation."
                            }
                        ]
                    }
                }
            await ws.send_json(init_conversation)
            logger.info(f"Sent initial conversation setup for call {call_sid}")
                
                # Also send a single response.create at initialization time
            await ws.send_json({"type": "response.create"})

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
            current_text_buffer = ""
            
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    msg_type = data.get("type", "unknown")
                    logger.debug(f"Received message type: {msg_type}")
                    
                    # Log any errors from the service
                    if msg_type == "error":
                        logger.error(f"Error from RTMT service: {data}")
                        continue
                        
                    # Handle session creation confirmation
                    if msg_type == "session.created":
                        logger.info(f"Session successfully created for call {call_sid}")
                        continue
                    
                    # Extract text from response items
                    if msg_type == "response.output_item.added":
                        item = data.get("item", {})
                        if item.get("type") == "text":
                            content = item.get("content", "")
                            if content:
                                current_text_buffer += content
                                logger.info(f"Added to text response: {content[:50]}...")
                    
                    # Text content completed, send it to subscribers
                    if msg_type == "response.output_item.done":
                        item = data.get("item", {})
                        if item.get("type") == "text" and current_text_buffer:
                            logger.info(f"Text response completed: {current_text_buffer[:50]}...")
                            await self.response_queues[call_sid].put(current_text_buffer)
                            current_text_buffer = ""  # Reset buffer
                    
                    # Handle audio.delta events for streaming audio responses
                    if msg_type == "response.audio.delta" and data.get("delta"):
                        # For now, we just log this - we're not sending audio back in this 
                        # implementation as that's handled in the exotel.py layer
                        logger.debug(f"Received audio delta for call {call_sid}")
                        
                    # Handle speech detection events
                    if msg_type == "input_audio_buffer.speech_started":
                        logger.info(f"Speech started detected for call {call_sid}")
                        
                    if msg_type == "input_audio_buffer.speech_stopped":
                        logger.info(f"Speech stopped detected for call {call_sid}")
                        
                    # Handle rate limits
                    if msg_type == "rate_limits.updated":
                        limits = data.get("rate_limits", {})
                        logger.debug(f"Rate limits updated for call {call_sid}: {limits}")
                        
                    # Response completed
                    if msg_type == "response.done":
                        logger.info(f"Response completed for call {call_sid}")
                        # If there's any remaining text in the buffer, send it
                        if current_text_buffer:
                            await self.response_queues[call_sid].put(current_text_buffer)
                            current_text_buffer = ""
                
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
        """
        Process audio chunk and return transcription if available.
        
        This method converts μ-law audio from Exotel to PCM16 format expected by Azure.
        """
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
            
            # Convert μ-law audio to PCM16
            try:
                # Assume audio_chunk is μ-law encoded from Exotel (8-bit μ-law, 8kHz)
                mulaw_data = np.frombuffer(audio_chunk, dtype=np.uint8)
                
                # First convert to linear PCM (still at 8kHz)
                pcm_data = self._mulaw_to_linear(mulaw_data)
                
                # Then upsample to the correct sample rate for Azure OpenAI (typically 24kHz)
                upsampled_data = self._upsample_audio(pcm_data, 8000, self.sample_rate)
                
                # Convert to bytes
                audio_bytes = upsampled_data.tobytes()
                
                # Convert to base64 for sending over WebSocket
                audio_base64 = base64.b64encode(audio_bytes).decode('ascii')
                
                # Send the converted PCM16 audio
                await ws.send_json({
                    "type": "input_audio_buffer.append",
                    "audio": audio_base64
                })
                
                logger.debug(f"Sent {len(audio_bytes)} bytes of PCM16 audio for call {call_sid}")
            except Exception as e:
                logger.error(f"Error converting audio: {str(e)}")
                # Fallback: send original audio
                audio_base64 = base64.b64encode(audio_chunk).decode('ascii')
                await ws.send_json({
                    "type": "input_audio_buffer.append", 
                    "audio": audio_base64
                })
                logger.warning(f"Used fallback method to send original audio for call {call_sid}")
            
            # Check if there's a response with a brief timeout
            try:
                return await asyncio.wait_for(self.response_queues[call_sid].get(), 0.5)
            except asyncio.TimeoutError:
                # No response available yet, that's normal
                return None
            
        except Exception as e:
            logger.error(f"Error processing audio for call {call_sid}: {str(e)}", exc_info=True)
            return None

    def _upsample_audio(self, audio_data, from_rate, to_rate):
        """Upsample audio to higher sample rate"""
        number_of_samples = round(len(audio_data) * to_rate / from_rate)
        resampled = signal.resample(audio_data, number_of_samples)
        return resampled.astype(np.int16)

    def _mulaw_to_linear(self, mulaw_data):
        """Convert mu-law audio to linear PCM"""
        # Standard mu-law decoding
        mu = 255
        y = mulaw_data.astype(np.float32)
        y = 2 * (y / mu) - 1
        x = np.sign(y) * (1 / mu) * ((1 + mu)**abs(y) - 1)
        return (x * 32767).astype(np.int16)  # Convert to 16-bit PCM

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
        
        if call_sid in self.audio_buffers:
            del self.audio_buffers[call_sid]
            
        logger.info(f"Closed RTMT connection for call {call_sid}")