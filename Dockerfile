# Build the local browser bundle once. Vite is not a runtime service.
FROM node:24.15.0-bookworm-slim@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d AS web-build

WORKDIR /build/web

COPY translations.py /build/translations.py
COPY assets/brand/ /build/assets/brand/
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build

# Shared, non-root Python runtime. The parser branches before migrations and
# browser assets are copied, so its image contains no application data layer.
FROM python:3.11-slim-bookworm@sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91 AS python-runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libglib2.0-0 libgl1 libmagic1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system clerksan \
    && useradd --system --gid clerksan --create-home --home-dir /var/lib/clerksan clerksan \
    && mkdir --parents /data/doc_store /run/clerksan-parser \
    && chown --recursive clerksan:clerksan /app /data /run/clerksan-parser

COPY requirements.lock ./
RUN python -m pip install --require-hashes --requirement requirements.lock --no-deps

COPY --chown=clerksan:clerksan clerksan/ ./clerksan/

# Dedicated parser target: executable only through its Unix socket protocol.
# Compose supplies the hard network/root/capability/cgroup boundary.
FROM python-runtime AS parser-runtime

ENV CLERKSAN_PARSER_SOCKET_PATH=/run/clerksan-parser/parser.sock

USER clerksan

ENTRYPOINT ["python", "-m", "clerksan.ingest.parser_service"]
CMD ["serve"]

# API/worker image. Compose overrides the command for the worker; the default
# command is the API. Local configuration, Git history, and receipt data never
# enter this image through the build context.
FROM python-runtime AS app-runtime

ENV CLERKSAN_UI_STATIC_DIR=/app/web-dist

COPY --chown=clerksan:clerksan migrations/ ./migrations/
COPY --from=web-build --chown=clerksan:clerksan /build/web/dist /app/web-dist

USER clerksan

EXPOSE 8000

CMD ["uvicorn", "clerksan.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
