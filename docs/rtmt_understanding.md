# RTMT System Understanding and Integration Guide

## System Overview
The Real-Time Middle Tier (RTMT) system is a WebSocket-based middleware that facilitates real-time communication between clients and Azure OpenAI services. It provides RAG (Retrieval-Augmented Generation) capabilities through Azure Cognitive Search integration.

## Core Components

### 1. RTMiddleTier Class
- **Purpose**: Main middleware component handling WebSocket connections and message routing
- **Key Features**:
  - WebSocket connection management
  - Message transformation and routing
  - Tool integration support
  - Authentication handling

### 2. Vector Search Setup (setup_intvect.py)
- **Purpose**: Configures Azure Cognitive Search for vector search capabilities
- **Components**:
  - Index creation with vector search fields
  - Skillset setup for document processing
  - Indexer configuration for document ingestion
  - Document upload management

### 3. Authentication System
- Supports both API Key and Azure AD token-based authentication
- Handles token refresh and validation
- Integrates with Azure OpenAI and Azure Cognitive Search security

## Data Flow
1. Client establishes WebSocket connection
2. Authentication tokens/keys are validated
3. Messages are processed through the middleware
4. Vector search queries are executed against Azure Cognitive Search
5. Results are processed and returned to the client

## External System Integration

### Integrating with Exotel IVR

#### Architecture Overview
To integrate RTMT with Exotel IVR, we need to create an adapter layer that bridges WebSocket and HTTP protocols:

```python
class ExotelAdapter:
    def __init__(self, rtmt_instance):
        self.rtmt = rtmt_instance
        self.session_manager = {}

    async def handle_incoming_call(self, exotel_request):
        # Create or retrieve session
        session_id = exotel_request.get('CallSid')
        
        # Initialize RTMT session if needed
        if session_id not in self.session_manager:
            self.session_manager[session_id] = await self.create_rtmt_session()

    async def process_dtmf(self, call_sid, digits):
        # Convert DTMF to text/intent
        # Process through RTMT
        session = self.session_manager.get(call_sid)
        if session:
            response = await session.process_message(digits)
            return self.convert_to_exotel_response(response)
```

#### Integration Steps
1. **Setup Exotel Webhook Endpoint**
   - Create HTTP endpoints for Exotel webhooks
   - Handle incoming calls and DTMF inputs

2. **Session Management**
   - Maintain mapping between Exotel call sessions and RTMT sessions
   - Handle session cleanup and resource management

3. **Response Processing**
   - Convert RTMT responses to Exotel-compatible format
   - Handle text-to-speech conversion if needed

4. **Error Handling**
   - Implement robust error handling
   - Provide fallback responses

#### WebSocket Message Flow
1. **Session Initialization**
   ```python
   async def create_rtmt_session(self):
       ws = await self.rtmt.create_websocket_connection()
       # Initialize session with system message
       await ws.send_json({
           "type": "session.update",
           "session": {
               "instructions": "You are an IVR assistant...",
               "temperature": 0.7,
               "max_response_output_tokens": 100
           }
       })
       return ws
   ```

2. **Message Processing**
   ```python
   async def process_message(self, session, message):
       # Send user message
       await session.send_json({
           "type": "conversation.item.create",
           "item": {
               "type": "message",
               "content": message
           }
       })
       
       # Process response stream
       response = ""
       async for msg in session:
           if msg.type == aiohttp.WSMsgType.TEXT:
               data = json.loads(msg.data)
               if data["type"] == "response.output_item.added":
                   if data["item"]["type"] == "text":
                       response += data["item"]["content"]
               elif data["type"] == "response.done":
                   break
       return response
   ```

#### Authentication and Security
1. **RTMT Authentication**
   - Support for both API Key and Bearer token:
   ```python
   if api_key:
       headers = {"api-key": api_key}
   else:
       token = await get_azure_token()
       headers = {"Authorization": f"Bearer {token}"}
   ```

2. **Exotel Webhook Security**
   - Validate incoming webhook signatures:
   ```python
   def verify_exotel_signature(request):
       signature = request.headers.get("X-Exotel-Signature")
       payload = await request.text()
       expected = hmac.new(
           WEBHOOK_SECRET.encode(),
           payload.encode(),
           hashlib.sha256
       ).hexdigest()
       return hmac.compare_digest(signature, expected)
   ```

#### Example Configuration and Setup
```python
from aiohttp import web
from rtmt import RTMiddleTier

async def create_exotel_app(rtmt_instance):
    app = web.Application()
    adapter = ExotelAdapter(rtmt_instance)
    
    # Configure middleware for signature verification
    @web.middleware
    async def auth_middleware(request, handler):
        if not await verify_exotel_signature(request):
            raise web.HTTPUnauthorized()
        return await handler(request)
    
    app.middlewares.append(auth_middleware)
    app.router.add_post('/exotel/call', adapter.handle_incoming_call)
    app.router.add_post('/exotel/dtmf', adapter.process_dtmf)
    
    return app
```

#### Error Handling and Resilience
1. **Connection Management**
   - Implement automatic reconnection:
   ```python
   async def ensure_connection(self, session_id):
       session = self.session_manager.get(session_id)
       if not session or session.closed:
           session = await self.create_rtmt_session()
           self.session_manager[session_id] = session
       return session
   ```

2. **Fallback Mechanisms**
   - Handle timeouts and errors gracefully:
   ```python
   async def process_with_fallback(self, call_sid, input_data):
       try:
           session = await self.ensure_connection(call_sid)
           response = await asyncio.wait_for(
               self.process_message(session, input_data),
               timeout=5.0
           )
       except asyncio.TimeoutError:
           response = "I'm sorry, I'm having trouble processing your request."
       except Exception as e:
           logger.error(f"Error processing message: {e}")
           response = "An error occurred. Please try again."
       return self.convert_to_exotel_response(response)
   ```
   - Implement rate limiting
   - Use secure communication channels

2. **Session Security**
   - Implement session timeout
   - Secure session data storage
   - Regular session cleanup

## Best Practices
1. **Error Handling**
   - Implement comprehensive error handling
   - Provide meaningful error messages
   - Log errors for debugging

2. **Monitoring**
   - Monitor WebSocket connections
   - Track session statistics
   - Set up alerts for system issues

3. **Performance**
   - Implement connection pooling
   - Cache frequently used data
   - Optimize message processing

## Testing
1. **Unit Tests**
   - Test individual components
   - Mock external services
   - Verify error handling

2. **Integration Tests**
   - Test end-to-end flows
   - Verify external system integration
   - Load testing

## Deployment
1. **Prerequisites**
   - Azure OpenAI service
   - Azure Cognitive Search
   - Exotel account and API credentials

2. **Configuration**
   - Environment variables
   - Service endpoints
   - Authentication settings

3. **Monitoring Setup**
   - Log aggregation
   - Performance metrics
   - Alert configuration