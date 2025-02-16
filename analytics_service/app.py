from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
import pika
import threading
import json
import logging
import time
import requests
from collections import defaultdict
from sqlalchemy import func, and_

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
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
    action = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

with app.app_context():
    db.create_all()

# In-memory cache for frequent queries
cache = {}
CACHE_EXPIRATION = 300  # 5 minutes

# RabbitMQ Consumer with enhanced error handling
def consume_user_interactions():
    while True:
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(host='rabbitmq'))
            channel = connection.channel()
            channel.queue_declare(queue='user_interactions', durable=True)

            def callback(ch, method, properties, body):
                try:
                    with app.app_context():
                        event = json.loads(body)
                        logger.info(f"Processing event: {event['event']}")
                        
                        if event['event'] == 'COURSE_VIEWED':
                            course_view = CourseView(
                                course_id=event['course_id'],
                                student_id=event['student_id'],
                                timestamp=datetime.fromisoformat(event['timestamp'])
                            )
                            db.session.add(course_view)
                            
                        elif event['event'] == 'COURSE_RATED':
                            course_rating = CourseRating(
                                course_id=event['course_id'],
                                student_id=event['student_id'],
                                rating=event['rating'],
                                timestamp=datetime.fromisoformat(event['timestamp'])
                            )
                            db.session.add(course_rating)
                            
                        elif event['event'] == 'PLAYLIST_INTERACTION':
                            interaction = PlaylistInteraction(
                                playlist_id=event['playlist_id'],
                                student_id=event['student_id'],
                                action=event['action'],
                                timestamp=datetime.fromisoformat(event['timestamp'])
                            )
                            db.session.add(interaction)
                            
                        db.session.commit()
                        ch.basic_ack(delivery_tag=method.delivery_tag)
                        
                except Exception as e:
                    logger.error(f"Event processing failed: {e}")
                    db.session.rollback()

            channel.basic_consume(queue='user_interactions', 
                                on_message_callback=callback,
                                auto_ack=False)
            channel.start_consuming()
            
        except Exception as e:
            logger.error(f"Connection error: {e}")
            time.sleep(5)

# Enhanced Analytics Endpoints
@app.route('/analytics/course/<int:course_id>', methods=['GET'])
def get_course_analytics(course_id):
    # Basic stats
    views_count = CourseView.query.filter_by(course_id=course_id).count()
    ratings = CourseRating.query.filter_by(course_id=course_id).all()
    
    # Rating distribution
    rating_dist = db.session.query(
        CourseRating.rating,
        func.count(CourseRating.id)
    ).filter_by(course_id=course_id).group_by(CourseRating.rating).all()
    
    # View trends (last 30 days)
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    view_trend = db.session.query(
        func.date(CourseView.timestamp),
        func.count(CourseView.id)
    ).filter(and_(
        CourseView.course_id == course_id,
        CourseView.timestamp >= thirty_days_ago
    )).group_by(func.date(CourseView.timestamp)).all()
    
    return jsonify({
        "course_id": course_id,
        "views": {
            "total": views_count,
            "trend": [{"date": date.strftime("%Y-%m-%d"), "count": count} for date, count in view_trend]
        },
        "ratings": {
            "average": sum(r.rating for r in ratings)/len(ratings) if ratings else 0,
            "distribution": {str(rating): count for rating, count in rating_dist},
            "total": len(ratings)
        }
    })

@app.route('/analytics/recommendations/<int:student_id>', methods=['GET'])
def get_personalized_recommendations(student_id):
    try:
        # Get student's branch from user service
        student_info = requests.get(f"http://user_service:3001/users/{student_id}").json()
        branch_response = requests.get(
            f"http://user_service:3001/users/{student_id}/branch",
            headers={'Authorization': request.headers.get('Authorization')},
            timeout=3
        )
        branch_id = branch_response.json()['branch_id']
        # Get viewed courses
        viewed_courses = {v.course_id for v in CourseView.query.filter_by(student_id=student_id).all()}
        
        # Get popular courses in branch
        branch_popular = get_branch_popular_courses(branch_id)
        
        # Get highly rated courses
        top_rated = get_top_rated_courses()
        
        # Get frequently paired courses
        paired_courses = get_frequently_paired(viewed_courses)
        
        # Combine and prioritize recommendations
        recommendations = prioritize_recommendations(
            viewed_courses,
            branch_popular,
            top_rated,
            paired_courses
        )
        
        return jsonify({"recommendations": recommendations[:10]})
    
    except Exception as e:
        logger.error(f"Recommendation error: {e}")
        return jsonify({"recom.": get_fallback_recommendations()})

