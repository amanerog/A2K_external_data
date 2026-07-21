# A2K Box -- REST transport (see README "Deploy to EKS").
# The MCP entrypoint (`python -m a2k_box.mcp_server`) is also present in this
# image and can be run instead by overriding CMD, but stdio-transport MCP is
# not meant to run as its own K8s workload -- see README for why.

FROM python:3.12-slim AS build

WORKDIR /build
COPY pyproject.toml ./
COPY a2k_box ./a2k_box

RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin a2kbox

WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Runtime-writable locations. In Kubernetes: the signing key is mounted
# read-only from a Secret (see deploy/deployment.yaml), and the local audit
# file lives on an emptyDir -- both are pod-local; the durable audit trail is
# the stdout copy (A2K_AUDIT_STDOUT=true, see gateway/audit.py) captured by
# cluster log aggregation.
RUN mkdir -p /app/keys /app/audit && chown -R a2kbox:a2kbox /app

ENV A2K_BOX_HOST=0.0.0.0 \
    A2K_BOX_PORT=8000 \
    A2K_SIGNING_KEY_PATH=/app/keys/gateway_ed25519.pem \
    A2K_AUDIT_LOG_PATH=/app/audit/audit.jsonl \
    A2K_AUDIT_STDOUT=true \
    A2K_BOX_MODE=mock

USER a2kbox
EXPOSE 8000

CMD ["uvicorn", "a2k_box.api.rest:app", "--host", "0.0.0.0", "--port", "8000"]
