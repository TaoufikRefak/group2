from datetime import datetime, timezone
from flask import Flask, jsonify, request, g
from flask_sqlalchemy import SQLAlchemy
import pika
import threading
import json
import logging
import time
from auth_lib import requires_role, decode_token

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///playlist.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database Models
class Playlist(db.Model):
    __tablename__ = 'playlists'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(120), nullable=False)
    student_id = db.Column(db.Integer, nullable=False)
    branch_id = db.Column(db.Integer, nullable=False)
    is_public = db.Column(db.Boolean, default=True)
    courses = db.relationship('PlaylistCourse', backref='playlist', cascade='all, delete-orphan')

class PlaylistCourse(db.Model):
    __tablename__ = 'playlist_courses'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    playlist_id = db.Column(db.Integer, db.ForeignKey('playlists.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.id'), nullable=False)

class Course(db.Model):
    __tablename__ = 'courses'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False)
    video_url = db.Column(db.String(255), nullable=False)
    branch_id = db.Column(db.Integer, nullable=False)

# Initialize database
with app.app_context():
    db.create_all()

# RabbitMQ Configuration
RABBITMQ_HOST = 'rabbitmq'

def publish_message(queue, message):
    try:
        connection = pika.BlockingConnection(pika.ConnectionParameters(RABBITMQ_HOST))
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
            connection = pika.BlockingConnection(pika.ConnectionParameters(RABBITMQ_HOST))
            channel = connection.channel()
            channel.queue_declare(queue='course_events', durable=True)

            def callback(ch, method, properties, body):
                try:
                    event = json.loads(body)
                    logger.info(f"Processing {event['event']} event")
                    
                    with app.app_context():
                        if event['event'] == 'COURSE_CREATED':
                            existing_course = Course.query.get(event['course_id'])
                            if existing_course:
                                existing_course.title = event['title']
                                existing_course.description = event.get('description', '')
                                existing_course.video_url = event['hls_url']
                                existing_course.branch_id = event['branch_id']
                            else:
                                new_course = Course(
                                    id=event['course_id'],
                                    title=event['title'],
                                    description=event.get('description', ''),
                                    video_url=event['hls_url'],
                                    branch_id=event['branch_id']
                                )
                                db.session.add(new_course)
                            db.session.commit()

                        elif event['event'] == 'COURSE_UPDATED':
                            course = Course.query.get(event['course_id'])
                            if course:
                                course.title = event['title']
                                course.description = event.get('description', '')
                                course.video_url = event['hls_url']
                                course.branch_id = event['branch_id']
                                db.session.commit()

                        elif event['event'] == 'COURSE_DELETED':
                            # Delete all playlist associations first
                            PlaylistCourse.query.filter_by(course_id=event['course_id']).delete()
                            # Delete the course itself
                            Course.query.filter_by(id=event['course_id']).delete()
                            db.session.commit()

                except Exception as e:
                    logger.error(f"Event processing error: {str(e)}")
                finally:
                    ch.basic_ack(delivery_tag=method.delivery_tag)

            channel.basic_consume(queue='course_events', on_message_callback=callback, auto_ack=False)
            channel.start_consuming()
        except Exception as e:
            logger.error(f"RabbitMQ connection error: {str(e)}")
            time.sleep(5)

threading.Thread(target=consume_course_events, daemon=True).start()

# API Endpoints
@app.route('/playlists', methods=['POST'])
@requires_role(['student'])
def create_playlist():
    data = request.get_json()
    if not data.get('name'):
        return jsonify({"error": "Playlist name required"}), 400

    try:
        playlist = Playlist(
            name=data['name'],
            student_id=g.user['user_id'],
            branch_id=g.user['branch_id'],
            is_public=data.get('is_public', True)
        )
        db.session.add(playlist)
        db.session.commit()
        return jsonify({"message": "Playlist created", "id": playlist.id}), 201
    except Exception as e:
        db.session.rollback()
        logger.error(f"Playlist creation error: {str(e)}")
        return jsonify({"error": "Playlist creation failed"}), 500

