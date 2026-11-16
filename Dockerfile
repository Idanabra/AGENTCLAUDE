FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py index.html ./
# Saved SAP settings live here; mount a persistent volume on /app/data
RUN mkdir -p /app/data
VOLUME ["/app/data"]
ENV PORT=8000 CONFIG_PATH=/app/data/sap_config.json
EXPOSE 8000
CMD ["python", "server.py"]
