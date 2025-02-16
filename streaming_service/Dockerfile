FROM python:3.9-slim

RUN apt-get update && \
    apt-get install -y \
    git \
    ffmpeg

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

# Use the GitHub token to install the private repo
ARG GITHUB_TOKEN
ENV GITHUB_TOKEN=${GITHUB_TOKEN}
RUN pip install --no-cache-dir  git+https://${GITHUB_TOKEN}@github.com/TaoufikRefak/auth_lib.git@main#egg=auth_lib

EXPOSE 3010

CMD ["python", "app.py"]