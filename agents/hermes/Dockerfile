ARG HERMES_BASE
FROM ${HERMES_BASE}

# ~/.hermes is owned by the host user; the base image remaps hermes to HERMES_UID at boot as
# root. Baking the remap in lets the container start as hermes (the base image's supported
# non-root start: uid == id -u hermes), so nothing runs as root (trivy DS-0002).
ARG HERMES_UID=501
ARG HERMES_GID=20

# The v0.23 base no longer ships uv on PATH and its venv has no pip; bring the same uv the base
# pins (pm/lock.json "uv" 0.13.5), fixed by index digest.
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv
COPY pyproject.toml /tmp/ohmyboring/pyproject.toml
RUN cd /opt/hermes && uv pip install --python /opt/hermes/.venv/bin/python3 -r /tmp/ohmyboring/pyproject.toml
# The v0.23 launcher replaces PYTHONPATH with REPO:SITE (scripts/build/launchers.py:66-67), so the
# compose PYTHONPATH=/host/oh-my-boring/src never reaches gateway or its cron scripts (card_door.py
# looks for <scripts>/../../src, which is /src here). A .pth in the venv puts src on every venv python.
RUN /opt/hermes/.venv/bin/python3 -c "import pathlib, sysconfig; pathlib.Path(sysconfig.get_path('purelib'), 'ohmyboring-src.pth').write_text('/host/oh-my-boring/src\n')"
RUN groupmod -o -g "${HERMES_GID}" hermes && usermod -u "${HERMES_UID}" -g "${HERMES_GID}" hermes

USER hermes
