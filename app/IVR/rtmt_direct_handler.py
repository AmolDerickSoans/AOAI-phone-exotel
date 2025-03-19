import asyncio
import json
import logging
import aiohttp
from typing import Dict, Optional

from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

logger = logging.getLogger('rtmt_direct')

class RTMTDirectHandler:
    def __init__(self, endpoint: str, deployment: str, credentials: AzureKeyCredential | DefaultAzureCredential, voice_choice: Optional[str] = None):
        self.endpoint = endpoint
        self.deployment = deployment
        self.voice_choice = voice_choice
        self.api_version = "2024-10-01-preview"
        
        # Handle credentials
        if isinstance(credentials, AzureKeyCredential):
            self.key = credentials.key
            self._token_provider = None
        else:
            self.key = None
            self._token_provider = get_bearer_token_provider(credentials, "https://cognitiveservices.azure.com/.default")
            self._token_provider()  # Warm up during startup
            
        # Store active connections
        self.connections = {}
        self.sessions = {}
        self.transcripts = {}
        
    async def create_rtmt_connection(self, call_sid: str):
        """Create a new RTMT connection for a specific call"""
        try:
            # Create a new session for this call
            session = aiohttp.ClientSession()
            self.sessions[call_sid] = session
            
            # Prepare headers
            headers = {}
            if self.key:
                headers["api-key"] = self.key
            else:
                headers["Authorization"] = f"Bearer {self._token_provider()}"
                
            # Connect to RTMT service
            params = {"api-version": self.api_version, "deployment": self.deployment}
            ws = await session.ws_connect(
                f"{self.endpoint}/openai/realtime",
                headers=headers,
                params=params
            )
            
            # Store the connection and initialize transcript buffer
            self.connections[call_sid] = ws
            self.transcripts[call_sid] = ""
            
            # Send session.create message with appropriate configurations
            await ws.send_json({
                "type": "session.create",
                "session": {
                    "instructions": "You're a voice assistant. Keep responses very brief, one or two sentences max.",
                    "temperature": 0.7,
                    "disable_audio": False,
                    "voice": self.voice_choice or "alloy"
                }
            })
            
            logger.info(f"Successfully created RTMT connection for call {call_sid}")
            return ws
            
        except Exception as e:
            logger.error(f"Error creating RTMT connection: {str(e)}")
            await self.close_connection(call_sid)
            raise
    
    async def process_audio(self, call_sid: str, audio_chunk: bytes) -> Optional[str]:
        """Process audio chunk and return transcription if available"""
        if call_sid not in self.connections:
            logger.warning(f"No connection found for call {call_sid}")
            return None
            
        ws = self.connections[call_sid]
        transcript = None
        
        try:
            # Send the audio chunk
            await ws.send_bytes(audio_chunk)
            
            # Process any available messages (non-blocking)
            async def process_messages():
                nonlocal transcript
                # Use a timeout to make this non-blocking
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=0.1)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        
                        # Handle different message types
                        if data.get("type") == "response.output_item.added":
                            item = data.get("item", {})
                            if item.get("type") == "text":
                                content = item.get("content", "")
                                if content:
                                    # Store and return only complete sentences
                                    self.transcripts[call_sid] += content
                                    transcript = self.transcripts[call_sid]
                                    logger.debug(f"Transcript for {call_sid}: {transcript}")
                                    
                        elif data.get("type") == "session.created":
                            logger.info(f"Session created for call {call_sid}")
                            
                        elif data.get("type") == "response.done":
                            # Reset transcript for next utterance
                            transcript = self.transcripts[call_sid]
                            self.transcripts[call_sid] = ""
                            
                except asyncio.TimeoutError:
                    pass  # No message available, that's fine
                except Exception as e:
                    logger.error(f"Error processing message: {str(e)}")
                    
            await process_messages()
            return transcript
            
        except Exception as e:
            logger.error(f"Error processing audio for call {call_sid}: {str(e)}")
            return None
    
    async def close_connection(self, call_sid: str):
        """Close an RTMT connection for a specific call"""
        try:
            if call_sid in self.connections:
                await self.connections[call_sid].close()
                del self.connections[call_sid]
                
            if call_sid in self.sessions:
                await self.sessions[call_sid].close()
                del self.sessions[call_sid]
                
            if call_sid in self.transcripts:
                del self.transcripts[call_sid]
                
            logger.info(f"Closed RTMT connection for call {call_sid}")
            
        except Exception as e:
            logger.error(f"Error closing RTMT connection: {str(e)}")