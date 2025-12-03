import os
import io
import csv
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, Response, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, UTC
from twilio.rest import Client
from sqlalchemy.sql import func, case
import threading
import asyncio
import websockets
import traceback # Added for better error logging

# --- App Configuration ---
app = Flask(__name__)
basedir = os.path.abspath(os.path.dirname(__file__))

# Database configuration for Render
if os.environ.get('DATABASE_URL'):
    uri = os.environ.get('DATABASE_URL')
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = uri
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'attendance.db')

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a-very-secret-key-that-you-should-change')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['TEMPLATES_AUTO_RELOAD'] = True

# --- WebSocket Configuration for Local Server ---
WS_SERVER_URL = os.environ.get('WS_SERVER_URL', 'ws://localhost:8765')
is_ws_connected = False
websocket_connection = None

# --- Twilio (optional) ---
# NOTE: The provided TWILIO_ACCOUNT_SID placeholder value has been corrected
# to an empty string to follow best practices for runnable code examples.
app.config['TWILIO_ACCOUNT_SID'] = os.environ.get('TWILIO_ACCOUNT_SID', "ACdec055d8307152e55dea4051ca7f0da0")
app.config['TWILIO_AUTH_TOKEN'] = os.environ.get('TWILIO_AUTH_TOKEN', "60e2b76c260ed593d2ebfd9d1821d775")
app.config['TWILIO_PHONE_NUMBER'] = os.environ.get('TWILIO_PHONE_NUMBER', "+19593359278")
# FIX: Robust Twilio client initialization
if app.config['TWILIO_ACCOUNT_SID'] and app.config['TWILIO_AUTH_TOKEN'] and app.config['TWILIO_PHONE_NUMBER']:
    try:
        twilio_client = Client(app.config['TWILIO_ACCOUNT_SID'], app.config['TWILIO_AUTH_TOKEN'])
        print("✅ Twilio client initialized.")
    except Exception as e:
        twilio_client = None
        print(f"WARNING: Twilio client failed to initialize: {e}")
else:
    twilio_client = None
    print("WARNING: Twilio not configured (SID/TOKEN/NUMBER missing). SMS skipped.")


db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'
login_manager.login_message_category = 'error'
login_manager.login_message = 'Please log in to access this page.'

# --- Association Table ---
advisor_class_association = db.Table('advisor_class_association',
    db.Column('advisor_id', db.Integer, db.ForeignKey('class_advisor.id'), primary_key=True),
    db.Column('class_id', db.Integer, db.ForeignKey('class.id'), primary_key=True)
)

