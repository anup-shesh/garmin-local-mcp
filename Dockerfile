# Runs the MCP server over stdio. The data directory is a volume so the
# warehouse survives container restarts:
#   docker build -t garmin-local-mcp .
#   docker run -i -v garmin-data:/data garmin-local-mcp
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV GARMIN_MCP_DATA_DIR=/data
VOLUME ["/data"]

ENTRYPOINT ["garmin-local-mcp"]
CMD ["serve"]
