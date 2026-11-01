# Pilot 主 API 与五个代理共用一个镜像，靠启动命令区分（见 EcomOps/deploy/docker-compose.full.yml）。
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pilot ./pilot
COPY stubs ./stubs
COPY static ./static
COPY contracts ./contracts
RUN useradd --system --uid 10001 app && chown -R app /app
USER app
EXPOSE 8085
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8085')+'/health',timeout=2)"
CMD ["python", "-m", "uvicorn", "pilot.main:app", "--host", "0.0.0.0", "--port", "8085"]