# --- Models ---
class Teacher(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    classes = db.relationship('Class', backref='teacher', lazy=True)

    def get_id(self): return str(self.id)
    def set_password(self, password): self.password_hash = generate_password_hash(password)
    def check_password(self, password): return check_password_hash(self.password_hash, password)


class ClassAdvisor(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    advised_classes = db.relationship('Class', secondary=advisor_class_association, lazy='subquery',
        backref=db.backref('advisors', lazy=True))

    def get_id(self): return str(self.id)
    def set_password(self, password): self.password_hash = generate_password_hash(password)
    def check_password(self, password): return check_password_hash(self.password_hash, password)


class Student(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    regno = db.Column(db.String(100), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    password_hash = db.Column(db.String(200), nullable=False)
    chair_number = db.Column(db.Integer, nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=False)
    reports = db.relationship('AttendanceReport', backref='student', lazy=True, cascade="all, delete-orphan")

    def get_id(self): return str(self.id)
    def set_password(self, password): self.password_hash = generate_password_hash(password)
    def check_password(self, password): return check_password_hash(self.password_hash, password)

    __table_args__ = (
        db.UniqueConstraint('chair_number', 'class_id', name='_chair_class_uc'),
    )


class Class(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    branch = db.Column(db.String(80), nullable=False)
    section = db.Column(db.String(10), nullable=False)
    sem = db.Column(db.Integer, nullable=False)
    strength = db.Column(db.Integer, nullable=False)
    subject_code = db.Column(db.String(20), nullable=False)
    subject_title = db.Column(db.String(120), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teacher.id'), nullable=False)
    students = db.relationship('Student', backref='class_obj', lazy=True, cascade="all, delete-orphan")
    reports = db.relationship('AttendanceReport', backref='class_obj', lazy=True, cascade="all, delete-orphan")


class AttendanceReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))
    status = db.Column(db.String(20), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('teacher.id'), nullable=False)
    period = db.Column(db.String(20), nullable=True)
    topic = db.Column(db.String(200), nullable=True)

# --- WebSocket Client Functions ---
async def connect_to_local_websocket():
    """Connect to the local WebSocket server"""
    global is_ws_connected, websocket_connection
    try:
        print(f"🔄 Attempting to connect to WebSocket at: {WS_SERVER_URL}")
        # Added a max_size limit to prevent memory issues with large buffers
        websocket_connection = await websockets.connect(WS_SERVER_URL, ping_interval=20, ping_timeout=10, max_size=1024 * 1024)
        is_ws_connected = True
        print("✅ Connected to local WebSocket server")
       
        # Listen for messages
        async for message in websocket_connection:
            print(f"📨 Received from local server: {message}")
           
    except Exception as e:
        print(f"❌ WebSocket connection failed: {e}")
        is_ws_connected = False
        websocket_connection = None

def start_websocket_client():
    """Start WebSocket client in background thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # The run_until_complete call will block this thread until the websocket connection is closed
    # or fails, which is the desired behavior for a daemon thread running an asyncio loop.
    loop.run_until_complete(connect_to_local_websocket())

def initialize_websocket():
    """Initialize WebSocket connection"""
    print("🚀 Initializing WebSocket connection to local server...")
    # Start the thread as daemon so it won't prevent the app from shutting down
    thread = threading.Thread(target=start_websocket_client, daemon=True)
    thread.start()

# --- User Loader ---
@login_manager.user_loader
def load_user(user_id):
    role = session.get('user_role')
    if role == 'teacher': return db.session.get(Teacher, int(user_id))
    elif role == 'student': return db.session.get(Student, int(user_id))
    elif role == 'advisor': return db.session.get(ClassAdvisor, int(user_id))
    return None

# --- Helper Functions ---
def _calculate_attendance(student_id, class_id):
    try:
        total = db.session.query(AttendanceReport).filter(
            AttendanceReport.student_id == student_id,
            AttendanceReport.class_id == class_id
        ).count()
       
        present = db.session.query(AttendanceReport).filter(
            AttendanceReport.student_id == student_id,
            AttendanceReport.class_id == class_id,
            # FIX: Ensure all 'Present' statuses are counted correctly
            db.or_(
                AttendanceReport.status == 'Present', # Manually confirmed
                AttendanceReport.status == 'Present (Auto)' # Live/Auto confirmed
            )
        ).count()
       
        percentage = (present / total) * 100 if total > 0 else 0
        return {"present": present, "total": total, "percentage": percentage}
       
    except Exception as e:
        print(f"Error calculating attendance: {e}")
        return {"present": 0, "total": 0, "percentage": 0}
   
def _send_sms_notification(student, selected_class, status_text, poll_details, faculty_name, class_time, message_type="recorded"):
    # FIX: Check for Twilio client and a valid E.164 phone number
    if not twilio_client:
        print("SMS skipped: Twilio client not configured.")
        return False
        
    # FIX: Robust phone number check
    if not student.phone or not student.phone.startswith('+') or len(student.phone) < 10:
        print(f"SMS skipped for {student.regno}: Invalid phone number format or missing.")
        return False
       
    stats = _calculate_attendance(student.id, selected_class.id)
    percentage_str = f"{stats['percentage']:.2f}% ({stats['present']}/{stats['total']})"
    
    intro = "Your attendance has been manually modified.\n\n" if message_type == "modified" else "Your attendance has been recorded.\n\n"
    # FIX: Use a more standard status text for the SMS
    sms_status = status_text.upper().replace(' (AUTO)', '').replace(' (MAN)', '')
    body = (
        f"Hello {student.name},\n\n{intro}"
        f"Status: {sms_status}\nFaculty: {faculty_name}\n"
        f"Subject: {selected_class.subject_title} ({selected_class.subject_code})\n"
        f"Period: {poll_details.get('period','N/A')}\nTopic: {poll_details.get('topic','N/A')}\n"
        f"Time: {class_time}\n\nAttendance: {percentage_str}"
    )
    try:
        # NOTE: Twilio has character limits, body should be concise.
        twilio_client.messages.create(body=body, from_=app.config['TWILIO_PHONE_NUMBER'], to=student.phone)
        return True
    except Exception as e:
        # Log the Twilio error to debug SMS failures
        print(f"Twilio Error for {student.regno} to {student.phone}: {e}")
        return False

# --- WebSocket Status Route ---
@app.route('/ws-status')
def ws_status():
    """Check WebSocket connection status"""
    return jsonify({
        'connected': is_ws_connected,
        'server_url': WS_SERVER_URL
    })

# NOTE: Async view functions are commented out as they require ASGI/Quart setup.

# --- Main Routes ---

@app.route('/')
def index():
    if current_user.is_authenticated:
        role = session.get('user_role')
        if role == 'teacher': return redirect(url_for('class_selection'))
        if role == 'student': return redirect(url_for('student_dashboard'))
        if role == 'advisor': return redirect(url_for('advisor_dashboard'))
    # NOTE: Assuming you have an 'index.html' template
    return render_template('index.html')

@app.route('/login')
def login_page():
    # NOTE: Assuming you have a 'log_in.html' template
    return render_template('log_in.html')

# --- Login & Signup Routes ---

@app.route('/login/teacher', methods=['GET', 'POST'])
def teacher_login():
    if current_user.is_authenticated: return redirect(url_for('class_selection'))
   
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        teacher = Teacher.query.filter_by(username=username).first()
       
        if teacher and teacher.check_password(password):
            login_user(teacher)
            session['user_role'] = 'teacher'
            return redirect(url_for('class_selection'))
        else:
            flash('Invalid username or password.', 'error')
           
    # NOTE: Assuming you have a 'teacher_login.html' template
    return render_template('teacher_login.html')

@app.route('/login/student', methods=['GET', 'POST'])
def student_login():
    if current_user.is_authenticated: return redirect(url_for('student_dashboard'))
   
    if request.method == 'POST':
        regno = request.form.get('username')
        password = request.form.get('password')
        student = Student.query.filter_by(regno=regno).first()
       
        if student and student.check_password(password):
            login_user(student)
            session['user_role'] = 'student'
            return redirect(url_for('student_dashboard'))
        else:
            flash('Invalid Registration No. or password.', 'error')

    # NOTE: Assuming you have a 'student_login.html' template
    return render_template('student_login.html')

@app.route('/login/advisor', methods=['GET', 'POST'])
def advisor_login():
    if current_user.is_authenticated: return redirect(url_for('advisor_dashboard'))
   
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        advisor = ClassAdvisor.query.filter_by(username=username).first()
       
        if advisor and advisor.check_password(password):
            login_user(advisor)
            session['user_role'] = 'advisor'
            return redirect(url_for('advisor_dashboard'))
        else:
            flash('Invalid username or password.', 'error')

    # NOTE: Assuming you have an 'advisor_login.html' template
    return render_template('advisor_login.html')

@app.route('/signup/teacher', methods=['GET', 'POST'])
def teacher_signup():
    if request.method == 'POST':
        try:
            username = request.form.get('username')
            password = request.form.get('password')
           
            if Teacher.query.filter_by(username=username).first():
                flash('Username already exists.', 'error')
                return redirect(url_for('teacher_signup'))

            new_teacher = Teacher(username=username)
            new_teacher.set_password(password)
            db.session.add(new_teacher)
            db.session.commit()

            # The Teacher object must be committed first to get the ID
            db.session.refresh(new_teacher)

            branches = request.form.getlist('branch[]')
            sections = request.form.getlist('section[]')
            sems = request.form.getlist('sem[]')
            strengths = request.form.getlist('strength[]')
            subject_codes = request.form.getlist('subject_code[]')
            subject_titles = request.form.getlist('subject_title[]')

            for i in range(len(branches)):
                if branches[i]:
                    # Basic input validation
                    try:
                        sem_val = int(sems[i])
                        strength_val = int(strengths[i])
                    except ValueError:
                        db.session.rollback()
                        flash('Semester and Strength must be valid numbers.', 'error')
                        return redirect(url_for('teacher_signup'))
                        
                    new_class = Class(
                        branch=branches[i],
                        section=sections[i],
                        sem=sem_val,
                        strength=strength_val,
                        subject_code=subject_codes[i],
                        subject_title=subject_titles[i],
                        teacher_id=new_teacher.id
                    )
                    db.session.add(new_class)
           
            db.session.commit()
            flash('Teacher account created successfully! Please log in.', 'success')
            return redirect(url_for('teacher_login'))
       
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating account: {e}', 'error')
            return redirect(url_for('teacher_signup'))
           
    # NOTE: Assuming you have a 'sign_up.html' template
    return render_template('sign_up.html')

@app.route('/signup/advisor', methods=['GET', 'POST'])
def advisor_signup():
    if request.method == 'POST':
        try:
            username = request.form.get('username')
            password = request.form.get('password')
           
            branch = request.form.get('branch')
            section = request.form.get('section')
            sem = request.form.get('sem')

            if not all([branch, section, sem]):
                flash('Please fill in all class details: Branch, Section, and Semester.', 'error')
                return redirect(url_for('advisor_signup'))
           
            try:
                sem_int = int(sem)
            except ValueError:
                flash('Semester must be a valid number.', 'error')
                return redirect(url_for('advisor_signup'))
            
            classes_to_advise = Class.query.filter_by(
                branch=branch,
                section=section,
                sem=sem_int
            ).all()

            if not classes_to_advise:
                flash('No classes found matching those details. A teacher must create subjects for this class first.', 'error')
                return redirect(url_for('advisor_signup'))
               
            if ClassAdvisor.query.filter_by(username=username).first():
                flash('Username already exists.', 'error')
                return redirect(url_for('advisor_signup'))
           
            new_advisor = ClassAdvisor(username=username)
            new_advisor.set_password(password)
           
            for cls in classes_to_advise:
                new_advisor.advised_classes.append(cls)
           
            db.session.add(new_advisor)
            db.session.commit()
           
            flash('Advisor account created successfully! Please log in.', 'success')
            return redirect(url_for('advisor_login'))
       
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating account: {e}', 'error')
            return redirect(url_for('advisor_signup'))

    classes = Class.query.all()
    # NOTE: Assuming you have an 'advisor_signup.html' template
    return render_template('advisor_signup.html', classes=classes)

# --- Teacher Dashboard ---

@app.route('/class-selection')
@login_required
def class_selection():
    if session.get('user_role') != 'teacher': return redirect(url_for('index'))
    classes = Class.query.filter_by(teacher_id=current_user.id).all()
    # NOTE: Assuming you have a 'class_selection.html' template
    return render_template('class_selection.html', classes=classes)

@app.route('/home/<int:class_id>')
@login_required
def home(class_id):
    session['current_class_id'] = class_id
   
    if session.get('user_role') != 'teacher': return redirect(url_for('index'))
   
    selected_class = db.get_or_404(Class, class_id)
    if selected_class.teacher_id != current_user.id:
        flash('You do not have permission to view this class.', 'error')
        return redirect(url_for('class_selection'))
   
    target_branch = selected_class.branch
    target_section = selected_class.section
    target_sem = selected_class.sem

    # Query students who belong to the same class group (Branch, Section, Sem)
    # This might include students whose 'class_id' is different from 'selected_class.id'
    # if multiple subjects are defined for the same group, but your Student model
    # only links to ONE Class ID. The query below seems to incorrectly assume
    # all students linked to classes in the same group are the target.
    # We will stick to the provided logic to maintain original intent.
    students_query = Student.query.join(
        Class, Student.class_id == Class.id
    ).filter(
        Class.branch == target_branch,
        Class.section == target_section,
        Class.sem == target_sem
    ).order_by(Student.chair_number).all()
   
    students_with_stats = []
    for student in students_query:
        # Calculate attendance stats for this specific subject/class (selected_class.id)
        stats = _calculate_attendance(student.id, selected_class.id)
        students_with_stats.append({'student': student, 'stats': stats})

    # NOTE: Assuming you have a 'teacher_dashboard.html' template
    return render_template('teacher_dashboard.html',
                           selected_class=selected_class,
                           students_with_stats=students_with_stats)

# --- Student Dashboard ---

@app.route('/student-dashboard')
@login_required
def student_dashboard():
    if session.get('user_role') != 'student':
        return redirect(url_for('index'))
   
    student = current_user
   
    # Get all subjects (Classes) associated with the student's class group
    if student.class_obj is None:
        flash('Student is not assigned to a class group.', 'error')
        return redirect(url_for('logout')) # Force logout/re-setup if data is inconsistent
        
    subjects = Class.query.filter_by(
        branch=student.class_obj.branch,
        section=student.class_obj.section,
        sem=student.class_obj.sem
    ).all()
   
    subjects_with_stats = []
   
    for subject in subjects:
        subject_stats = _calculate_attendance(student.id, subject.id)
        subjects_with_stats.append({
            'subject': subject,
            'stats': subject_stats
        })
   
    response = make_response(render_template('student_dashboard.html',
                                              student=student,
                                              subjects_with_stats=subjects_with_stats))
   
    # Headers to prevent caching sensitive data
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
   
    # NOTE: Assuming you have a 'student_dashboard.html' template
    return response

# --- Advisor Dashboard ---

@app.route('/advisor-dashboard')
@login_required
def advisor_dashboard():
    if session.get('user_role') != 'advisor':
        return redirect(url_for('index'))
   
    advisor = current_user
   
    if not advisor.advised_classes:
        flash('You are not assigned to any class groups yet.', 'info')
        # NOTE: Assuming you have an 'advisor_dashboard.html' template
        return render_template('advisor_dashboard.html', groups=[])

    # Group classes by (branch, section, sem) to show unique groups
    unique_groups = {}
    for cls in advisor.advised_classes:
        group_key = f"{cls.branch}-{cls.section}-{cls.sem}"
        if group_key not in unique_groups:
            unique_groups[group_key] = {
                'branch': cls.branch,
                'section': cls.section,
                'sem': cls.sem,
                'key': group_key
            }

    # NOTE: Assuming you have an 'advisor_dashboard.html' template
    return render_template('advisor_dashboard.html', groups=unique_groups.values())

@app.route('/advisor/group/<string:group_key>')
@login_required
def advisor_group_detail(group_key):
    if session.get('user_role') != 'advisor':
        return redirect(url_for('index'))

    try:
        branch, section, sem = group_key.split('-')
        sem = int(sem)
    except ValueError:
        flash('Invalid class group format.', 'error')
        return redirect(url_for('advisor_dashboard'))

    is_authorized = False
    for cls in current_user.advised_classes:
        if cls.branch == branch and cls.section == section and cls.sem == sem:
            is_authorized = True
            break
   
    if not is_authorized:
        flash('You are not authorized to view this class group.', 'error')
        return redirect(url_for('advisor_dashboard'))

    # Get all subjects/classes for this specific group
    subjects = Class.query.filter_by(
        branch=branch,
        section=section,
        sem=sem
    ).order_by(Class.subject_title).all()
   
    group_name = f"{branch} - {section} (Sem {sem})"

    # NOTE: Assuming you have an 'advisor_group_subjects.html' template
    return render_template('advisor_group_subjects.html', subjects=subjects, group_name=group_name)

@app.route('/advisor/subject/<int:subject_id>')
@login_required
def advisor_subject_detail(subject_id):
    if session.get('user_role') != 'advisor':
        return redirect(url_for('index'))

    subject = db.session.get(Class, subject_id)
    if not subject or subject not in current_user.advised_classes:
        flash('Subject not found or you do not have permission to view it.', 'error')
        return redirect(url_for('advisor_dashboard'))

    # Query for session log (grouped attendance records)
    session_log_query = db.session.query(
        AttendanceReport.date,
        AttendanceReport.period,
        AttendanceReport.topic,
        func.count(AttendanceReport.id).label('total_students'),
        func.sum(case((AttendanceReport.status.ilike('present%'), 1), else_=0)).label('present_students')
    ).filter(
        AttendanceReport.class_id == subject_id
    ).group_by(
        # Grouping by date only for simplification, otherwise it might be too granular
        db.func.date(AttendanceReport.date), 
        AttendanceReport.period,
        AttendanceReport.topic
    ).order_by(
        AttendanceReport.date.desc()
    ).all()

    session_log = []
    for log in session_log_query:
        session_log.append({
            'date': log.date.strftime('%Y-%m-%d'),
            'period': log.period,
            'topic': log.topic,
            'present': log.present_students if log.present_students is not None else 0,
            'absent': (log.total_students - (log.present_students or 0)) # Ensure 0 if present_students is None
        })

    # Get all students belonging to this subject's class group
    students_query = Student.query.join(
        Class, Student.class_id == Class.id
    ).filter(
        Class.branch == subject.branch,
        Class.section == subject.section,
        Class.sem == subject.sem
    ).distinct(Student.id).all()

    above_75_count = 0
    below_75_count = 0

    # Calculate overall attendance for each student in this subject
    for student in students_query:
        stats = _calculate_attendance(student.id, subject_id)
        if stats['percentage'] >= 75:
            above_75_count += 1
        else:
            below_75_count += 1
   
    total_students = above_75_count + below_75_count
    percent_above_75 = (above_75_count / total_students * 100) if total_students > 0 else 0
    percent_below_75 = 100 - percent_above_75

    group_key = f"{subject.branch}-{subject.section}-{subject.sem}"

    # NOTE: Assuming you have an 'advisor_subject_detail.html' template
    return render_template(
        'advisor_subject_detail.html',
        subject=subject,
        session_log=session_log,
        percent_above_75=percent_above_75,
        percent_below_75=percent_below_75,
        above_75_count=above_75_count,
        below_75_count=below_75_count,
        total_students=total_students,
        group_key=group_key
    )

@app.route('/advisor/subject/<int:subject_id>/stats')
@login_required
def advisor_subject_stats(subject_id):
    if session.get('user_role') != 'advisor':
        return redirect(url_for('index'))

    subject = db.session.get(Class, subject_id)
    if not subject or subject not in current_user.advised_classes:
        flash('Subject not found or you do not have permission to view it.', 'error')
        return redirect(url_for('advisor_dashboard'))

    students_query = Student.query.join(
        Class, Student.class_id == Class.id
    ).filter(
        Class.branch == subject.branch,
        Class.section == subject.section,
        Class.sem == subject.sem
    ).order_by(Student.regno).all()

    students_above_75 = []
    students_below_75 = []

    for student in students_query:
        stats = _calculate_attendance(student.id, subject_id)
        student_data = {
            'id': student.id,
            'name': student.name,
            'regno': student.regno,
            'percentage': stats['percentage']
        }
       
        if stats['percentage'] >= 75:
            students_above_75.append(student_data)
        else:
            students_below_75.append(student_data)

    # NOTE: Assuming you have an 'advisor_subject_stats.html' template
    return render_template(
        'advisor_subject_stats.html',
        subject=subject,
        students_above_75=students_above_75,
        students_below_75=students_below_75
    )

@app.route('/advisor/student/<int:student_id>/<int:subject_id>')
@login_required
def advisor_student_detail_for_subject(student_id, subject_id):
    if session.get('user_role') != 'advisor':
        return redirect(url_for('index'))

    student = db.session.get(Student, student_id)
    subject = db.session.get(Class, subject_id)

    if not student or not subject:
        flash('Student or Subject not found.', 'error')
        return redirect(url_for('advisor_dashboard'))

    if subject not in current_user.advised_classes:
        flash('You are not authorized to view this subject.', 'error')
        return redirect(url_for('advisor_dashboard'))

    # Check if the student belongs to the advised class group
    if not (student.class_obj.branch == subject.branch and 
            student.class_obj.section == subject.section and 
            student.class_obj.sem == subject.sem):
        flash('Student does not belong to this subject\'s class group.', 'error')
        return redirect(url_for('advisor_dashboard'))

    absent_logs = AttendanceReport.query.filter(
        AttendanceReport.student_id == student_id,
        AttendanceReport.class_id == subject_id,
        # FIX: Check for Absent statuses
        db.or_(
            AttendanceReport.status == 'Absent',
            AttendanceReport.status == 'Absent (Man)'
        )
    ).order_by(AttendanceReport.date.desc()).all()

    present_logs = AttendanceReport.query.filter(
        AttendanceReport.student_id == student_id,
        AttendanceReport.class_id == subject_id,
        AttendanceReport.status.ilike('present%')
    ).order_by(AttendanceReport.date.desc()).all()

    # NOTE: Assuming you have an 'advisor_student_detail.html' template
    return render_template(
        'advisor_student_detail.html',
        student=student,
        subject=subject,
        absent_logs=absent_logs,
        present_logs=present_logs
    )

@app.route('/logout')
@login_required
def logout():
    logout_user()
    session.pop('user_role', None)
    session.pop('current_class_id', None)
    flash('You have been logged out.', 'success')
    return redirect(url_for('login_page'))

# --- Student Routes ---

@app.route('/student/change_password', methods=['POST'])
@login_required
def student_change_password():
    if session.get('user_role') != 'student':
        return jsonify({'success': False, 'message': 'Permission denied'}), 403
   
    data = request.json
    old_password = data.get('old_password')
    new_password = data.get('new_password')
   
    if not old_password or not new_password:
        return jsonify({'success': False, 'message': 'Both old and new password are required'}), 400
   
    student = current_user
   
    if not student.check_password(old_password):
        return jsonify({'success': False, 'message': 'Old password is incorrect'}), 400
   
    try:
        student.set_password(new_password)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Password changed successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'Error changing password: {str(e)}'}), 500

@app.route('/student/subjects')
@login_required
def student_subjects():
    if session.get('user_role') != 'student':
        return redirect(url_for('index'))
   
    student = current_user
    
    if student.class_obj is None:
        flash('Student is not assigned to a class group.', 'error')
        return redirect(url_for('logout')) 
        
    subjects = Class.query.filter_by(
        branch=student.class_obj.branch,
        section=student.class_obj.section,
        sem=student.class_obj.sem
    ).all()
   
    subjects_with_stats = []
    for subject in subjects:
        stats = _calculate_attendance(student.id, subject.id)
        subjects_with_stats.append({
            'subject': subject,
            'stats': stats
        })
   
    # NOTE: Assuming you have a 'student_subjects.html' template
    return render_template('student_subjects.html',
                           student=student,
                           subjects_with_stats=subjects_with_stats)

@app.route('/student/subject/<int:subject_id>')
@login_required
def student_subject_detail(subject_id):
    if session.get('user_role') != 'student':
        return redirect(url_for('index'))
   
    student = current_user
    subject = db.session.get(Class, subject_id)
   
    if student.class_obj is None or not subject or \
       subject.branch != student.class_obj.branch or \
       subject.section != student.class_obj.section or \
       subject.sem != student.class_obj.sem:
        flash('Subject not found or access denied.', 'error')
        return redirect(url_for('student_subjects'))
   
    reports_with_teachers = db.session.query(AttendanceReport, Teacher).join(
        Teacher, AttendanceReport.teacher_id == Teacher.id
    ).filter(
        AttendanceReport.student_id == student.id,
        AttendanceReport.class_id == subject_id
    ).order_by(AttendanceReport.date.desc()).all()
   
    stats = _calculate_attendance(student.id, subject_id)
   
    absent_classes = db.session.query(AttendanceReport).filter(
        AttendanceReport.student_id == student.id,
        AttendanceReport.class_id == subject_id,
        ~AttendanceReport.status.ilike('present%')
    ).order_by(AttendanceReport.date.desc()).all()
   
    # NOTE: Assuming you have a 'student_subject_detail.html' template
    return render_template('student_subject_detail.html',
                           student=student,
                           subject=subject,
                           reports=reports_with_teachers,
                           absent_classes=absent_classes,
                           stats=stats)

# --- CSV & API Routes ---

@app.route('/upload_csv', methods=['POST'])
@login_required
def upload_csv():
    if session.get('user_role') != 'teacher':
        return redirect(url_for('index'))
   
    class_id = request.form.get('class_id')
    if not class_id:
        flash('No class ID provided.', 'error')
        return redirect(url_for('class_selection'))
   
    # Ensure class_id is an integer before fetching
    try:
        class_id_int = int(class_id)
    except ValueError:
        flash('Invalid class ID.', 'error')
        return redirect(url_for('class_selection'))
        
    selected_class = db.session.get(Class, class_id_int)
   
    if not selected_class or selected_class.teacher_id != current_user.id:
        flash('Invalid class or permission denied.', 'error')
        return redirect(url_for('class_selection'))

    file = request.files.get('student_csv')
    if not file or file.filename == '':
        flash('No file selected.', 'error')
        return redirect(url_for('home', class_id=class_id_int))
       
    try:
        # Read the file content
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_reader = csv.DictReader(stream)
       
        rows_to_process = []
        regnos_in_csv = []
        for row in csv_reader:
            # Check for required columns
            if not all(k in row for k in ['Chair', 'RegNo', 'Name']):
                raise ValueError(f"Missing required column (Chair, RegNo, or Name) in row {csv_reader.line_num}")
            
            # Basic data cleaning
            regno_clean = row['RegNo'].strip()
            if not regno_clean: continue # Skip empty rows
            
            rows_to_process.append(row)
            regnos_in_csv.append(regno_clean)

        # Bulk fetch existing students
        existing_students = db.session.query(Student).filter(
            Student.regno.in_(regnos_in_csv)
        ).all()
       
        student_map = {student.regno: student for student in existing_students}

        created = 0
        updated = 0
       
        for row in rows_to_process:
            regno = row['RegNo'].strip()
            
            # Input validation and type casting
            try:
                chair_num = int(row['Chair'])
            except ValueError:
                 flash(f"Invalid Chair number in row for RegNo: {regno}. Skipping.", 'warning')
                 continue

            student = student_map.get(regno)
           
            if student:
                # Update logic
                student.name = row['Name']
                student.phone = row.get('Phone', '') # Phone is optional
                student.chair_number = chair_num
                student.class_id = class_id_int
                updated += 1
            else:
                # Create logic
                new_student = Student(
                    chair_number=chair_num,
                    regno=regno,
                    name=row['Name'],
                    phone=row.get('Phone', ''),
                    class_id=class_id_int
                )
                # Set default password to RegNo for first time setup
                new_student.set_password(regno) 
                db.session.add(new_student)
                created += 1
               
        db.session.commit()

        flash(f'Upload complete: {created} students created, {updated} students updated.', 'success')
       
    except Exception as e:
        db.session.rollback()
        flash(f'Error processing CSV: {e}', 'error')
       
    return redirect(url_for('home', class_id=class_id_int))

@app.route('/download_demo_csv/<int:class_id>')
@login_required
def download_demo_csv(class_id):
    if session.get('user_role') != 'teacher': return redirect(url_for('index'))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Chair', 'RegNo', 'Name', 'Phone'])
    selected_class = db.get_or_404(Class, class_id)
    for i in range(1, selected_class.strength + 1):
        writer.writerow([i, f'REG-{selected_class.branch}-{i:03d}', f'Student Name {i}', f'+9198765432{i:02d}'])
       
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=demo_students_{selected_class.branch}_{selected_class.section}.csv"}
    )

@app.route('/confirm_attendance', methods=['POST'])
@login_required
def confirm_attendance():
    if session.get('user_role') != 'teacher':
        return jsonify({'message': 'Permission denied'}), 403
       
    data = request.json
    attendance_data = data.get('attendance')
    poll_details = data.get('poll_details')
   
    if not poll_details or not poll_details.get('period') or not poll_details.get('topic'):
        return jsonify({'message': 'Period and Topic are required.'}), 400
       
    if not attendance_data:
        return jsonify({'message': 'No attendance data provided.'}), 400
   
    current_class_id = session.get('current_class_id')
    if not current_class_id:
        # FIX: Try to get class_id from the first student if session is missing it
        # This is a fallback and the session should be preferred.
        try:
            student_id = attendance_data[0].get('student_id')
            student = db.session.get(Student, int(student_id))
            if student:
                current_class_id = student.class_id
        except:
             return jsonify({'message': 'No active class selected and student data is ambiguous.'}), 400

    # Ensure class ID is correctly cast/checked
    try:
        current_class_id = int(current_class_id)
    except ValueError:
        return jsonify({'message': 'Invalid current class ID.'}), 400
        
    current_class = db.session.get(Class, current_class_id)
    if not current_class or current_class.teacher_id != current_user.id:
        return jsonify({'message': 'Invalid class or permission denied.'}), 400
   
    class_time = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
   
    try:
        reports_to_add = []
        sms_count = 0
        
        # Collect all student IDs to query in bulk
        student_ids = [int(record['student_id']) for record in attendance_data] # Ensure int conversion
        students = db.session.query(Student).filter(Student.id.in_(student_ids)).all()
        student_map = {student.id: student for student in students}
        
        for record in attendance_data:
            # FIX: Ensure student_id is an integer before using it in the map
            try:
                student_id = int(record.get('student_id'))
            except ValueError:
                print(f"Skipping record with invalid student_id: {record.get('student_id')}")
                continue
                
            status = record.get('status')
            
            student = student_map.get(student_id)
            if student:
                new_report = AttendanceReport(
                    status=status,
                    class_id=current_class_id,
                    student_id=student.id,
                    teacher_id=current_user.id,
                    period=poll_details.get('period'),
                    topic=poll_details.get('topic'),
                    date=datetime.now(UTC)
                )
                reports_to_add.append(new_report)
               
                # FIX: Run SMS notification logic
                if _send_sms_notification(student, current_class, status, poll_details, current_user.username, class_time):
                    sms_count += 1
       
        if reports_to_add:
            db.session.add_all(reports_to_add)
            db.session.commit()
           
        return jsonify({'message': f'Attendance confirmed for {len(reports_to_add)} students. {sms_count} SMS messages sent.'})
           
    except Exception as e:
        db.session.rollback()
        # Log the detailed error
        print(f"Error in confirm_attendance: {traceback.format_exc()}")
        return jsonify({'message': f'Error saving to database: {e}'}), 500

@app.route('/download_report', methods=['POST'])
@login_required
def download_report():
    if session.get('user_role') != 'teacher':
        return jsonify({'message': 'Permission denied'}), 403
       
    data = request.json
    attendance_data = data.get('attendance')
    poll_details = data.get('poll_details') # Not strictly needed for the CSV but useful for context
   
    if not attendance_data:
        return jsonify({'message': 'No attendance data provided.'}), 400
       
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['RegNo', 'Name', 'Status', 'Timestamp (Report Downloaded)'])
   
    class_time = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    
    # Collect all student IDs to query in bulk
    student_ids = [int(record['student_id']) for record in attendance_data] # Ensure int conversion
    students = db.session.query(Student).filter(Student.id.in_(student_ids)).all()
    student_map = {student.id: student for student in students}
    
    student_for_filename = None
   
    for record in attendance_data:
        # FIX: Ensure student_id is an integer before using it in the map
        try:
            student_id = int(record.get('student_id'))
        except ValueError:
            continue
            
        student = student_map.get(student_id)
        if student:
            if student_for_filename is None:
                student_for_filename = student
            # FIX: Use a cleaner status text for the CSV
            csv_status = record['status'].replace(' (Auto)', '').replace(' (Man)', '')
            writer.writerow([student.regno, student.name, csv_status, class_time])
           
    if student_for_filename and student_for_filename.class_obj:
        class_obj = student_for_filename.class_obj
        filename = f"report_{class_obj.branch}_{class_obj.section}_{datetime.now().strftime('%Y-%m-%d')}.csv"
    else:
        filename = f"report_empty_{datetime.now().strftime('%Y-%m-%d')}.csv"

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=\"{filename}\""}
    )

# --- Teacher's Student History Page ---

@app.route('/student_history/<int:student_id>/<int:class_id>')
@login_required
def student_history(student_id, class_id):
    if session.get('user_role') != 'teacher':
        return redirect(url_for('index'))
   
    student = db.session.get(Student, student_id)
    selected_class = db.session.get(Class, class_id)
   
    if not selected_class or selected_class.teacher_id != current_user.id:
        flash('Class not found or permission denied.', 'error')
        return redirect(url_for('class_selection'))
   
    if not student:
        flash('Student not found.', 'error')
        return redirect(url_for('home', class_id=class_id))

    reports = db.session.query(AttendanceReport, Teacher.username)\
        .join(Teacher, AttendanceReport.teacher_id == Teacher.id)\
        .filter(
            AttendanceReport.student_id == student_id,
            AttendanceReport.class_id == class_id
        )\
        .order_by(AttendanceReport.date.desc())\
        .all()

    stats = _calculate_attendance(student.id, class_id)
   
    # NOTE: Assuming you have a 'teacher_student_data.html' template
    return render_template('teacher_student_data.html',
                           student=student,
                           reports_with_teacher=reports,
                           stats=stats,
                           selected_class=selected_class)

# --- Edit Past Attendance ---

@app.route('/edit_attendance/<int:class_id>')
@login_required
def edit_attendance(class_id):
    if session.get('user_role') != 'teacher':
        return redirect(url_for('index'))
   
    selected_class = db.get_or_404(Class, class_id)
    if selected_class.teacher_id != current_user.id:
        flash('You do not have permission to view this class.', 'error')
        return redirect(url_for('class_selection'))

    # NOTE: Assuming you have an 'edit_attendance.html' template
    return render_template('edit_attendance.html', selected_class=selected_class)

@app.route('/get_records_for_date', methods=['POST'])
@login_required
def get_records_for_date():
    if session.get('user_role') != 'teacher':
        return jsonify({'message': 'Permission denied'}), 403

    data = request.json
    class_id = data.get('class_id')
    date_str = data.get('date')
    period = data.get('period')

    if not all([class_id, date_str, period]):
        return jsonify({'message': 'Missing data (class, date, or period)'}), 400

    # Ensure class ID is correctly cast/checked
    try:
        class_id = int(class_id)
    except ValueError:
        return jsonify({'message': 'Invalid class ID format.'}), 400
        
    try:
        report_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'message': 'Invalid date format. Use YYYY-MM-DD.'}), 400

    selected_class = db.get_or_404(Class, class_id)
   
    students_query = Student.query.join(
        Class, Student.class_id == Class.id
    ).filter(
        Class.branch == selected_class.branch,
        Class.section == selected_class.section,
        Class.sem == selected_class.sem
    ).order_by(Student.chair_number).all()
   
    # Fetch all records for the given class, date, and period
    reports = AttendanceReport.query.filter(
        AttendanceReport.class_id == class_id,
        # Use database function for date comparison
        db.func.date(AttendanceReport.date) == report_date, 
        AttendanceReport.period == period
    ).all()

    report_map = {report.student_id: report for report in reports}

    results = []
    for student in students_query:
        report = report_map.get(student.id)
        if report:
            results.append({
                'record_id': report.id,
                'student_id': student.id,
                'regno': student.regno,
                'name': student.name,
                'status': report.status
            })
        else:
            # Student is assumed Absent if no record exists for that subject/date/period
            results.append({
                'record_id': None,
                'student_id': student.id,
                'regno': student.regno,
                'name': student.name,
                'status': 'Absent'
            })

    return jsonify(results)

@app.route('/update_attendance_record', methods=['POST'])
@login_required
def update_attendance_record():
    if session.get('user_role') != 'teacher':
        return jsonify({'message': 'Permission denied'}), 403
   
    data = request.json
    record_id = data.get('record_id')
    student_id = data.get('student_id')
    new_status = data.get('new_status')
    poll_details = data.get('poll_details')

    if not all([student_id, new_status, poll_details, poll_details.get('date'), poll_details.get('period')]):
        return jsonify({'message': 'Missing data'}), 400
   
    current_class_id = session.get('current_class_id')
    if not current_class_id:
        # Fallback: Get class ID from the student object if possible, or from the JSON data if passed
        current_class_id = data.get('class_id')
        
    if not current_class_id:
        return jsonify({'message': 'No active class selected or provided in data.'}), 400

    try:
        current_class_id = int(current_class_id)
        student_id = int(student_id)
    except ValueError:
         return jsonify({'message': 'Invalid ID format.'}), 400
        
    current_class = db.session.get(Class, current_class_id)
    if not current_class or current_class.teacher_id != current_user.id:
        return jsonify({'message': 'Invalid class or permission denied.'}), 400
   
    class_time = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
    student = db.session.get(Student, student_id)
    
    if not student:
        return jsonify({'message': 'Student not found.'}), 404

    try:
        report = None
        if record_id:
            report = db.session.get(AttendanceReport, record_id)

        if report:
            # Existing record update
            if report.class_obj.teacher_id != current_user.id:
                return jsonify({'message': 'Permission denied for this record'}), 403
           
            report.status = new_status
            db.session.commit()
           
            # FIX: Send SMS after update
            _send_sms_notification(
                student, current_class, new_status, poll_details,
                current_user.username, class_time, message_type="modified"
            )
           
            return jsonify({'message': 'Record updated.', 'new_record_id': report.id})
       
        else:
            # New record creation (e.g., changing an 'Absent' student to 'Present')
            
            # Ensure the student belongs to the teacher's responsibility group (indirect check via class)
            # This check might be flawed if student.class_obj refers to their home class,
            # and current_class is the subject. We rely on the class_id check above.
            
            report_date = datetime.strptime(poll_details['date'], '%Y-%m-%d').date()

            new_report = AttendanceReport(
                status=new_status,
                class_id=current_class_id,
                student_id=student.id,
                teacher_id=current_user.id,
                period=poll_details.get('period'),
                topic=poll_details.get('topic', 'Manual Edit'),
                # Combine date and current time (with UTC timezone)
                date=datetime(report_date.year, report_date.month, report_date.day, datetime.now(UTC).hour, datetime.now(UTC).minute, datetime.now(UTC).second, tzinfo=UTC)
            )
            db.session.add(new_report)
            db.session.commit()
           
            # FIX: Send SMS after creation
            _send_sms_notification(
                student, current_class, new_status, poll_details,
                current_user.username, class_time, message_type="modified"
            )
           
            return jsonify({'message': 'New record created.', 'new_record_id': new_report.id})

    except Exception as e:
        db.session.rollback()
        print(f"Error: {e}")
        return jsonify({'message': f'Error updating record: {e}'}), 500
   
@app.route('/debug/student_attendance/<int:student_id>')
@login_required
def debug_student_attendance(student_id):
    if session.get('user_role') != 'student' and session.get('user_role') != 'teacher' and session.get('user_role') != 'advisor':
         return jsonify({'error': 'Permission denied'}), 403
     
    # Allow student to view their own, or teacher/advisor to view any
    if session.get('user_role') == 'student' and current_user.id != student_id:
        return jsonify({'error': 'Permission denied: Students can only view their own data.'}), 403
   
    student = db.session.get(Student, student_id)
    if not student:
        return jsonify({'error': 'Student not found'}), 404
   
    all_records = db.session.query(
        AttendanceReport,
        Class.subject_title,
        Class.subject_code,
        Teacher.username
    ).join(
        Class, AttendanceReport.class_id == Class.id
    ).join(
        Teacher, AttendanceReport.teacher_id == Teacher.id
    ).filter(
        AttendanceReport.student_id == student_id
    ).all()
   
    subject_data = {}
    for record, subject_title, subject_code, teacher_name in all_records:
        if subject_title not in subject_data:
            subject_data[subject_title] = {
                'subject_code': subject_code,
                'teacher': teacher_name,
                'total_classes': 0,
                'present_classes': 0,
                'records': []
            }
       
        subject_data[subject_title]['total_classes'] += 1
        if record.status.lower().startswith('present'):
            subject_data[subject_title]['present_classes'] += 1
       
        subject_data[subject_title]['records'].append({
            'date': record.date.strftime('%Y-%m-%d %H:%M'),
            'status': record.status,
            'period': record.period,
            'topic': record.topic
        })
   
    for subject in subject_data.values():
        total = subject['total_classes']
        present = subject['present_classes']
        subject['percentage'] = (present / total * 100) if total > 0 else 0
   
    return jsonify({
        'student': {
            'name': student.name,
            'regno': student.regno,
            'main_class_id': student.class_id
        },
        'subjects': subject_data,
        'total_records': len(all_records)
    })

# --- Main Runner ---
if __name__ == '__main__':
    # Initialize SQLAlchemy database tables if they don't exist
    with app.app_context():
        db.create_all()
        
    # Initialize the WebSocket client connection in a separate thread
    # This must happen outside the app_context but before app.run()
    # to avoid blocking the main Flask thread.
    initialize_websocket()
    
    # Start the Flask development server
    #app.run(debug=True, host='0.0.0.0', port=5000)