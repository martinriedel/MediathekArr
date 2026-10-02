FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RULESETS_DB_PATH=/data/rulesets.sqlite

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rulesets_service ./rulesets_service

VOLUME /data
EXPOSE 5008
ENTRYPOINT ["python", "-m", "rulesets_service"]
CMD ["serve"]
