# Image for the in-cluster control pod.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir aiohttp==3.10.10

COPY server.py /app/server.py

EXPOSE 8443 8444
CMD ["python3", "-u", "/app/server.py"]
