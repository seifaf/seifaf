FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py sal_persona.txt ./
ENV SAL_LIVE_PORT=8765
EXPOSE 8765
CMD ["python", "server.py"]
