from datetime import datetime
from flask import Flask, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
import pika
import json
import logging
from auth_lib import generate_token, decode_token  # Import from your auth_lib

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# App configuration
app.config['SECRET_KEY'] = 'mysecurekey'  # Replace with a secure key (if not using .env)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///auth.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database model for Authentication Service
class AuthUser(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # e.g., student, teacher, admin, moderateur
    branch_id = db.Column(db.Integer, nullable=True)  # Nullable for roles like moderateur

# Create the database tables if they don't exist
with app.app_context():
    db.create_all()

# RabbitMQ Publisher
def publish_message(queue, message):
    try:
        connection = pika.BlockingConnection(pika.ConnectionParameters(host='rabbitmq'))
        channel = connection.channel()
        channel.queue_declare(queue=queue, durable=True)
        channel.basic_publish(exchange='', routing_key=queue, body=json.dumps(message))
        connection.close()
        logger.info(f"Message published to queue '{queue}': {message}")
    except Exception as e:
        logger.error(f"Failed to publish message: {e}")

# Route to register a new user
@app.route('/auth/register', methods=['POST'])
def register():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    role = data.get('role')  # Role is mandatory
    branch_id = data.get('branch_id')  # Nullable for roles like moderateur

    if not email or not password or not role:
        return jsonify({"message": "Email, password, and role are required"}), 400

    if role in ['student', 'teacher'] and branch_id is None:
        return jsonify({"message": "Branch ID is required for students and teachers"}), 400

    if AuthUser.query.filter_by(email=email).first():
        return jsonify({"message": "User already exists"}), 409

    hashed_password = generate_password_hash(password)
    new_user = AuthUser(email=email, password=hashed_password, role=role, branch_id=branch_id)
    db.session.add(new_user)
    db.session.commit()

    # Publish USER_CREATED event
    publish_message('user_events', {
        "event": "USER_CREATED",
        "user_id": new_user.id,
        "email": new_user.email,
        "role": new_user.role,
        "branch_id": new_user.branch_id
    })

    return jsonify({"message": "User registered successfully"}), 201

# Route to log in and get a JWT token using auth_lib
@app.route('/auth/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"message": "Email and password are required"}), 400

    user = AuthUser.query.filter_by(email=email).first()
    if user and check_password_hash(user.password, password):
        # Use auth_lib to generate the token
        token = generate_token(
            {
                'user_id': user.id,
                'role': user.role,
                'branch_id': user.branch_id
            },
            expires_in=3600  # 1 hour expiry
        )
        return jsonify({"token": token}), 200

    return jsonify({"message": "Invalid email or password"}), 401

# RabbitMQ Consumer for user synchronization
def consume_user_events():
    try:
        connection = pika.BlockingConnection(pika.ConnectionParameters(host='rabbitmq'))
        channel = connection.channel()
        channel.queue_declare(queue='user_events', durable=True)

        def callback(ch, method, properties, body):
            event = json.loads(body)
            logger.info(f"Received event: {event}")
            if event['event'] == 'USER_CREATED':
                with app.app_context():
                    if not AuthUser.query.filter_by(id=event['user_id']).first():
                        new_user = AuthUser(
                            id=event['user_id'],
                            email=event['email'],
                            password='SYNCED_USER',  # Placeholder for hashed password
                            role=event['role'],
                            branch_id=event['branch_id']
                        )
                        db.session.add(new_user)
                        db.session.commit()
                        logger.info(f"User {event['user_id']} synced to auth service")

        channel.basic_consume(queue='user_events', on_message_callback=callback, auto_ack=True)
        logger.info("Waiting for user events...")
        channel.start_consuming()
    except pika.exceptions.AMQPConnectionError as e:
        logger.error(f"Connection error: {e}. Retrying in 5 seconds...")
        datetime.time.sleep(5)

@app.route('/auth/validate', methods=['POST'])
def validate_token():
    token = request.get_data(as_text=True)
    if not token:
        return jsonify({"message": "Token is required"}), 400

    try:
        # Remove "Bearer " prefix if present
        token = token.replace("Bearer ", "")
        # Use auth_lib to decode the token
        decoded_token = decode_token(token)
        return jsonify({"message": "Token is valid", "payload": decoded_token}), 200
    except jwt.ExpiredSignatureError:
        return jsonify({"message": "Token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"message": "Invalid token"}), 401

# Start RabbitMQ consumer in a separate thread
import threading
consumer_thread = threading.Thread(target=consume_user_events)
consumer_thread.daemon = True
consumer_thread.start()

# Main application entry point
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3000)
