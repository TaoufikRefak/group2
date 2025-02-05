# Dockerfile for User Service

# Use an official Python image as a base
FROM python:3.9-slim

# Set the working directory
WORKDIR /app
RUN apt-get update && apt-get install -y git
COPY .env .env
# Copy the current directory contents into the container
COPY . /app

# Install required dependencies
RUN pip install --no-cache-dir flask flask_sqlalchemy pika
RUN pip install git+https://${GITHUB_TOKEN}@github.com/TaoufikRefak/auth_lib.git@main#egg=auth_lib

# Expose the port the service listens on
EXPOSE 3001

# Command to run the application
CMD ["python", "app.py"]
