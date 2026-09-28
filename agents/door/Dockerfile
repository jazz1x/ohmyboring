FROM python:3.12-slim

RUN useradd --uid 10001 --create-home door

WORKDIR /app

# One source of versions: requirements.txt is the whole Python surface here — fastapi/uvicorn/
# psycopg for the door itself, langgraph/langchain/slack-sdk/pydantic for the card programs
# the POST /run/* routes execute as subprocesses (installed into the image, not mounted).
COPY requirements.txt requirements.txt
# requirements.txt names bare psycopg; the slim image has no libpq, so the door needs the binary wheel.
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]>=3.2"

COPY agents/door/ agents/door/
COPY agents/slack/ agents/slack/
COPY agents/shared/ agents/shared/
COPY agents/memory/ agents/memory/
COPY agents/hermes/ agents/hermes/
COPY data/contract/engine-contract.json data/contract/engine-contract.json

USER 10001

CMD ["python3", "-m", "uvicorn", "agents.door.door:app", "--host", "0.0.0.0", "--port", "7710"]
