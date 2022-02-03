import base64
import getpass
import hashlib
import logging
import socket
import struct

import pywintypes
import sspi
import sspicon
import win32security
from httpcore.backends.asyncio import AsyncIOStream
from httpx import Auth, Request
from httpx._exceptions import HTTPError

from ..models import ResponseMixin


logger = logging.getLogger(__name__)


class NegotiateAuth(Auth):

    def __init__(
        self,
        username: str = None,
        domain: str = None,
        delegate: bool = False,
        host: str = None
    ) -> None:
        
        super().__init__()
        self._auth_info = None
        if username is not None:
            password = getpass.getpass(f"Enter password for user account '{username}': ")
            domain = domain or '.'
            self._auth_info = (username, domain, password)
        self._delegate = delegate
        self._host = host

    def auth_flow(self, request: Request):
        
        if request.headers.get("authorization") is not None:
            yield request
            return
        
        response_1: ResponseMixin = yield request
        auth_headers = response_1.headers.get_list("www-authenticate")
        auth_scheme = None
        allowed_schemes = ("Negotiate", "NTLM")
        for allowed_scheme in allowed_schemes:
            if auth_scheme is not None:
                break
            for auth_header in auth_headers:
                if allowed_scheme.lower() in auth_header.lower():
                    auth_scheme = allowed_scheme
                    break
        if not auth_scheme:
            # Server did not respond with Negotiate or NTLM
            return
        
        if self._host is None:
            self._host = request.url.host
            try:
                self._host = socket.getaddrinfo(self._host, None, 0, 0, 0, socket.AI_CANONNAME)[0][3]
                request.headers["host"] = self._host
            except socket.gaierror as err:
                logger.info('Skipping canonicalization of name %s due to error: %r', self._host, err)
        
        scheme = request.url.scheme
        targetspn = '{}/{}'.format(scheme, self._host)

        # request mutual auth by default
        scflags = sspicon.ISC_REQ_MUTUAL_AUTH
        if self._delegate:
            scflags |= sspicon.ISC_REQ_DELEGATE

        # Set up SSPI connection structure
        pkg_info = win32security.QuerySecurityPackageInfo(auth_scheme)
        clientauth = sspi.ClientAuth(
            auth_scheme,
            targetspn=targetspn,
            auth_info=self._auth_info,
            scflags=scflags,
            datarep=sspicon.SECURITY_NETWORK_DREP
        )
        sec_buffer = win32security.PySecBufferDescType()
        try:
            self.set_initial_challenge_header(
                request,
                response_1,
                auth_scheme,
                pkg_info,
                clientauth,
                sec_buffer
            )
            conn_id = response_1.extensions.get("conn_id")
            if not conn_id:
                logger.info(
                    "Did not get a connection id after initial request. Auth flow "
                    "cannot guarentee same connection will be used on next request "
                    "which will cause an authentication issue."
                )
            else:
                request.extensions.update(
                    {"conn_id": conn_id}
                )
            response_2: ResponseMixin = yield request
        except (pywintypes.error, AssertionError):
            return
        
        if response_2.status_code != 401:
            # Kerberos may have succeeded; if so, finalize our auth context
            final = response_2.headers.get("www-authenticate")
            if final is not None:
                try:
                    # Sometimes Windows seems to forget to prepend 'Negotiate' to
                    # the success response, and we get just a bare chunk of base64
                    # token. Not sure why.
                    final = final.replace(auth_scheme, '', 1).lstrip()
                    tokenbuf = win32security.PySecBufferType(pkg_info['MaxToken'], sspicon.SECBUFFER_TOKEN)
                    tokenbuf.Buffer = base64.b64decode(final.encode('ASCII'))
                    sec_buffer.append(tokenbuf)
                    error, auth = clientauth.authorize(sec_buffer)
                    logger.debug(
                        "Kerberos Authentication succeeded - error=%s authenticated=%s",
                        error, clientauth.authenticated
                    )
                except TypeError:
                    pass
            return
        
        # if kerberos failed, do NTLM
        try:
            self.try_ntlm(
                request,
                response_2,
                auth_scheme,
                pkg_info,
                clientauth,
                sec_buffer
            )
            if response_2.extensions.get("conn_id") != request.extensions.get("conn_id"):
                logger.debug(
                    "Request not handled on same connection during initial challenge"
                )
            yield request
        except pywintypes.error:
            return

    def set_initial_challenge_header(
        self,
        request: Request,
        response: ResponseMixin,
        auth_scheme: str,
        pkg_info: win32security.QuerySecurityPackageInfo,
        clientauth: sspi.ClientAuth,
        sec_buffer: win32security.PySecBufferDescType
    ) -> None:

        # Channel Binding Hash (aka Extended Protection for Authentication)
        # If this is a SSL connection, we need to hash the peer certificate,
        # prepend the RFC5929 channel binding type,
        # and stuff it into a SEC_CHANNEL_BINDINGS structure.
        # This should be sent along in the initial handshake or Kerberos auth will fail.
        network_stream: AsyncIOStream = response.extensions.get("network_stream")
        assert network_stream is not None and isinstance(network_stream, AsyncIOStream)
        try:
            peercert = network_stream._stream.extra_attributes.get("peer_certificate_binary")
        except AttributeError:
            logger.debug("No peercert in ssl context")
            peercert = None
        if peercert is not None:
            # peercert = bytes(json.dumps(peercert), 'ASCII')
            md = hashlib.sha256()
            md.update(peercert)
            appdata = 'tls-server-end-point:'.encode('ASCII')+md.digest()
            cbtbuf = win32security.PySecBufferType(
                pkg_info['MaxToken'], sspicon.SECBUFFER_CHANNEL_BINDINGS
            )
            cbtbuf.Buffer = struct.pack(
                'LLLLLLLL{}s'.format(len(appdata)),
                0,
                0,
                0,
                0,
                0,
                0,
                len(appdata),
                32,
                appdata
            )
            sec_buffer.append(cbtbuf)
        
        # this is important for some web applications that store
        # authentication-related info in cookies
        if response.headers.get("set-cookie") is not None:
            request.headers["cookie"] = response.headers["set-cookie"]
        
        try:
            error, auth = clientauth.authorize(sec_buffer)
            auth_header = f"{auth_scheme} {base64.b64encode(auth[0].Buffer).decode('ascii')}"
            request.headers["authorization"] = auth_header
            logger.debug(
                'Sending Initial Context Token - error=%s authenticated=%s',
                error, clientauth.authenticated
            )
        except pywintypes.error as err:
            logger.debug(
                "Error calling %s: %s",
                err[1], err[2], exc_info=err
            )
            raise err
        return
    
    def try_ntlm(
        self,
        request: Request,
        response: ResponseMixin,
        auth_scheme: str,
        pkg_info: win32security.QuerySecurityPackageInfo,
        clientauth: sspi.ClientAuth,
        sec_buffer: win32security.PySecBufferDescType
    ) -> None:

        # Extract challenge message from server
        challenge_header = response.headers["www-authenticate"]
        challenge = [val[len(auth_scheme)+1:] for val in challenge_header.split(', ') if auth_scheme in val]
        if len(challenge) > 1:
            raise HTTPError(f"Did not get exactly one {auth_scheme} challenge from server.")
        
        tokenbuf = win32security.PySecBufferType(pkg_info['MaxToken'], sspicon.SECBUFFER_TOKEN)
        tokenbuf.Buffer = base64.b64decode(challenge[0])
        sec_buffer.append(tokenbuf)
        logger.debug('Got Challenge Token (NTLM)')

        # Perform next authorization step
        try:
            error, auth = clientauth.authorize(sec_buffer)
            auth_header = f"{auth_scheme} {base64.b64encode(auth[0].Buffer).decode('ascii')}"
            request.headers["authorization"] = auth_header
            logger.debug(
                'Sending response - error=%s authenticated=%s',
                error, clientauth.authenticated
            )
        except pywintypes.error as err:
            logger.debug(
                "Error calling %s: %s",
                err[1], err[2], exc_info=err
            )
            raise err
        return