# Build stage: fetch the official QuickJS-NG executable for EJS challenges.
# Only the qjs binary is copied to runtime; no toolchain ships in the image.
FROM python:3.14-slim AS qjs-fetch
ARG TARGETARCH
RUN python3 - <<'EOF'
import hashlib
import os
import urllib.request

arch = os.environ.get('TARGETARCH', '')
mapping = {'amd64': ('x86_64', '30fbd0b6434b03d2955cfacf7715a15793ae5fb39a115090e042285f5b07a421'),
           'arm64': ('aarch64', 'e3dbfa1ca39fd72b51e26b1ce2f72078ee683366bcda1bd6e881ae70c085034d')}
if arch not in mapping:
    raise SystemExit(f'Unsupported TARGETARCH for qjs: {arch!r}')
suffix, digest = mapping[arch]
url = f'https://github.com/quickjs-ng/quickjs/releases/download/v0.12.0/qjs-linux-{suffix}'
with urllib.request.urlopen(url, timeout=120) as response, open('/tmp/qjs', 'wb') as sink:
    sink.write(response.read())
actual = hashlib.sha256(open('/tmp/qjs', 'rb').read()).hexdigest()
if actual != digest:
    raise SystemExit(f'qjs checksum mismatch: {actual}')
os.chmod('/tmp/qjs', 0o755)
EOF
RUN /tmp/qjs -e 'console.log(1+1)'

FROM python:3.14-slim
LABEL org.opencontainers.image.source="https://github.com/ridhoarmand/backend-nonton-yuks" \
      org.opencontainers.image.description="Metadata-only video extraction API for Nonton Yuks"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=qjs-fetch /tmp/qjs /usr/local/bin/qjs
COPY api.py .
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-c", "import http.client; c=http.client.HTTPConnection('127.0.0.1',8080,timeout=3); c.request('GET','/healthz'); r=c.getresponse(); c.close(); raise SystemExit(0 if r.status == 200 else 1)"]
CMD ["python", "api.py"]
