# -*- test-case-name: twisted.web.test.test_newclient -*-
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
An U{HTTP 1.1<http://www.w3.org/Protocols/rfc2616/rfc2616.html>} client.

The way to use the functionality provided by this module is to:

  - Connect a L{HTTP11ClientProtocol} to an HTTP server
  - Create a L{Request} with the appropriate data
  - Pass the request to L{HTTP11ClientProtocol.request}
  - The returned Deferred will fire with a L{Response} object
  - Create a L{IProtocol} provider which can handle the response body
  - Connect it to the response with L{Response.deliverBody}
  - When the protocol's C{connectionLost} method is called, the response is
    complete.  See L{Response.deliverBody} for details.

Various other classes in this module support this usage:

  - HTTPParser is the basic HTTP parser.  It can handle the parts of HTTP which
    are symmetric between requests and responses.

  - HTTPClientParser extends HTTPParser to handle response-specific parts of
    HTTP.  One instance is created for each request to parse the corresponding
    response.
"""

__metaclass__ = type

from zope.interface import implements

from twisted.python import log
from twisted.python.reflect import fullyQualifiedName
from twisted.python.failure import Failure
from twisted.python.compat import set
from twisted.internet.interfaces import IConsumer, IPushProducer
from twisted.internet.error import ConnectionDone
from twisted.internet.defer import Deferred, succeed, fail, maybeDeferred
from twisted.internet.protocol import Protocol
from twisted.protocols.basic import LineReceiver
from twisted.web.iweb import UNKNOWN_LENGTH
from twisted.web.http_headers import Headers
from twisted.web.http import NO_CONTENT, NOT_MODIFIED
from twisted.web.http import _DataLoss, PotentialDataLoss
from twisted.web.http import _IdentityTransferDecoder, _ChunkedTransferDecoder

# States HTTPParser can be in
STATUS = 'STATUS'
HEADER = 'HEADER'
BODY = 'BODY'
DONE = 'DONE'


class BadHeaders(Exception):
    """
    Headers passed to L{Request} were in some way invalid.
    """



class ExcessWrite(Exception):
    """
    The body L{IBodyProducer} for a request tried to write data after
    indicating it had finished writing data.
    """


class ParseError(Exception):
    """
    Some received data could not be parsed.

    @ivar data: The string which could not be parsed.
    """
    def __init__(self, reason, data):
        Exception.__init__(self, reason, data)
        self.data = data



class BadResponseVersion(ParseError):
    """
    The version string in a status line was unparsable.
    """



class _WrapperException(Exception):
    """
    L{_WrapperException} is the base exception type for exceptions which
    include one or more other exceptions as the low-level causes.

    @ivar reasons: A list of exceptions.  See subclass documentation for more
        details.
    """
    def __init__(self, reasons):
        Exception.__init__(self, reasons)
        self.reasons = reasons



class RequestGenerationFailed(_WrapperException):
    """
    There was an error while creating the bytes which make up a request.

    @ivar reasons: A C{list} of one or more L{Failure} instances giving the
        reasons the request generation was considered to have failed.
    """



class RequestTransmissionFailed(_WrapperException):
    """
    There was an error while sending the bytes which make up a request.

    @ivar reasons: A C{list} of one or more L{Failure} instances giving the
        reasons the request transmission was considered to have failed.
    """



class ConnectionAborted(Exception):
    """
    The connection was explicitly aborted by application code.
    """



class WrongBodyLength(Exception):
    """
    An L{IBodyProducer} declared the number of bytes it was going to
    produce (via its C{length} attribute) and then produced a different number
    of bytes.
    """



class ResponseDone(Exception):
    """
    L{ResponseDone} may be passed to L{IProtocol.connectionLost} on the
    protocol passed to L{Response.deliverBody} and indicates that the entire
    response has been delivered.
    """



class ResponseFailed(_WrapperException):
    """
    L{ResponseFailed} indicates that all of the response to a request was not
    received for some reason.

    @ivar reasons: A C{list} of one or more L{Failure} instances giving the
        reasons the response was considered to have failed.
    """



class RequestNotSent(Exception):
    """
    L{RequestNotSent} indicates that an attempt was made to issue a request but
    for reasons unrelated to the details of the request itself, the request
    could not be sent.  For example, this may indicate that an attempt was made
    to send a request using a protocol which is no longer connected to a
    server.
    """



def _callAppFunction(function):
    """
    Call C{function}.  If it raises an exception, log it with a minimal
    description of the source.

    @return: C{None}
    """
    try:
        function()
    except:
        log.err(None, "Unexpected exception from %s" % (
                fullyQualifiedName(function),))



class HTTPParser(LineReceiver):
    """
    L{HTTPParser} handles the parsing side of HTTP processing. With a suitable
    subclass, it can parse either the client side or the server side of the
    connection.

    @ivar headers: All of the non-connection control message headers yet
        received.

    @ivar state: State indicator for the response parsing state machine.  One
        of C{STATUS}, C{HEADER}, C{BODY}, C{DONE}.

    @ivar _partialHeader: C{None} or a C{list} of the lines of a multiline
        header while that header is being received.
    """

    # NOTE: According to HTTP spec, we're supposed to eat the
    # 'Proxy-Authenticate' and 'Proxy-Authorization' headers also, but that
    # doesn't sound like a good idea to me, because it makes it impossible to
    # have a non-authenticating transparent proxy in front of an authenticating
    # proxy. An authenticating proxy can eat them itself. -jknight
    #
    # Further, quoting
    # http://homepages.tesco.net/J.deBoynePollard/FGA/web-proxy-connection-header.html
    # regarding the 'Proxy-Connection' header:
    #
    #    The Proxy-Connection: header is a mistake in how some web browsers
    #    use HTTP. Its name is the result of a false analogy. It is not a
    #    standard part of the protocol. There is a different standard
    #    protocol mechanism for doing what it does. And its existence
    #    imposes a requirement upon HTTP servers such that no proxy HTTP
    #    server can be standards-conforming in practice.
    #
    # -exarkun

    # Some servers (like http://news.ycombinator.com/) return status lines and
    # HTTP headers delimited by \n instead of \r\n.
    delimiter = '\n'

    CONNECTION_CONTROL_HEADERS = set([
            'content-length', 'connection', 'keep-alive', 'te', 'trailers',
            'transfer-encoding', 'upgrade', 'proxy-connection'])

    def connectionMade(self):
        self.headers = Headers()
        self.connHeaders = Headers()
        self.state = STATUS
        self._partialHeader = None


    def switchToBodyMode(self, decoder):
        """
        Switch to body parsing mode - interpret any more bytes delivered as
        part of the message body and deliver them to the given decoder.
        """
        if self.state == BODY:
            raise RuntimeError("already in body mode")

        self.bodyDecoder = decoder
        self.state = BODY
        self.setRawMode()


    def lineReceived(self, line):
        """
        Handle one line from a response.
        """
        # Handle the normal CR LF case.
        if line[-1:] == '\r':
            line = line[:-1]

        if self.state == STATUS:
            self.statusReceived(line)
            self.state = HEADER
        elif self.state == HEADER:
            if not line or line[0] not in ' \t':
                if self._partialHeader is not None:
                    header = ''.join(self._partialHeader)
                    name, value = header.split(':', 1)
                    value = value.strip()
                    self.headerReceived(name, value)
                if not line:
                    # Empty line means the header section is over.
                    self.allHeadersReceived()
                else:
                    # Line not beginning with LWS is another header.
                    self._partialHeader = [line]
            else:
                # A line beginning with LWS is a continuation of a header
                # begun on a previous line.
                self._partialHeader.append(line)


    def rawDataReceived(self, data):
        """
        Pass data from the message body to the body decoder object.
        """
        self.bodyDecoder.dataReceived(data)


    def isConnectionControlHeader(self, name):
        """
        Return C{True} if the given lower-cased name is the name of a
        connection control header (rather than an entity header).

        According to RFC 2616, section 14.10, the tokens in the Connection
        header are probably relevant here.  However, I am not sure what the
        practical consequences of either implementing or ignoring that are.
        So I leave it unimplemented for the time being.
        """
        return name in self.CONNECTION_CONTROL_HEADERS


    def statusReceived(self, status):
        """
        Callback invoked whenever the first line of a new message is received.
        Override this.

        @param status: The first line of an HTTP request or response message
            without trailing I{CR LF}.
        @type status: C{str}
        """


    def headerReceived(self, name, value):
        """
        Store the given header in C{self.headers}.
        """
        name = name.lower()
        if self.isConnectionControlHeader(name):
            headers = self.connHeaders
        else:
            headers = self.headers
        headers.addRawHeader(name, value)


    def allHeadersReceived(self):
        """
        Callback invoked after the last header is passed to C{headerReceived}.
        Override this to change to the C{BODY} or C{DONE} state.
        """
        self.switchToBodyMode(None)



class HTTPClientParser(HTTPParser):
    """
    An HTTP parser which only handles HTTP responses.

    @ivar request: The request with which the expected response is associated.
    @type request: L{Request}

    @ivar NO_BODY_CODES: A C{set} of response codes which B{MUST NOT} have a
        body.

    @ivar finisher: A callable to invoke when this response is fully parsed.

    @ivar _responseDeferred: A L{Deferred} which will be called back with the
        response when all headers in the response have been received.
        Thereafter, C{None}.
    """
    NO_BODY_CODES = set([NO_CONTENT, NOT_MODIFIED])

    _transferDecoders = {
        'chunked': _ChunkedTransferDecoder,
        }

    bodyDecoder = None

    def __init__(self, request, finisher):
        self.request = request
        self.finisher = finisher
        self._responseDeferred = Deferred()


    def parseVersion(self, strversion):
        """
        Parse version strings of the form Protocol '/' Major '.' Minor. E.g.
        'HTTP/1.1'.  Returns (protocol, major, minor).  Will raise ValueError
        on bad syntax.
        """
        try:
            proto, strnumber = strversion.split('/')
            major, minor = strnumber.split('.')
            major, minor = int(major), int(minor)
        except ValueError, e:
            raise BadResponseVersion(str(e), strversion)
        if major < 0 or minor < 0:
            raise BadResponseVersion("version may not be negative", strversion)
        return (proto, major, minor)


    def statusReceived(self, status):
        """
        Parse the status line into its components and create a response object
        to keep track of this response's state.
        """
        parts = status.split(' ', 2)
        if len(parts) != 3:
            raise ParseError("wrong number of parts", status)

        try:
            statusCode = int(parts[1])
        except ValueError:
            raise ParseError("non-integer status code", status)

        self.response = Response(
            self.parseVersion(parts[0]),
            statusCode,
            parts[2],
            self.headers,
            self.transport)


    def _finished(self, rest):
        """
        Called to indicate that an entire response has been received.  No more
        bytes will be interpreted by this L{HTTPClientParser}.  Extra bytes are
        passed up and the state of this L{HTTPClientParser} is set to I{DONE}.

        @param rest: A C{str} giving any extra bytes delivered to this
            L{HTTPClientParser} which are not part of the response being
            parsed.
        """
        self.state = DONE
        self.finisher(rest)


    def isConnectionControlHeader(self, name):
        """
        Content-Length in the response to a HEAD request is an entity header,
        not a connection control header.
        """
        if self.request.method == 'HEAD' and name == 'content-length':
            return False
        return HTTPParser.isConnectionControlHeader(self, name)


    def allHeadersReceived(self):
        """
        Figure out how long the response body is going to be by examining
        headers and stuff.
        """
        if (self.response.code in self.NO_BODY_CODES
            or self.request.method == 'HEAD'):
            self.response.length = 0
            self._finished(self.clearLineBuffer())
        else:
            transferEncodingHeaders = self.connHeaders.getRawHeaders(
                'transfer-encoding')
            if transferEncodingHeaders:

                # This could be a KeyError.  However, that would mean we do not
                # know how to decode the response body, so failing the request
                # is as good a behavior as any.  Perhaps someday we will want
                # to normalize/document/test this specifically, but failing
                # seems fine to me for now.
                transferDecoder = self._transferDecoders[transferEncodingHeaders[0].lower()]

                # If anyone ever invents a transfer encoding other than
                # chunked (yea right), and that transfer encoding can predict
                # the length of the response body, it might be sensible to
                # allow the transfer decoder to set the response object's
                # length attribute.
            else:
                contentLengthHeaders = self.connHeaders.getRawHeaders('content-length')
                if contentLengthHeaders is None:
                    contentLength = None
                elif len(contentLengthHeaders) == 1:
                    contentLength = int(contentLengthHeaders[0])
                    self.response.length = contentLength
                else:
                    # "HTTP Message Splitting" or "HTTP Response Smuggling"
                    # potentially happening.  Or it's just a buggy server.
                    raise ValueError(
                        "Too many Content-Length headers; response is invalid")

                if contentLength == 0:
                    self._finished(self.clearLineBuffer())
                    transferDecoder = None
                else:
                    transferDecoder = lambda x, y: _IdentityTransferDecoder(
                        contentLength, x, y)

            if transferDecoder is None:
                self.response._bodyDataFinished()
            else:
                # Make sure as little data as possible from the response body
                # gets delivered to the response object until the response
                # object actually indicates it is ready to handle bytes
                # (probably because an application gave it a way to interpret
                # them).
                self.transport.pauseProducing()
                self.switchToBodyMode(transferDecoder(
                        self.response._bodyDataReceived,
                        self._finished))

        # This must be last.  If it were first, then application code might
        # change some state (for example, registering a protocol to receive the
        # response body).  Then the pauseProducing above would be wrong since
        # the response is ready for bytes and nothing else would ever resume
        # the transport.
        self._responseDeferred.callback(self.response)
        del self._responseDeferred


    def connectionLost(self, reason):
        if self.bodyDecoder is not None:
            try:
                try:
                    self.bodyDecoder.noMoreData()
                except PotentialDataLoss:
                    self.response._bodyDataFinished(Failure())
                except _DataLoss:
                    self.response._bodyDataFinished(
                        Failure(ResponseFailed([reason, Failure()])))
                else:
                    self.response._bodyDataFinished()
            except:
                # Handle exceptions from both the except suites and the else
                # suite.  Those functions really shouldn't raise exceptions,
                # but maybe there's some buggy application code somewhere
                # making things difficult.
                log.err()
        elif self.state != DONE:
            self._responseDeferred.errback(Failure(ResponseFailed([reason])))
            del self._responseDeferred



class Request:
    """
    A L{Request} instance describes an HTTP request to be sent to an HTTP
    server.
    
    @ivar method: The HTTP method to for this request, ex: 'GET', 'HEAD',
        'POST', etc.
    @type method: C{str}
    
    @ivar uri: The relative URI of the resource to request.  For example,
        C{'/foo/bar?baz=quux'}.
    @type uri: C{str}
    
    @ivar headers: Headers to be sent to the server.  It is important to
        note that this object does not create any implicit headers.  So it
        is up to the HTTP Client to add required headers such as 'Host'.
    @type headers: L{twisted.web.http_headers.Headers}
    
    @ivar bodyProducer: C{None} or an L{IBodyProducer} provider which
        produces the content body to send to the remote HTTP server.
    
    @ivar persistent: Set to C{True} when you use HTTP persistent connecton.
    @type persistent: Boolean
    """
    def __init__(self, method, uri, headers, bodyProducer, persistent=False):
        self.method = method
        self.uri = uri
        self.headers = headers
        self.bodyProducer = bodyProducer
        self.persistent = persistent


    def _writeHeaders(self, transport, TEorCL):
        hosts = self.headers.getRawHeaders('host', ())
        if len(hosts) != 1:
            raise BadHeaders("Exactly one Host header required")

        # In the future, having the protocol version be a parameter to this
        # method would probably be good.  It would be nice if this method
        # weren't limited to issueing HTTP/1.1 requests.
        requestLines = []
        requestLines.append(
            '%s %s HTTP/1.1\r\n' % (self.method, self.uri))
        if self.persistent:
            requestLines.append('Connection: Keep-Alive\r\n')
        else:
            requestLines.append('Connection: close\r\n')
        
        if TEorCL is not None:
            requestLines.append(TEorCL)
        for name, values in self.headers.getAllRawHeaders():
            requestLines.extend(['%s: %s\r\n' % (name, v) for v in values])
        requestLines.append('\r\n')
        transport.writeSequence(requestLines)


    def _writeToChunked(self, transport):
        """
        Write this request to the given transport using chunked
        transfer-encoding to frame the body.
        """
        self._writeHeaders(transport, 'Transfer-Encoding: chunked\r\n')
        encoder = ChunkedEncoder(transport)
        encoder.registerProducer(self.bodyProducer, True)
        d = self.bodyProducer.startProducing(encoder)

        def cbProduced(ignored):
            encoder.unregisterProducer()
        def ebProduced(err):
            encoder._allowNoMoreWrites()
            # Don't call the encoder's unregisterProducer because it will write
            # a zero-length chunk.  This would indicate to the server that the
            # request body is complete.  There was an error, though, so we
            # don't want to do that.
            transport.unregisterProducer()
            return err
        d.addCallbacks(cbProduced, ebProduced)
        return d


    def _writeToContentLength(self, transport):
        """
        Write this request to the given transport using content-length to frame
        the body.
        """
        self._writeHeaders(
            transport,
            'Content-Length: %d\r\n' % (self.bodyProducer.length,))

        # This Deferred is used to signal an error in the data written to the
        # encoder below.  It can only errback and it will only do so before too
        # many bytes have been written to the encoder and before the producer
        # Deferred fires.
        finishedConsuming = Deferred()

        # This makes sure the producer writes the correct number of bytes for
        # the request body.
        encoder = LengthEnforcingConsumer(
            self.bodyProducer, transport, finishedConsuming)

        transport.registerProducer(self.bodyProducer, True)

        finishedProducing = self.bodyProducer.startProducing(encoder)

        def combine(consuming, producing):
            # This Deferred is returned and will be fired when the first of
            # consuming or producing fires.
            ultimate = Deferred()

            # Keep track of what has happened so far.  This initially
            # contains None, then an integer uniquely identifying what
            # sequence of events happened.  See the callbacks and errbacks
            # defined below for the meaning of each value.
            state = [None]

            def ebConsuming(err):
                if state == [None]:
                    # The consuming Deferred failed first.  This means the
                    # overall writeTo Deferred is going to errback now.  The
                    # producing Deferred should not fire later (because the
                    # consumer should have called stopProducing on the
                    # producer), but if it does, a callback will be ignored
                    # and an errback will be logged.
                    state[0] = 1
                    ultimate.errback(err)
                else:
                    # The consuming Deferred errbacked after the producing
                    # Deferred fired.  This really shouldn't ever happen.
                    # If it does, I goofed.  Log the error anyway, just so
                    # there's a chance someone might notice and complain.
                    log.err(
                        err,
                        "Buggy state machine in %r/[%d]: "
                        "ebConsuming called" % (self, state[0]))

            def cbProducing(result):
                if state == [None]:
                    # The producing Deferred succeeded first.  Nothing will
                    # ever happen to the consuming Deferred.  Tell the
                    # encoder we're done so it can check what the producer
                    # wrote and make sure it was right.
                    state[0] = 2
                    try:
                        encoder._noMoreWritesExpected()
                    except:
                        # Fail the overall writeTo Deferred - something the
                        # producer did was wrong.
                        ultimate.errback()
                    else:
                        # Success - succeed the overall writeTo Deferred.
                        ultimate.callback(None)
                # Otherwise, the consuming Deferred already errbacked.  The
                # producing Deferred wasn't supposed to fire, but it did
                # anyway.  It's buggy, but there's not really anything to be
                # done about it.  Just ignore this result.

            def ebProducing(err):
                if state == [None]:
                    # The producing Deferred failed first.  This means the
                    # overall writeTo Deferred is going to errback now.
                    # Tell the encoder that we're done so it knows to reject
                    # further writes from the producer (which should not
                    # happen, but the producer may be buggy).
                    state[0] = 3
                    encoder._allowNoMoreWrites()
                    ultimate.errback(err)
                else:
                    # The producing Deferred failed after the consuming
                    # Deferred failed.  It shouldn't have, so it's buggy.
                    # Log the exception in case anyone who can fix the code
                    # is watching.
                    log.err(err, "Producer is buggy")

            consuming.addErrback(ebConsuming)
            producing.addCallbacks(cbProducing, ebProducing)

            return ultimate

        d = combine(finishedConsuming, finishedProducing)
        def f(passthrough):
            # Regardless of what happens with the overall Deferred, once it
            # fires, the producer registered way up above the definition of
            # combine should be unregistered.
            transport.unregisterProducer()
            return passthrough
        d.addBoth(f)
        return d


    def writeTo(self, transport):
        """
        Format this L{Request} as an HTTP/1.1 request and write it to the given
        transport.  If bodyProducer is not None, it will be associated with an
        L{IConsumer}.

        @return: A L{Deferred} which fires with C{None} when the request has
            been completely written to the transport or with a L{Failure} if
            there is any problem generating the request bytes.
        """
        if self.bodyProducer is not None:
            if self.bodyProducer.length is UNKNOWN_LENGTH:
                return self._writeToChunked(transport)
            else:
                return self._writeToContentLength(transport)
        else:
            self._writeHeaders(transport, None)
            return succeed(None)


    def stopWriting(self):
        """
        Stop writing this request to the transport.  This can only be called
        after C{writeTo} and before the L{Deferred} returned by C{writeTo}
        fires.  It should cancel any asynchronous task started by C{writeTo}.
        The L{Deferred} returned by C{writeTo} need not be fired if this method
        is called.
        """
        # If bodyProducer is None, then the Deferred returned by writeTo has
        # fired already and this method cannot be called.
        _callAppFunction(self.bodyProducer.stopProducing)



class LengthEnforcingConsumer:
    """
    An L{IConsumer} proxy which enforces an exact length requirement on the
    total data written to it.

    @ivar _length: The number of bytes remaining to be written.

    @ivar _producer: The L{IBodyProducer} which is writing to this
        consumer.

    @ivar _consumer: The consumer to which at most C{_length} bytes will be
        forwarded.

    @ivar _finished: A L{Deferred} which will be fired with a L{Failure} if too
        many bytes are written to this consumer.
    """
    def __init__(self, producer, consumer, finished):
        self._length = producer.length
        self._producer = producer
        self._consumer = consumer
        self._finished = finished


    def _allowNoMoreWrites(self):
        """
        Indicate that no additional writes are allowed.  Attempts to write
        after calling this method will be met with an exception.
        """
        self._finished = None


    def write(self, bytes):
        """
        Write C{bytes} to the underlying consumer unless
        C{_noMoreWritesExpected} has been called or there are/have been too
        many bytes.
        """
        if self._finished is None:
            # No writes are supposed to happen any more.  Try to convince the
            # calling code to stop calling this method by calling its
            # stopProducing method and then throwing an exception at it.  This
            # exception isn't documented as part of the API because you're
            # never supposed to expect it: only buggy code will ever receive
            # it.
            self._producer.stopProducing()
            raise ExcessWrite()

        if len(bytes) <= self._length:
            self._length -= len(bytes)
            self._consumer.write(bytes)
        else:
            # No synchronous exception is raised in *this* error path because
            # we still have _finished which we can use to report the error to a
            # better place than the direct caller of this method (some
            # arbitrary application code).
            _callAppFunction(self._producer.stopProducing)
            self._finished.errback(WrongBodyLength("too many bytes written"))
            self._allowNoMoreWrites()


    def _noMoreWritesExpected(self):
        """
        Called to indicate no more bytes will be written to this consumer.
        Check to see that the correct number have been written.

        @raise WrongBodyLength: If not enough bytes have been written.
        """
        if self._finished is not None:
            self._allowNoMoreWrites()
            if self._length:
                raise WrongBodyLength("too few bytes written")



def makeStatefulDispatcher(name, template):
    """
    Given a I{dispatch} name and a function, return a function which can be
    used as a method and which, when called, will call another method defined
    on the instance and return the result.  The other method which is called is
    determined by the value of the C{_state} attribute of the instance.

    @param name: A string which is used to construct the name of the subsidiary
        method to invoke.  The subsidiary method is named like C{'_%s_%s' %
        (name, _state)}.

    @param template: A function object which is used to give the returned
        function a docstring.

    @return: The dispatcher function.
    """
    def dispatcher(self, *args, **kwargs):
        func = getattr(self, '_' + name + '_' + self._state, None)
        if func is None:
            raise RuntimeError(
                "%r has no %s method in state %s" % (self, name, self._state))
        return func(*args, **kwargs)
    dispatcher.__doc__ = template.__doc__
    return dispatcher



class Response:
    """
    A L{Response} instance describes an HTTP response received from an HTTP
    server.

    L{Response} should not be subclassed or instantiated.

    @ivar version: A three-tuple describing the protocol and protocol version
        of the response.  The first element is of type C{str}, the second and
        third are of type C{int}.  For example, C{('HTTP', 1, 1)}.
    @type version: C{tuple}

    @ivar code: The HTTP status code of this response.
    @type code: C{int}

    @ivar phrase: The HTTP reason phrase of this response.
    @type phrase: C{str}

    @ivar headers: The HTTP response headers of this response.
    @type headers: L{Headers}

    @ivar length: The number of bytes expected to be in the body of this
        response or L{UNKNOWN_LENGTH} if the server did not indicate how many
        bytes to expect.  For I{HEAD} responses, this will be 0; if the
        response includes a I{Content-Length} header, it will be available in
        C{headers}.
    @type length: C{int} or something else

    @ivar _transport: The transport which is delivering this response.

    @ivar _bodyProtocol: The L{IProtocol} provider to which the body is
        delivered.  C{None} before one has been registered with
        C{deliverBody}.

    @ivar _bodyBuffer: A C{list} of the strings passed to C{bodyDataReceived}
        before C{deliverBody} is called.  C{None} afterwards.

    @ivar _state: Indicates what state this L{Response} instance is in,
        particularly with respect to delivering bytes from the response body
        to an application-suppled protocol object.  This may be one of
        C{'INITIAL'}, C{'CONNECTED'}, C{'DEFERRED_CLOSE'}, or C{'FINISHED'},
        with the following meanings:

          - INITIAL: This is the state L{Response} objects start in.  No
            protocol has yet been provided and the underlying transport may
            still have bytes to deliver to it.

          - DEFERRED_CLOSE: If the underlying transport indicates all bytes
            have been delivered but no application-provided protocol is yet
            available, the L{Response} moves to this state.  Data is
            buffered and waiting for a protocol to be delivered to.

          - CONNECTED: If a protocol is provided when the state is INITIAL,
            the L{Response} moves to this state.  Any buffered data is
            delivered and any data which arrives from the transport
            subsequently is given directly to the protocol.

          - FINISHED: If a protocol is provided in the DEFERRED_CLOSE state,
            the L{Response} moves to this state after delivering all
            buffered data to the protocol.  Otherwise, if the L{Response} is
            in the CONNECTED state, if the transport indicates there is no
            more data, the L{Response} moves to this state.  Nothing else
            can happen once the L{Response} is in this state.
    """

    length = UNKNOWN_LENGTH

    _bodyProtocol = None
    _bodyFinished = False

    def __init__(self, version, code, phrase, headers, _transport):
        self.version = version
        self.code = code
        self.phrase = phrase
        self.headers = headers
        self._transport = _transport
        self._bodyBuffer = []
        self._state = 'INITIAL'


    def deliverBody(self, protocol):
        """
        Register an L{IProtocol} provider to receive the response body.

        The protocol will be connected to a transport which provides
        L{IPushProducer}.  The protocol's C{connectionLost} method will be
        called with:

            - ResponseDone, which indicates that all bytes from the response
              have been successfully delivered.

            - PotentialDataLoss, which indicates that it cannot be determined
              if the entire response body has been delivered.  This only occurs
              when making requests to HTTP servers which do not set
              I{Content-Length} or a I{Transfer-Encoding} in the response.

            - ResponseFailed, which indicates that some bytes from the response
              were lost.  The C{reasons} attribute of the exception may provide
              more specific indications as to why.
        """
    deliverBody = makeStatefulDispatcher('deliverBody', deliverBody)


    def _deliverBody_INITIAL(self, protocol):
        """
        Deliver any buffered data to C{protocol} and prepare to deliver any
        future data to it.  Move to the C{'CONNECTED'} state.
        """
        # Now that there's a protocol to consume the body, resume the
        # transport.  It was previously paused by HTTPClientParser to avoid
        # reading too much data before it could be handled.
        self._transport.resumeProducing()

        protocol.makeConnection(self._transport)
        self._bodyProtocol = protocol
        for data in self._bodyBuffer:
            self._bodyProtocol.dataReceived(data)
        self._bodyBuffer = None
        self._state = 'CONNECTED'


    def _deliverBody_CONNECTED(self, protocol):
        """
        It is invalid to attempt to deliver data to a protocol when it is
        already being delivered to another protocol.
        """
        raise RuntimeError(
            "Response already has protocol %r, cannot deliverBody "
            "again" % (self._bodyProtocol,))


    def _deliverBody_DEFERRED_CLOSE(self, protocol):
        """
        Deliver any buffered data to C{protocol} and then disconnect the
        protocol.  Move to the C{'FINISHED'} state.
        """
        # Unlike _deliverBody_INITIAL, there is no need to resume the
        # transport here because all of the response data has been received
        # already.  Some higher level code may want to resume the transport if
        # that code expects further data to be received over it.

        protocol.makeConnection(self._transport)

        for data in self._bodyBuffer:
            protocol.dataReceived(data)
        self._bodyBuffer = None
        protocol.connectionLost(self._reason)
        self._state = 'FINISHED'


    def _deliverBody_FINISHED(self, protocol):
        """
        It is invalid to attempt to deliver data to a protocol after the
        response body has been delivered to another protocol.
        """
        raise RuntimeError(
            "Response already finished, cannot deliverBody now.")


    def _bodyDataReceived(self, data):
        """
        Called by HTTPClientParser with chunks of data from the response body.
        They will be buffered or delivered to the protocol passed to
        deliverBody.
        """
    _bodyDataReceived = makeStatefulDispatcher('bodyDataReceived',
                                               _bodyDataReceived)


    def _bodyDataReceived_INITIAL(self, data):
        """
        Buffer any data received for later delivery to a protocol passed to
        C{deliverBody}.

        Little or no data should be buffered by this method, since the
        transport has been paused and will not be resumed until a protocol
        is supplied.
        """
        self._bodyBuffer.append(data)


    def _bodyDataReceived_CONNECTED(self, data):
        """
        Deliver any data received to the protocol to which this L{Response}
        is connected.
        """
        self._bodyProtocol.dataReceived(data)


    def _bodyDataReceived_DEFERRED_CLOSE(self, data):
        """
        It is invalid for data to be delivered after it has been indicated
        that the response body has been completely delivered.
        """
        raise RuntimeError("Cannot receive body data after _bodyDataFinished")


    def _bodyDataReceived_FINISHED(self, data):
        """
        It is invalid for data to be delivered after the response bofdy has
        been delivered to a protocol.
        """
        raise RuntimeError("Cannot receive body data after protocol disconnected")


    def _bodyDataFinished(self, reason=None):
        """
        Called by HTTPClientParser when no more body data is available.  If the
        optional reason is supplied, this indicates a problem or potential
        problem receiving all of the response body.
        """
    _bodyDataFinished = makeStatefulDispatcher('bodyDataFinished',
                                               _bodyDataFinished)


    def _bodyDataFinished_INITIAL(self, reason=None):
        """
        Move to the C{'DEFERRED_CLOSE'} state to wait for a protocol to
        which to deliver the response body.
        """
        self._state = 'DEFERRED_CLOSE'
        if reason is None:
            reason = Failure(ResponseDone("Response body fully received"))
        self._reason = reason


    def _bodyDataFinished_CONNECTED(self, reason=None):
        """
        Disconnect the protocol and move to the C{'FINISHED'} state.
        """
        if reason is None:
            reason = Failure(ResponseDone("Response body fully received"))
        self._bodyProtocol.connectionLost(reason)
        self._bodyProtocol = None
        self._state = 'FINISHED'


    def _bodyDataFinished_DEFERRED_CLOSE(self):
        """
        It is invalid to attempt to notify the L{Response} of the end of the
        response body data more than once.
        """
        raise RuntimeError("Cannot finish body data more than once")


    def _bodyDataFinished_FINISHED(self):
        """
        It is invalid to attempt to notify the L{Response} of the end of the
        response body data more than once.
        """
        raise RuntimeError("Cannot finish body data after protocol disconnected")



class ChunkedEncoder:
    """
    Helper object which exposes L{IConsumer} on top of L{HTTP11ClientProtocol}
    for streaming request bodies to the server.
    """
    implements(IConsumer)

    def __init__(self, transport):
        self.transport = transport


    def _allowNoMoreWrites(self):
        """
        Indicate that no additional writes are allowed.  Attempts to write
        after calling this method will be met with an exception.
        """
        self.transport = None


    def registerProducer(self, producer, streaming):
        """
        Register the given producer with C{self.transport}.
        """
        self.transport.registerProducer(producer, streaming)


    def write(self, data):
        """
        Write the given request body bytes to the transport using chunked
        encoding.

        @type data: C{str}
        """
        if self.transport is None:
            raise ExcessWrite()
        self.transport.writeSequence(("%x\r\n" % len(data), data, "\r\n"))


    def unregisterProducer(self):
        """
        Indicate that the request body is complete and finish the request.
        """
        self.write('')
        self.transport.unregisterProducer()
        self._allowNoMoreWrites()



class TransportProxyProducer:
    """
    An L{IPushProducer} implementation which wraps another such thing and
    proxies calls to it until it is told to stop.

    @ivar _producer: The wrapped L{IPushProducer} provider or C{None} after
        this proxy has been stopped.
    """
    implements(IPushProducer)

    # LineReceiver uses this undocumented attribute of transports to decide
    # when to stop calling lineReceived or rawDataReceived (if it finds it to
    # be true, it doesn't bother to deliver any more data).  Set disconnecting
    # to False here and never change it to true so that all data is always
    # delivered to us and so that LineReceiver doesn't fail with an
    # AttributeError.
    disconnecting = False

    def __init__(self, producer):
        self._producer = producer


    def _stopProxying(self):
        """
        Stop forwarding calls of L{IPushProducer} methods to the underlying
        L{IPushProvider} provider.
        """
        self._producer = None


    def stopProducing(self):
        """
        Proxy the stoppage to the underlying producer, unless this proxy has
        been stopped.
        """
        if self._producer is not None:
            self._producer.stopProducing()


    def resumeProducing(self):
        """
        Proxy the resumption to the underlying producer, unless this proxy has
        been stopped.
        """
        if self._producer is not None:
            self._producer.resumeProducing()


    def pauseProducing(self):
        """
        Proxy the pause to the underlying producer, unless this proxy has been
        stopped.
        """
        if self._producer is not None:
            self._producer.pauseProducing()



class HTTP11ClientProtocol(Protocol):
    """
    L{HTTP11ClientProtocol} is an implementation of the HTTP 1.1 client
    protocol.  It supports as few features as possible.

    @ivar _parser: After a request is issued, the L{HTTPClientParser} to
        which received data making up the response to that request is
        delivered.

    @ivar _finishedRequest: After a request is issued, the L{Deferred} which
        will fire when a L{Response} object corresponding to that request is
        available.  This allows L{HTTP11ClientProtocol} to fail the request
        if there is a connection or parsing problem.

    @ivar _currentRequest: After a request is issued, the L{Request}
        instance used to make that request.  This allows
        L{HTTP11ClientProtocol} to stop request generation if necessary (for
        example, if the connection is lost).

    @ivar _transportProxy: After a request is issued, the
        L{TransportProxyProducer} to which C{_parser} is connected.  This
        allows C{_parser} to pause and resume the transport in a way which
        L{HTTP11ClientProtocol} can exert some control over.

    @ivar _responseDeferred: After a request is issued, the L{Deferred} from
        C{_parser} which will fire with a L{Response} when one has been
        received.  This is eventually chained with C{_finishedRequest}, but
        only in certain cases to avoid double firing that Deferred.

    @ivar _state: Indicates what state this L{HTTP11ClientProtocol} instance
        is in with respect to transmission of a request and reception of a
        response.  This may be one of the following strings:

          - QUIESCENT: This is the state L{HTTP11ClientProtocol} instances
            start in.  Nothing is happening: no request is being sent and no
            response is being received or expected.

          - TRANSMITTING: When a request is made (via L{request}), the
            instance moves to this state.  L{Request.writeTo} has been used
            to start to send a request but it has not yet finished.

          - TRANSMITTING_AFTER_RECEIVING_RESPONSE: The server has returned a
            complete response but the request has not yet been fully sent
            yet.  The instance will remain in this state until the request
            is fully sent.

          - GENERATION_FAILED: There was an error while the request.  The
            request was not fully sent to the network.

          - WAITING: The request was fully sent to the network.  The
            instance is now waiting for the response to be fully received.

          - ABORTING: Application code has requested that the HTTP connection
            be aborted.

          - CONNECTION_LOST: The connection has been lost.

    """
    _state = 'QUIESCENT'
    _parser = None
    persistent = False

    @property
    def state(self):
        return self._state

    def request(self, request):
        """
        Issue C{request} over C{self.transport} and return a L{Deferred} which
        will fire with a L{Response} instance or an error.

        @param request: The object defining the parameters of the request to
           issue.
        @type request: L{Request}

        @rtype: L{Deferred}
        @return: The deferred may errback with L{RequestGenerationFailed} if
            the request was not fully written to the transport due to a local
            error.  It may errback with L{RequestTransmissionFailed} if it was
            not fully written to the transport due to a network error.  It may
            errback with L{ResponseFailed} if the request was sent (not
            necessarily received) but some or all of the response was lost.  It
            may errback with L{RequestNotSent} if it is not possible to send
            any more requests using this L{HTTP11ClientProtocol}.
        """
        self.persistent = request.persistent
        if self._state != 'QUIESCENT':
            return fail(RequestNotSent())

        self._state = 'TRANSMITTING'
        _requestDeferred = maybeDeferred(request.writeTo, self.transport)
        self._finishedRequest = Deferred()

        # Keep track of the Request object in case we need to call stopWriting
        # on it.
        self._currentRequest = request

        self._transportProxy = TransportProxyProducer(self.transport)
        self._parser = HTTPClientParser(request, self._finishResponse)
        self._parser.makeConnection(self._transportProxy)
        self._responseDeferred = self._parser._responseDeferred

        def cbRequestWrotten(ignored):
            if self._state == 'TRANSMITTING':
                self._state = 'WAITING'
                self._responseDeferred.chainDeferred(self._finishedRequest)

        def ebRequestWriting(err):
            if self._state == 'TRANSMITTING':
                self._state = 'GENERATION_FAILED'
                self.transport.loseConnection()
                self._finishedRequest.errback(
                    Failure(RequestGenerationFailed([err])))
            else:
                log.err(err, 'Error writing request, but not in valid state '
                             'to finalize request: %s' % self._state)

        _requestDeferred.addCallbacks(cbRequestWrotten, ebRequestWriting)

        return self._finishedRequest


    def _finishResponse(self, rest):
        """
        Called by an L{HTTPClientParser} to indicate that it has parsed a
        complete response.

        @param rest: A C{str} giving any trailing bytes which were given to
            the L{HTTPClientParser} which were not part of the response it
            was parsing.
        """
        assert self._state in ('WAITING', 'TRANSMITTING')

        if self._state == 'WAITING':
            self._state = 'QUIESCENT'
        else:
            # The server sent the entire response before we could send the
            # whole request.  That sucks.  Oh well.  Fire the request()
            # Deferred with the response.  But first, make sure that if the
            # request does ever finish being written that it won't try to fire
            # that Deferred.
            self._state = 'TRANSMITTING_AFTER_RECEIVING_RESPONSE'
            self._responseDeferred.chainDeferred(self._finishedRequest)
        
        reason = ConnectionDone("synthetic!")
        
        if self._parser is not None:
            connection = self._parser.connHeaders.getRawHeaders('Connection')
            keepalive = connection and connection[0].lower() == 'keep-alive'
        else:
            keepalive = False
            
        if self.persistent and keepalive:
            self._disconnectParser(Failure(reason))
        else:
            self._giveUp(Failure(reason))
            self.abort()


    def _disconnectParser(self, reason):
        """
        If there is still a parser, call its C{connectionLost} method with the
        given reason.  If there is not, do nothing.

        @type reason: L{Failure}
        """
        if self._parser is not None:
            parser = self._parser
            self._parser = None

            # The parser is no longer allowed to do anything to the real
            # transport.  Stop proxying from the parser's transport to the real
            # transport before telling the parser it's done so that it can't do
            # anything.
            self._transportProxy._stopProxying()

            parser.connectionLost(reason)


    def _giveUp(self, reason):
        """
        Lose the underlying connection and disconnect the parser with the given
        L{Failure}.

        Use this method instead of calling the transport's loseConnection
        method directly otherwise random things will break.
        """
        self.transport.loseConnection()
        self._disconnectParser(reason)


    def dataReceived(self, bytes):
        """
        Handle some stuff from some place.
        """
        try:
            self._parser.dataReceived(bytes)
        except:
            self._giveUp(Failure())


    def connectionLost(self, reason):
        """
        The underlying transport went away.  If appropriate, notify the parser
        object.
        """
    connectionLost = makeStatefulDispatcher('connectionLost', connectionLost)


    def _connectionLost_QUIESCENT(self, reason):
        """
        Nothing is currently happening.  Move to the C{'CONNECTION_LOST'}
        state but otherwise do nothing.
        """
        self._state = 'CONNECTION_LOST'


    def _connectionLost_GENERATION_FAILED(self, reason):
        """
        The connection was in an inconsistent state.  Move to the
        C{'CONNECTION_LOST'} state but otherwise do nothing.
        """
        self._state = 'CONNECTION_LOST'


    def _connectionLost_TRANSMITTING(self, reason):
        """
        Fail the L{Deferred} for the current request, notify the request
        object that it does not need to continue transmitting itself, and
        move to the C{'CONNECTION_LOST'} state.
        """
        self._state = 'CONNECTION_LOST'
        self._finishedRequest.errback(
            Failure(RequestTransmissionFailed([reason])))
        del self._finishedRequest

        # Tell the request that it should stop bothering now.
        self._currentRequest.stopWriting()


    def _connectionLost_WAITING(self, reason):
        """
        Disconnect the response parser so that it can propagate the event as
        necessary (for example, to call an application protocol's
        C{connectionLost} method, or to fail a request L{Deferred}) and move
        to the C{'CONNECTION_LOST'} state.
        """
        self._disconnectParser(reason)
        self._state = 'CONNECTION_LOST'


    def _connectionLost_ABORTING(self, reason):
        """
        Disconnect the response parser with a L{ConnectionAborted} failure, and
        move to the C{'CONNECTION_LOST'} state.
        """
        self._disconnectParser(Failure(ConnectionAborted()))
        self._state = 'CONNECTION_LOST'


    def abort(self):
        """
        Close the connection and cause all outstanding L{request} L{Deferred}s
        to fire with an error.
        """
        self.transport.loseConnection()
        self._state = 'ABORTING'
