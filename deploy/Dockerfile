# 앱 파드 이미지: 프론트(정적) + 백(API·에이전트) + MCP 클라이언트 + 배치 스크립트
# 로컬(arm64)·사내(x86)는 각자 환경에서 빌드
# bookworm 고정: 부동 태그 python:3.12-slim은 trixie(Debian13)로 올라가 libaio1t64 문제 발생 →
# bookworm(Debian12)은 libaio1(libaio.so.1)이 그대로라 Instant Client가 바로 인식.
FROM python:3.12-slim-bookworm
WORKDIR /srv

# Oracle Instant Client (ORACLE_MODE=thick 용). ldconfig 등록 → init_oracle_client()가 lib_dir 없이 탐색.
# thin 모드만 쓸 거면 이 블록은 제거 가능.
# 사내 표준 19c에 맞춰 19.28로 고정(버전 없는 latest는 23ai로 붙음). 최신으로 올리려면 URL만 교체.
# arm64는 URL을 .../instantclient-basiclite-linux.arm64-19.28...zip 로 교체.
ARG IC_URL=https://download.oracle.com/otn_software/linux/instantclient/1928000/instantclient-basiclite-linux.x64-19.28.0.0.0dbru.zip
RUN apt-get update \
 && apt-get install -y --no-install-recommends libaio1 curl unzip ca-certificates \
 && mkdir -p /opt/oracle \
 && curl -fsSL -o /tmp/ic.zip "$IC_URL" \
 && unzip -q /tmp/ic.zip -d /opt/oracle && rm /tmp/ic.zip \
 && echo /opt/oracle/instantclient* > /etc/ld.so.conf.d/oracle-ic.conf && ldconfig \
 && apt-get purge -y curl unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent agent
COPY web web
COPY core core
COPY search search
COPY ingestion ingestion
COPY graph graph
ENV PYTHONUNBUFFERED=1
EXPOSE 8500
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8500"]
