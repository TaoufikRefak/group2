from datetime import datetime
from flask import Flask, jsonify, request, g
from flask_sqlalchemy import SQLAlchemy
import pika
import threading
import json
import logging
import time
from auth_lib import requires_role, decode_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///user.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    role = db.Column(db.String(20), nullable=False)  # student, teacher, admin, moderator
    branch_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def serialize(self):
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "branch_id": self.branch_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat()
        }

with app.app_context():
    db.create_all()

# RabbitMQ Setup
def publish_message(queue, message):
    try:
        connection = pika.BlockingConnection(pika.ConnectionParameters(host='rabbitmq'))
        channel = connection.channel()
        channel.queue_declare(queue=queue, durable=True)
        channel.basic_publish(
            exchange='',
            routing_key=queue,
            body=json.dumps(message),
            properties=pika.BasicProperties(delivery_mode=2)
        )
        connection.close()
    except Exception as e:
        logger.error(f"Failed to publish message: {e}")

# User Endpoints
@app.route('/users/<int:user_id>', methods=['GET'])
@requires_role(['student', 'teacher', 'admin'])
def get_user(user_id):
    try:
        requester = g.user
        target_user = User.query.get_or_404(user_id)
        
        # Authorization checks
        if requester['role'] == 'student' and requester['user_id'] != user_id:
            return jsonify({"error": "Unauthorized access"}), 403
            
        if requester['role'] == 'teacher' and not (
            requester['user_id'] == user_id or 
            (target_user.role == 'student' and target_user.branch_id == requester['branch_id'])
        ):
            return jsonify({"error": "Unauthorized access"}), 403

        return jsonify(target_user.serialize()), 200
    except Exception as e:
        logger.error(f"Error fetching user: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/users/<int:user_id>', methods=['PUT'])
@requires_role(['admin', 'teacher', 'student'])
def update_user(user_id):
    try:
        requester = g.user
        target_user = User.query.get_or_404(user_id)
        data = request.get_json()

        # Authorization
        if requester['role'] != 'admin' and requester['user_id'] != user_id:
            return jsonify({"error": "Unauthorized update"}), 403

        # Prevent role/branch modification by non-admins
        if 'role' in data and requester['role'] != 'admin':
            return jsonify({"error": "Only admins can modify roles"}), 403
            
        if 'branch_id' in data and requester['role'] != 'admin':
            return jsonify({"error": "Only admins can modify branches"}), 403

        # Update fields
        if 'name' in data:
            target_user.name = data['name']
        if 'email' in data:
            target_user.email = data['email']
        if 'role' in data:
            target_user.role = data['role']
        if 'branch_id' in data:
            target_user.branch_id = data['branch_id']

        db.session.commit()

        publish_message('user_events', {
            "event": "USER_UPDATED",
            "user_id": target_user.id,
            "name": target_user.name,
            "email": target_user.email,
            "role": target_user.role,
            "branch_id": target_user.branch_id
        })

        return jsonify(target_user.serialize()), 200
    except Exception as e:
        logger.error(f"Error updating user: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/users/<int:user_id>', methods=['DELETE'])
@requires_role(['admin'])
def delete_user(user_id):
    try:
        target_user = User.query.get_or_404(user_id)
        db.session.delete(target_user)
        db.session.commit()

        publish_message('user_events', {
            "event": "USER_DELETED",
            "user_id": user_id
        })

        return jsonify({"message": "User deleted successfully"}), 200
    except Exception as e:
        logger.error(f"Error deleting user: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/users', methods=['GET'])
@requires_role(['admin'])
def list_users():
    try:
        role_filter = request.args.get('role')
        branch_filter = request.args.get('branch_id')
        
        query = User.query
        if role_filter:
            query = query.filter_by(role=role_filter)
        if branch_filter:
            query = query.filter_by(branch_id=branch_filter)
            
        users = query.all()
        return jsonify([u.serialize() for u in users]), 200
    except Exception as e:
        logger.error(f"Error listing users: {e}")
        return jsonify({"error": "Internal server error"}), 500


# Add this endpoint to support analytics recommendations
@app.route('/users/<int:user_id>/branch', methods=['GET'])
@requires_role(['admin', 'teacher', 'student'])
def get_user_branch(user_id):
    user = User.query.get_or_404(user_id)
    return jsonify({"user_id": user_id, "branch_id": user.branch_id}), 200
@app.route('/users/branches/<int:branch_id>', methods=['GET'])
@requires_role(['admin', 'teacher'])
def get_branch_users(branch_id):
    try:
        requester = g.user
        if requester['role'] == 'teacher' and requester['branch_id'] != branch_id:
            return jsonify({"error": "Unauthorized branch access"}), 403

        users = User.query.filter_by(branch_id=branch_id).all()
        return jsonify([u.serialize() for u in users]), 200
    except Exception as e:
        logger.error(f"Error fetching branch users: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify(status="ok"), 200

# Enhanced RabbitMQ Consumer
def consume_user_events():
    while True:
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(host='rabbitmq'))
            channel = connection.channel()
            channel.queue_declare(queue='user_events', durable=True)

            def callback(ch, method, properties, body):
                try:
                    with app.app_context():
                        event = json.loads(body)
                        logger.info(f"Processing event: {event['event']}")

                        if event['event'] == 'USER_CREATED':
                            user = User.query.get(event['user_id'])
                            if not user:
                                user = User(
                                    id=event['user_id'],
                                    name=event.get('name', 'New User'),
                                    email=event['email'],
                                    role=event['role'],
                                    branch_id=event.get('branch_id')
                                )
                                db.session.add(user)
                                db.session.commit()

                        elif event['event'] == 'USER_UPDATED':
                            user = User.query.get(event['user_id'])
                            if user:
                                user.name = event.get('name', user.name)
                                user.email = event.get('email', user.email)
                                user.role = event.get('role', user.role)
                                user.branch_id = event.get('branch_id', user.branch_id)
                                db.session.commit()

                        elif event['event'] == 'USER_DELETED':
                            user = User.query.get(event['user_id'])
                            if user:
                                db.session.delete(user)
                                db.session.commit()

                except Exception as e:
                    logger.error(f"Event processing error: {e}")
                finally:
                    ch.basic_ack(delivery_tag=method.delivery_tag)

            channel.basic_consume(queue='user_events', on_message_callback=callback, auto_ack=False)
            channel.start_consuming()
        except Exception as e:
            logger.error(f"Connection error: {e}")
            time.sleep(5)

threading.Thread(target=consume_user_events, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3001)