FROM python:3.12-slim
WORKDIR /srv
# osmium 휠이 libexpat을 동적 링크함(slim에는 없음). tzdata는 로그·생성시각을 한국 시간으로.
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 tzdata && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir osmium==4.3.2
COPY app.py build.py extra.py famous_lines.json index.html mountain_junctions.js ./
ENV DATA_ROOT=/data PYTHONUNBUFFERED=1 PORT=8000 JUNCTIONS_JS=/srv/mountain_junctions.js TZ=Asia/Seoul
EXPOSE 8000
CMD ["python", "app.py"]
