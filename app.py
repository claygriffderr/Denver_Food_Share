import os
import uuid
import boto3
import math
from geopy.geocoders import Nominatim
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone

# Load variables from the .env file
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'a_very_secret_key_for_testing'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///foodshare.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# -------------------------------------------------------------
# CLOUDFLARE R2 SETUP (Module 5)
# -------------------------------------------------------------
# We use boto3 to connect to Cloudflare's S3-compatible API
s3 = boto3.client(
    's3',
    endpoint_url=os.getenv('R2_ENDPOINT_URL'),
    aws_access_key_id=os.getenv('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('R2_SECRET_ACCESS_KEY')
)
R2_BUCKET_NAME = os.getenv('R2_BUCKET_NAME')
R2_PUBLIC_CUSTOM_DOMAIN = os.getenv('R2_PUBLIC_CUSTOM_DOMAIN')

# -------------------------------------------------------------
# LOGIN MANAGER SETUP (Module 2)
# -------------------------------------------------------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# -------------------------------------------------------------
# DATABASE MODELS (Module 3 & 5)
# -------------------------------------------------------------
class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), nullable=False, unique=True)
    email_encrypted = db.Column(db.String(255), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)

class Post(db.Model):
    __tablename__ = 'posts'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    status = db.Column(db.String(20), default='active')
    image_url = db.Column(db.String(255), nullable=True)
    uploader_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    # NEW MODULAR CATEGORIES (Replacing the old is_refrigerated column)
    is_vegetarian = db.Column(db.Boolean, default=False)
    is_vegan = db.Column(db.Boolean, default=False)
    prep_type = db.Column(db.String(30), default='homemade') # homemade, store_bought, groceries
    is_frozen = db.Column(db.Boolean, default=False)

    uploader = db.relationship('User', backref='posts')

class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_read = db.Column(db.Boolean, default=False)
    
    sender = db.relationship('User', foreign_keys=[sender_id], backref='sent_messages')
    receiver = db.relationship('User', foreign_keys=[receiver_id], backref='received_messages')

# -------------------------------------------------------------
# GEOSPATIAL LOGIC (Module 6)
# -------------------------------------------------------------

def calculate_distance(lat1, lon1, lat2, lon2):
    # Radius of the earth in miles. (Use 6371 for kilometers)
    R = 3958.8 
    
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    distance = R * c
    return distance

# -------------------------------------------------------------
# CORE ROUTING (Modules 1, 2, 4, 5, 7)
# -------------------------------------------------------------

@app.route('/')
def home():
    # 1. Fetch all active posts from the database
    all_active_posts = Post.query.filter_by(status='active').order_by(Post.timestamp.desc()).all()
    
    # 2. If the user is a guest (not logged in), just show them everything
    if not current_user.is_authenticated or not current_user.latitude:
        for post in all_active_posts:
            post.distance = None # Default value so the HTML doesn't crash
        return render_template('index.html', posts=all_active_posts)

    # 3. If they are logged in, filter the posts by distance
    SEARCH_RADIUS_MILES = 15
    nearby_posts = []
    
    for post in all_active_posts:
        # Check if the person who posted the food has coordinates saved
        if post.uploader.latitude and post.uploader.longitude:
            # Run our Haversine math!
            dist = calculate_distance(
                current_user.latitude, current_user.longitude,
                post.uploader.latitude, post.uploader.longitude
            )
            
            # If the food is within our radius, keep it and attach the distance
            if dist <= SEARCH_RADIUS_MILES:
                post.distance = round(dist, 1) # Round to 1 decimal place (e.g., 3.2 miles)
                nearby_posts.append(post)
        else:
            # If the uploader is an older test account without a zip code, keep it but mark distance unknown
            post.distance = None
            nearby_posts.append(post)

    # Optional: Sort the feed so the closest food is at the very top
    nearby_posts.sort(key=lambda x: x.distance if x.distance is not None else float('inf'))

    return render_template('index.html', posts=nearby_posts)

@app.route('/create_post', methods=['GET', 'POST'])
@login_required
def create_post():
    if request.method == 'POST':
        title_input = request.form['title']
        desc_input = request.form['description']
        
        # Capture our new boolean tags
        is_vegetarian_input = 'is_vegetarian' in request.form
        is_vegan_input = 'is_vegan' in request.form
        is_frozen_input = 'is_frozen' in request.form
        
        # Capture our radio selection string
        prep_type_input = request.form.get('prep_type', 'homemade')
        
        # Handle File Upload
        file = request.files.get('image')
        unique_filename = None
        if file and file.filename != '':
            ext = os.path.splitext(file.filename)[1]
            unique_filename = f"{uuid.uuid4().hex}{ext}"
            try:
                s3.upload_fileobj(file, R2_BUCKET_NAME, unique_filename)
            except Exception as e:
                print(f"Upload Failed: {e}")
                return "<h1>Upload Error</h1><p>Could not send image to Cloudflare R2.</p>"

        # Create the post with the new modular parameters
        new_post = Post(
            title=title_input,
            description=desc_input,
            is_vegetarian=is_vegetarian_input,
            is_vegan=is_vegan_input,
            prep_type=prep_type_input,
            is_frozen=is_frozen_input,
            image_url=unique_filename,
            uploader_id=current_user.id
        )
        
        db.session.add(new_post)
        db.session.commit()
        
        return redirect(url_for('home'))
        
    return render_template('create_post.html')

