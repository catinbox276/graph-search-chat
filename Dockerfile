# 앱 파드 이미지: 프론트(정적) + 백(API·에이전트) + MCP 클라이언트 + 배치 스크립트
# 로컬(arm64)·사내(x86)는 각자 환경에서 빌드
FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent agent
COPY tools tools
COPY app app
COPY poc poc
COPY scripts scripts
ENV PYTHONUNBUFFERED=1
EXPOSE 8500
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8500"]
