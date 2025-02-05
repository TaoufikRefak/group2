FROM tiangolo/nginx-rtmp:latest

RUN apt-get update && \
    apt-get install -y \
    python3 \
    python3-pip \
    git \
    ffmpeg

COPY nginx.conf /etc/nginx/nginx.conf
COPY app.py requirements.txt /app/

WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install git+https://${GITHUB_TOKEN}@github.com/TaoufikRefak/auth_lib.git@main#egg=auth_lib


EXPOSE 1935 3010

CMD ["sh", "-c", "nginx -g 'daemon off;' & python3 app.py"]