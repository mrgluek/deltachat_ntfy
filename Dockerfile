FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bin/deltachat-rpc-server /usr/local/bin/deltachat-rpc-server
COPY . .
ENV DC_DB_DIR=/app/data
ENV DB_PATH=/app/data/ntfy.db
VOLUME /app/data
EXPOSE 8080
CMD ["python", "-u", "bot.py", "serve"]
