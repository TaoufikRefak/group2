from datetime import datetime, timezone
import time
from flask import Flask, jsonify, request, g
from flask_sqlalchemy import SQLAlchemy
import pika
import threading
import json
import logging
import os
import uuid
from werkzeug.utils import secure_filename
from auth_lib import requires_role, decode_token
from threading import Lock

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///course.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = '/app/videos'
app.config['ALLOWED_EXTENSIONS'] = {'mp4', 'mov', 'avi', 'mkv'}
app.config['HLS_OUTPUT'] = '/app/hls'

db = SQLAlchemy(app)

class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False)
    teacher_id = db.Column(db.Integer, nullable=False)
    branch_id = db.Column(db.Integer, nullable=False)
    video_filename = db.Column(db.String(255), nullable=False)
    hls_playlist = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), 
                         onupdate=lambda: datetime.now(timezone.utc))

    def serialize(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "teacher_id": self.teacher_id,
            "branch_id": self.branch_id,
            "hls_url": f"{os.environ['HLS_BASE_URL']}/{self.video_filename}/playlist.m3u8",
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat()
        }

with app.app_context():
    db.create_all()

# RabbitMQ setup
connection = None
channel = None
lock = Lock()

def init_rabbitmq():
    global connection, channel
    with lock:
        if not connection or connection.is_closed:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters('rabbitmq')
            )
            channel = connection.channel()
            channel.queue_declare(queue='user_interactions', durable=True)

def publish_message(queue, message):
    try:
        init_rabbitmq()
        with lock:
            channel.basic_publish(
                exchange='',
                routing_key=queue,
                body=json.dumps(message),
                properties=pika.BasicProperties(delivery_mode=2)
            )
    except Exception as e:
        logger.error(f"RabbitMQ error: {e}")
        init_rabbitmq()

