# HTTPX_Extensions
A client extension of HTTPX AsyncClient with support for Negotiate/NTLM auth via SSPI
## Background
httpx_extensions is an extension of the AsyncClient from [HTTPX](https://www.python-httpx.org/). It modifies the way the connection pooling works to give the user control of which connection in the pool a given request is sent on. This is critical for connection based authentication methods such as NTLM. The [requests](https://docs.python-requests.org/en/latest/) library has worked with connection based authentication methods in the past because the response objects maintain a reference to the underlying connection used which can be reused in a subclass of [AuthBase](https://docs.python-requests.org/en/latest/api/#requests.auth.AuthBase). As far as I know, there is no mainstream asynchronous library with this type of support. httpx_extensions attempts to build on top all the great features of HTTPX (namely the generator based authentication flow) while giving the user some level of control over connection management.

httpx_extensions was born out of necessity but with a limited scope. The company I work for (or worked for depending on when you're reading this) uses OSI PI for their process data historian. As we drove towards a smarter automation and alerting there was a need programmatically access A LOT of PI data through Python for ML workflows and fault detection. The web API was secured by Kerberos and fell back on NTLM. Multithreading with the requests library worked well enough  but I was still lacking speed partially due to inefficient code design and also just because there were only so many threads I could spawn for concurrent requests. Not to mention there's the additional overhead and complexity of threading compared to asyncio. I only needed to make get requests (they wouldn't let me post data anyway) so I went looking for an async client that supported Kerberos authentication. GSSAPI didnt work for me and I think that was mostly do to the fact I didn't understand Kerberos and credential cache (still dont) but I also think my domain controller had something to do with it. SSPI was valid alternative that worked well (credit [brandond](https://github.com/brandond) for the [requests-negotiate-sspi](https://github.com/brandond/requests-negotiate-sspi) package). But, that needed to be integrated into an asynchronous framework. So that takes us to this disclaimer...

**Disclaimer: httpx_extensions was developed with GET requests in mind. It was built on top of HTTPX AsyncClient API which is a full featured client but it has not been tested to work with other HTTPX features such as proxy mapping, event hooks, or even basic stuff such as POST requests. I would like to test all these features down the line but for now, you may not get the results you expect. Theoretically, all these features still work but it has not been tested.** 
## Installation
You can install httpx_extensions via pip

    pip install httpx_extensions
## Docs
Refer to the HTTPX [AsyncClient](https://www.python-httpx.org/async/) documentation as the API is identical. Read the rest of this document to understand the minor differences
## Usage
httpx_extensions is exactly what it sounds like, it is an extension of HTTPX thus the API is identical to the HTTPX [AsyncClient](https://www.python-httpx.org/async/) with a few important caveats...

 - The client only works with HTTP 1.1 connections. HTTP 2 uses multiplexing instead of connection pooling to achieve concurrent request so all requests are sent using the same TCP connection. Thus connection management is not a requirement in HTTP 2. If you attempt to use HTTP 2 with the ExClient it will raise an AssertionError
 - You cannot override the transport used by the client. The option is shown in order to be fully combatable the HTTPX API but the client will always use the AsyncHTTPTransportMixin class
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

 - The connection has expired
 - The connection was forced to close after a previous response either by some error in the connection object or by the server
 - The connection was not released properly back to the pool. Note that this would be a bug but a warning log will be issued saying "Connection id {conn_id} was given in request extensions but the connection is not reserved."

In all cases a log with level WARNING will be issued so be sure to configure basic logging for debugging.
### Disabling Connection Management
Connection management is actually disabled by default (kinda). The user needs to explicitly assign a "conn_id" to the request object otherwise the client will release the connection back to the pool. So in the two example auth flows above; the first where we did not assign a "conn_id" key to the extensions attribute, the connection will automatically be released back to the pool. In the second example where we do assign the "conn_id" key, that connection will continue to be reserved. The user has complete control on when connections get released back to the pool
## How it Works
HTTPX is built on top of [httpcore](https://github.com/encode/httpcore) which handles the actual connection pooling, sending requests, and receiving responses. The default connection pool implicitly releases connections back to the pool once the request-response cycle has completed. In HTTPX reading the response content to completion completes the cycle and releases the connection. The biggest change httpx_extensions makes is the how the httpcore AsyncConnectionPool works. Rather than connections being implicitly released they need to be explicitly released by calling the "release" method on the byte stream returned from  httpcore. This all happens automatically without any need for the user to manage the release of connections. Requiring an explicit release of the connection back to the pool ensures that connection is still available to be used again by subsequent requests in an auth flow should the user who wrote the auth flow choose to do so. The connection is then released once the auth flow completes. For streams, the connection is released when the response is closed.

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
In the above example we have a connection pool with 2 available connections and we want to make 3 requests. The first 2 requests are handled by the 2 connections in the pool. We receive a 401 response for both requests. The request -response cycle is complete but we are not done so the connections are flagged as reserved. The auth flow for each request adds an authorization header and submits the requests back to the pool. The requests reference the connections they were first sent on so the pool assigns the appropriate to the 2 requests. We receive 200 responses on the next cycle and the auth flow completes. Those 2 connections are now considered available again so request 3 can now be processed
## To Do

 - Integration tests: I want to fully test this API using the applicable tests from HTTPX. If someone wants to give it a go let me know how it goes
 - Explore known issues
	- When tested on an API that is known to support Kerberos, the auth always falls back to NTLM. But, when using requests-negotiate-sspi with requests, Kerberos succeeds
	- NTLM is supposed to be a connection based authentication method so how come when you make subsequent requests on a connection that was authenticated previously you need to run through the process again (which takes 3 round trips). This may be unique to the server that I was connecting too but would be interested to see if others experience similar issues
## Supports

 - Python 3.6+
 - httpx 0.22.0
 - pywin32==303 (for Negotiate Auth)

