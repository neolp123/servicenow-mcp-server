"""
ServiceNow MCP SSE Server Implementation
"""

import argparse
import logging
import os

import uvicorn
from dotenv import load_dotenv
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from servicenow_mcp.server import ServiceNowMCP
from servicenow_mcp.utils.config import AuthConfig, AuthType, BasicAuthConfig, ServerConfig

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def create_sse_server_app(mcp_server) -> Starlette:
    """
    Create Starlette app with SSE transport for MCP server.
    
    Args:
        mcp_server: The low-level MCP Server instance
        
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
        """Handle POST requests to /messages endpoint."""
        logger.info(f"POST message from {request.client}")
        return await sse_transport.handle_post_message(
            request.scope,
            request.receive,
            request._send
        )
    
    async def health_handler(request: Request):
        """Health check endpoint."""
        return JSONResponse({
            "status": "healthy",
            "service": "servicenow-mcp-sse",
            "version": "0.1.0"
        })
    
    async def root_handler(request: Request):
        """Root endpoint with service information."""
        return JSONResponse({
            "service": "ServiceNow MCP Server",
            "transport": "SSE",
            "version": "0.1.0",
            "endpoints": {
                "sse": "/sse",
                "messages": "/messages",
                "health": "/health"
            }
        })
    
    # Create Starlette app with routes
    app = Starlette(
        debug=True,
        routes=[
            Route("/", endpoint=root_handler, methods=["GET"]),
            Route("/health", endpoint=health_handler, methods=["GET"]),
            Route("/sse", endpoint=sse_handler, methods=["GET"]),
            Route("/messages", endpoint=messages_handler, methods=["POST"]),
        ],
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
    
    # Create Starlette app with SSE transport
    starlette_app = create_sse_server_app(mcp_server)
    
    logger.info("ServiceNow MCP SSE server created successfully")
    
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
    
    logger.info("Environment variables loaded successfully")
    
    # Create server and app
    try:
        mcp_server, starlette_app = create_servicenow_sse_server(
            instance_url=instance_url,
            username=username,
            password=password
        )
        
        logger.info(f"Starting server on {args.host}:{args.port}")
        
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
        logger.info(f"Health check available at: http://{args.host}:{args.port}/health")
        
        server.run()
        
    except Exception as e:
        logger.error(f"Failed to start server: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()