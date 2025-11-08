"""
ServiceNow MCP SSE Server Implementation with API Key Authentication
MODIFIED: Supports both SSE mode and stateless HTTP mode for ServiceNow compatibility
"""

import argparse
import hashlib
import json
import logging
import os
from typing import Dict

import uvicorn
from dotenv import load_dotenv
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from servicenow_mcp.server import ServiceNowMCP
from servicenow_mcp.utils.config import AuthConfig, AuthType, BasicAuthConfig, ServerConfig

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Global storage for stateless sessions
stateless_sessions: Dict[str, dict] = {}


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Middleware to validate API key for protected endpoints."""
    
    async def dispatch(self, request: Request, call_next):
        # Skip auth for health check
        if request.url.path == "/health" or request.url.path == "/":
            return await call_next(request)
        
        # Get API key from environment
        expected_api_key = os.getenv("MCP_API_KEY")
        
        # If no API key is configured, allow all requests (backward compatible)
        if not expected_api_key:
            logger.warning("MCP_API_KEY not set - running without authentication!")
            return await call_next(request)
        
        # Check API key in header
        provided_api_key = request.headers.get("X-API-Key") or request.headers.get("Authorization", "").replace("Bearer ", "")
        
        if provided_api_key != expected_api_key:
            logger.warning(f"Invalid API key attempt from {request.client}")
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "message": "Invalid or missing API key"}
            )
        
        # Store validated API key in request state for stateless mode
        request.state.api_key = provided_api_key
        
        return await call_next(request)


def create_sse_server_app(mcp_server, servicenow_mcp_instance) -> Starlette:
    """
    Create Starlette app with SSE transport for MCP server.
    
    Args:
        mcp_server: The low-level MCP Server instance
        servicenow_mcp_instance: The ServiceNowMCP instance for direct tool calls
        
    Returns:
        Configured Starlette application
    """
    # Create SSE transport with /messages path
    sse_transport = SseServerTransport("/messages")
    
    async def sse_handler(request: Request):
        """Handle SSE connection endpoint."""
        logger.info(f"SSE connection request from {request.client}")
        
        try:
            # Connect SSE and get streams
            async with sse_transport.connect_sse(
                request.scope,
                request.receive,
                request._send,
            ) as streams:
                logger.info("SSE streams established, running MCP server")
                
                # Run MCP server with the streams
                await mcp_server.run(
                    streams[0],  # read_stream
                    streams[1],  # write_stream
                    mcp_server.create_initialization_options(),
                )
                
        except Exception as e:
            logger.error(f"Error in SSE handler: {e}", exc_info=True)
            raise
    
    async def messages_handler(request: Request):
        """
        Handle POST requests to /messages endpoint.
        Supports both:
        1. SSE mode: session_id in query params (original behavior)
        2. Stateless mode: no session_id, uses API key for session management
        """
        logger.info(f"POST message from {request.client}")
        
        # Check if this is SSE mode (has session_id) or stateless mode
        session_id = request.query_params.get("session_id")
        
        if session_id:
            # SSE mode - use the original SSE transport handler
            logger.info(f"SSE mode: session_id={session_id}")
            return await sse_transport.handle_post_message(
                request.scope,
                request.receive,
                request._send
            )
        else:
            # Stateless mode - handle directly
            logger.info("Stateless mode: handling direct MCP request")
            return await handle_stateless_request(request, servicenow_mcp_instance)
    
    async def handle_stateless_request(request: Request, mcp_instance):
        """
        Handle stateless MCP requests (ServiceNow compatibility mode).
        Creates a virtual session based on API key.
        """
        try:
            # Get API key from request state (set by middleware)
            api_key = getattr(request.state, 'api_key', None)
            if not api_key:
                return JSONResponse(
                    status_code=401,
                    content={
                        "jsonrpc": "2.0",
                        "error": {"code": -32001, "message": "API key required for stateless mode"},
                        "id": None
                    }
                )
            
            # Create a deterministic session identifier from API key
            session_key = hashlib.sha256(api_key.encode()).hexdigest()[:16]
            
            # Parse the JSON-RPC request
            body = await request.body()
            try:
                rpc_request = json.loads(body)
            except json.JSONDecodeError as e:
                return JSONResponse(
                    status_code=400,
                    content={
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": f"Parse error: {e}"},
                        "id": None
                    }
                )
            
            method = rpc_request.get("method")
            params = rpc_request.get("params", {})
            request_id = rpc_request.get("id")
            
            logger.info(f"Stateless request: method={method}, session={session_key}")
            
            # Initialize session if needed
            if session_key not in stateless_sessions:
                stateless_sessions[session_key] = {
                    "initialized": False,
                    "capabilities": {}
                }
                logger.info(f"Created new stateless session: {session_key}")
            
            session = stateless_sessions[session_key]
            
            # Handle different MCP methods
            if method == "initialize":
                logger.info("Handling initialize request")
                session["initialized"] = True
                session["capabilities"] = params.get("capabilities", {})
                
                # Use the client's requested protocol version for compatibility
                client_version = params.get("protocolVersion", "2024-11-05")
                
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": client_version,  # Echo back client's version
                        "capabilities": {
                            "tools": {}  # Server supports tools
                        },
                        "serverInfo": {
                            "name": "ServiceNow MCP Server",
                            "version": "0.1.0"
                        }
                    }
                }
                return JSONResponse(response)
            
            elif method == "notifications/initialized":
                logger.info("Handling initialized notification")
                # No response needed for notifications
                return Response(status_code=200)
            
            elif method == "tools/list":
                logger.info("Handling tools/list request")
                
                if not session.get("initialized"):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "jsonrpc": "2.0",
                            "error": {"code": -32002, "message": "Session not initialized"},
                            "id": request_id
                        }
                    )
                
                # Get tools from the MCP instance
                tools_list = await mcp_instance._list_tools_impl()
                
                # Convert to JSON-RPC response format
                tools_json = [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.inputSchema
                    }
                    for tool in tools_list
                ]
                
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": tools_json
                    }
                }
                
                logger.info(f"Returning {len(tools_json)} tools")
                return JSONResponse(response)
            
            elif method == "tools/call":
                logger.info("Handling tools/call request")
                
                if not session.get("initialized"):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "jsonrpc": "2.0",
                            "error": {"code": -32002, "message": "Session not initialized"},
                            "id": request_id
                        }
                    )
                
                tool_name = params.get("name")
                tool_arguments = params.get("arguments", {})
                
                logger.info(f"Calling tool: {tool_name}")
                
                try:
                    # Call the tool
                    result = await mcp_instance._call_tool_impl(tool_name, tool_arguments)
                    
                    # Convert result to JSON-RPC response
                    response = {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": result[0].text
                                }
                            ]
                        }
                    }
                    
                    return JSONResponse(response)
                    
                except Exception as e:
                    logger.error(f"Error calling tool {tool_name}: {e}", exc_info=True)
                    return JSONResponse(
                        status_code=500,
                        content={
                            "jsonrpc": "2.0",
                            "error": {
                                "code": -32603,
                                "message": f"Tool execution error: {str(e)}"
                            },
                            "id": request_id
                        }
                    )
            
            else:
                logger.warning(f"Unknown method: {method}")
                return JSONResponse(
                    status_code=400,
                    content={
                        "jsonrpc": "2.0",
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                        "id": request_id
                    }
                )
        
        except Exception as e:
            logger.error(f"Error in stateless handler: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": f"Internal error: {str(e)}"},
                    "id": None
                }
            )
    
    async def health_handler(request: Request):
        """Health check endpoint."""
        return JSONResponse({
            "status": "healthy",
            "service": "servicenow-mcp-sse",
            "version": "0.1.0",
            "modes": ["sse", "stateless"],
            "authentication": "enabled" if os.getenv("MCP_API_KEY") else "disabled"
        })
    
    async def root_handler(request: Request):
        """Root endpoint with service information."""
        return JSONResponse({
            "service": "ServiceNow MCP Server",
            "transport": "SSE + Stateless HTTP",
            "version": "0.1.0",
            "endpoints": {
                "sse": "/sse (for persistent SSE connections)",
                "messages": "/messages (supports both SSE with ?session_id=xxx and stateless mode)",
                "health": "/health"
            },
            "modes": {
                "sse": "POST /sse to establish connection, then POST /messages?session_id=xxx",
                "stateless": "POST /messages directly with X-API-Key header (ServiceNow compatible)"
            },
            "authentication": "API Key required" if os.getenv("MCP_API_KEY") else "None"
        })
    
    # Create Starlette app with routes and middleware
    app = Starlette(
        debug=True,
        routes=[
            Route("/", endpoint=root_handler, methods=["GET"]),
            Route("/health", endpoint=health_handler, methods=["GET"]),
            Route("/sse", endpoint=sse_handler, methods=["GET"]),
            Route("/messages", endpoint=messages_handler, methods=["POST"]),
        ],
        middleware=[
            Middleware(APIKeyMiddleware)
        ]
    )
    
    return app


def create_servicenow_sse_server(instance_url: str, username: str, password: str):
    """
    Factory function to create ServiceNow MCP server with SSE transport.
    
    Args:
        instance_url: ServiceNow instance URL
        username: ServiceNow username
        password: ServiceNow password
        
    Returns:
        Tuple of (mcp_server, starlette_app)
    """
    logger.info(f"Creating ServiceNow MCP server for: {instance_url}")
    
    # Create auth configuration
    auth_config = AuthConfig(
        type=AuthType.BASIC,
        basic=BasicAuthConfig(username=username, password=password)
    )
    
    # Create server configuration
    server_config = ServerConfig(
        instance_url=instance_url,
        auth=auth_config
    )
    
    # Create MCP server instance
    servicenow_mcp = ServiceNowMCP(server_config)
    mcp_server = servicenow_mcp.start()
    
    # Create Starlette app with SSE transport AND stateless support
    starlette_app = create_sse_server_app(mcp_server, servicenow_mcp)
    
    logger.info("ServiceNow MCP SSE server created successfully (SSE + Stateless modes)")
    
    return mcp_server, starlette_app


def main():
    """Main entry point for SSE server."""
    load_dotenv()
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="ServiceNow MCP Server with SSE Transport"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)"
    )
    args = parser.parse_args()
    
    # Get environment variables
    instance_url = os.getenv("SERVICENOW_INSTANCE_URL")
    username = os.getenv("SERVICENOW_USERNAME")
    password = os.getenv("SERVICENOW_PASSWORD")
    api_key = os.getenv("MCP_API_KEY")
    
    # Validate required environment variables
    missing_vars = []
    if not instance_url:
        missing_vars.append("SERVICENOW_INSTANCE_URL")
    if not username:
        missing_vars.append("SERVICENOW_USERNAME")
    if not password:
        missing_vars.append("SERVICENOW_PASSWORD")
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        logger.error("Please set these in your .env file or environment")
        raise ValueError(f"Missing environment variables: {missing_vars}")
    
    # Warn if API key is not set
    if not api_key:
        logger.warning("="*70)
        logger.warning("MCP_API_KEY is not set!")
        logger.warning("Server is running WITHOUT authentication")
        logger.warning("Set MCP_API_KEY environment variable for production")
        logger.warning("="*70)
    else:
        logger.info("API Key authentication enabled")
        logger.info("Server supports BOTH SSE and Stateless HTTP modes")
    
    logger.info("Environment variables loaded successfully")
    
    # Create server and app
    try:
        mcp_server, starlette_app = create_servicenow_sse_server(
            instance_url=instance_url,
            username=username,
            password=password
        )
        
        logger.info(f"Starting server on {args.host}:{args.port}")
        logger.info("="*70)
        logger.info("SUPPORTED MODES:")
        logger.info("1. SSE Mode: GET /sse, then POST /messages?session_id=xxx")
        logger.info("2. Stateless Mode: POST /messages directly (ServiceNow compatible)")
        logger.info("="*70)
        
        # Configure uvicorn with SSE-friendly settings
        config = uvicorn.Config(
            starlette_app,
            host=args.host,
            port=args.port,
            log_level="info",
            access_log=True,
            timeout_keep_alive=0,  # Disable timeout for SSE
        )
        
        server = uvicorn.Server(config)
        
        logger.info(f"SSE endpoint available at: http://{args.host}:{args.port}/sse")
        logger.info(f"Messages endpoint: http://{args.host}:{args.port}/messages")
        logger.info(f"Health check available at: http://{args.host}:{args.port}/health")
        
        server.run()
        
    except Exception as e:
        logger.error(f"Failed to start server: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()