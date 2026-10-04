ARG HERMES_BASE
FROM ${HERMES_BASE}

# ~/.hermes is owned by the host user; the base image remaps hermes to HERMES_UID at boot as
# root. Baking the remap in lets the container start as hermes (the base image's supported
# non-root start: uid == id -u hermes), so nothing runs as root (trivy DS-0002).
ARG HERMES_UID=501
ARG HERMES_GID=20

COPY pyproject.toml /tmp/ohmyboring/pyproject.toml
RUN cd /opt/hermes && uv pip install --python /opt/hermes/.venv/bin/python3 -r /tmp/ohmyboring/pyproject.toml
RUN groupmod -o -g "${HERMES_GID}" hermes && usermod -u "${HERMES_UID}" -g "${HERMES_GID}" hermes

USER hermes