def get_fallback_recommendations():
    try:
        # Get top rated courses as fallback
        top_rated = db.session.query(
            CourseRating.course_id,
            func.avg(CourseRating.rating).label('avg_rating')
        ).group_by(CourseRating.course_id).having(
            func.count(CourseRating.id) >= 5
        ).order_by(func.avg(CourseRating.rating).desc()).limit(10).all()
        
        return [course_id for course_id, _ in top_rated]
    except Exception as e:
        logger.error(f"Fallback recommendations failed: {e}")
        return []

def get_branch_popular_courses(branch_id):
    # Get popular courses in the same branch from course service
    courses = requests.get(f"http://course_service:3002/courses?branch_id={branch_id}").json()
    course_ids = [c['id'] for c in courses]
    
    # Get view counts for these courses
    views = db.session.query(
        CourseView.course_id,
        func.count(CourseView.id).label('views')
    ).filter(CourseView.course_id.in_(course_ids)).group_by(CourseView.course_id).all()
    
    return sorted(views, key=lambda x: x[1], reverse=True)

def get_top_rated_courses():
    cache_key = 'top_rated'
    if cache_key in cache:
        return cache[cache_key]
    
    result = db.session.query(
        CourseRating.course_id,
        func.avg(CourseRating.rating).label('avg_rating'),
        func.count(CourseRating.id).label('rating_count')
    ).group_by(CourseRating.course_id).having(func.count(CourseRating.id) >= 5).order_by(
        func.avg(CourseRating.rating).desc()
    ).limit(50).all()
    
    cache[cache_key] = [r[0] for r in result]
    return cache[cache_key]

def get_frequently_paired(viewed_courses):
    if not viewed_courses:
        return []
        
    # Find courses frequently paired with viewed courses in playlists
    paired = db.session.query(
        PlaylistInteraction.playlist_id,
        func.group_concat(CourseRating.course_id)
    ).join(CourseRating, PlaylistInteraction.playlist_id == CourseRating.course_id).filter(
        CourseRating.course_id.in_(viewed_courses)
    ).group_by(PlaylistInteraction.playlist_id).all()
    
    course_counts = defaultdict(int)
    for playlist, courses in paired:
        for course in courses.split(','):
            if course not in viewed_courses:
                course_counts[course] += 1
                
    return sorted(course_counts.items(), key=lambda x: x[1], reverse=True)
def prioritize_recommendations(viewed, branch_popular, top_rated, paired):
    recommendations = []
    
    # Add paired courses with high frequency
    recommendations.extend([c for c, _ in paired[:5]])
    
    # Add branch popular not viewed
    for course_id, _ in branch_popular:
        if course_id not in viewed:
            recommendations.append(course_id)
            
    # Add top rated not viewed
    for course_id in top_rated:
        if course_id not in viewed:
            recommendations.append(course_id)
            
    return list(dict.fromkeys(recommendations))  # Remove duplicates

@app.route('/analytics/engagement', methods=['GET'])
def get_engagement_metrics():
    # Daily active users
    daily_active = db.session.query(
        func.date(CourseView.timestamp),
        func.count(func.distinct(CourseView.student_id))
    ).filter(CourseView.timestamp >= datetime.now(timezone.utc) - timedelta(days=7)).group_by(
        func.date(CourseView.timestamp)
    ).all()
    
    # Course completion rates (assuming completion events)
    # Add your completion tracking logic here
    
    return jsonify({
        "daily_active_users": [{"date": date.strftime("%Y-%m-%d"), "count": count} for date, count in daily_active],
        "weekly_engagement": {
            "total_views": CourseView.query.filter(
                CourseView.timestamp >= datetime.now(timezone.utc) - timedelta(days=7)
            ).count(),
            "total_ratings": CourseRating.query.filter(
                CourseRating.timestamp >= datetime.now(timezone.utc) - timedelta(days=7)
            ).count()
        }
    })

# Start RabbitMQ consumer
consumer_thread = threading.Thread(target=consume_user_interactions)
consumer_thread.daemon = True
consumer_thread.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3005)