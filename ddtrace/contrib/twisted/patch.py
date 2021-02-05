import functools
import sys

from ddtrace.vendor import six

from ddtrace import Pin, config
from ddtrace.compat import contextvars
from ddtrace.contrib import func_name
from ddtrace.contrib.dbapi import TracedConnection
from ddtrace.ext import errors
from ddtrace.propagation import http as http_prop
from ddtrace.utils.formats import asbool, get_env

from .. import trace_utils


config._add(
    "twisted",
    dict(
        _default_service="twisted",
        distributed_tracing=asbool(get_env("twisted", "distributed_tracing", default=True)),
        split_by_domain=asbool(get_env("twisted", "split_by_domain", default=False)),
    ),
)


@trace_utils.with_traced_module
def deferred_init(twisted, pin, func, instance, args, kwargs):
    # Create a new context for this Deferred.
    ctx = contextvars.copy_context()
    instance.__ctx = ctx
    return func(*args, **kwargs)


@trace_utils.with_traced_module
def deferred_callback(twisted, pin, func, instance, args, kwargs):
    span = getattr(instance, "__ddspan", None)

    if span and not span.finished:
        for n, cb in enumerate(instance.callbacks):
            # cb is a tuple of
            # (
            #   (callback, callbackArgs, callbackKWArgs),
            #   (errback, errbackArgs, errbackKWArgs)
            # )
            span.set_tag("callback.%d" % n, func_name(cb[0][0]))
            span.set_tag("errback.%d" % n, func_name(cb[1][0]))

    return func(*args, **kwargs)


@trace_utils.with_traced_module
def deferred_errback(twisted, pin, func, instance, args, kwargs):
    span = getattr(instance, "__ddspan", None)
    if span and not span.finished:
        if len(args) > 0:
            f = args[0]
            if hasattr(f, "getTraceback"):
                tb = f.getTraceback()
                span.set_tag(errors.ERROR_STACK, tb)
            if hasattr(f, "getErrorMessage"):
                span.set_tag(errors.ERROR_MSG, f.getErrorMessage())
            if hasattr(f, "type"):
                span.set_tag(errors.ERROR_TYPE, f.type)
        span.error = 1

    return func(*args, **kwargs)


@trace_utils.with_traced_module
def deferred_addCallbacks(twisted, pin, func, instance, args, kwargs):
    callback = args[0] or kwargs.pop("callback")

    @functools.wraps(callback)
    def _callback(*args, **kwargs):
        ctx = instance.__ctx

        # ctx.run could raise a RuntimeError if the context is already
        # activated. This should not happen in practice even if there
        # is a recursive callback since the wrapper will not be called
        # with the recursion call.
        # eg.
        # Consider the callback function
        # def callback(n):
        #     return callback(n-1) if n > 1 else 0
        #
        # this function will be intercepted and replaced with a wrapped
        # version when addCallbacks is called.
        # When the function is invoked the recursive callback(n-1) call
        # will not call the wrapping code again.
        # Regardless, let's safe-guard it as to not interfere with the
        # original code.
        try:
            return ctx.run(callback, *args, **kwargs)
        except RuntimeError as e:
            if "cannot enter context" in str(e):
                return callback(*args, **kwargs)
            exc_type, exc_val, exc_tb = sys.exc_info()
            six.reraise(exc_type, exc_val, exc_tb)

    newargs = list(args)
    newargs[0] = _callback
    return func(*tuple(newargs), **kwargs)


@trace_utils.with_traced_module
def threadpool_callInThreadWithCallback(twisted, pin, func, instance, args, kwargs):
    f = args[1]
    ctx = contextvars.copy_context()

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return ctx.run(f, *args, **kwargs)

    newargs = list(args)
    newargs[1] = wrapper
    return func(*newargs, **kwargs)


@trace_utils.with_traced_module
def connectionpool___init__(twisted, pin, func, instance, args, kwargs):
    try:
        return func(*args, **kwargs)
    finally:
        f = instance.connectionFactory

        def factory(*args, **kwargs):
            return TracedConnection(f(*args, **kwargs), pin)

        instance.connectionFactory = factory


@trace_utils.with_traced_module
def connectionpool_runinteraction(twisted, pin, func, instance, args, kwargs):
    with pin.tracer.trace("twisted.db"):
        return func(*args, **kwargs)


