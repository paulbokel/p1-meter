FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY p1collector ./p1collector
USER nobody
CMD ["python", "-m", "p1collector.main"]
