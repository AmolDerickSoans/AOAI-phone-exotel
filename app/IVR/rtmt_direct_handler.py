import asyncio
import json
import logging
from typing import Dict, Optional

from aiohttp import web
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from ..backend.rtmt import RTMiddleTier

logger = logging.getLogger('rtmt_direct')

class RTMTDirectHandler:
    def __init__(self, endpoint: str, deployment: str, credentials: AzureKeyCredential | DefaultAzureCredential, voice_choice: Optional[str] = None):
        self.rtmt = RTMiddleTier(endpoint, deployment, credentials, voice_choice)
        self.connections: Dict[str, web.WebSocketResponse] = {}
        
    async def create_rtmt_connection(self, call_sid: str) -> web.WebSocketResponse:
        """Create a new RTMT connection for a specific call"""
        try:
            # Create a new WebSocket response
            ws = web.WebSocketResponse()
            
            # Initialize session with RTMT
            await ws.prepare(web.Request())  # Create a dummy request object for WebSocket preparation
            
            # Store the connection
            self.connections[call_sid] = ws
            
            # Start the RTMT message forwarding
            asyncio.create_task(self.rtmt._forward_messages(ws))
            
            logger.info(f"Successfully created RTMT connection for call {call_sid}")
            return ws
            
        except Exception as e:
            logger.error(f"Error creating RTMT connection: {str(e)}")
            raise
    
    async def process_audio(self, call_sid: str, audio_chunk: bytes) -> Optional[str]:
        """Process audio chunk and return transcription if available"""
        try:
            if call_sid not in self.connections:
                return None
                
            ws = self.connections[call_sid]
            
            # Send audio chunk to RTMT
            await ws.send_bytes(audio_chunk)
            
            # Process response
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    
                    # Handle different message types
                    if data.get("type") == "response.output_item.added":
                        item = data.get("item", {})
                        if item.get("type") == "text":
                            return item.get("content")
                            
                    elif data.get("type") == "response.done":
                        break
                        
            return None
            
        except Exception as e:
            logger.error(f"Error processing audio: {str(e)}")
            return None
    
    async def close_connection(self, call_sid: str):
        """Close an RTMT connection for a specific call"""
        if call_sid in self.connections:
            try:
                await self.connections[call_sid].close()
                logger.info(f"Closed RTMT connection for call {call_sid}")
            except Exception as e:
                logger.error(f"Error closing RTMT connection: {str(e)}")
            finally:
                del self.connections[call_sid]