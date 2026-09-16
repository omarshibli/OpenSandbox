# Copyright 2025 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
FastAPI application entry point for OpenSandbox Lifecycle API.

This module initializes the FastAPI application with middleware, routes,
and configuration for the sandbox lifecycle management service.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp

from opensandbox_server.config import load_config
from opensandbox_server.integrations.renew_intent import start_renew_intent_consumer
from opensandbox_server.logging_config import configure_logging
from opensandbox_server.startup_guard import api_key_confirm
from opensandbox_server.tenants import (
    validate_tenant_config,
    validate_tenant_namespaces_on_startup,
    TenantProvider,
)

# The deployed package version, resolved at runtime from installed metadata.
# Exposed via GET /version (not /openapi.json). Mirrors
# cli/src/opensandbox_cli/__init__.py; falls back when the package metadata is
# unavailable (e.g. running from a source checkout without install).
try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("opensandbox-server")
except Exception:
    __version__ = "0.0.0-dev"

# info.version in /openapi.json and /docs is the API *contract* version, not the
# package/release version. Keep it in sync with specs/sandbox-lifecycle.yml
# (info.version); specs/* are the public contract source of truth (AGENTS.md).
API_CONTRACT_VERSION = "0.1.0"

# Load configuration before initializing routers/middleware
app_config = load_config()
_log_config = configure_logging(app_config.log)
validate_tenant_config(app_config)


def _build_tenant_provider(config) -> TenantProvider | None:
    if config.tenants is None:
        return None
    if config.tenants.provider == "http":
        from opensandbox_server.tenants.http_provider import (
            HTTPTenantProvider,
            HTTPTenantProviderConfig,
        )

        http_cfg = HTTPTenantProviderConfig(
            endpoint=config.tenants.endpoint,
            max_stale_seconds=config.tenants.max_stale_seconds,
            timeout_seconds=config.tenants.timeout_seconds,
            auth_header=config.tenants.auth_header,
            auth_token=config.tenants.auth_token,
        )
        return HTTPTenantProvider(http_cfg)
    from opensandbox_server.tenants.file_provider import FileTenantProvider

    return FileTenantProvider()


tenant_provider: TenantProvider | None = _build_tenant_provider(app_config)

from opensandbox_server.api.devops import router as devops_router  # noqa: E402
from opensandbox_server.api.metrics import router as metrics_router  # noqa: E402
from opensandbox_server.api.pool import router as pool_router  # noqa: E402
from opensandbox_server.api.lifecycle import router, sandbox_service, snapshot_service  # noqa: E402
from opensandbox_server.api.proxy import router as proxy_router  # noqa: E402
from opensandbox_server.api.network_policy import router as policy_router  # noqa: E402
from opensandbox_server.api.templates import router as templates_router  # noqa: E402
from opensandbox_server.integrations.otel import setup_otel_metrics, shutdown_otel_metrics  # noqa: E402
from opensandbox_server.integrations.renew_intent.proxy_renew import ProxyRenewCoordinator  # noqa: E402
from opensandbox_server.middleware.auth import AuthMiddleware  # noqa: E402
from opensandbox_server.middleware.date_header import DateHeaderMiddleware  # noqa: E402
from opensandbox_server.middleware.http_metrics import HttpMetricsMiddleware  # noqa: E402
from opensandbox_server.middleware.request_id import RequestIdMiddleware  # noqa: E402
from opensandbox_server.repositories.snapshots.factory import close_snapshot_repository  # noqa: E402
from opensandbox_server.services.extension_service import require_extension_service  # noqa: E402
from opensandbox_server.services.runtime_resolver import (  # noqa: E402
    validate_secure_runtime_on_startup,
)

logger = logging.getLogger(__name__)


