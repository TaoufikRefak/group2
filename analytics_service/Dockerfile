# Dockerfile for Analytics Service

# Use an official Python image as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app
ARG GITHUB_TOKEN
ENV GITHUB_TOKEN=${GITHUB_TOKEN}
RUN apt-get update && apt-get install -y git
COPY .env .env
# Copy the current directory contents into the container at /app
COPY . /app

# Install dependencies
RUN pip install --no-cache-dir flask flask_sqlalchemy pika requests

RUN pip install --no-cache-dir git+https://${GITHUB_TOKEN}@github.com/TaoufikRefak/auth_lib.git@main#egg=auth_lib

# Expose the port the app runs on
EXPOSE 3004

# Define the command to run the application
CMD ["python", "app.py"]
