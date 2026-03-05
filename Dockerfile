FROM python:3.13-slim-bookworm

# Install Chromium + driver from Debian apt (matched versions, correct glibc)
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        ca-certificates \
        fonts-liberation \
        libnss3 \
        libxss1 \
    && rm -rf /var/lib/apt/lists/*

# Verify binaries exist and print versions
RUN chromium --version && chromedriver --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
ENV PORT=8080

CMD ["python", "server.py"]
