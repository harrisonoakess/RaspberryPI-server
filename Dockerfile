# Multi-stage build for the single Railway service: the React dashboard is
# compiled by Node, and only its build output is copied into the Python runtime
# image. Both base images are pinned to an exact patch tag *and* an immutable
# digest, so a rebuild months from now produces the same toolchain.
#
# Railway's Root Directory is the repository root (so this context contains both
# frontend/ and server/) and its Config File Path is /server/railway.json.

# --- Stage 1: compile the dashboard ------------------------------------------
# Matches frontend/.nvmrc so local development and this build cannot drift.
FROM node:22.23.2-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46 AS frontend

WORKDIR /build

# Dependencies first: this layer is reused whenever only source files change.
#
# `npm ci` refuses to install from a lockfile that does not resolve on *this*
# platform. Rolldown's optional wasm fallback pulls @emnapi/*, and a lockfile
# written by a newer npm on macOS omits those entries, which fails here. Always
# regenerate the lockfile with the npm this image ships (10.9.8, bundled with
# Node 22.23.2) — see the README's "Regenerating the frontend lockfile".
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/tsconfig.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src

# The build fails the image if types or tests fail, so a broken dashboard can
# never reach the runtime stage.
RUN npm run typecheck \
    && npm test \
    && npm run build

# --- Stage 2: the FastAPI runtime --------------------------------------------
FROM python:3.13.14-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/server

COPY server/requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

# Explicit targets only: no node_modules, no frontend sources, no tests, no Pi
# code, and nothing else from the build context.
COPY server/main.py server/dashboard.py ./
COPY --from=frontend /build/dist ./static

# Railway supplies $PORT. `main:app` still resolves because the working
# directory is the server package root, exactly as under Nixpacks.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
