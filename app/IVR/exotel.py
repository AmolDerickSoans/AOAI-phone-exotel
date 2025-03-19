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
from typing import Dict, List, Any
from dotenv import load_dotenv
from .rtmt_direct_handler import RTMTDirectHandler
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
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
    def __init__(self, endpoint: str, deployment: str, credentials, voice_choice: str = None):
        self.rtmt_handler = RTMTDirectHandler(endpoint, deployment, credentials, voice_choice)
        self.connections = {}  # Store connections by call_sid

    async def create_connection(self, call_sid: str):
        """Create a new RTMT connection for a specific call"""
        try:
            connection = await self.rtmt_handler.create_rtmt_connection(call_sid)
            self.connections[call_sid] = connection
            return connection
        except Exception as e:
            logger.error(f"Error creating RTMT connection: {str(e)}")
            raise

    async def process_audio(self, call_sid: str, audio_chunk: bytes):
        """Process audio chunk through RTMT"""
        try:
            response = await self.rtmt_handler.process_audio(call_sid, audio_chunk)
            if response:
                # Forward the response to subscribers
                if call_sid in subscribers:
                    message = json.dumps({
                        "call_sid": call_sid,
                        "transcript": response,
                        "is_final": True
                    })
                    for client_queue in subscribers[call_sid]:
                        client_queue.put_nowait(message)
        except Exception as e:
            logger.error(f"Error processing audio: {str(e)}")

    async def close_connection(self, call_sid: str):
        """Close an RTMT connection for a specific call"""
        try:
            await self.rtmt_handler.close_connection(call_sid)
            if call_sid in self.connections:
                del self.connections[call_sid]
        except Exception as e:
            logger.error(f"Error closing RTMT connection: {str(e)}")

# Create a global audio handler
# audio_handler = AudioHandler() # Moved inside main to use environment variable
 
### Starting Exotel Web Handler
async def exotel_handler(exotel_ws):
    stream_sid = None
    call_sid = None
    input_audio_queue = asyncio.Queue()  # Queue to store input audio for echo
    connection_id = id(exotel_ws)
    logger.info(f'New Exotel connection established. Connection ID: {connection_id}')

    # Buffer for audio processing
    BUFFER_SIZE = 20 * 160
    logger.debug(f'Using buffer size: {BUFFER_SIZE} bytes')

    async def exotel_receiver(exotel_ws):
        nonlocal stream_sid, call_sid
        inbuffer = bytearray(b'')
        logger.info(f'Exotel receiver started for connection {connection_id}')

        # Variables to track timestamps
        inbound_chunks_started = False
        latest_inbound_timestamp = 0

        try:
            async for message in exotel_ws:
                try:
                    data = json.loads(message)
                    logger.debug(f'Received message from Exotel connection {connection_id}: {data}')

                    if data['event'] == 'start':
                        start = data['start']
                        stream_sid = start['stream_sid']
                        call_sid = start['call_sid']
                        logger.info(f'Call started - Connection: {connection_id}, Stream SID: {stream_sid}, Call SID: {call_sid}')

                        # Initialize subscribers list for this call_sid
                        subscribers[call_sid] = []
                        logger.debug(f'Initialized subscribers list for call {call_sid}')

                        # Create a new RTMT connection for this call
                        logger.info(f'Creating RTMT connection for call {call_sid}')
                        connection = await audio_handler.create_connection(call_sid)
                        logger.info(f'RTMT connection created successfully for call {call_sid}')

                    elif data['event'] == 'connected':
                        logger.info(f'Call connected - Connection: {connection_id}, Stream SID: {stream_sid}')

                    elif data['event'] == 'media':
                        media = data['media']
                        chunk = base64.b64decode(media['payload'])
                        timestamp = int(media['timestamp'])
                        logger.debug(f'Received media chunk - Connection: {connection_id}, Timestamp: {timestamp}, Size: {len(chunk)} bytes')

                        # Process inbound audio
                        if inbound_chunks_started:
                            if latest_inbound_timestamp + 20 < timestamp:
                                bytes_to_fill = 8 * (timestamp - (latest_inbound_timestamp + 20))
                                logger.debug(f'Filling gap in audio stream - Size: {bytes_to_fill} bytes')
                                inbuffer.extend(b'\xff' * bytes_to_fill)
                        else:
                            inbound_chunks_started = True
                            latest_inbound_timestamp = timestamp
                            logger.debug('First audio chunk received, starting audio processing')

                        latest_inbound_timestamp = timestamp
                        inbuffer.extend(chunk)

                        # Store the input audio chunk for sending back (echo)
                        input_audio_queue.put_nowait(chunk)
                        logger.debug(f'Audio chunk queued for echo - Size: {len(chunk)} bytes')

                        # If we have enough audio data, send it to RTMT
                        while len(inbuffer) >= BUFFER_SIZE:
                            if call_sid in audio_handler.connections:
                                try:
                                    logger.debug(f'Processing audio buffer - Size: {BUFFER_SIZE} bytes')
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
                        logger.info(f'Call stopped - Connection: {connection_id}, Call SID: {call_sid}')

                        # Close the RTMT connection for this call
                        logger.info(f'Closing RTMT connection for call {call_sid}')
                        await audio_handler.close_connection(call_sid)

                        # Notify subscribers that the call has ended
                        if call_sid in subscribers:
                            logger.info(f'Notifying {len(subscribers[call_sid])} subscribers about call end')
                            for client in subscribers[call_sid]:
                                client.put_nowait('close')

                            del subscribers[call_sid]
                            logger.debug(f'Removed subscribers for call {call_sid}')

                        break

                except json.JSONDecodeError as e:
                    logger.error(f'Invalid JSON received from Exotel connection {connection_id}: {str(e)}')

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f'Exotel connection {connection_id} closed unexpectedly: {str(e)}')
        except Exception as e:
            logger.error(f'Error in exotel_receiver for connection {connection_id}: {str(e)}')

            # Close the RTMT connection if there was an error
            if call_sid:
                logger.info(f'Closing RTMT connection for call {call_sid} due to error')
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