async def _sweep_s3_orphan_pvs_periodically(provisioner: Any, interval_seconds: int) -> None:
    """
    Re-run the s3 orphan PV sweep forever. Controller-driven TTL expiry never
    calls ``delete_sandbox``, so the PVC goes with ownerReference GC and the
    Released PV would otherwise sit there until the next restart.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            deleted = await asyncio.to_thread(provisioner.sweep_orphans)
            if deleted:
                logger.info("Periodic sweep removed %d orphaned s3 PersistentVolumes", deleted)
        except Exception as sweep_exc:
            logger.warning("Periodic sweep of s3 PersistentVolumes failed: %s", sweep_exc)


class _DateHeaderFastAPI(FastAPI):
    """Keep Date handling outside Starlette's server error middleware."""

    def build_middleware_stack(self) -> ASGIApp:
        return DateHeaderMiddleware(super().build_middleware_stack())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if tenant_provider is None:
        try:
            api_key_confirm(configured_api_key=app_config.server.api_key)
        except Exception as exc:
            logger.error(f"API key startup confirmation failed: {exc}")
            os._exit(1)

    if tenant_provider is not None:
        tenant_provider.start()
        sandbox_service.set_tenant_provider(tenant_provider)

        if app_config.runtime.type == "kubernetes":
            # OSEP-0014: the Kubernetes backend validates every enumerable
            # tenant namespace before serving traffic. Fsb CR readers are
            # lazy and must not block a legacy-only deployment at startup.
            try:
                from opensandbox_server.services.k8s.client import K8sClient

                core_v1_api = K8sClient(app_config.kubernetes).get_core_v1_api()
                validate_tenant_namespaces_on_startup(tenant_provider, core_v1_api)
            except Exception as exc:
                logger.error(f"Tenant namespace validation failed: {exc}")
                os._exit(1)
        else:
            logger.warning(
                "Skipping direct tenant namespace startup validation for the fsb runtime; "
                "Cluster credentials and CR permissions are checked on first read."
            )

    from anyio.to_thread import current_default_thread_limiter

    current_default_thread_limiter().total_tokens = app_config.server.thread_pool_size

    app.state.http_client = httpx.AsyncClient(timeout=180.0)

    try:
        docker_client = None
        k8s_client = None
        runtime_type = app_config.runtime.type

        if runtime_type == "docker":
            import docker

            docker_client = docker.from_env()
            logger.info("Validating secure runtime for Docker backend")
        elif runtime_type == "kubernetes":
            from opensandbox_server.services.k8s.client import K8sClient

            k8s_client = K8sClient(app_config.kubernetes)
            logger.info("Validating secure runtime for Kubernetes backend")

        await validate_secure_runtime_on_startup(
            app_config,
            docker_client=docker_client,
            k8s_client=k8s_client,
        )

        if k8s_client is not None:
            from opensandbox_server.services.k8s.s3_volume import S3VolumeProvisioner

            s3_provisioner = S3VolumeProvisioner(k8s_client, app_config.storage)
            try:
                deleted = await asyncio.to_thread(s3_provisioner.sweep_orphans)
                if deleted:
                    logger.info("Startup sweep removed %d orphaned s3 PersistentVolumes", deleted)
            except Exception as sweep_exc:
                logger.warning("Startup sweep of s3 PersistentVolumes failed: %s", sweep_exc)

            sweep_interval = app_config.storage.s3_orphan_sweep_interval_seconds
            if sweep_interval > 0:
                app.state.s3_sweep_task = asyncio.create_task(
                    _sweep_s3_orphan_pvs_periodically(s3_provisioner, sweep_interval)
                )

    except Exception as exc:
        logger.error(f"Secure runtime validation failed: {exc}")
        raise

    ext = require_extension_service(sandbox_service)
    app.state.renew_intent_consumer = await start_renew_intent_consumer(
        app_config,
        sandbox_service,
        ext,
    )
    app.state.renew_intent_runner = app.state.renew_intent_consumer

    app.state.proxy_renew_coordinator = ProxyRenewCoordinator(
        app_config,
        app.state.renew_intent_consumer,
    )

    setup_otel_metrics(app_config.otel)

    yield

    sweep_task = getattr(app.state, "s3_sweep_task", None)
    if sweep_task is not None:
        sweep_task.cancel()
        with suppress(asyncio.CancelledError):
            await sweep_task

    consumer = getattr(app.state, "renew_intent_consumer", None)
    if consumer is not None:
        await consumer.stop()
    shutdown_otel_metrics()
    sandbox_service.close()
    snapshot_service.close()
    close_snapshot_repository()
    from opensandbox_server.api.templates import close_template_service  # noqa: E402

    close_template_service()
    if tenant_provider is not None:
        tenant_provider.close()
    await app.state.http_client.aclose()


