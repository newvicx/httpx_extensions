# HTTPX_Extensions
A client extension of HTTPX AsyncClient with connection pool management features
## Background
httpx_extensions is an extension of the AsyncClient from [HTTPX](https://www.python-httpx.org/). It modifies the way the connection pooling works so users can control exactly which open connection a request is sent on. This comes in handy for connection based authentication methods such as NTLM. When using HTTP 1.1 with connection pooling, the stock AsyncClient (and other clients from of other async frameworks such as aiohttp) implicitely release a connection back to the pool as soon as the request/response cycle is complete. When doing something like NTLM which takes 3 request/response cycles, there is no guarantee that the next 2 requests will go out on the same connection as the first. httpx_extensions attempts to solve this problem by introducing the concept of "reserved connections" and not releasing connections back to the pool until the final response (regardless of the number of request/response cycles) is served up to the user. In 99% of cases, this feature is not required and in those cases you should use the AsyncClient from HTTPX or any other async client of your choosing. But, if you are doing something like NTLM or some other workflow that requires you to control which requests are sent on which connection, httpx_extensions fits the bill

## Installation
You can install httpx_extensions via pip

    pip install httpx-extensions
## Docs
Refer to the HTTPX [AsyncClient](https://www.python-httpx.org/async/) documentation as the API is identical. Read the rest of this document to understand the minor differences
## Compatability
 - httpx_extensions is an extension of HTTPX (duh) thus the API is identical to the HTTPX AsyncClient and nearly all of the code snippets from HTTPX can be used with httpx_extensions by simply swapping the AsyncClient for the ExClient
 - All HTTPX models such as Headers, Limits, Request are compatible and should be used as httpx_extensions does not ship with these models. The lone exception to this is the Response object. httpx_extensions returns instances of the ResponseMixin class. For all intents and purposes, from a user perspective, the ResponseMixin object is identical to the HTTPX Response object and should be treated as such
 - All other HTTPX features are supported as well with only a couple of caveats. See Unsupported Features below
## Unsupported Features
 - http2: When using http2, only one connection is used so there is no need to add additional logic for connection management with http2. In this case you should just use the AsyncClient. Attempting to make a request with http2=True will raise a RuntimeError
 - Custom Transports: You can pass an instance of AsyncHTTPTransportMixin to the constructor call for the ExClient but it must be an instance of AsyncHTTPTransportMixin. You can also create transports which inherit from AsyncHTTPTransportMixin but this is not recommended
 - app: Calling into python web apps through the app parameter is not supported. These transports dont use httpcore which is where all the connection management happens
## Usage
### Making a Request

    import asyncio
    from httpx import Headers
    from httpx_extensions import ExClient
	
	async def main():
		url = "http://example.com"
		headers = Headers(
			dict(
				accept="application/json",
				connection="Keep Alive"
			)
		)
		async with ExClient(headers=headers) as client:
			response = await client.get(url)
	
	if __name__ == "__main__":
		loop = asyncio.get_event_loop()
		loop.run_until_complete(main())
Compare that to making a request through HTTPX

    import asyncio
    from httpx import AsyncClient, Headers
 
    async def main():
	    url = "http://example.com"
	    headers = Headers(
    		dict(
    			accept="application/json",
    			connection="Keep Alive"
    		)
    	)
    	async with AsyncClient(headers=headers) as client:
    		response = await client.get(url)
### Connection Management
httpx_extensions provides a "conn_id" key on the "extensions" attribute of the response. The most common place to access this is in an authentication workflow but you can see how to access it following the example above.

    import asyncio
    from httpx import Headers
    from httpx_extensions import ExClient
    
    async def main():
    	url = "http://example.com"
    	headers = Headers(
    		dict(
    			accept="application/json",
    			connection="Keep Alive"
    		)
    	)
    	async with ExClient(headers=headers) as client:
    		response = await client.get(url)
    	print(response.extensions["conn_id"])
	
	if __name__ == "__main__":
		loop = asyncio.get_event_loop()
		loop.run_until_complete(main())

So how do you leverage this an authentication workflow? Lets consider a simple auth flow...

    from httpx import Auth
	
	class SimpleAuthFlow(Auth):
		def auth_flow(request: Request):
			response = yield request
			# Do something here
			yield request

The second time you yield request there is no guarantee that request will use the same underlying connection to fulfill the request. But, if we change this flow slightly...

    from httpx import Auth
	
	class SimpleAuthFlowWithConnManagement(Auth):
		def auth_flow(request: Request):
			response = yield request
			# Do something here
			request.extensions["conn_id"] = response.extensions["conn_id"]
			yield request
By assigning a "conn_id" to the request, the underlying connection pool will attempt to use that same connection to fulfill the request. The only time this wont happen is if...

 - The connection was forced to close after a previous response either by some error or the Connection Close header was issued by the server
 - The connection has expired
 - The connection has already been released back to the pool. This can happen with redirects (see special considerations below)

Users will receive a warning in the first case where the connection has closed. To avoid case 2, it might be best to not set a keepalive expiry but you are not restricted from doing so
## Special Considerations
 - Redirects: If follow_redirects=True, the conn_id will be automatically appended to the redirect request and the connection pool will reuse that connection. However, if follow_redirects=False, sending the next_request from the resulting response object is not guarenteed to use the same connection even if the conn_id from the response is attached to next_request. If conn_id is appended to next_request a warning log will be issued saying "Connection id {conn_id} was given in request extensions but the connection is not reserved. {Request} is not guarenteed to use the desired connection"
 - You can set the keepalive_expiry and max_keepalive_connections limits to anything but 0. Having connections that automatically close when the request/response cycle is done defeats the purpose of what this package was built for. Setting either property to 0 will raise a ValueError
## How it Works
For the curious such...
HTTPX is built on top of [httpcore](https://github.com/encode/httpcore) which handles the actual connection pooling, sending requests, and receiving responses. The default connection pool implicitly releases connections back to the pool once the request-response cycle has completed. In HTTPX reading the response content to completion completes the cycle and releases the connection. The biggest change httpx_extensions makes is the how the httpcore AsyncConnectionPool works. Rather than connections being implicitly released they need to be explicitly released by calling the "release" method on the byte stream returned from  httpcore. This all happens automatically without any need for the user to manage the release of connections. Requiring an explicit release of the connection back to the pool ensures that connection is still available to be used again by subsequent requests in an auth flow should the user who wrote the auth flow choose to do so. The connection is then released once the auth flow completes. For streaming responses, the connection is released when the response is closed.

To achieve this, the concept of "reserved connections" are introduced into the connection pool interface. Connections in the pool are assigned a unique connection id when they are opened. When a request-response cycle is completed, instead of releasing the connection back to the pool it is categorized as "reserved". The reserved connection is released back to the pool by explicitly calling a release method with a reference to the connection id. Adding this concept of reserved connections has the side effect (could be good or bad) of ensuring auth flows are completed before additional requests begin processing if the connection pool limit is reached. 

Lets consider an example below to illustrate how the connection management works and show how auth flows are followed to completion before subsequent requests are processed.

                                   ┌─────────────────────┐  ┌─────────────────────┐
                                   │Available Connections│  │Reserved  Connections│
                                   └─────────────────────┘  └─────────────────────┘
       ┌────────────┐                   ┌───────────┐
       │            ├──────────────────►│           │
       │ Request 1  │                   │  Conn 1   │
       │            │◄──────────────────┤           │
       └────────────┘       401         └───────────┘
	   ┌────────────┐                   ┌───────────┐
	   |            ├──────────────────►│           │
	   │ Request 2                      │  Conn 2   │
	   │            │◄──────────────────┤           │
	   └────────────┘       401         └───────────┘
       ┌────────────┐
       │            │
       │ Request 3  │Enqueued
       │            │
       └────────────┘
      ──────────────────────────────────────────────────────────────────────Next──
       ┌────────────┐                                           ┌───────────┐
       │            ├─────────────────────────────────────────-►│           │
       │ Request 1  │                                           │  Conn 1   │
       │ ID: Conn 1 │◄──────────────────────────────────────────┤           │
       └────────────┘                200(Complete)              └───────────┘
       ┌────────────┐                                           ┌───────────┐
       │            ├─────────────────────────────────────────-►│           │
       │ Request 2  │                                           │  Conn 2   │
       │ ID: Conn 2 │◄──────────────────────────────────────────┤           │
       └────────────┘                200(Complete)              └───────────┘
       ┌────────────┐
       │            │
       │ Request 3  │Enqueued
       │            │
       └────────────┘
       ──────────────────────────────────────────────────────────────────────Next──
       ┌────────────┐                   ┌───────────┐
       │            ├──────────────────►│           │
       │ Request 3  │                   │  Conn 1   │
       │            │◄──────────────────┤           │
       └────────────┘   200(Complete)   └───────────┘
	                                    ┌───────────┐
	                                    │           │
	                                    │  Conn 2   │
	                                    |           │
	                                    └───────────┘
In the above example we have a connection pool with 2 available connections and we want to make 3 requests. The first 2 requests are handled in order by the 2 connections in the pool. We receive a 401 response for both requests. The request/response cycle is complete but we are not done so the connections are flagged as reserved. The auth flow for each request adds an authorization header and submits the requests back to the pool. The requests reference the connections they were first sent on so the pool assigns the appropriate connection to the 2 requests. We receive 200 responses on the next cycle and the auth flow completes. Those 2 connections are now considered available again so request 3 can now be processed
## Supports

 - Python 3.6+
 - httpx 0.22.0