@trace_utils.with_traced_module
def httpclientfactory___init__(twisted, pin, func, instance, args, kwargs):
    func(*args, **kwargs)

    if hasattr(instance, "deferred"):
        span = pin.tracer.start_span("twisted.http", child_of=pin.tracer.active(), activate=False)
        instance.__ddspan = span

        if config.twisted.distributed_tracing:
            propagator = http_prop.HTTPPropagator()
            propagator.inject(span.context, instance.headers)

        def fin(_):
            span.finish()

        def err(_):
            span.error = 1
            span.finish()

        instance.deferred.addCallbacks(fin, err)


@trace_utils.with_traced_module
def agent_request(twisted, pin, func, instance, args, kwargs):
    span = pin.tracer.trace("twisted.agent.request")
    ctx = pin.tracer.get_call_context()
    if len(args) > 2:
        headers = args[2]
        if headers is None:
            headers = twisted.web.http_headers.Headers()
            newargs = list(args)
            newargs[2] = headers
            args = tuple(newargs)
    elif "headers" in kwargs:
        headers = kwargs.get("headers")
        if headers is None:
            headers = twisted.web.http_headers.Headers()
            kwargs["headers"] = headers
    else:
        headers = twisted.web.http_headers.Headers()
        kwargs["headers"] = headers

    headers.addRawHeader(http_prop.HTTP_HEADER_TRACE_ID, str(ctx.trace_id))
    headers.addRawHeader(http_prop.HTTP_HEADER_PARENT_ID, str(ctx.span_id))

    if ctx.sampling_priority:
        headers.addRawHeader(http_prop.HTTP_HEADER_SAMPLING_PRIORITY, str(ctx.sampling_priority))
    if ctx.dd_origin:
        headers.addRawHeader(http_prop.HTTP_HEADER_ORIGIN, str(ctx.dd_origin))

    d = func(*args, **kwargs)

    def finish_span(_):
        span.finish()
        return _

    d.addCallback(finish_span)
    return d


def patch():
    import twisted

    if getattr(twisted, "__datadog_patch", False):
        return

    Pin().onto(twisted)

    trace_utils.wrap("twisted.internet.defer", "Deferred.__init__", deferred_init(twisted))
    trace_utils.wrap("twisted.internet.defer", "Deferred.callback", deferred_callback(twisted))
    trace_utils.wrap("twisted.internet.defer", "Deferred.errback", deferred_errback(twisted))
    trace_utils.wrap("twisted.internet.defer", "Deferred.addCallbacks", deferred_addCallbacks(twisted))
    trace_utils.wrap(
        "twisted.python.threadpool", "ThreadPool.callInThreadWithCallback", threadpool_callInThreadWithCallback(twisted)
    )
    trace_utils.wrap("twisted.enterprise.adbapi", "ConnectionPool.__init__", connectionpool___init__(twisted))
    trace_utils.wrap(
        "twisted.enterprise.adbapi", "ConnectionPool._runInteraction", connectionpool_runinteraction(twisted)
    )
    trace_utils.wrap("twisted.web.client", "HTTPClientFactory.__init__", httpclientfactory___init__(twisted))
    trace_utils.wrap("twisted.web.client", "Agent.request", agent_request(twisted))

    setattr(twisted, "__datadog_patch", True)


def unpatch():
    import twisted

    if not getattr(twisted, "__datadog_patch", False):
        return

    trace_utils.unwrap(twisted.internet.defer.Deferred, "__init__")
    trace_utils.unwrap(twisted.internet.defer.Deferred, "callback")
    trace_utils.unwrap(twisted.internet.defer.Deferred, "errback")
    trace_utils.unwrap(twisted.internet.defer.Deferred, "addCallbacks")
    trace_utils.unwrap(twisted.python.threadpool.ThreadPool, "callInThreadWithCallback")
    trace_utils.unwrap(twisted.enterprise.adbapi.ConnectionPool, "__init__")
    trace_utils.unwrap(twisted.web.client.HTTPClientFactory, "__init__")
    trace_utils.unwrap(twisted.web.client.Agent, "request")

    setattr(twisted, "__datadog_patch", False)