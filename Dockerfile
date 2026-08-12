# A2K Box -- REST transport (see README "Deploy to EKS").
# The MCP entrypoint (`python -m a2k.mcp_server`, streamable-http on
# 0.0.0.0:8000/mcp) is also present in this image and can be run instead by
# overriding the entrypoint command, but AgentCore Runtime deploys don't use
# this Dockerfile -- see README "Deploy to AgentCore".

FROM registry.global.ccc.srvb.bo.paas.cloudcenter.corp/produban/python-314-ubi9:1.1.20.RELEASE AS build

USER root

RUN pip config --user set global.index https://nexus.alm.europe.cloudcenter.corp/repository/pypi-public/simple && \
    pip config --user set global.index-url https://nexus.alm.europe.cloudcenter.corp/repository/pypi-public/simple && \
    pip config --user set global.trusted-host nexus.alm.europe.cloudcenter.corp

WORKDIR /build
COPY Pipfile Pipfile.lock ./
COPY pyproject.toml ./
COPY a2k ./a2k

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir pipenv==2023.12.1 && \
    pipenv requirements > requirements.txt && \
    pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt && \
    pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM registry.global.ccc.srvb.bo.paas.cloudcenter.corp/produban/python-314-ubi9:1.1.20.RELEASE

USER root

ENV APP_HOME=/opt
WORKDIR $APP_HOME

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

COPY entrypoint.sh ./

# Runtime-writable locations. The local audit file lives on an emptyDir in
# Kubernetes -- pod-local; the durable audit trail is the stdout copy
# (A2K_AUDIT_STDOUT=true, see gateway/audit.py) captured by cluster log
# aggregation.
RUN mkdir -p audit && \
    chmod +x entrypoint.sh && \
    chmod -R 775 $APP_HOME

ENV SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    A2K_BOX_HOST=0.0.0.0 \
    A2K_BOX_PORT=8080 \
    A2K_AUDIT_LOG_PATH=$APP_HOME/audit/audit.jsonl \
    A2K_AUDIT_STDOUT=true \
    A2K_BOX_MODE=mock

ENV UID=29000
EXPOSE 8080

ENTRYPOINT ["/bin/bash", "entrypoint.sh"]

USER $UID
