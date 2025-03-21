import asyncio
import base64
import json
import sys
import websockets
import ssl
from pydub import AudioSegment
import numpy as np
import os
import time
import logging
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from .rtmt_direct_handler import RTMTDirectHandler
load_dotenv()

# Set up proper logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)  # Force output to stdout
    ]
)

logger = logging.getLogger('websocket_server')

subscribers = {}

class AudioHandler:
    def __init__(self, endpoint: str, deployment: str, credentials: AzureKeyCredential | DefaultAzureCredential, voice_choice: Optional[str] = None):
        self.rtmt_handler = RTMTDirectHandler(endpoint, deployment, credentials, voice_choice)
        self.connections = {}  # Store connections by call_sid

    async def create_connection(self, call_sid: str):
        """Create a new RTMT connection for a specific call"""
        try:
            connection = await self.rtmt_handler.create_rtmt_connection(call_sid)
            self.connections[call_sid] = connection
            logger.info(f"Successfully created RTMT connection for call {call_sid}")
            return connection
        except Exception as e:
            logger.error(f"Error creating RTMT connection: {str(e)}")
            raise

    async def process_audio(self, call_sid: str, audio_chunk: bytes) -> Optional[str]:
        """Process audio chunk and return transcription if available"""
        try:
            transcript = await self.rtmt_handler.process_audio(call_sid, audio_chunk)
            if transcript and call_sid in subscribers:
                # Convert result to JSON string with is_final always True for RTMT
                message = json.dumps({
                    "call_sid": call_sid,
                    "transcript": transcript,
                    "is_final": True
                })
                
                for client_queue in subscribers[call_sid]:
                    client_queue.put_nowait(message)
            return transcript
        except Exception as e:
            logger.error(f"Error processing audio: {str(e)}")
            return None

    async def close_connection(self, call_sid: str):
        """Close an RTMT connection for a specific call"""
        try:
            await self.rtmt_handler.close_connection(call_sid)
            if call_sid in self.connections:
                del self.connections[call_sid]
            logger.info(f"Closed RTMT connection for call {call_sid}")
        except Exception as e:
            logger.error(f"Error closing RTMT connection: {str(e)}")

# Create a global audio handler
# audio_handler = AudioHandler() # Moved inside main to use environment variable

