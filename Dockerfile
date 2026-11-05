# Inspect-only image for MCP directories (Glama): it lists OMX's tools and refuses every call.
# It holds no config, key or daemon, so it cannot trade. To trade, install OMX on your own machine (README).
FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir /app
CMD ["omx-hyperliquid", "--inspect"]
