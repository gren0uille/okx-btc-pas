FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .
COPY superset/bootstrap.py ./superset/bootstrap.py
CMD ["python", "-m", "okx_btc_pas.pipeline"]