# Helper functions
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def convert_to_hls(input_path, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    output_playlist = os.path.join(output_dir, "playlist.m3u8")
    os.system(f"ffmpeg -i {input_path} -codec: copy -start_number 0 -hls_time 10 -hls_list_size 0 -f hls {output_playlist}")
    return f"courses/{os.path.basename(output_dir)}/playlist.m3u8"

# Course Endpoints
@app.route('/courses', methods=['POST'])
@requires_role(['teacher', 'admin'])
def create_course():
    try:
        user = g.user
        if user['role'] == 'teacher' and int(request.form.get('branch_id')) != user['branch_id']:
            return jsonify({"error": "Cannot create course in other branches"}), 403

        title = request.form.get('title')
        description = request.form.get('description')
        teacher_id = user['user_id'] if user['role'] == 'teacher' else request.form.get('teacher_id')
        branch_id = user['branch_id'] if user['role'] == 'teacher' else request.form.get('branch_id')
        video = request.files.get('video')

        if not all([title, description, teacher_id, branch_id]) or not video:
            return jsonify({"error": "All fields are required"}), 400

        if not allowed_file(video.filename):
            return jsonify({"error": "Invalid file type"}), 400

        filename = f"{uuid.uuid4()}_{secure_filename(video.filename)}"
        video_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        video.save(video_path)

        hls_output_dir = os.path.join(app.config['HLS_OUTPUT'], filename)
        hls_playlist = convert_to_hls(video_path, hls_output_dir)

        new_course = Course(
            title=title,
            description=description,
            teacher_id=teacher_id,
            branch_id=branch_id,
            video_filename=filename,
            hls_playlist=hls_playlist
        )
        db.session.add(new_course)
        db.session.commit()

        publish_message('course_events', {
            "event": "COURSE_CREATED",
            "course_id": new_course.id,
            "teacher_id": teacher_id,
            "branch_id": branch_id,
            "hls_url": f"{os.environ['HLS_BASE_URL']}/{filename}/playlist.m3u8"
        })

        return jsonify(new_course.serialize()), 201

    except Exception as e:
        logger.error(f"Course creation failed: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/courses', methods=['GET'])
@requires_role(['student', 'teacher', 'admin'])
def get_courses():
    try:
        user = g.user
        query = Course.query
        
        if user['role'] == 'student':
            query = query.filter_by(branch_id=user['branch_id'])
        elif user['role'] == 'teacher':
            query = query.filter_by(branch_id=user['branch_id'])
        
        if 'branch_id' in request.args:
            if user['role'] in ['admin']:
                query = query.filter_by(branch_id=request.args.get('branch_id'))
        
        courses = query.all()
        return jsonify([c.serialize() for c in courses]), 200
    except Exception as e:
        logger.error(f"Failed to fetch courses: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/courses/<int:course_id>', methods=['GET'])
@requires_role(['student', 'teacher', 'admin'])
def get_course(course_id):
    try:
        course = Course.query.get_or_404(course_id)
        user = g.user
        
        if user['role'] == 'student' and course.branch_id != user['branch_id']:
            return jsonify({"error": "Unauthorized access"}), 403
            
        return jsonify(course.serialize()), 200
    except Exception as e:
        logger.error(f"Failed to fetch course: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/courses/<int:course_id>', methods=['PUT'])
@requires_role(['teacher', 'admin'])
def update_course(course_id):
    try:
        user = g.user
        course = Course.query.get_or_404(course_id)
        
        if user['role'] == 'teacher' and (course.teacher_id != user['user_id'] or course.branch_id != user['branch_id']):
            return jsonify({"error": "Unauthorized to update this course"}), 403

        data = request.form
        if 'title' in data:
            course.title = data['title']
        if 'description' in data:
            course.description = data['description']
        
        db.session.commit()
        return jsonify(course.serialize()), 200
    except Exception as e:
        logger.error(f"Course update failed: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/courses/<int:course_id>', methods=['DELETE'])
@requires_role(['teacher', 'admin'])
def delete_course(course_id):
    try:
        user = g.user
        course = Course.query.get_or_404(course_id)
        
        if user['role'] == 'teacher' and (course.teacher_id != user['user_id'] or course.branch_id != user['branch_id']):
            return jsonify({"error": "Unauthorized to delete this course"}), 403

        db.session.delete(course)
        db.session.commit()
        return jsonify({"message": "Course deleted successfully"}), 200
    except Exception as e:
        logger.error(f"Course deletion failed: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/teachers/<int:teacher_id>/courses', methods=['GET'])
@requires_role(['student', 'teacher', 'admin'])
def get_teacher_courses(teacher_id):
    try:
        user = g.user
        courses = Course.query.filter_by(teacher_id=teacher_id)
        
        if user['role'] == 'student':
            courses = courses.filter_by(branch_id=user['branch_id'])
        elif user['role'] == 'teacher':
            courses = courses.filter_by(branch_id=user['branch_id'])
            
        return jsonify([c.serialize() for c in courses]), 200
    except Exception as e:
        logger.error(f"Failed to fetch teacher courses: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/branches/<int:branch_id>/courses', methods=['GET'])
@requires_role(['admin', 'teacher', 'student'])
def get_branch_courses(branch_id):
    try:
        user = g.user
        if user['role'] == 'student' and branch_id != user['branch_id']:
            return jsonify({"error": "Unauthorized branch access"}), 403

        courses = Course.query.filter_by(branch_id=branch_id)
        return jsonify([c.serialize() for c in courses]), 200
    except Exception as e:
        logger.error(f"Failed to fetch branch courses: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/courses/<int:course_id>/view', methods=['POST'])
@requires_role(['student'])
def track_course_view(course_id):
    try:
        user = g.user
        course = Course.query.get_or_404(course_id)
        
        if course.branch_id != user['branch_id']:
            return jsonify({"error": "Course not available in your branch"}), 403

        publish_message('user_interactions', {
            "event": "COURSE_VIEWED",
            "course_id": course_id,
            "student_id": user['user_id'],
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        return jsonify({"message": "View tracked"}), 200
    except Exception as e:
        logger.error(f"View tracking failed: {e}")
        return jsonify({"error": "Internal server error"}), 500

# RabbitMQ Consumer
def consume_course_events():
    while True:
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(host='rabbitmq'))
            channel = connection.channel()
            channel.queue_declare(queue='course_events', durable=True)

            def callback(ch, method, properties, body):
                event = json.loads(body)
                logger.info(f"Processing event: {event['event']}")

            channel.basic_consume(queue='course_events', on_message_callback=callback, auto_ack=True)
            channel.start_consuming()
        except Exception as e:
            logger.error(f"RabbitMQ connection error: {e}")
            time.sleep(5)

threading.Thread(target=consume_course_events, daemon=True).start()

if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['HLS_OUTPUT'], exist_ok=True)
    app.run(host='0.0.0.0', port=3002)