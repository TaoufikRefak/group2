# Dockerfile for Playlist Service

# Use an official Python image as a base
FROM python:3.9-slim

# Set the working directory
WORKDIR /app
ARG GITHUB_TOKEN
ENV GITHUB_TOKEN=${GITHUB_TOKEN}
RUN apt-get update && apt-get install -y git
COPY .env .env
# Copy the current directory contents into the container
COPY . /app

# Install dependencies
RUN pip install --no-cache-dir flask flask_sqlalchemy pika requests

RUN pip install --no-cache-dir git+https://${GITHUB_TOKEN}@github.com/TaoufikRefak/auth_lib.git@main#egg=auth_lib

# Expose the port the service runs on
EXPOSE 3003

# Define the command to run the application
CMD ["python", "app.py"]
