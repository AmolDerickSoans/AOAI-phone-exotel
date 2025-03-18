# Audio System and Exotel Integration Guide

## System Overview
The audio handling system consists of two main components: `rtmt_audio_handler.py` and `exotel.py`. These components work together to provide real-time audio processing and IVR capabilities through Azure OpenAI's RTMT service.

## RTMTAudioHandler (rtmt_audio_handler.py)

### Purpose
The RTMTAudioHandler class serves as a bridge between the audio stream and Azure OpenAI's real-time middleware tier (RTMT). It handles:
- WebSocket connection management
- Audio stream processing
- Real-time transcription and response generation

### Key Components

#### 1. Connection Management
```python
class RTMTAudioHandler:
    def __init__(self, endpoint, deployment, credentials, voice_choice):
        # Initialize with Azure OpenAI configuration
```
- Manages WebSocket connections per call session
- Supports both API key and Azure AD authentication
- Maintains connection state for multiple concurrent calls

#### 2. Audio Processing
- Processes audio chunks in real-time
- Handles streaming audio data to RTMT
- Manages response generation and transcription

#### 3. Session Management
- Creates and maintains RTMT sessions
- Handles session initialization and cleanup
- Manages voice preferences and response settings

## Exotel Integration (exotel.py)

### Purpose
The Exotel integration module provides a WebSocket server that:
- Handles incoming Exotel IVR calls
- Processes audio streams
- Manages client connections for real-time updates

### Key Components

#### 1. AudioHandler Class
```python
class AudioHandler:
    def __init__(self, endpoint, deployment, credentials, voice_choice):
        # Initialize RTMT handler for audio processing
```
- Wraps RTMTAudioHandler for Exotel integration
- Manages audio processing and response generation
- Handles connection lifecycle

#### 2. WebSocket Server
- Provides endpoints for Exotel and client connections
- Routes messages between components
- Manages subscriber notifications

#### 3. Audio Processing Pipeline
1. Receives audio chunks from Exotel
2. Buffers and processes audio data
3. Sends processed audio to RTMT
4. Handles responses and transcriptions

## Integration Guide

### Setting Up the System

1. **Environment Configuration**
```bash
# Required environment variables
AZURE_OPENAI_ENDPOINT="your_endpoint"
AZURE_OPENAI_REALTIME_DEPLOYMENT="deployment_name"
AZURE_OPENAI_API_KEY="your_api_key"
AZURE_OPENAI_REALTIME_VOICE_CHOICE="alloy"  # Optional
PORT=10000  # WebSocket server port
```

2. **Starting the Server**
```bash
python exotel.py
```

### Integrating with Exotel

1. **Configure Exotel Webhook**
- Set up webhook URL: `ws://your_server:10000/exotel`
- Configure audio streaming settings in Exotel

2. **Client Integration**
- Connect to `ws://your_server:10000/client`
- Send call_sid to subscribe to updates
- Receive real-time transcriptions and responses

### WebSocket Communication

#### Exotel WebSocket Events

1. **Start Event**
```json
{
    "event": "start",
    "start": {
        "stream_sid": "stream_id",
        "call_sid": "call_id"
    }
}
```

2. **Media Event**
```json
{
    "event": "media",
    "media": {
        "payload": "base64_encoded_audio",
        "timestamp": "timestamp"
    }
}
```

3. **Stop Event**
```json
{
    "event": "stop"
}
```

#### Client WebSocket Messages

1. **Subscription**
- Send: `call_sid`
- Receive: Real-time transcription updates

2. **Transcription Update**
```json
{
    "call_sid": "call_id",
    "transcript": "transcribed_text",
    "is_final": true
}
```

### Best Practices

1. **Error Handling**
- Implement reconnection logic
- Handle network interruptions
- Provide fallback responses

2. **Performance Optimization**
- Buffer audio appropriately
- Monitor memory usage
- Handle concurrent calls efficiently

3. **Security**
- Use secure WebSocket connections (WSS)
- Implement authentication
- Validate incoming connections

### Monitoring and Maintenance

1. **Logging**
- Monitor WebSocket connections
- Track audio processing performance
- Log error conditions

2. **Scaling**
- Monitor system resources
- Handle multiple concurrent calls
- Implement load balancing if needed

## Troubleshooting

### Common Issues

1. **Connection Problems**
- Check network connectivity
- Verify WebSocket server status
- Confirm environment variables

2. **Audio Processing Issues**
- Check audio format and encoding
- Verify buffer sizes
- Monitor RTMT connection status

3. **Performance Issues**
- Monitor memory usage
- Check CPU utilization
- Optimize buffer sizes

### Debug Tools

1. **Logging**
```python
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

2. **WebSocket Testing**
- Use tools like `wscat` for testing
- Monitor WebSocket traffic
- Verify message formats

## Security Considerations

1. **Authentication**
- Implement token-based auth
- Validate client connections
- Secure API keys

2. **Data Protection**
- Use encryption for sensitive data
- Implement access controls
- Follow security best practices

## Future Improvements

1. **Feature Enhancements**
- Add support for more voice options
- Implement advanced audio processing
- Add real-time analytics

2. **Performance Optimization**
- Optimize buffer management
- Implement caching
- Enhance error handling

3. **Integration Extensions**
- Support additional IVR platforms
- Add more client interfaces
- Implement additional analytics