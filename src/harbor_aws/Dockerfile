# harbor-control image — runs the in-cluster control gateway.
#
# The control gateway is a tiny aiohttp HTTP server (for the Mac side) plus
# a TCP listener for inbound connections from runner pods. Pure Python with
# one dep: aiohttp. The Dockerfile is intentionally minimal — no build steps,
# no apt-get layer, fast rebuilds when server.py changes.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir aiohttp==3.10.10

COPY server.py /app/server.py

EXPOSE 8443 8444
CMD ["python3", "-u", "/app/server.py"]
