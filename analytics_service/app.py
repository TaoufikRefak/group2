from datetime import datetime, timezone
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
import pika
import threading
import json
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///analytics.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database models
class CourseView(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    course_id = db.Column(db.Integer, nullable=False)
    student_id = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class CourseRating(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    course_id = db.Column(db.Integer, nullable=False)
    student_id = db.Column(db.Integer, nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class PlaylistInteraction(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    playlist_id = db.Column(db.Integer, nullable=False)
    student_id = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(50), nullable=False)  # e.g., 'added', 'removed'
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

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

# RabbitMQ Consumer for user and course interactions (views, ratings, playlist interactions)
# Ensure your consumer has proper error handling
# Update the consumer with better error handling
def consume_user_interactions():
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
                        logger.info(f"Processing event: {event}")
                        
                        # Your existing event handling code
                        if event['event'] == 'COURSE_VIEWED':
                            course_view = CourseView(
                                course_id=event['course_id'],
                                student_id=event['student_id'],
                                timestamp=datetime.fromisoformat(event.get('timestamp'))
                            )
                            db.session.add(course_view)
                            db.session.commit()  # Now works inside context
                            logger.info(f"Recorded view for course {event['course_id']}")
                            
                        # Other event handlers...
                        
                except Exception as e:
                    logger.error(f"Processing failed: {e}")
                finally:
                    ch.basic_ack(delivery_tag=method.delivery_tag)

            channel.basic_consume(queue='user_interactions', 
                                on_message_callback=callback,
                                auto_ack=False)
            channel.start_consuming()
        except Exception as e:
            logger.error(f"Connection error: {e}")
            time.sleep(5)
@app.route('/analytics/course_views/<int:course_id>', methods=['GET'])
def get_course_views(course_id):
    views_count = CourseView.query.filter_by(course_id=course_id).count()
    return jsonify({"course_id": course_id, "views_count": views_count})

# Endpoint to fetch average course rating
@app.route('/analytics/course_rating/<int:course_id>', methods=['GET'])
def get_course_rating(course_id):
    ratings = CourseRating.query.filter_by(course_id=course_id).all()
    if ratings:
        average_rating = sum([rating.rating for rating in ratings]) / len(ratings)
        return jsonify({"course_id": course_id, "average_rating": average_rating})
    else:
        return jsonify({"course_id": course_id, "average_rating": "No ratings yet"})

# Endpoint to fetch playlist interaction statistics
@app.route('/analytics/playlist_interactions/<int:playlist_id>', methods=['GET'])
def get_playlist_interactions(playlist_id):
    interactions = PlaylistInteraction.query.filter_by(playlist_id=playlist_id).all()
    return jsonify([
        {
            "id": interaction.id,
            "student_id": interaction.student_id,
            "action": interaction.action,
            "timestamp": interaction.timestamp
        }
        for interaction in interactions
    ])

# Endpoint to fetch general statistics
@app.route('/analytics/statistics', methods=['GET'])
def get_statistics():
    total_courses = db.session.query(CourseView.course_id).distinct().count()
    total_views = db.session.query(CourseView.id).count()
    total_ratings = db.session.query(CourseRating.id).count()

    return jsonify({
        "total_courses": total_courses,
        "total_views": total_views,
        "total_ratings": total_ratings
    })


# In get_recommendations endpoint
def get_recommendations(student_id):
    try:
        response = request.get(f"http://analytics_service:3004/analytics/course_rating")
        popular_courses = response.json()['top_rated']
        
        return jsonify({
            "recommended_courses": popular_courses[:5]
        })
    except Exception as e:
        logger.error(f"Recommendation failed: {e}")
        return jsonify({"recommended_courses": []})
    
# New endpoint
@app.route('/analytics/top_rated', methods=['GET'])
def get_top_rated():
    ratings = db.session.query(
        CourseRating.course_id,
        db.func.avg(CourseRating.rating).label('average_rating')
    ).group_by(CourseRating.course_id).order_by(db.desc('average_rating')).limit(10).all()
    
    return jsonify([{
        "course_id": r[0],
        "average_rating": float(r[1])
    } for r in ratings])


# Start RabbitMQ consumer in a separate thread
consumer_thread = threading.Thread(target=consume_user_interactions)
consumer_thread.daemon = True
consumer_thread.start()

# Endpoint to fetch course views count

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3005)
