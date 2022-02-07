# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## 0.1.0 (February 6, 2022)

The first beta release of http_extensions

### Added
* Tests! - Fully tested for integration with HTTPX (0.22.0). See "Unsupported Features" sections of the README for unsupported features
* You can now create instances of AsnycHTTPTransportMixin and pass those to the client. However, only AsyncHTTPTransportMixin instances are supported. Passing an unsupported transport instance will raise a ValueError
* MockTransport class

### Fixed
* Setting 'follow_redirects=True' would break the response relationship to a connection and it wasnt guarenteed that a redirect request would use the same connection. This has been changed so now the client appends the conn_id to the redirect request. This behavior can be changed by implementing a "request" event hook which could check for and remove conn_id from the request extensions

### Removed
* Negotiate SSPI auth flow - Must be installed separately through pip. See "Auth Flows" section of README
* Sync methods on ResponseMixin no longer raise NotImplementedError

### Changed
* Using http2=True raises a RuntimeError
* Neither of the limits 'keepalive_expiry' and 'max_keepalive_connections' can be 0, it defeats the purpose of what httpx_extensions was built for. Passing either of these parameters with a value of 0 will raise a ValueError