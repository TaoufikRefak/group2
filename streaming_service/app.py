from datetime import datetime
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
import pika
import threading
import json
import logging
import time
import os
from auth_lib import requires_role, decode_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///streaming.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class StreamingSession(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    course_id = db.Column(db.Integer, nullable=False)
    student_id = db.Column(db.Integer, nullable=False)
    stream_key = db.Column(db.String(255), nullable=False, unique=True)
    streaming_url = db.Column(db.String(255), nullable=False)
    timestamp = db.Column(db.DateTime, default=db.func.current_timestamp())

with app.app_context():
    db.create_all()

def publish_message(queue, message):
    try:
        connection = pika.BlockingConnection(pika.ConnectionParameters(host='rabbitmq'))
        channel = connection.channel()
        channel.queue_declare(queue=queue, durable=True)
        channel.basic_publish(exchange='', routing_key=queue, body=json.dumps(message))
        connection.close()
        logger.info(f"Published to {queue}: {message}")
    except Exception as e:
        logger.error(f"Failed to publish message: {e}")

def generate_stream_key(course_id, student_id):
    return f"course_{course_id}_student_{student_id}"

def get_streaming_url(stream_key):
    return f"{os.environ['NGINX_RTMP_URL']}/{stream_key}"

def handle_streaming_operations(course_id=None, student_id=None, stream_key=None):
    try:
        if stream_key:
            parts = stream_key.split('_')
            if len(parts) != 4 or parts[0] != 'course' or parts[2] != 'student':
                raise ValueError("Invalid stream key format")
            course_id = int(parts[1])
            student_id = int(parts[3])

        if not stream_key:
            stream_key = generate_stream_key(course_id, student_id)

        streaming_url = get_streaming_url(stream_key)

        session = StreamingSession.query.filter_by(
            course_id=course_id,
            student_id=student_id
        ).first()

        if not session:
            session = StreamingSession(
                course_id=course_id,
                student_id=student_id,
                stream_key=stream_key,
                streaming_url=streaming_url
            )
            db.session.add(session)

        db.session.commit()
        return streaming_url, session

    except Exception as e:
        logger.error(f"Stream operation error: {e}")
        raise

@app.route('/stream/start', methods=['POST', 'GET'])
def handle_stream_start():
    try:
        if request.method == 'POST':
            stream_key = request.form.get('name')
            # Extract IDs from stream key
            parts = stream_key.split('_')
            course_id = int(parts[1])
            student_id = int(parts[3])
            streaming_url, session = handle_streaming_operations(stream_key=stream_key)
        else:
            course_id = int(request.args.get('course_id'))
            student_id = int(request.args.get('student_id'))
            streaming_url, session = handle_streaming_operations(course_id, student_id)

        # Publish course view event
        publish_message('user_interactions', {
            "event": "COURSE_VIEWED",
            "course_id": course_id,
            "student_id": student_id,
            "timestamp": datetime.utcnow().isoformat()
        })

        return jsonify({
            "streaming_url": streaming_url,
            "stream_key": session.stream_key
        }), 200

    except Exception as e:
        logger.error(f"Stream start failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/stream/stop', methods=['POST', 'DELETE'])
def handle_stream_stop():
    try:
        if request.method == 'POST':
            stream_key = request.form.get('name')
            session = StreamingSession.query.filter_by(stream_key=stream_key).first()
        else:
            session_id = request.args.get('session_id')
            session = StreamingSession.query.get(session_id)

        if not session:
            return jsonify({"error": "Session not found"}), 404

        publish_message('streaming_events', {
            "event": "STREAMING_STOPPED",
            "course_id": session.course_id,
            "student_id": session.student_id
        })

        db.session.delete(session)
        db.session.commit()
        return jsonify({"message": "Stream stopped"}), 200

    except Exception as e:
        logger.error(f"Stream stop failed: {e}")
        return jsonify({"error": str(e)}), 500

def consume_course_views():
    while True:
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(host='rabbitmq'))
            channel = connection.channel()
            channel.queue_declare(queue='user_interactions', durable=True)

            def callback(ch, method, properties, body):
                try:
                    # WRAP IN APPLICATION CONTEXT
                    with app.app_context():
                        event = json.loads(body)
                        logger.info(f"Processing event: {event['event']}")
                        
                        if event['event'] == 'COURSE_VIEWED':
                            handle_streaming_operations(
                                event['course_id'],
                                event['student_id']
                            )
                except Exception as e:
                    logger.error(f"Event processing failed: {e}")
                finally:
                    ch.basic_ack(delivery_tag=method.delivery_tag)

            channel.basic_consume(queue='user_interactions', 
                                on_message_callback=callback,
                                auto_ack=False)  # Keep manual ack
            channel.start_consuming()
        except Exception as e:
            logger.error(f"Connection error: {e}")
            time.sleep(5)

threading.Thread(target=consume_course_views, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3010)