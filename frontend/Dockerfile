# A2K frontend -- build context is this directory (frontend/), not the repo
# root: `docker build -t a2k-frontend:latest frontend/`. Santander's standard
# base image/pipenv pattern (produban python-313-ubi9), same one already
# proven for other services on this cluster -- do not swap in the root
# Dockerfile's python-314-ubi9/multi-stage-wheel pattern here, the two are
# deliberately different base images and this is the one this service must
# use.

FROM registry.global.ccc.srvb.bo.paas.cloudcenter.corp/produban/python-313-ubi9:1.8.13.RELEASE

COPY . .

USER root

RUN pip config --user set global.index https://nexus.alm.europe.cloudcenter.corp/repository/pypi-public/simple
RUN pip config --user set global.index-url https://nexus.alm.europe.cloudcenter.corp/repository/pypi-public/simple
RUN pip config --user set global.trusted-host nexus.alm.europe.cloudcenter.corp

RUN pip install pipenv==2023.12.1 --no-cache-dir
RUN pipenv requirements > requirements.txt
RUN pip install -r requirements.txt

RUN rm requirements.txt

ENV SSL_CERT_FILE /etc/pki/tls/certs/ca-bundle.crt
ENV REQUESTS_CA_BUNDLE /etc/pki/tls/certs/ca-bundle.crt

RUN chmod -R 775 /opt

ENV UID 29000
ENV APP_HOME /opt

RUN chmod +x entrypoint.sh
ENTRYPOINT ["/bin/bash", "entrypoint.sh"]

USER $UID
