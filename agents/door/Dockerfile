FROM python:3.12-slim

RUN useradd --uid 10001 --create-home door

WORKDIR /app

RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29"

COPY agents/door/ agents/door/
COPY data/contract/engine-contract.json data/contract/engine-contract.json

USER 10001

CMD ["python3", "-m", "uvicorn", "agents.door.door:app", "--host", "0.0.0.0", "--port", "7710"]
