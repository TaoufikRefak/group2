from datetime import datetime
from flask import Flask, jsonify, request, g
from flask_sqlalchemy import SQLAlchemy
import os
import uuid
import logging
import subprocess
from auth_lib import requires_role
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///streaming.db'
app.config['UPLOAD_FOLDER'] = '/tmp/hls'
app.config['ALLOWED_EXTENSIONS'] = {'mp4', 'mov', 'avi', 'mkv'}
db = SQLAlchemy(app)

# Environment configuration
MIST_RTMP_URL = os.getenv('MIST_RTMP_URL', 'rtmp://nginx_rtmp/live')
CLIENT_HLS_BASE = os.getenv('CLIENT_HLS_BASE', 'http://nginx_gateway/hls')

class StreamingSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    stream_id = db.Column(db.String(255), unique=True)
    course_id = db.Column(db.Integer)
    user_id = db.Column(db.Integer)
    hls_url = db.Column(db.String(512))
    last_position = db.Column(db.Integer, default=0)
    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    end_time = db.Column(db.DateTime)
    status = db.Column(db.String(20), default='created')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def generate_hls(video_path, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    
    ffmpeg_command = [
        'ffmpeg',
        '-i', video_path,
        '-profile:v', 'baseline',
        '-level', '3.0',
        '-s', '640x360',
        '-start_number', '0',
        '-hls_time', '10',
        '-hls_list_size', '0',
        '-f', 'hls',
        f'{output_dir}/index.m3u8'
    ]
    
    try:
        subprocess.run(ffmpeg_command, check=True, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        os.chmod(output_dir, 0o777)  # Ensure proper permissions
        return True
    except subprocess.CalledProcessError as e:
        app.logger.error(f"FFmpeg failed: {e.stderr.decode()}")
        return False

@app.route('/api/stream/start', methods=['POST'])
@requires_role(['teacher', 'admin'])
def start_stream():
    try:
        user = g.user
        course_id = request.json['course_id']
        stream_key = f"live_{course_id}_{uuid.uuid4().hex[:8]}"
        
        # Create output directory for HLS
        output_dir = os.path.join(app.config['UPLOAD_FOLDER'], stream_key)
        os.makedirs(output_dir, exist_ok=True)
        os.chmod(output_dir, 0o777)

        ingest_url = f"{MIST_RTMP_URL}/{stream_key}"
        hls_url = f"{CLIENT_HLS_BASE}/{stream_key}/index.m3u8"

        session = StreamingSession(
            stream_id=stream_key,
            course_id=course_id,
            user_id=user['user_id'],
            hls_url=hls_url,
            status='active'
        )
        db.session.add(session)
        db.session.commit()

        return jsonify({
            "ingest_url": ingest_url,
            "hls_url": hls_url,
            "stream_id": stream_key
        }), 200

    except Exception as e:
        app.logger.error(f"Stream start failed: {str(e)}")
        return jsonify({"error": "Stream initialization failed"}), 500

@app.route('/api/stream/upload', methods=['POST'])
@requires_role(['teacher', 'admin'])
def upload_vod():
    try:
        user = g.user
        course_id = request.form['course_id']
        video_file = request.files['video']
        
        if not video_file or not allowed_file(video_file.filename):
            return jsonify({"error": "Invalid video file"}), 400

        stream_key = f"vod_{course_id}_{uuid.uuid4().hex[:8]}"
        output_dir = os.path.join(app.config['UPLOAD_FOLDER'], stream_key)
        os.makedirs(output_dir, exist_ok=True)
        
        # Save original file
        filename = secure_filename(video_file.filename)
        video_path = os.path.join(output_dir, filename)
        video_file.save(video_path)
        
        # Generate HLS
        if not generate_hls(video_path, output_dir):
            return jsonify({"error": "Failed to process video"}), 500

        hls_url = f"{CLIENT_HLS_BASE}/{stream_key}/index.m3u8"

        session = StreamingSession(
            stream_id=stream_key,
            course_id=course_id,
            user_id=user['user_id'],
            hls_url=hls_url,
            status='ready'
        )
        db.session.add(session)
        db.session.commit()

        return jsonify({
            "hls_url": hls_url,
            "stream_id": stream_key
        }), 200

    except Exception as e:
        app.logger.error(f"VOD upload failed: {str(e)}")
        return jsonify({"error": "VOD processing failed"}), 500

# ... keep other endpoints the same ...


@app.route('/api/stream/stop/<string:stream_id>', methods=['POST'])
@requires_role(['teacher', 'admin'])
def stop_stream(stream_id):
    try:
        session = StreamingSession.query.filter_by(stream_id=stream_id).first()
        if not session:
            return jsonify({"error": "Stream not found"}), 404

        session.status = 'ended'
        session.end_time = datetime.utcnow()
        db.session.commit()

        return jsonify({"message": "Stream stopped"}), 200

    except Exception as e:
        app.logger.error(f"Stream stop failed: {str(e)}")
        return jsonify({"error": "Stream termination failed"}), 500

@app.route('/api/progress', methods=['POST'])
@requires_role(['student'])
def save_progress():
    try:
        data = request.json
        session = StreamingSession.query.filter_by(
            course_id=data['course_id'],
            user_id=g.user['user_id'],
            status='active'
        ).first()

        if session:
            session.last_position = data['position']
            db.session.commit()

        return jsonify({"message": "Progress saved"}), 200

    except Exception as e:
        app.logger.error(f"Progress save failed: {str(e)}")
        return jsonify({"error": "Progress update failed"}), 500


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.chmod(app.config['UPLOAD_FOLDER'], 0o777)  # Ensure writable
    app.run(host='0.0.0.0', port=3010)