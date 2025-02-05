from datetime import datetime, timezone
from flask import Flask, jsonify, request, g
from flask_sqlalchemy import SQLAlchemy
import pika
import threading
import json
import logging
import time
import requests
from auth_lib import requires_role, decode_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///playlist.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database models
class Playlist(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(120), nullable=False)
    student_id = db.Column(db.Integer, nullable=False)
    is_public = db.Column(db.Boolean, default=True)
    courses = db.relationship('PlaylistCourses', backref='playlist', lazy=True)

class PlaylistCourses(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    playlist_id = db.Column(db.Integer, db.ForeignKey('playlist.id'), nullable=False)
    course_id = db.Column(db.Integer, nullable=False)

class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False)
    video_url = db.Column(db.String(255), nullable=False)
    branch_id = db.Column(db.Integer, nullable=False)

with app.app_context():
    db.create_all()

# RabbitMQ setup
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
        logger.info(f"Published to {queue}: {message}")
    except Exception as e:
        logger.error(f"Failed to publish message: {e}")

def consume_course_events():
    while True:
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(host='rabbitmq'))
            channel = connection.channel()
            channel.queue_declare(queue='course_events', durable=True)

            def callback(ch, method, properties, body):
                try:
                    with app.app_context():
                        event = json.loads(body)
                        logger.info(f"Received event: {event['event']}")
                        
                        if event['event'] == 'COURSE_CREATED':
                            course = Course.query.get(event['course_id'])
                            if not course:
                                course = Course(
                                    id=event['course_id'],
                                    title=event['title'],
                                    description=event.get('description', ''),
                                    video_url=event['hls_url'],
                                    branch_id=event['branch_id']
                                )
                            else:
                                course.title = event['title']
                                course.video_url = event['hls_url']
                                course.branch_id = event['branch_id']
                            
                            db.session.add(course)
                            db.session.commit()

                except Exception as e:
                    logger.error(f"Event processing error: {e}")
                finally:
                    ch.basic_ack(delivery_tag=method.delivery_tag)

            channel.basic_consume(queue='course_events', on_message_callback=callback, auto_ack=False)
            channel.start_consuming()
        except Exception as e:
            logger.error(f"Connection error: {e}")
            time.sleep(5)

threading.Thread(target=consume_course_events, daemon=True).start()

@app.route('/playlists', methods=['POST'])
@requires_role(['student'])
def create_playlist():
    user = g.user
    data = request.get_json()
    
    if not data.get('name'):
        return jsonify({"error": "Playlist name is required"}), 400

    try:
        new_playlist = Playlist(
            name=data['name'],
            student_id=user['user_id'],
            is_public=data.get('is_public', True)
        )
        db.session.add(new_playlist)
        db.session.commit()
        return jsonify({
            "message": "Playlist created",
            "playlist_id": new_playlist.id
        }), 201
    except Exception as e:
        db.session.rollback()
        logger.error(f"Database error: {e}")
        return jsonify({"error": "Playlist creation failed"}), 500

@app.route('/playlists/<int:playlist_id>/courses', methods=['POST'])
@requires_role(['student'])
def add_course_to_playlist(playlist_id):
    user = g.user
    data = request.get_json()
    
    playlist = Playlist.query.get_or_404(playlist_id)
    if playlist.student_id != user['user_id']:
        return jsonify({"error": "Unauthorized playlist access"}), 403

    course_id = data.get('course_id')
    if not course_id:
        return jsonify({"error": "Course ID required"}), 400

    try:
        # Verify course exists and belongs to student's branch
        course = Course.query.get(course_id)
        if not course or course.branch_id != user['branch_id']:
            return jsonify({"error": "Course not available in your branch"}), 400

        # Check existing association
        if PlaylistCourses.query.filter_by(playlist_id=playlist_id, course_id=course_id).first():
            return jsonify({"error": "Course already in playlist"}), 400

        playlist_course = PlaylistCourses(playlist_id=playlist_id, course_id=course_id)
        db.session.add(playlist_course)
        db.session.commit()

        publish_message('user_interactions', {
            "event": "PLAYLIST_INTERACTION",
            "playlist_id": playlist_id,
            "student_id": user['user_id'],
            "action": "added",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        return jsonify({"message": "Course added to playlist"}), 201

    except Exception as e:
        db.session.rollback()
        logger.error(f"Database error: {e}")
        return jsonify({"error": "Failed to add course"}), 500

@app.route('/playlists/<int:playlist_id>', methods=['PUT'])
@requires_role(['student'])
def update_playlist(playlist_id):
    user = g.user
    data = request.get_json()
    
    playlist = Playlist.query.get_or_404(playlist_id)
    if playlist.student_id != user['user_id']:
        return jsonify({"error": "Unauthorized access"}), 403

    if 'name' in data:
        playlist.name = data['name']
    if 'is_public' in data:
        playlist.is_public = data['is_public']
    
    db.session.commit()
    return jsonify({"message": "Playlist updated"}), 200

@app.route('/playlists/<int:playlist_id>', methods=['DELETE'])
@requires_role(['student'])
def delete_playlist(playlist_id):
    user = g.user
    playlist = Playlist.query.get_or_404(playlist_id)
    
    if playlist.student_id != user['user_id']:
        return jsonify({"error": "Unauthorized access"}), 403

    db.session.delete(playlist)
    db.session.commit()
    return jsonify({"message": "Playlist deleted"}), 200

@app.route('/playlists/<int:playlist_id>', methods=['GET'])
@requires_role(['student'])
def get_playlist(playlist_id):
    playlist = Playlist.query.get_or_404(playlist_id)
    
    if not playlist.is_public and playlist.student_id != g.user['user_id']:
        return jsonify({"error": "Private playlist"}), 403

    courses = [
        {
            "id": pc.course_id,
            "title": Course.query.get(pc.course_id).title,
            "description": Course.query.get(pc.course_id).description
        } for pc in playlist.courses
    ]
    
    return jsonify({
        "id": playlist.id,
        "name": playlist.name,
        "owner": playlist.student_id,
        "courses": courses
    }), 200

@app.route('/playlists/public', methods=['GET'])
@requires_role(['student'])
def get_public_playlists():
    user = g.user
    playlists = Playlist.query.filter_by(is_public=True).join(Course, PlaylistCourses.course_id == Course.id).filter(Course.branch_id == user['branch_id'])
    
    result = []
    for playlist in playlists:
        result.append({
            "id": playlist.id,
            "name": playlist.name,
            "owner": playlist.student_id,
            "course_count": len(playlist.courses)
        })
    
    return jsonify(result), 200

@app.route('/students/<int:student_id>/playlists', methods=['GET'])
@requires_role(['student'])
def get_student_playlists(student_id):
    user = g.user
    if student_id != user['user_id']:
        return jsonify({"error": "Unauthorized access"}), 403

    playlists = Playlist.query.filter_by(student_id=student_id)
    return jsonify([{
        "id": p.id,
        "name": p.name,
        "is_public": p.is_public,
        "course_count": len(p.courses)
    } for p in playlists]), 200

@app.route('/recommendations', methods=['GET'])
@requires_role(['student'])
def get_recommendations():
    user = g.user
    try:
        response = requests.get(
            f"http://analytics_service:3004/analytics/top_rated?branch_id={user['branch_id']}",
            timeout=3
        )
        popular_courses = response.json() if response.ok else []
        return jsonify({"recommended_courses": popular_courses[:5]})
    except requests.exceptions.RequestException as e:
        logger.error(f"Analytics service error: {e}")
        return jsonify({"recommended_courses": []})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3004)