import logging
import os
import secrets
import sys
import threading
import time
from flask import Response, g, jsonify, request

log = logging.getLogger(__name__)


def _random_hex(n_bytes: int) -> str:
    return secrets.token_hex(n_bytes)


class OTelInstrumentor:
    """Instrumentação OpenTelemetry para o analytics-service (HTTP + Consumer SQS W3C TraceContext + Métricas Prometheus)."""

    def __init__(self, app, default_service_name: str):
        self.app = app
        self.service_name = os.getenv("OTEL_SERVICE_NAME", default_service_name)
        self.otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
        self.sdk_disabled = (
            os.getenv("OTEL_SDK_DISABLED", "false").lower() == "true"
            or "pytest" in sys.modules
        )
        self.chaos_enabled = False
        self._lock = threading.Lock()
        self._request_counts = {}
        self._duration_sums_ms = {}
        self._sqs_processed_total = 0
        self._sqs_errors_total = 0

        self._setup_otel_sdk(app)
        self._register_hooks_and_routes(app)

    def _setup_otel_sdk(self, app):
        if self.sdk_disabled or not self.otlp_endpoint:
            log.info(
                f"[OTel] Inicializado em modo leve/local para '{self.service_name}'"
            )
            return

        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
            from opentelemetry.instrumentation.flask import FlaskInstrumentor
            from opentelemetry.instrumentation.logging import LoggingInstrumentor
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            resource = Resource.create({"service.name": self.service_name})
            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter(endpoint=f"{self.otlp_endpoint}/v1/traces")
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)

            FlaskInstrumentor().instrument_app(app)
            BotocoreInstrumentor().instrument()
            LoggingInstrumentor().instrument(set_logging_format=True)
            log.info(
                f"[OTel] SDK OpenTelemetry Python ativo para '{self.service_name}' -> {self.otlp_endpoint}"
            )
        except Exception as exc:
            log.warning(
                f"[OTel] Usando exportador OTLP/HTTP nativo para '{self.service_name}': {exc}"
            )

    def _register_hooks_and_routes(self, app):
        @app.before_request
        def _before_request():
            g.otel_start_time = time.time()
            tp = request.headers.get("traceparent", "")
            parts = tp.split("-")
            if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
                g.trace_id = parts[1]
                g.parent_span_id = parts[2]
            else:
                g.trace_id = _random_hex(16)
                g.parent_span_id = ""
            g.span_id = _random_hex(8)
            g.traceparent = f"00-{g.trace_id}-{g.span_id}-01"

            if self.chaos_enabled and request.path not in (
                "/health",
                "/metrics",
                "/chaos/fail",
            ):
                return (
                    jsonify(
                        {
                            "error": "Falha simulada (Chaos Engineering / Self-Healing Test)",
                            "service": self.service_name,
                            "traceId": g.trace_id,
                        }
                    ),
                    500,
                )
            return None

        @app.after_request
        def _after_request(response):
            if hasattr(g, "traceparent"):
                response.headers["traceparent"] = g.traceparent

            if request.path not in ("/metrics", "/chaos/fail"):
                start = getattr(g, "otel_start_time", time.time())
                duration_ms = (time.time() - start) * 1000.0
                self.record_metric(
                    request.method, request.path, response.status_code, duration_ms
                )
            return response

        @app.route("/metrics", methods=["GET"])
        def _metrics_endpoint():
            lines = [
                "# HELP http_server_requests_total Total de requisicoes HTTP processadas",
                "# TYPE http_server_requests_total counter",
            ]
            with self._lock:
                for (method, route, status), count in self._request_counts.items():
                    lines.append(
                        f'http_server_requests_total{{service="{self.service_name}",method="{method}",route="{route}",status="{status}"}} {count}'
                    )
                lines.append(
                    "# HELP sqs_messages_processed_total Total de eventos SQS processados pelo analytics-service"
                )
                lines.append("# TYPE sqs_messages_processed_total counter")
                lines.append(
                    f'sqs_messages_processed_total{{service="{self.service_name}",status="ok"}} {self._sqs_processed_total}'
                )
                lines.append(
                    f'sqs_messages_processed_total{{service="{self.service_name}",status="error"}} {self._sqs_errors_total}'
                )
            return Response(
                "\n".join(lines) + "\n",
                mimetype="text/plain; version=0.0.4; charset=utf-8",
            )

        @app.route("/chaos/fail", methods=["GET", "POST"])
        def _chaos_endpoint():
            enable_arg = request.args.get("enable")
            if request.method == "POST" or enable_arg is not None:
                self.chaos_enabled = str(enable_arg).lower() in ("true", "1")
                log.warning(
                    f"[CHAOS] Modo de falha proposital em '{self.service_name}' alterado para: {self.chaos_enabled}"
                )
            return (
                jsonify(
                    {
                        "service": self.service_name,
                        "chaos_enabled": self.chaos_enabled,
                    }
                ),
                200,
            )

    def record_metric(self, method: str, route: str, status: int, duration_ms: float):
        key = (method, route, int(status))
        with self._lock:
            self._request_counts[key] = self._request_counts.get(key, 0) + 1
            self._duration_sums_ms[key] = (
                self._duration_sums_ms.get(key, 0.0) + duration_ms
            )

    def record_sqs_consumer_span(
        self,
        traceparent: str,
        flag_name: str,
        start_time: float,
        end_time: float,
        is_error: bool = False,
    ):
        """Extrai o traceparent dos MessageAttributes do SQS e exporta um Span CONSUMER conectado ao evaluation-service."""
        with self._lock:
            if is_error:
                self._sqs_errors_total += 1
            else:
                self._sqs_processed_total += 1

        parts = (traceparent or "").split("-")
        if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
            trace_id = parts[1]
            parent_span_id = parts[2]
        else:
            trace_id = _random_hex(16)
            parent_span_id = ""
        span_id = _random_hex(8)

        self.export_span_async(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name="sqs.process toogle-events",
            kind=5,  # SPAN_KIND_CONSUMER
            start_time=start_time,
            end_time=end_time,
            attributes={
                "messaging.system": "aws_sqs",
                "messaging.destination.name": "toogle-events",
                "messaging.operation": "process",
                "db.system": "dynamodb",
                "db.operation": "PutItem",
                "feature_flag.name": str(flag_name),
            },
            is_error=is_error,
        )

    def export_span_async(
        self,
        trace_id: str,
        span_id: str,
        parent_span_id: str,
        name: str,
        kind: int,
        start_time: float,
        end_time: float,
        attributes: dict,
        is_error: bool = False,
    ):
        if self.sdk_disabled or not self.otlp_endpoint:
            return

        def _send():
            import requests as req_lib

            otel_attrs = [
                {"key": k, "value": {"stringValue": str(v)}}
                for k, v in attributes.items()
            ]
            payload = {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"stringValue": self.service_name},
                                },
                                {
                                    "key": "telemetry.sdk.name",
                                    "value": {"stringValue": "opentelemetry"},
                                },
                                {
                                    "key": "telemetry.sdk.language",
                                    "value": {"stringValue": "python"},
                                },
                            ]
                        },
                        "scopeSpans": [
                            {
                                "scope": {
                                    "name": f"{self.service_name}-tracer",
                                    "version": "1.0.0",
                                },
                                "spans": [
                                    {
                                        "traceId": trace_id,
                                        "spanId": span_id,
                                        "parentSpanId": parent_span_id,
                                        "name": name,
                                        "kind": kind,
                                        "startTimeUnixNano": str(
                                            int(start_time * 1_000_000_000)
                                        ),
                                        "endTimeUnixNano": str(
                                            int(end_time * 1_000_000_000)
                                        ),
                                        "attributes": otel_attrs,
                                        "status": {"code": 2 if is_error else 1},
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
            try:
                req_lib.post(
                    f"{self.otlp_endpoint}/v1/traces",
                    json=payload,
                    timeout=2,
                )
            except Exception as exc:
                log.debug(f"[OTel] Falha ao exportar span OTLP: {exc}")

        threading.Thread(target=_send, daemon=True).start()


def init_telemetry(app, default_service_name: str) -> OTelInstrumentor:
    return OTelInstrumentor(app, default_service_name)
