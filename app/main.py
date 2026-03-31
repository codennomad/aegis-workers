"""FastAPI application factory and lifecycle management."""
from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import secure

from app.config import get_settings
from app.db.session import dispose_engine
from app.logging_config import configure_logging
from app.routes.jobs import router as jobs_router, health_router
from app.metrics import get_metrics_response

logger = structlog.get_logger(__name__)


def _configure_otel(settings: object) -> None:
    """Initialize OpenTelemetry tracing pipeline."""
    from app.config import Settings
    assert isinstance(settings, Settings)

    resource = Resource(attributes={SERVICE_NAME: settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup and shutdown lifecycle."""
    settings = get_settings()
    configure_logging()

    logger.info(
        "application_starting",
        env=settings.app_env,
        service=settings.otel_service_name,
    )

    try:
        _configure_otel(settings)
    except Exception as exc:
        logger.warning("otel_init_failed", error=str(exc))

    yield

    logger.info("application_shutting_down")
    await dispose_engine()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Async Jobs System",
        description="Production-grade async job processing API",
        version="1.0.0",
        docs_url="/docs" if settings.app_debug else None,
        redoc_url="/redoc" if settings.app_debug else None,
        lifespan=lifespan,
    )

    # -------------------------------------------------------------------------
    # Rate limiting
    # -------------------------------------------------------------------------
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[f"{settings.rate_limit_per_minute}/minute"],
    )
    app.state.limiter = limiter
    app.add_exception_handler(
        RateLimitExceeded,
        _rate_limit_exceeded_handler,  # type: ignore[arg-type]
    )

    # -------------------------------------------------------------------------
    # Security headers middleware
    # -------------------------------------------------------------------------
    secure_headers = secure.Secure(
        server=secure.Server().set(""),  # Hide server header
        xfo=secure.XFrameOptions().deny(),
        xct=secure.XContentTypeOptions(),
        hsts=secure.StrictTransportSecurity().max_age(31536000),
    )

    @app.middleware("http")
    async def set_security_headers(request: Request, call_next: object) -> Response:
        from collections.abc import Callable
        assert callable(call_next)
        response: Response = await call_next(request)  # type: ignore[misc]
        secure_headers.framework.fastapi(response)  # type: ignore[attr-defined]
        response.headers.pop("x-powered-by", None)
        return response

    # -------------------------------------------------------------------------
    # CORS
    # -------------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_hosts,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Idempotency-Key"],
    )

    # -------------------------------------------------------------------------
    # Routes
    # -------------------------------------------------------------------------
    app.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
    app.include_router(health_router, tags=["health"])

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        """Prometheus metrics endpoint — no auth required for scraper."""
        content, content_type = get_metrics_response()
        return Response(content=content, media_type=content_type)

    # -------------------------------------------------------------------------
    # Global exception handler — never expose stack traces to clients
    # -------------------------------------------------------------------------
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    # -------------------------------------------------------------------------
    # OpenTelemetry instrumentation
    # -------------------------------------------------------------------------
    FastAPIInstrumentor.instrument_app(app)

    return app


app = create_app()
