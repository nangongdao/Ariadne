# ---- 阶段 1：构建前端 ----
FROM node:22-slim AS web-build

WORKDIR /web

# 先只拷依赖清单，让 npm ci 层能被缓存
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build

# ---- 阶段 2：运行时 ----
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# alembic.ini 不在 wheel 里（hatch 只打包 src/），迁移入口按工作目录找它
COPY pyproject.toml README.md alembic.ini ./
COPY src/ ./src/
COPY deploy/ ./deploy/

RUN pip install --no-cache-dir ".[server]"

# 前端构建产物由 API 托管（见 src/ariadne/api/static.py）
COPY --from=web-build /web/dist ./web/dist

# 非 root 运行
RUN useradd --create-home --uid 10001 ariadne \
    && mkdir -p /app/data \
    && chown -R ariadne:ariadne /app
USER ariadne

EXPOSE 8000

CMD ["ariadne-api"]