@app.route('/delete_post/<int:post_id>')
@login_required
def delete_post(post_id):
    post = Post.query.get_or_404(post_id)
    
    # Security Check: Only the owner can delete it
    if post.uploader_id == current_user.id:
        post.status = 'deleted' # Soft delete!
        db.session.commit()
        
    return redirect(url_for('home'))

@app.route('/edit_post/<int:post_id>', methods=['GET', 'POST'])
@login_required
def edit_post(post_id):
    post = Post.query.get_or_404(post_id)
    
    # Security Check: Only the owner can edit it
    if post.uploader_id != current_user.id:
        return redirect(url_for('home'))
        
    if request.method == 'POST':
        # Update text fields
        post.title = request.form['title']
        post.description = request.form['description']
        
        # Update boolean tags
        post.is_vegetarian = 'is_vegetarian' in request.form
        post.is_vegan = 'is_vegan' in request.form
        post.is_frozen = 'is_frozen' in request.form
        
        # Update radio string
        post.prep_type = request.form.get('prep_type', 'homemade')
        
        # Optional Image Update (Only upload if they provided a new file)
        file = request.files.get('image')
        if file and file.filename != '':
            ext = os.path.splitext(file.filename)[1]
            unique_filename = f"{uuid.uuid4().hex}{ext}"
            try:
                s3.upload_fileobj(file, R2_BUCKET_NAME, unique_filename)
                post.image_url = unique_filename
            except Exception as e:
                print(f"Upload Failed: {e}")
                
        db.session.commit()
        return redirect(url_for('home'))
        
    # If it's a GET request, show them the edit form
    return render_template('edit_post.html', post=post)


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None # Create an empty error variable to send to the HTML
    
    if request.method == 'POST':
        username_input = request.form['username']
        email_input = request.form['email']
        password_input = request.form['password']
        zip_code_input = request.form['zip_code']
        
        hashed_pw = generate_password_hash(password_input)
        
        lat, lon = None, None
        try:
            geolocator = Nominatim(user_agent="DenverFoodShareApp")
            location = geolocator.geocode(f"{zip_code_input}, USA") 
            if location:
                lat = location.latitude
                lon = location.longitude
        except Exception as e:
            print(f"Geocoding error: {e}")
        
        new_user = User(
            username=username_input, 
            email_encrypted=email_input, 
            password_hash=hashed_pw,
            latitude=lat,
            longitude=lon
        )
        
        # We wrap the database save in a try/except block
        try:
            db.session.add(new_user)
            db.session.commit()
            login_user(new_user)
            return redirect(url_for('home'))
        except IntegrityError:
            # If the database panics (duplicate user/email), undo the action!
            db.session.rollback()
            # Set the error message to display on the frontend
            error = "Username or Email already exists. Please choose another."
            
    # We pass the error variable to the template so it can display it
    return render_template('register.html', error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email_input = request.form['email']
        password_input = request.form['password']
        user = User.query.filter_by(email_encrypted=email_input).first()
        if user and check_password_hash(user.password_hash, password_input):
            login_user(user)
            return redirect(url_for('home'))
        else:
            return "<h1>Error</h1><p>Invalid email or password.</p>"
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

@app.route('/inbox')
@login_required
def inbox():
    my_messages = Message.query.filter_by(receiver_id=current_user.id).order_by(Message.timestamp.desc()).all()
    return render_template('inbox.html', messages=my_messages)

@app.route('/conversation/<int:other_user_id>', methods=['GET', 'POST'])
@login_required
def conversation(other_user_id):
    # Prevent messaging yourself
    if other_user_id == current_user.id:
        return redirect(url_for('inbox'))
        
    other_user = User.query.get_or_404(other_user_id)
    
    # 1. Handle sending a new message in the thread
    if request.method == 'POST':
        message_content = request.form['content']
        new_message = Message(
            sender_id=current_user.id,
            receiver_id=other_user.id,
            content=message_content
        )
        db.session.add(new_message)
        db.session.commit()
        # Refresh the page to show the new message
        return redirect(url_for('conversation', other_user_id=other_user.id))
        
    # 2. Fetch the entire chat history between these two specific users
    chat_history = Message.query.filter(
        or_(
            (Message.sender_id == current_user.id) & (Message.receiver_id == other_user.id),
            (Message.sender_id == other_user.id) & (Message.receiver_id == current_user.id)
        )
    ).order_by(Message.timestamp.asc()).all() # .asc() puts the oldest messages at the top
    
    # 3. Mark any unread messages from them as "read" now that we are looking at the thread
    for msg in chat_history:
        if msg.receiver_id == current_user.id and not msg.is_read:
            msg.is_read = True
    db.session.commit()
    
    return render_template('conversation.html', messages=chat_history, other_user=other_user)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)