### Starting exotel Web Handler
async def exotel_handler(exotel_ws):
    stream_sid = None
    call_sid = None
    input_audio_queue = asyncio.Queue()  # Queue to store input audio for echo

    # Buffer for audio processing
    BUFFER_SIZE = 20 * 160  # Same as your original code

    async def exotel_receiver(exotel_ws):
        nonlocal stream_sid, call_sid
        inbuffer = bytearray(b'')
        logger.info('exotel_receiver started')

        # Variables to track timestamps
        inbound_chunks_started = False
        latest_inbound_timestamp = 0

        try:
            async for message in exotel_ws:
                data = json.loads(message)

                if data['event'] == 'start':
                    start = data['start']
                    stream_sid = start['stream_sid']
                    call_sid = start['call_sid']
                    logger.info(f'Call started: {call_sid}')

                    # Initialize subscribers list for this call_sid
                    subscribers[call_sid] = []

                    # Create a new RTMT connection for this call
                    connection = await audio_handler.create_connection(call_sid)

                elif data['event'] == 'connected':
                    logger.info('Call connected')

                elif data['event'] == 'media':
                    media = data['media']
                    chunk = base64.b64decode(media['payload'])

                    # Process inbound audio
                    if inbound_chunks_started:
                        if latest_inbound_timestamp + 20 < int(media['timestamp']):
                            bytes_to_fill = 8 * (int(media['timestamp']) - (latest_inbound_timestamp + 20))
                            # Fill with silence (0xff for mulaw)
                            inbuffer.extend(b'\xff' * bytes_to_fill)
                    else:
                        inbound_chunks_started = True
                        latest_inbound_timestamp = int(media['timestamp'])

                    latest_inbound_timestamp = int(media['timestamp'])
                    inbuffer.extend(chunk)

                    # Store the input audio chunk for sending back (echo)
                    input_audio_queue.put_nowait(chunk)

                    # If we have enough audio data, send it to RTMT
                    while len(inbuffer) >= BUFFER_SIZE:
                        if call_sid in audio_handler.connections:
                            try:
                                await audio_handler.process_audio(call_sid, inbuffer[:BUFFER_SIZE])
                            except Exception as e:
                                logger.error(f'Error processing audio for call {call_sid}: {str(e)}')
                                logger.info(f'Attempting to reconnect RTMT for call {call_sid}')
                                await audio_handler.close_connection(call_sid)
                                connection = await audio_handler.create_connection(call_sid)
                            inbuffer = inbuffer[BUFFER_SIZE:]
                        else:
                            logger.warning(f'No RTMT connection found for call {call_sid}')
                            break

                elif data['event'] == 'stop':
                    logger.info('Call stopped')

                    # Close the Deepgram connection for this call
                    await audio_handler.close_connection(call_sid)

                    # Notify subscribers that the call has ended
                    if call_sid in subscribers:
                        for client in subscribers[call_sid]:
                            client.put_nowait('close')

                        del subscribers[call_sid]

                    break

        except Exception as e:
            logger.error(f"Error in exotel_receiver: {str(e)}")

            # Close the Deepgram connection if there was an error
            if call_sid:
                await audio_handler.close_connection(call_sid)

    async def exotel_sender(exotel_ws):
        ''' Send the received input audio back to exotel (echo functionality) '''
        logger.info('exotel_sender started')

        try:
            while True:
                chunk = await input_audio_queue.get()
                if not chunk:
                    break

                # Chunk the audio as per Exotel requirements
                EXOTEL_MIN_CHUNK_SIZE = 3200
                EXOTEL_MAX_CHUNK_SIZE = 100000
                EXOTEL_CHUNK_MULTIPLE = 320

                exotel_audio = np.frombuffer(chunk, dtype=np.uint8)
                exotel_audio_bytes = exotel_audio.tobytes()

                valid_chunk_size = max(
                    EXOTEL_MIN_CHUNK_SIZE,
                    min(
                        EXOTEL_MAX_CHUNK_SIZE,
                        (len(exotel_audio_bytes) // EXOTEL_CHUNK_MULTIPLE) * EXOTEL_CHUNK_MULTIPLE
                    )
                )

                chunked_payloads = [
                    exotel_audio_bytes[i:i + valid_chunk_size]
                    for i in range(0, len(exotel_audio_bytes), valid_chunk_size)
                ]

                # Send each chunk with appropriate metadata
                for chunk in chunked_payloads:
                    audio_payload = base64.b64encode(chunk).decode("ascii")
                    audio_delta = {
                        "event": "media",
                        "stream_sid": stream_sid,
                        "media": {
                            "payload": audio_payload
                        }
                    }

                    await exotel_ws.send(json.dumps(audio_delta))

        except Exception as e:
            logger.error(f"Error in exotel_sender: {str(e)}")

    # Start the tasks
    await asyncio.gather(
        exotel_receiver(exotel_ws),
        exotel_sender(exotel_ws)
    )

    await exotel_ws.close()

async def client_handler(client_ws):
    client_queue = asyncio.Queue()

    # First tell the client all active calls
    await client_ws.send(json.dumps(list(subscribers.keys())))
    logger.info(f"Sent active calls to client: {list(subscribers.keys())}")

    try:
        # Get the call_sid from the client
        call_sid = await client_ws.recv()
        call_sid = call_sid.strip()
        logger.info(f"Client requested to subscribe to call_sid: {call_sid}")

        if call_sid in subscribers:
            subscribers[call_sid].append(client_queue)
            logger.info(f"Client subscribed to call_sid: {call_sid}")
        else:
            logger.warning(f"Client tried to subscribe to non-existent call_sid: {call_sid}")
            await client_ws.close()
            return

        async def client_sender(client_ws):
            try:
                while True:
                    message = await client_queue.get()
                    if message == 'close':
                        logger.info("Received close message for client")
                        break

                    await client_ws.send(message)
            except Exception as e:
                logger.error(f"Error sending to client: {str(e)}")
            finally:
                # Remove this client queue from subscribers
                if call_sid in subscribers and client_queue in subscribers[call_sid]:
                    subscribers[call_sid].remove(client_queue)
                    logger.info(f"Removed client from subscribers for call_sid: {call_sid}")

        await client_sender(client_ws)

    except Exception as e:
        logger.error(f"Error in client handler: {str(e)}")

    await client_ws.close()

async def router(websocket, path):
    if path == '/client':
        logger.info('Client connection incoming')
        await client_handler(websocket)
    elif path == '/exotel':
        logger.info('exotel connection incoming')
        await exotel_handler(websocket)

def main():
    # Get port from environment variable (Render sets this for you)
    port = int(os.environ.get("PORT", 10000))

    # Initialize the AudioHandler with the API key
    

    # Bind to all interfaces (0.0.0.0) instead of just localhost
    host = '0.0.0.0'

    # Create the server
    server = websockets.serve(router, host, port)
    logger.info(f'Server starting on ws://{host}:{port}')

    # Run the server
    asyncio.get_event_loop().run_until_complete(server)
    logger.info('Server started successfully')
    asyncio.get_event_loop().run_forever()

if __name__ == '__main__':
    logger.info('Starting application')
    sys.exit(main() or 0)