@app.route('/playlists/<int:playlist_id>/courses', methods=['POST'])
@requires_role(['student'])
def add_course_to_playlist(playlist_id):
    data = request.get_json()
    course_id = data.get('course_id')
    
    if not course_id:
        return jsonify({"error": "Course ID required"}), 400

    try:
        playlist = Playlist.query.filter_by(
            id=playlist_id,
            student_id=g.user['user_id']
        ).first_or_404()

        course = Course.query.filter_by(
            id=course_id,
            branch_id=g.user['branch_id']
        ).first_or_404()

        if PlaylistCourse.query.filter_by(
            playlist_id=playlist_id,
            course_id=course_id
        ).first():
            return jsonify({"error": "Course already in playlist"}), 400

        association = PlaylistCourse(playlist_id=playlist_id, course_id=course_id)
        db.session.add(association)
        db.session.commit()

        publish_message('user_interactions', {
            "event": "PLAYLIST_UPDATE",
            "user_id": g.user['user_id'],
            "playlist_id": playlist_id,
            "course_id": course_id,
            "action": "add",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        return jsonify({"message": "Course added to playlist"}), 201

    except Exception as e:
        db.session.rollback()
        logger.error(f"Course addition error: {str(e)}")
        return jsonify({"error": "Failed to add course"}), 500

@app.route('/playlists/<int:playlist_id>/courses/<int:course_id>', methods=['DELETE'])
@requires_role(['student'])
def remove_course_from_playlist(playlist_id, course_id):
    try:
        playlist = Playlist.query.filter_by(
            id=playlist_id,
            student_id=g.user['user_id']
        ).first_or_404()

        association = PlaylistCourse.query.filter_by(
            playlist_id=playlist_id,
            course_id=course_id
        ).first_or_404()

        db.session.delete(association)
        db.session.commit()

        publish_message('user_interactions', {
            "event": "PLAYLIST_UPDATE",
            "user_id": g.user['user_id'],
            "playlist_id": playlist_id,
            "course_id": course_id,
            "action": "remove",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        return jsonify({"message": "Course removed from playlist"}), 200

    except Exception as e:
        db.session.rollback()
        logger.error(f"Course removal error: {str(e)}")
        return jsonify({"error": "Failed to remove course"}), 500

@app.route('/playlists/<int:playlist_id>', methods=['PUT'])
@requires_role(['student'])
def update_playlist(playlist_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    try:
        playlist = Playlist.query.filter_by(
            id=playlist_id,
            student_id=g.user['user_id']
        ).first_or_404()

        if 'name' in data:
            new_name = data['name'].strip()
            if not new_name:
                return jsonify({"error": "Playlist name cannot be empty"}), 400
            playlist.name = new_name

        if 'is_public' in data:
            playlist.is_public = data['is_public']

        db.session.commit()
        return jsonify({"message": "Playlist updated"}), 200

    except Exception as e:
        db.session.rollback()
        logger.error(f"Playlist update error: {str(e)}")
        return jsonify({"error": "Failed to update playlist"}), 500

@app.route('/playlists/public', methods=['GET'])
@requires_role(['student'])
def get_public_playlists():
    try:
        playlists = Playlist.query.filter_by(
            is_public=True,
            branch_id=g.user['branch_id']
        ).all()

        return jsonify([{
            "id": p.id,
            "name": p.name,
            "owner": p.student_id,
            "course_count": len(p.courses)
        } for p in playlists]), 200

    except Exception as e:
        logger.error(f"Public playlists error: {str(e)}")
        return jsonify({"error": "Failed to retrieve playlists"}), 500

@app.route('/playlists/<int:playlist_id>', methods=['GET'])
@requires_role(['student'])
def get_playlist(playlist_id):
    try:
        playlist = Playlist.query.get_or_404(playlist_id)
        
        if playlist.student_id != g.user['user_id']:
            if not playlist.is_public:
                return jsonify({"error": "Unauthorized access"}), 403
            if playlist.branch_id != g.user['branch_id']:
                return jsonify({"error": "Unauthorized access"}), 403

        return jsonify({
            "id": playlist.id,
            "name": playlist.name,
            "is_public": playlist.is_public,
            "courses": [{
                "id": pc.course_id,
                "title": Course.query.get(pc.course_id).title
            } for pc in playlist.courses]
        }), 200

    except Exception as e:
        logger.error(f"Get playlist error: {str(e)}")
        return jsonify({"error": "Failed to retrieve playlist"}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Resource not found"}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3004)