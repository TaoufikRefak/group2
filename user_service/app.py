from datetime import datetime
import time
from flask import Flask, jsonify, request, g
from flask_sqlalchemy import SQLAlchemy
import pika
import threading
import json
import logging
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
    role = db.Column(db.String(20), nullable=False)
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

@app.route('/users/<int:user_id>', methods=['GET'])
@requires_role(['admin', 'teacher', 'student'])
def get_user(user_id):
    try:
        requester = g.user
        user = User.query.get_or_404(user_id)
        
        # Authorization checks
        if requester['role'] == 'student' and user_id != requester['user_id']:
            return jsonify({"error": "Unauthorized access"}), 403
            
        if requester['role'] == 'teacher' and (user.branch_id != requester['branch_id'] or user.role not in ['student', 'teacher']):
            return jsonify({"error": "Unauthorized access"}), 403

        return jsonify(user.serialize()), 200
    except Exception as e:
        logger.error(f"Error fetching user: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/users', methods=['GET'])
@requires_role(['admin'])
def get_all_users():
    try:
        role = request.args.get('role')
        branch_id = request.args.get('branch_id')
        
        query = User.query
        if role:
            query = query.filter_by(role=role)
        if branch_id:
            query = query.filter_by(branch_id=branch_id)
            
        users = query.all()
        return jsonify([u.serialize() for u in users]), 200
    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/users/<int:user_id>', methods=['PUT'])
@requires_role(['admin'])
def update_user(user_id):
    try:
        data = request.get_json()
        user = User.query.get_or_404(user_id)
        
        # Validate role-specific constraints
        if data.get('role') in ['student', 'teacher'] and not data.get('branch_id'):
            return jsonify({"error": "Branch ID required for this role"}), 400
            
        if data.get('role') == 'admin' and data.get('branch_id'):
            return jsonify({"error": "Admins cannot have branch association"}), 400

        user.name = data.get('name', user.name)
        user.role = data.get('role', user.role)
        user.branch_id = data.get('branch_id', user.branch_id)
        db.session.commit()

        publish_message('user_events', {
            "event": "USER_UPDATED",
            "user_id": user.id,
            "name": user.name,
            "role": user.role,
            "branch_id": user.branch_id
        })

        return jsonify(user.serialize()), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating user: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/users/<int:user_id>', methods=['DELETE'])
@requires_role(['admin'])
def delete_user(user_id):
    try:
        user = User.query.get_or_404(user_id)
        db.session.delete(user)
        db.session.commit()

        publish_message('user_events', {
            "event": "USER_DELETED",
            "user_id": user.id
        })

        return jsonify({"message": "User deleted successfully"}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting user: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/branches/<int:branch_id>/users', methods=['GET'])
@requires_role(['admin', 'teacher'])
def get_branch_users(branch_id):
    try:
        requester = g.user
        if requester['role'] == 'teacher' and requester['branch_id'] != branch_id:
            return jsonify({"error": "Unauthorized branch access"}), 403

        users = User.query.filter_by(branch_id=branch_id)
        return jsonify([u.serialize() for u in users]), 200
    except Exception as e:
        logger.error(f"Error fetching branch users: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/health", methods=["GET"])
def health_check():
    try:
        # Basic database health check
        User.query.first()
        return jsonify({
            "status": "healthy",
            "database": "connected",
            "timestamp": datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "unhealthy"}), 500

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
                            # Validate required fields
                            if event['role'] in ['student', 'teacher'] and not event.get('branch_id'):
                                logger.error("Missing branch_id for role")
                                return

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
                                logger.info(f"Created user {event['user_id']}")

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