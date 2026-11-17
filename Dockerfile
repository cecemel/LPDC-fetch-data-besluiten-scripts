FROM python:3.12-slim AS ttl2svg

ARG TTL2SVG_REF=dbf5246bf1385052aaf549d9fdc0ac3d7cdce9a4

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git init -q /src \
    && git -C /src remote add origin https://github.com/cecemel/ttl2svg.git \
    && git -C /src fetch --depth 1 origin "$TTL2SVG_REF" \
    && git -C /src checkout -q FETCH_HEAD

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends graphviz \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fetch_besluiten.py config.json ./
COPY --from=ttl2svg /src/visualize_ttl.py /src/LICENSE ./vendor/ttl2svg/

ENTRYPOINT ["python3", "fetch_besluiten.py"]