app = _DateHeaderFastAPI(
    title="OpenSandbox Lifecycle API",
    version=API_CONTRACT_VERSION,
    description="The Sandbox Lifecycle API coordinates how untrusted workloads are created, "
                "executed, paused, resumed, and finally disposed.",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.state.config = app_config
app.state.tenant_provider = tenant_provider

# User middleware run in reverse order of addition: last added = first to run.
# DateHeaderMiddleware wraps the complete stack, including ServerErrorMiddleware.
# Add auth and CORS first so they run after RequestIdMiddleware.
app.add_middleware(AuthMiddleware, config=app_config, tenant_provider=tenant_provider)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# RequestIdMiddleware wraps auth and CORS so every response (including 401 from
# AuthMiddleware) gets X-Request-ID and logs have request_id in context.
app.add_middleware(RequestIdMiddleware)
# HttpMetricsMiddleware is the outermost user middleware so auth failures and
# other early responses are included. Unmatched routes use the bounded "unknown" label.
app.add_middleware(HttpMetricsMiddleware)

# Include API routes at root and versioned prefix.
# IMPORTANT: non-proxy routers MUST be registered before proxy_router
# because proxy_router contains catch-all routes that would swallow diagnostics paths.
app.include_router(router)
app.include_router(devops_router)
app.include_router(pool_router)
app.include_router(templates_router)
app.include_router(proxy_router)
app.include_router(policy_router)
app.include_router(router, prefix="/v1")
app.include_router(devops_router, prefix="/v1")
app.include_router(pool_router, prefix="/v1")
app.include_router(templates_router, prefix="/v1")
app.include_router(metrics_router, prefix="/v1")
app.include_router(proxy_router, prefix="/v1")
app.include_router(policy_router, prefix="/v1")

DEFAULT_ERROR_CODE = "GENERAL::UNKNOWN_ERROR"
DEFAULT_ERROR_MESSAGE = "An unexpected error occurred."


def _normalize_error_detail(detail: Any) -> dict[str, str]:
    """
    Ensure HTTP errors always conform to {"code": "...", "message": "..."}.
    """
    if isinstance(detail, dict):
        code = detail.get("code") or DEFAULT_ERROR_CODE
        message = detail.get("message") or DEFAULT_ERROR_MESSAGE
        return {"code": code, "message": message}
    message = str(detail) if detail else DEFAULT_ERROR_MESSAGE
    return {"code": DEFAULT_ERROR_CODE, "message": message}


@app.exception_handler(HTTPException)
async def sandbox_http_exception_handler(request: Request, exc: HTTPException):
    """
    Flatten FastAPI HTTPException payload to the standard error schema.
    """
    content = _normalize_error_detail(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=exc.headers,
    )


@app.get("/health")
async def health_check():
    """
    Health check endpoint.

    Returns:
        dict: Health status
    """
    return {"status": "healthy"}


@app.get("/version")
async def version_info():
    """
    Return the deployed server package version (resolved from installed metadata).

    Returns:
        dict: Package version
    """
    return {"version": __version__}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "opensandbox_server.main:app",
        host=app_config.server.host,
        port=app_config.server.port,
        reload=True,
        log_config=_log_config,
        timeout_keep_alive=app_config.server.timeout_keep_alive,
        loop=app_config.server.loop,
        http=app_config.server.http,
        date_header=False,
        timeout_graceful_shutdown=app_config.server.timeout_graceful_shutdown,
    )
