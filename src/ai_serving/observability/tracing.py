"""OpenTelemetry spans with metadata only; never record exception messages."""

from contextlib import contextmanager
from functools import wraps
import json
import logging

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode


class MetadataExporter(SpanExporter):
    def export(self, spans):
        for item in spans:
            logging.getLogger("ai_serving.trace").info(json.dumps({
                "span": item.name, "trace_id": format(item.context.trace_id, "032x"),
                "span_id": format(item.context.span_id, "016x"),
                "parent_id": format(item.parent.span_id, "016x") if item.parent else None,
                "duration_ms": (item.end_time - item.start_time) / 1e6,
                "status": item.status.status_code.name, "attributes": dict(item.attributes),
            }))
        return SpanExportResult.SUCCESS


def configure_tracing(exporter=None):
    global _provider, _tracer
    _provider = TracerProvider()
    _provider.add_span_processor(SimpleSpanProcessor(exporter or MetadataExporter()))
    _tracer = _provider.get_tracer("ai_serving")
    return _provider


configure_tracing()


def start_span(name: str, **attributes):
    return _tracer.start_span(name, attributes=attributes)


@contextmanager
def span(name: str, **attributes):
    with _tracer.start_as_current_span(name, attributes=attributes, record_exception=False,
                                       set_status_on_exception=False) as current:
        try:
            yield current
        except BaseException:
            current.set_status(Status(StatusCode.ERROR))
            raise


def traced(name: str):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with span(name):
                return await function(*args, **kwargs)
        return wrapped
    return decorate