async def client_handler(websocket):
    """Handle WebSocket connections from clients who want to subscribe to call transcripts"""
    client_queue = asyncio.Queue()

    # First tell the client all active calls
    await websocket.send(json.dumps(list(subscribers.keys())))
    logger.info(f"Sent active calls to client: {list(subscribers.keys())}")

    try:
        # Get the message from the client
        message = await websocket.recv()
        try:
            data = json.loads(message)
            # Handle different message types
            if data.get('event') == 'connected':
                logger.info("Client sent connected event")
                # Keep connection open and wait for future messages
                message = await websocket.recv()
                data = json.loads(message)

            # Extract call_sid from start event or raw data
            call_sid = None
            if isinstance(data, dict):
                if data.get('event') == 'start' and 'start' in data:
                    call_sid = data['start'].get('call_sid')
                    logger.info(f"Extracted call_sid from start event: {call_sid}")
                elif 'call_sid' in data:
                    call_sid = data['call_sid']
                    logger.info(f"Extracted call_sid from data: {call_sid}")
            else:
                # If not a dict, treat as raw call_sid
                call_sid = data if isinstance(data, str) else message.strip()
            
            logger.info(f"Client requested to subscribe to call_sid: {call_sid}")

            if not call_sid:
                logger.error("No valid call_sid found in client message")
                await websocket.send(json.dumps({"status": "error", "message": "Invalid call_sid"}))
                return

            # Initialize the subscribers list for this call_sid if it doesn't exist
            if call_sid not in subscribers:
                subscribers[call_sid] = []
                logger.info(f"Created new subscribers list for call_sid: {call_sid}")

            # Add the client queue to subscribers
            subscribers[call_sid].append(client_queue)
            logger.info(f"Client subscribed to call_sid: {call_sid}")
            
            # Send confirmation to client
            await websocket.send(json.dumps({"status": "success", "message": "Successfully subscribed to call"}))

            # Initialize subscription success flag
            subscription_success = True
            
            if not subscription_success:
                try:
                    await websocket.send(json.dumps({"status": "error", "message": "Call initialization timeout"}))
                    logger.warning(f"Timeout waiting for call_sid: {call_sid} to be available")
                    await websocket.close()
                except websockets.exceptions.ConnectionClosed:
                    logger.warning(f"Client connection closed after timeout for call {call_sid}")
                return

            async def client_sender(websocket):
                try:
                    while True:
                        message = await client_queue.get()
                        if message == 'close':
                            logger.info("Received close message for client")
                            break

                        await websocket.send(message)
                except Exception as e:
                    logger.error(f"Error sending to client: {str(e)}")
                finally:
                    # Remove this client queue from subscribers
                    if call_sid in subscribers and client_queue in subscribers[call_sid]:
                        subscribers[call_sid].remove(client_queue)
                        logger.info(f"Removed client from subscribers for call_sid: {call_sid}")

            await client_sender(websocket)
        except json.JSONDecodeError:
            # If not valid JSON, treat as raw call_sid
            call_sid = message.strip()
            logger.info(f"Treating message as raw call_sid: {call_sid}")
            # Continue with existing call_sid handling logic
            if call_sid in subscribers:
                subscribers[call_sid].append(client_queue)
                logger.info(f"Client subscribed to call_sid: {call_sid}")
            else:
                logger.warning(f"Client tried to subscribe to non-existent call_sid: {call_sid}")
                await websocket.close()
                return

    except websockets.exceptions.ConnectionClosed:
        logger.info('Client connection closed normally')
    except Exception as e:
        logger.error(f'Error in client handler: {str(e)}')
    finally:
        await websocket.close()

