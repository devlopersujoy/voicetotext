FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates cmake g++ git make curl
RUN git clone --depth 1 --branch v1.7.6 https://github.com/ggml-org/whisper.cpp /src
WORKDIR /src
RUN cmake -B build -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DWHISPER_BUILD_TESTS=OFF && cmake --build build --target whisper-cli -j2
RUN curl -fL --retry 3 https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny-q5_1.bin -o /model.bin && echo "818710568da3ca15689e31a743197b520007872ff9576237bda97bd1b469c3d7  /model.bin" | sha256sum -c -
FROM denoland/deno:bin AS jsruntime
FROM python:3.12-slim-bookworm
COPY --from=jsruntime /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/bin/whisper-cli /usr/local/bin/
COPY --from=build /model.bin /opt/model.bin
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && whisper-cli --help
COPY app.py media_links.py languages.json ./
COPY templates templates
RUN useradd -m appuser
USER appuser
CMD ["sh","-c","exec gunicorn -w 1 --threads 6 --bind 0.0.0.0:${PORT:-10000} app:app"]
