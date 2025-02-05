# Use an official Python image as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies (to support private git repositories)
RUN apt-get update && apt-get install -y git
COPY .env .env

# Copy the current directory contents into the container at /app
COPY . /app

# Install dependencies listed in requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install git+https://${GITHUB_TOKEN}@github.com/TaoufikRefak/auth_lib.git@main#egg=auth_lib


# Install other dependencies if necessary
RUN pip install pyjwt --upgrade

# Expose the port the app runs on
EXPOSE 3000

# Define the command to run the application
CMD ["python", "app.py"]