async def router(websocket, path):
    try:
        logger.info(f'Incoming connection request for path: {path}')
        logger.debug(f'WebSocket headers: {websocket.request_headers}')
        logger.debug(f'WebSocket remote: {websocket.remote_address}')

        if path == '/client':
            logger.info('Client connection incoming')
            await client_handler(websocket)
        elif path == '/exotel':
            logger.info('Exotel connection incoming')
            await exotel_handler(websocket)
        else:
            logger.warning(f'Invalid path requested: {path}')
            await websocket.close(code=4004, reason='Invalid path')
    except websockets.exceptions.InvalidHandshake as e:
        logger.error(f'WebSocket handshake failed: {str(e)}')
        raise
    except Exception as e:
        logger.error(f'Unexpected error in router: {str(e)}')
        raise

def main():
    # Get configuration from environment variables
    port = int(os.environ.get("PORT", 10000))
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    deployment = os.environ.get("AZURE_OPENAI_REALTIME_DEPLOYMENT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    voice_choice = os.environ.get("AZURE_OPENAI_REALTIME_VOICE_CHOICE", "alloy")
    ssl_context = None
    
    logger.info('Initializing server with configuration:')
    logger.info(f'Port: {port}')
    logger.info(f'Azure OpenAI Endpoint: {endpoint}')
    logger.info(f'Azure OpenAI Deployment: {deployment}')
    logger.info(f'Voice Choice: {voice_choice}')
    
    # Configure SSL for production environment
    if os.environ.get("RENDER") or os.environ.get("PRODUCTION"):
        logger.info('Production environment detected, configuring SSL')
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        # In production, we're behind a proxy that handles SSL
        ssl_context = None
        logger.info('SSL configuration disabled - using proxy SSL termination')

    # Validate required configuration
    missing_configs = []
    if not endpoint:
        missing_configs.append('AZURE_OPENAI_ENDPOINT')
    if not deployment:
        missing_configs.append('AZURE_OPENAI_REALTIME_DEPLOYMENT')
    
    if missing_configs:
        logger.error(f'Missing required environment variables: {", ".join(missing_configs)}')
        return 1

    # Initialize the AudioHandler with RTMT configuration
    global audio_handler
    try:
        credentials = AzureKeyCredential(api_key) if api_key else DefaultAzureCredential()
        logger.info('Initializing AudioHandler with RTMT configuration')
        audio_handler = AudioHandler(endpoint, deployment, credentials, voice_choice)
        logger.info('AudioHandler initialized successfully')
    except Exception as e:
        logger.error(f'Failed to initialize AudioHandler: {str(e)}')
        return 1

    # Bind to all interfaces (0.0.0.0) instead of just localhost
    host = '0.0.0.0'

    try:
        # Create the server
        server = websockets.serve(
            router,
            host,
            port,
            ssl=ssl_context,
            compression=None,  # Disable compression to work better with proxies
            max_size=10 * 1024 * 1024  # 10MB max message size
        )
        protocol = "wss" if ssl_context else "ws"
        logger.info(f'Server starting on {protocol}://{host}:{port}')

        # Run the server
        asyncio.get_event_loop().run_until_complete(server)
        logger.info('Server started successfully')
        asyncio.get_event_loop().run_forever()
    except Exception as e:
        logger.error(f'Failed to start server: {str(e)}')
        return 1

if __name__ == '__main__':
    logger.info('Starting application')
    sys.exit(main() or 0)