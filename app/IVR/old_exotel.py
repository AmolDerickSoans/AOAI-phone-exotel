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
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
from typing import Dict, List, Any
from dotenv import load_dotenv
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
    def __init__(self, deepgram_api_key):
        # Initialize the Deepgram client with your API key
        self.deepgram = DeepgramClient(deepgram_api_key)  # Replace with your API key
        self.connections = {}  # Store connections by call_sid

    async def create_deepgram_connection(self, call_sid):
        """Create a new Deepgram connection for a specific call"""
        try:
            # Configure Deepgram options
            options = LiveOptions(
                model="nova-3",
                punctuate=True,
                language="en-US",
                encoding="mulaw",  # Using mulaw to match your current implementation
                channels=1,        # Using 1 channels since exotel sends 1 channel audio
                sample_rate=8000,  # Using 8000 sample rate as in your original code
                interim_results=True,
                utterance_end_ms="1000",
                vad_events=True,
                # multichannel=False,  # Disable multichannel processing. Exotel sends single channel audio
                smart_format=True  # Enable smart formatting for better readability
            )

            # Create the connection with Deepgram
            dg_connection = self.deepgram.listen.websocket.v("1")

            # Set up event handlers
            dg_connection.on(LiveTranscriptionEvents.Open, self.on_open)
            dg_connection.on(LiveTranscriptionEvents.Transcript,
                            lambda client, result, **kwargs: self.on_message(client, result, call_sid, **kwargs))
            dg_connection.on(LiveTranscriptionEvents.Metadata, self.on_metadata)
            dg_connection.on(LiveTranscriptionEvents.SpeechStarted, self.on_speech_started)
            dg_connection.on(LiveTranscriptionEvents.UtteranceEnd, self.on_utterance_end)
            dg_connection.on(LiveTranscriptionEvents.Error, self.on_error)
            dg_connection.on(LiveTranscriptionEvents.Close, self.on_close)

            # Start the connection - Do not await if start() returns a boolean
            dg_connection.start(options)
            logger.info(f"Successfully started Deepgram connection for call {call_sid}")

            # Store the connection
            self.connections[call_sid] = dg_connection

            return dg_connection

        except Exception as e:
            logger.error(f"Error starting Deepgram connection: {str(e)}")
            raise

    def on_open(self, client, open, **kwargs):
        """Handle connection open event"""
        logger.info(f"Deepgram connection opened: {open}")

    def on_message(self, client, result, call_sid, **kwargs):
        """Handle transcript message event"""
        logger.info(f"Received message data: {result}") 
        try:
            # Extract the transcript from the result
            if hasattr(result, 'results') and result.results:
                for r in result.results:
                    if hasattr(r, 'channels') and r.channels:  # Check if channels attribute exists and is not empty
                        for channel in r.channels:
                            if hasattr(channel, 'alternatives') and channel.alternatives:  # Check if alternatives attribute exists and is not empty
                                for alternative in channel.alternatives:
                                    transcript = alternative.transcript
                                    if transcript.strip():
                                        is_final = alternative.confidence > 0.8  # Example: Adjust threshold as needed
                                        logger.info(f'Call {call_sid} transcript {"(final)" if is_final else "(interim)"}: {transcript}')
                                        logger.debug(f'Full transcript result: {json.dumps(alternative.__dict__)}')  # Serialize to dict

                                        # Forward the transcript to all subscribers for this call
                                        if call_sid in subscribers:
                                            # Convert result to JSON string
                                            message = json.dumps({
                                                "call_sid": call_sid,
                                                "transcript": transcript,
                                                "is_final": is_final
                                            })

                                            for client_queue in subscribers[call_sid]:
                                                client_queue.put_nowait(message)
                                    else:
                                        logger.debug("Empty transcript received from Deepgram.")
                            else:
                                logger.debug("No alternatives found in channel.")
                    else:
                        logger.debug("No channels found in result.")
            else:
                logger.debug("No results found in Deepgram response.")

        except Exception as e:
            logger.error(f"Error in on_message: {str(e)}", exc_info=True) # Include traceback

    def on_metadata(self, client, metadata, **kwargs):
        """Handle metadata event"""
        logger.info(f"Received metadata: {metadata}")

    def on_speech_started(self, client, speech_started, **kwargs):
        """Handle speech started event"""
        logger.info(f"Speech started: {speech_started}")

    def on_utterance_end(self, client, utterance_end, **kwargs):
        """Handle utterance end event"""
        logger.info(f"Utterance end: {utterance_end}")

    def on_error(self, client, error, **kwargs):
        """Handle error event"""
        logger.error(f"Deepgram error: {error}")

    def on_close(self, client, close, **kwargs):
        """Handle connection close event"""
        logger.info(f"Deepgram connection closed: {close}")

    async def close_connection(self, call_sid):
        """Close a Deepgram connection for a specific call"""
        if call_sid in self.connections:
            try:
                self.connections[call_sid].finish()
                logger.info(f"Closed Deepgram connection for call {call_sid}")
            except Exception as e:
                logger.error(f"Error closing Deepgram connection: {str(e)}")
            finally:
                del self.connections[call_sid]

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

                    # Create a new Deepgram connection for this call
                    dg_connection = await audio_handler.create_deepgram_connection(call_sid)

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

                    # If we have enough audio data, send it to Deepgram
                    while len(inbuffer) >= BUFFER_SIZE: # Changed 'if' to 'while' to process all full buffers
                        # Get the Deepgram connection for this call
                        if call_sid in audio_handler.connections:
                            dg_connection = audio_handler.connections[call_sid]

                            # Send the audio data to Deepgram
                            try:
                                dg_connection.send(inbuffer[:BUFFER_SIZE])
                            except Exception as e:
                                logger.error(f"Error sending audio to Deepgram: {str(e)}")
                                await audio_handler.close_connection(call_sid)
                                dg_connection = await audio_handler.create_deepgram_connection(call_sid)

                            # Remove the sent chunk from the buffer
                            inbuffer = inbuffer[BUFFER_SIZE:]
                        else:
                            logger.warning(f"No Deepgram connection found for call_sid: {call_sid}")
                            break # Exit the while loop if no connection is found

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
    deepgram_api_key = os.environ.get("DEEPGRAM_API_KEY")

    if not deepgram_api_key:
        logger.error("DEEPGRAM_API_KEY environment variable not set.")
        return 1

    # Initialize the AudioHandler with the API key
    global audio_handler
    audio_handler = AudioHandler(deepgram_api_key)

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