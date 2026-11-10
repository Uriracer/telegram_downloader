# syntax=docker/dockerfile:1
# Telegram Downloader — multi-arch image (linux/amd64, linux/arm64).
# Build: docker build -t telegram-downloader .
ARG PYTHON_VERSION=3.13

# --------------------------------------------------------------------------- 7-Zip
# Debian's 7zip package (dfsg) cannot open RAR, and 7zip-rar / p7zip-rar do not
# exist in trixie: the official 7zz from 7-zip.org opens RAR5 and multipart RAR.
# The checksums were taken from the files served by www.7-zip.org on 2026-11-10.
FROM debian:trixie-slim AS sevenzip
ARG TARGETARCH
ARG SEVENZIP_VERSION=2501
ARG SEVENZIP_SHA256_AMD64=4ca3b7c6f2f67866b92622818b58233dc70367be2f36b498eb0bdeaaa44b53f4
ARG SEVENZIP_SHA256_ARM64=39c5140f02ce4436599303c59a149f654cb1bbc47cdc105a942120d747ae040d
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH:-amd64}" in \
         amd64) arch=x64;   sum="$SEVENZIP_SHA256_AMD64" ;; \
         arm64) arch=arm64; sum="$SEVENZIP_SHA256_ARM64" ;; \
         *) echo "Arquitectura no suportada: $TARGETARCH" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/7z.tar.xz "https://www.7-zip.org/a/7z${SEVENZIP_VERSION}-linux-${arch}.tar.xz" \
    && echo "$sum  /tmp/7z.tar.xz" | sha256sum -c - \
    && mkdir /tmp/7z && tar -xJf /tmp/7z.tar.xz -C /tmp/7z \
    && install -m 0755 /tmp/7z/7zzs /usr/local/bin/7zz \
    && install -m 0644 /tmp/7z/License.txt /usr/local/share/7zip-License.txt \
    && /usr/local/bin/7zz i > /dev/null

# --------------------------------------------------------------------------- build
FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY pyproject.toml README.md LICENSE ./
COPY tgdl ./tgdl
RUN pip install --no-deps . \
    && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} +

# --------------------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim
ARG VERSION=dev
ARG REVISION=unknown
ARG SOURCE=https://github.com/Uriracer/telegram_downloader
LABEL org.opencontainers.image.title="Telegram Downloader" \
      org.opencontainers.image.description="Recupera els fitxers dels teus xats de Telegram cap a Jellyfin, Sonarr i Radarr" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.licenses="MIT"

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=sevenzip /usr/local/bin/7zz /usr/local/bin/7zz
COPY --from=sevenzip /usr/local/share/7zip-License.txt /usr/local/share/doc/7zip/License.txt
COPY --from=build /opt/venv /opt/venv
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh && mkdir -p /config /downloads

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TGDL_DATA_DIR=/config \
    TGDL_DOWNLOAD_DIR=/downloads \
    TGDL_HOST=0.0.0.0 \
    TGDL_PORT=8766 \
    TGDL_7ZZ=/usr/local/bin/7zz \
    PUID=1000 \
    PGID=1000 \
    UMASK=022 \
    TZ=Europe/Madrid

EXPOSE 8766
VOLUME ["/config"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('TGDL_PORT', '8766'), timeout=4)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD []
