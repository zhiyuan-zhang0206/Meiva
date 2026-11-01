# syntax=docker/dockerfile:1.7
# Pre-baked Linux (ubuntu:24.04, linux/amd64) CI runner image for Meiva.
#
# Eliminates per-job apt-get and binary bootstrap overhead for heavy backend
# data-plane and Playwright E2E shards by pre-installing:
#   - PostgreSQL 17 host/client tools (PGDG) + pinned PgBouncer + Redis 8.2
#   - Vendored Zonky PostgreSQL + pgvector runtime (CI_VENDORED_RUNTIME_ROOT)
#   - Pinned uv + CPython (from .python-version) in /opt/uv-python
#   - Node.js 22 LTS + npm + Corepack + pnpm (COREPACK_HOME=/opt/corepack)
#   - Playwright Chromium Ubuntu 24.04 system shared libraries and fonts
#   - Non-root `runner` user (UID 1001 / GID 1001) matching GitHub-hosted
#     ubuntu-24.04 runners so mounted workspaces need no chown and PostgreSQL
#     `initdb` / `postgres` never run as root (UID 0).

ARG UV_VERSION="0.13.1"

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv_bin

FROM ubuntu:24.04

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG UV_VERSION
ARG PGBOUNCER_APT_VERSION="1.26.0-1.pgdg24.04+1"
ARG REDIS_APT_VERSION="6:8.2.*"

LABEL org.opencontainers.image.source="https://github.com/zhiyuan-zhang0206/Meiva" \
      org.opencontainers.image.description="Pre-baked Ubuntu 24.04 CI runner image for Meiva data-plane and Playwright E2E jobs"

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/postgresql/17/bin" \
    UV_PYTHON_INSTALL_DIR="/opt/uv-python" \
    CI_VENDORED_RUNTIME_ROOT="/opt/meiva-vendored-pg/runtime" \
    COREPACK_ENABLE_DOWNLOAD_PROMPT="0" \
    COREPACK_HOME="/opt/corepack"

# Layer 1: Base CLI, build essentials, locales, and Playwright Chromium Ubuntu
# 24.04 (noble) OS shared libraries + fonts (exact closure from playwright's
# ubuntu24.04-x64 `tools` + `chromium` native dependency manifest).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        bzip2 \
        ca-certificates \
        curl \
        file \
        fonts-freefont-ttf \
        fonts-ipafont-gothic \
        fonts-liberation \
        fonts-noto-color-emoji \
        fonts-tlwg-loma-otf \
        fonts-unifont \
        fonts-wqy-zenhei \
        git \
        gnupg \
        iproute2 \
        jq \
        libasound2t64 \
        libatk-bridge2.0-0t64 \
        libatk1.0-0t64 \
        libatspi2.0-0t64 \
        libcairo2 \
        libcups2t64 \
        libdbus-1-3 \
        libdrm2 \
        libfontconfig1 \
        libfreetype6 \
        libgbm1 \
        libglib2.0-0t64 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpq-dev \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        locales \
        lsb-release \
        lsof \
        openssh-client \
        pkg-config \
        procps \
        psmisc \
        python3 \
        python3-dev \
        python3-venv \
        ripgrep \
        sudo \
        tzdata \
        unzip \
        xfonts-cyrillic \
        xfonts-scalable \
        xvfb \
        xz-utils \
        zstd \
    && locale-gen en_US.UTF-8 \
    && update-locale LANG=en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin

# Layer 2: PGDG PostgreSQL 17 host/client tools, pinned PgBouncer, pinned Redis
# 8.2, and Node.js 22 LTS (+ npm, Corepack, and pre-warmed pnpm).
COPY .github/actions/install-pg-redis/ACCC4CF8.asc /tmp/pgdg.asc
COPY .github/actions/install-pg-redis/redis-archive-keyring.asc /tmp/redis.asc

RUN set -euo pipefail \
    && install -d -m 0755 /usr/share/keyrings /etc/postgresql-common \
    && echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.conf \
    && gpg --batch --yes --dearmor -o /usr/share/keyrings/pgdg.gpg /tmp/pgdg.asc \
    && gpg --batch --yes --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg /tmp/redis.asc \
    && curl -fsSL --retry 5 --retry-all-errors https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --batch --yes --dearmor -o /usr/share/keyrings/nodesource.gpg \
    && rm -f /tmp/pgdg.asc /tmp/redis.asc \
    && codename="$(lsb_release -cs)" \
    && printf '%s\n' \
        "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt ${codename}-pgdg main" \
        "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb ${codename} main" \
        "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/meiva-ci.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        nodejs \
        postgresql-17 \
        "pgbouncer=${PGBOUNCER_APT_VERSION}" \
        "redis-server=${REDIS_APT_VERSION}" \
        "redis-tools=${REDIS_APT_VERSION}" \
    && apt-mark hold pgbouncer redis-server redis-tools \
    && redis_installed="$(dpkg-query -W -f='${Version}' redis-server)" \
    && case "${redis_installed}" in 6:8.2.*) ;; *) echo "unexpected redis-server ${redis_installed}" >&2; exit 1 ;; esac \
    && pgbouncer_installed="$(dpkg-query -W -f='${Version}' pgbouncer)" \
    && test "${pgbouncer_installed}" = "${PGBOUNCER_APT_VERSION}" \
    && node --version | grep -Eq '^v22\.' \
    && npm install -g --force corepack@latest \
    && mkdir -p /opt/corepack \
    && corepack enable --install-directory /usr/local/bin \
    && corepack prepare pnpm@latest --activate \
    && pnpm --version \
    && npm cache clean --force \
    && rm -rf /root/.npm /root/.cache /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin

# Layer 3: Pinned uv + CPython interpreter from .python-version.
COPY --from=uv_bin /uv /uvx /usr/local/bin/
COPY .python-version /tmp/.python-version

RUN set -euo pipefail \
    && test "$(uv --version | awk '{print $2}')" = "${UV_VERSION}" \
    && py_ver="$(tr -d '[:space:]' < /tmp/.python-version)" \
    && mkdir -p /opt/uv-python \
    && UV_PYTHON_BIN_DIR=/usr/local/bin uv python install "${py_ver}" --default \
    && python3 --version | grep -Fq "${py_ver}" \
    && rm -f /tmp/.python-version \
    && rm -rf /root/.cache

# Layer 4: Vendored Zonky PostgreSQL + pgvector runtime (single source of pins:
# base/cluster/dataplane/runtime_binaries.py).
COPY base/cluster/dataplane/runtime_binaries.py /tmp/runtime_binaries.py

RUN SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt python3 - <<'PY'
import ast
import hashlib
import io
import lzma
import pathlib
import shutil
import subprocess
import tarfile
import time
import urllib.request
import zipfile

source = pathlib.Path("/tmp/runtime_binaries.py").read_text(encoding="utf-8")
module = ast.parse(source)
constants: dict[str, object] = {}
wanted = {"PG_VERSION", "_MAVEN_BASE", "PG_ARTIFACTS", "PGVECTOR_VERSION", "PGVECTOR_ARTIFACTS"}
for node in module.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
        if name in wanted:
            constants[name] = ast.literal_eval(node.value)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        name = node.target.id
        if name in wanted:
            constants[name] = ast.literal_eval(node.value)

missing = wanted - constants.keys()
if missing:
    raise RuntimeError(f"missing constants in runtime_binaries.py: {sorted(missing)}")

pg_version = str(constants["PG_VERSION"])
maven_base = str(constants["_MAVEN_BASE"])
pg_artifacts = constants["PG_ARTIFACTS"]
pgvector_version = str(constants["PGVECTOR_VERSION"])
pgvector_artifacts = constants["PGVECTOR_ARTIFACTS"]
assert isinstance(pg_artifacts, dict) and isinstance(pgvector_artifacts, dict)

def fetch_verified(url: str, expected_sha256: str) -> bytes:
    last_err: Exception | None = None
    for attempt in range(1, 6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "meiva-ci-image-builder/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                blob = resp.read()
            actual = hashlib.sha256(blob).hexdigest()
            if actual != expected_sha256:
                raise RuntimeError(f"sha256 mismatch for {url}: expected {expected_sha256}, got {actual}")
            return blob
        except Exception as exc:
            last_err = exc
            if attempt < 5:
                time.sleep(attempt * 3)
    raise RuntimeError(f"failed to fetch {url}: {last_err}") from last_err

artifact, pg_sha = pg_artifacts["linux-x86_64"]
pg_url = f"{maven_base}/{artifact}/{pg_version}/{artifact}-{pg_version}.jar"
jar_bytes = fetch_verified(pg_url, pg_sha)

pg_dir = pathlib.Path("/opt/meiva-vendored-pg/runtime/pg") / pg_version
pg_dir.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(io.BytesIO(jar_bytes)) as jar:
    txz_names = [n for n in jar.namelist() if n.endswith(".txz")]
    if len(txz_names) != 1:
        raise RuntimeError(f"expected one .txz in {pg_url}, got {txz_names}")
    txz_bytes = jar.read(txz_names[0])
with tarfile.open(fileobj=io.BytesIO(txz_bytes), mode="r:xz") as tar:
    tar.extractall(pg_dir, filter="data")

vec_url, vec_sha = pgvector_artifacts["linux-x86_64"]
deb_bytes = fetch_verified(vec_url, vec_sha)
if not deb_bytes.startswith(b"!<arch>\n"):
    raise RuntimeError("pgvector artifact is not an ar archive")
pos = 8
data_member: bytes | None = None
while pos + 60 <= len(deb_bytes):
    name = deb_bytes[pos : pos + 16].decode("ascii", "replace").strip().rstrip("/")
    size = int(deb_bytes[pos + 48 : pos + 58].decode("ascii", "replace").strip())
    member = deb_bytes[pos + 60 : pos + 60 + size]
    if name == "data.tar.xz":
        data_member = member
        break
    pos += 60 + size + (size % 2)
if data_member is None:
    raise RuntimeError("no data.tar.xz in pgvector deb")

major = pg_version.split(".", 1)[0]
sql_name = f"vector--{pgvector_version}.sql"
mapping = {
    f"usr/lib/postgresql/{major}/lib/vector.so": pg_dir / "lib/postgresql/vector.so",
    f"usr/share/postgresql/{major}/extension/vector.control": pg_dir / "share/postgresql/extension/vector.control",
    f"usr/share/postgresql/{major}/extension/{sql_name}": pg_dir / f"share/postgresql/extension/{sql_name}",
}
with tarfile.open(fileobj=io.BytesIO(lzma.decompress(data_member)), mode="r:") as tar:
    for src, dst in mapping.items():
        member_obj = None
        for cand in (src, f"./{src}"):
            try:
                member_obj = tar.getmember(cand)
                break
            except KeyError:
                continue
        if member_obj is None:
            raise RuntimeError(f"missing {src} in pgvector deb")
        extracted = tar.extractfile(member_obj)
        assert extracted is not None
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(extracted.read())

for binary in (pg_dir / "bin/initdb", pg_dir / "bin/postgres", pg_dir / "bin/pg_ctl", pg_dir / "lib/postgresql/vector.so"):
    if not binary.exists():
        raise RuntimeError(f"missing expected vendored file {binary}")
    out = subprocess.check_output(["ldd", str(binary)], text=True)
    if "not found" in out:
        raise RuntimeError(f"missing shared library for {binary}:\n{out}")

pathlib.Path("/tmp/runtime_binaries.py").unlink()
print(f"vendored Postgres {pg_version} + pgvector {pgvector_version} verified at {pg_dir}")
PY

# Layer 5: Non-root `runner` user (UID 1001 / GID 1001) matching GitHub-hosted
# ubuntu-24.04 runners, with passwordless sudo and shared runtime ownership.
RUN groupadd --gid 1001 runner \
    && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/runner \
    && chmod 0440 /etc/sudoers.d/runner \
    && chown -R runner:runner /opt/uv-python /opt/meiva-vendored-pg /opt/corepack /home/runner \
    && chmod -R a+rX /opt/uv-python /opt/meiva-vendored-pg /opt/corepack \
    && chmod 1777 /tmp /var/tmp

USER runner
WORKDIR /home/runner
