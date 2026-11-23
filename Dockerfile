FROM python:3.13-slim
WORKDIR /app
COPY requirements-runtime.lock .
RUN pip install --no-cache-dir -r requirements-runtime.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
COPY scripts ./scripts
RUN mkdir /data && chown 10001:10001 /data
ENV STOCKHOLD_DATABASE_URL=sqlite:////data/stockhold.db
USER 10001:10001
CMD ["python", "scripts/demo.py"]
