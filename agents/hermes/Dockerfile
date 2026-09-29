ARG HERMES_BASE
FROM ${HERMES_BASE}

COPY pyproject.toml /tmp/ohmyboring/pyproject.toml
RUN cd /opt/hermes && uv pip install --python /opt/hermes/.venv/bin/python3 -r /tmp/ohmyboring/pyproject.toml
