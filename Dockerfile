# 计算服务镜像：**数据不打进去**（构建期不拷任何 blk；运行期用 volume 挂到 /data 并设 DATA_DIR）。
#   docker build -t missile-backend .
#   docker run --rm -p 8080:8080 -v <本机数据目录>:/data/2.59.0.28:ro \
#              -e DATA_DIR=/data/2.59.0.28 missile-backend
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /srv

# 先只装依赖（利用层缓存；missile-solver 从公开发布装）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷应用代码（app/ 里没有任何数据文件）
COPY app ./app
COPY LICENSE README.md ./

# 非 root 跑
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8080
# 启动即校验 DATA_DIR（缺件/哈希不符 ⇒ 进程直接退出，见 app/physics.py 的 data_gate）
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
