from PIL import Image
import pytesseract
import io, os
import re, fitz
import sendgrid
from sendgrid.helpers.mail import Mail as SGMail
from dotenv import load_dotenv
import os, markdown, datetime
from flask import Flask, render_template, request, redirect, session, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_groq import ChatGroq

from flask_mail import Mail, Message
from flask_dance.contrib.google import make_google_blueprint, google

if os.name == "nt":
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, ".env")

load_dotenv(env_path)

print("ENV PATH:", env_path)
print("CLIENT ID:", os.getenv("GOOGLE_CLIENT_ID"))
print(os.listdir(BASE_DIR))

DATA_DIR = "__data__"
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

app = Flask(__name__)

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 465
app.config['MAIL_USE_TLS'] = False
app.config['MAIL_USE_SSL'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME')

mail = Mail(app)

app.config['SECRET_KEY'] = 'chatscholar_secret_key_2024'
import os

# ✅ Replace SQLite with this
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', 'sqlite:///chatscholar.db'
).replace('postgres://', 'postgresql://')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # ✅ allows HTTP for local dev

google_bp = make_google_blueprint(
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    scope=[
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile"
    ],
    redirect_to="google_login"
)

app.register_blueprint(google_bp, url_prefix="/login")

app.config["SESSION_COOKIE_NAME"] = "chat_scholar_session"
app.config["SESSION_PERMANENT"] = False

import warnings
warnings.filterwarnings("ignore", message="Scope has changed")

# print(info)

@app.route("/google_login")
def google_login():
    if not google.authorized:
        return redirect(url_for("google.login"))

    resp = google.get("/oauth2/v2/userinfo")

    if not resp.ok:
        return redirect(url_for("login"))

    info = resp.json()

    email = info.get("email")
    name = info.get("name")

    if not name or name.strip() == "":
        name = email.split("@")[0]

    username = email.split("@")[0]

    user = User.query.filter_by(email=email).first()

    if not user:
        user = User(
            username=username,
            full_name=name,   # ✅ always filled
            email=email,
            password=bcrypt.generate_password_hash("google_user").decode("utf-8"),
            is_verified=True
        )

        db.session.add(user)
        db.session.commit()

    login_user(user)
    add_notification(user.id, "Welcome back!")

    return redirect(url_for("home"))

@app.route("/clear_session")
def clear_session():
    session.clear()
    return "Session cleared"

# =====================
# DATABASE MODELS
# =====================

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(50), unique=True, nullable=False)
    full_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)

    date_of_birth = db.Column(db.Date, nullable=True)

    is_verified = db.Column(db.Boolean, default=False)
    otp = db.Column(db.String(6), nullable=True)
    otp_expiry = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.datetime.now)

    # Profile fields
    profile_image = db.Column(db.String(200), default="default.png")
    bio = db.Column(db.String(300))
    college = db.Column(db.String(100))
    phone = db.Column(db.String(20))

    # Relationships
    pdfs = db.relationship('PDFHistory', backref='user', lazy=True)
    chats = db.relationship('ChatHistory', backref='user', lazy=True)

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    message = db.Column(db.String(300))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.now)

class PDFHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    filename = db.Column(db.String(200), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.datetime.now)
    chats = db.relationship('ChatHistory', backref='pdf', lazy=True)

class ChatHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    pdf_id = db.Column(db.Integer, db.ForeignKey('pdf_history.id'), nullable=False)
    role = db.Column(db.String(10), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.now)

class EssayHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    essay_text = db.Column(db.Text, nullable=False)
    result = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.now)
    user = db.relationship('User', backref='essays')

import random
import re as regex_module

def generate_otp():
    return str(random.randint(100000, 999999))

def send_otp_email(email, otp, full_name):
    try:
        sg = sendgrid.SendGridAPIClient(
            api_key=os.environ.get('SENDGRID_API_KEY')
        )
        message = SGMail(
            from_email=os.environ.get('MAIL_FROM', 'nikhilsinghal2023@gmail.com'),
            to_emails=email,
            subject='Chat Scholar - Email Verification',
            html_content=f"""
            <div style="font-family:Inter,sans-serif;max-width:480px;margin:auto;
                        background:#031427;color:#d3e4fe;padding:40px;border-radius:16px;">
                <h2 style="color:#adc6ff;">Chat Scholar</h2>
                <p>Hello <strong>{full_name}</strong>!</p>
                <p>Your verification code is:</p>
                <div style="background:#1b2b3f;padding:24px;border-radius:12px;
                            text-align:center;margin:24px 0;">
                    <span style="font-size:36px;font-weight:900;
                                 letter-spacing:12px;color:#4d8eff;">
                        {otp}
                    </span>
                </div>
                <p style="color:#8c909f;">This code expires in 10 minutes.</p>
            </div>
            """
        )
        sg.send(message)
        return True
    except Exception as e:
        print(f"SendGrid error: {str(e)}")
        return False

def validate_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return regex_module.match(pattern, email)

def validate_password(password):
    # Min 8 chars, 1 uppercase, 1 lowercase, 1 digit, 1 special char
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if not regex_module.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"
    if not regex_module.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"
    if not regex_module.search(r'\d', password):
        return False, "Password must contain at least one number"
    if not regex_module.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return False, "Password must contain at least one special character"
    return True, "Valid"

def validate_age(dob_str):
    try:
        dob = datetime.datetime.strptime(dob_str, '%Y-%m-%d').date()
        today = datetime.date.today()
        age = (today - dob).days // 365
        if age < 13:
            return False, "You must be at least 13 years old"
        if age > 120:
            return False, "Please enter a valid date of birth"
        return True, dob
    except:
        return False, "Invalid date format"

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.context_processor
def inject_user():
    return dict(current_user=current_user)

def add_notification(user_id, msg):
    new = Notification(user_id=user_id, message=msg)
    db.session.add(new)
    db.session.commit()

# =====================
# IN-MEMORY SESSION STORE
# =====================

pdf_sessions = {}
active_session = None
rubric_text = ""

# ✅ Groq LLM - no daily limit issues
# ✅ Safe initialization - won't crash if key is missing

GROQ_API_KEY = os.environ.get('GROQ_API_KEY') or os.getenv('GROQ_API_KEY')

if GROQ_API_KEY:
    llm = ChatGroq(
        model="llama-3.1-8b-instant",
        temperature=0.3,
        api_key=GROQ_API_KEY
    )
else:
    llm = None
    print("WARNING: GROQ_API_KEY not set")

# =====================
# HELPER FUNCTIONS
# =====================

# ✅ Global PDF text storage (replaces vectorstore)
pdf_text_store = {}  # { session_name: full_text }

def get_pdf_text(pdf_docs):
    """Extract text from PDF files using PyMuPDF"""
    text = ""
    for pdf in pdf_docs:
        pdf_text = ""
        try:
            pdf.seek(0)
            pdf_bytes = pdf.read()
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            for page in doc:
                extracted = page.get_text()
                if extracted:
                    text += extracted + '\n'
                    pdf_text += extracted + '\n'
            doc.close()
        except Exception as e:
            pdf_text = f"Could not extract text: {str(e)}"

        # Save to file as before
        filename = os.path.join(DATA_DIR, pdf.filename)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(pdf_text)

    return text


def get_text_chunks(text):
    """Split text into chunks of 1000 chars with 200 overlap"""
    chunks = []
    chunk_size = 1000
    overlap = 200
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


def get_vectorstore(text_chunks):
    """
    No longer uses FAISS or SentenceTransformer.
    Just joins chunks and stores as plain text.
    Returns the full text string instead of a vectorstore object.
    """
    if not text_chunks:
        raise ValueError("No text found in the uploaded PDF.")
    return "\n".join(text_chunks)


def get_conversation_chain(vectorstore):
    """
    No longer uses LangChain chains.
    Returns the raw text so we can pass it to Groq directly.
    """
    return vectorstore  # just return the text


def ask_groq(question, pdf_text, chat_history=[]):
    """
    Send question + PDF context directly to Groq API.
    No embeddings, no vector search — just context injection.
    """
    # Truncate PDF text to fit context window (keep first 6000 chars)
    context = pdf_text[:6000] if pdf_text else "No document loaded."

    # Build conversation history string (last 6 messages)
    history_text = ""
    for msg in chat_history[-6:]:
        if isinstance(msg, dict):
            role = "User" if msg.get('type') == 'human' else "Assistant"
            content = msg.get('content', '')
        else:
            role = "User" if msg.role == 'human' else "Assistant"
            content = msg.content
        history_text += f"{role}: {content}\n"

    messages = [
        SystemMessage(content=f"""You are Chat Scholar, an intelligent AI assistant 
that helps users understand documents. Answer questions based on the document below.
If the answer is not in the document, say so clearly.

DOCUMENT CONTENT:
{context}"""),
        HumanMessage(content=f"""Previous conversation:
{history_text}

Current question: {question}""")
    ]

    response = llm.invoke(messages)
    return response.content


def extract_text_from_file(file):
    """Extract text from PDF, image, txt, or docx files"""
    filename = file.filename.lower()
    ext = filename.rsplit('.', 1)[-1] if '.' in filename else ''

    # PDF
    if ext == 'pdf':
        text = ''
        file.seek(0)
        pdf_bytes = file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc:
            extracted = page.get_text()
            if extracted:
                text += extracted + '\n'
        doc.close()
        return text.strip()

    # Images
    elif ext in ['jpg', 'jpeg', 'png']:
        file.seek(0)
        image = Image.open(file)
        # ✅ Only use pytesseract if available
        try:
            text = pytesseract.image_to_string(image)
            return text.strip()
        except Exception:
            return "Image text extraction not available on this server."

    # Plain text
    elif ext == 'txt':
        file.seek(0)
        return file.read().decode('utf-8', errors='ignore')

    # Word documents
    elif ext == 'docx':
        from docx import Document
        file.seek(0)
        doc = Document(file)
        text = '\n'.join([para.text for para in doc.paragraphs])
        return text.strip()

    return ''


ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png', 'txt', 'docx'}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def chat_render(**kwargs):
    user_pdfs = []
    if current_user.is_authenticated:
        user_pdfs = PDFHistory.query.filter_by(
            user_id=current_user.id
        ).order_by(PDFHistory.uploaded_at.desc()).all()
    return render_template('new_chat.html',
                           sessions=list(pdf_sessions.keys()),
                           active=active_session,
                           user_pdfs=user_pdfs,
                           **kwargs)

def get_external_resources(topic):
    messages = [
        SystemMessage(content="""
            You are a research assistant. Given a topic, return exactly 3 verified
            educational resource links in this EXACT format:
            1. [Title](URL) - Source name
            2. [Title](URL) - Source name
            3. [Title](URL) - Source name
            Only use: Wikipedia, Khan Academy, MIT OpenCourseWare, Coursera,
            ArXiv, Google Scholar, BBC, Nature, Science Daily.
            Return ONLY the 3 links, no extra text.
        """),
        HumanMessage(content=f"Find 3 verified educational resources about: {topic}")
    ]
    try:
        response = llm.invoke(messages)
        return markdown.markdown(response.content)
    except Exception:
        return None

def get_followup_questions(question, answer):
    messages = [
        SystemMessage(content="""
            Suggest exactly 3 short follow-up questions based on the Q&A.
            Return ONLY 3 questions, one per line, no numbering.
        """),
        HumanMessage(content=f"Question: {question}\nAnswer: {answer}")
    ]
    try:
        response = llm.invoke(messages)
        questions = [q.strip() for q in response.content.strip().split('\n') if q.strip()]
        return questions[:3]
    except Exception:
        return []

def _grade_essay(essay):
    import re
    essay = re.sub(r'\(cid:\d+\)', '', essay).strip()
    essay = essay[:3000] if len(essay) > 3000 else essay

    if not essay:
        return "<p style='color:red;'>⚠ Could not extract readable text.</p>"

    messages = [
        SystemMessage(content=f"""
            You are an English essay grading expert.
            Evaluate based on this rubric: {rubric_text}

            You MUST respond in this EXACT structured format:

            ## Grade: X/10

            ## Strengths
            - **Point 1 title**: explanation here
            - **Point 2 title**: explanation here
            - **Point 3 title**: explanation here

            ## Weaknesses
            - **Point 1 title**: explanation here
            - **Point 2 title**: explanation here
            - **Point 3 title**: explanation here

            ## Suggestions for Improvement
            - **Point 1 title**: explanation here
            - **Point 2 title**: explanation here
            - **Point 3 title**: explanation here

            STRICT RULES:
            - Always use ## for section headings
            - Always use - for bullet points
            - Always bold the point title using **title**
            - Never write plain paragraphs
            - Each bullet must have a bold title followed by colon and explanation
        """)
    ]
    messages.append(HumanMessage(content="ESSAY: " + essay))
    try:
        response = llm.invoke(messages)
        return markdown.markdown(response.content)
    except Exception as e:
        return f"<p style='color:red;'>⚠ Error: {str(e)}</p>"

@app.route("/delete_essay/<int:essay_id>", methods=["POST"])
@login_required
def delete_essay(essay_id):

    essay = EssayHistory.query.get_or_404(essay_id)

    # Security check
    if essay.user_id != current_user.id:
        return redirect(url_for("essay_history"))

    db.session.delete(essay)
    db.session.commit()

    return redirect(url_for("essay_history"))

# =====================
# AUTH ROUTES
# =====================
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect('/')
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        dob_str = request.form.get('date_of_birth', '')

        if not all([full_name, username, email, password, dob_str]):
            return render_template('signup.html', error="All fields are required.")
        if len(full_name) < 2:
            return render_template('signup.html', error="Please enter your full name.")
        if len(username) < 3:
            return render_template('signup.html', error="Username must be at least 3 characters.")
        if not validate_email(email):
            return render_template('signup.html', error="Please enter a valid email address.")
        if password != confirm_password:
            return render_template('signup.html', error="Passwords do not match.")

        is_valid_pw, pw_msg = validate_password(password)
        if not is_valid_pw:
            return render_template('signup.html', error=pw_msg)

        is_valid_age, dob_result = validate_age(dob_str)
        if not is_valid_age:
            return render_template('signup.html', error=dob_result)

        if User.query.filter_by(email=email).first():
            return render_template('signup.html', error="Email already registered.")
        if User.query.filter_by(username=username).first():
            return render_template('signup.html', error="Username already taken.")

        # ✅ Generate OTP
        otp = generate_otp()
        otp_expiry = datetime.datetime.now() + datetime.timedelta(minutes=10)

        # ✅ Try sending email with short timeout
        sent = send_otp_email(email, otp, full_name)

        if sent:
            # ✅ Email worked — store in session and go to OTP page
            session['pending_user'] = {
                'full_name': full_name,
                'username': username,
                'email': email,
                'password': bcrypt.generate_password_hash(password).decode('utf-8'),
                'date_of_birth': dob_str,
                'otp': otp,
                'otp_expiry': otp_expiry.isoformat()
            }
            return redirect('/verify_otp')
        else:
            # ✅ Email failed (Railway blocks SMTP)
            # Create user directly and auto-verify
            try:
                dob = datetime.datetime.strptime(dob_str, '%Y-%m-%d').date()
            except ValueError:
                dob = None

            try:
                new_user = User(
                    full_name=full_name,
                    username=username,
                    email=email,
                    password=bcrypt.generate_password_hash(password).decode('utf-8'),
                    date_of_birth=dob,
                    is_verified=True  # ✅ auto verify
                )
                db.session.add(new_user)
                db.session.commit()
                login_user(new_user)
                return redirect('/')
            except Exception as e:
                db.session.rollback()
                print(f"User creation error: {str(e)}")
                return render_template('signup.html',
                                       error=f"Signup failed: {str(e)}")

    return render_template('signup.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/verify_otp', methods=['GET', 'POST'])
def verify_otp():
    pending = session.get('pending_user')
    if not pending:
        return redirect('/signup')

    if request.method == 'POST':
        entered_otp = request.form.get('otp', '').strip()
        action = request.form.get('action', '')

        # ✅ Resend OTP
        if action == 'resend':
            otp = generate_otp()
            otp_expiry = datetime.datetime.now() + datetime.timedelta(minutes=10)
            pending['otp'] = otp
            pending['otp_expiry'] = otp_expiry.isoformat()
            session['pending_user'] = pending
            send_otp_email(pending['email'], otp, pending['full_name'])
            return render_template('verify_otp.html',
                                   email=pending['email'],
                                   success="New OTP sent to your email.")

        # ✅ Verify OTP
        expiry = datetime.datetime.fromisoformat(pending['otp_expiry'])
        if datetime.datetime.now() > expiry:
            return render_template('verify_otp.html',
                                   email=pending['email'],
                                   error="OTP has expired. Please request a new one.")

        if entered_otp != pending['otp']:
            return render_template('verify_otp.html',
                                   email=pending['email'],
                                   error="Invalid OTP. Please try again.")

        # ✅ Create user
        dob = datetime.datetime.strptime(pending['date_of_birth'], '%Y-%m-%d').date()
        user = User(
            full_name=pending['full_name'],
            username=pending['username'],
            email=pending['email'],
            password=pending['password'],
            date_of_birth=dob,
            is_verified=True
        )
        db.session.add(user)
        db.session.commit()
        session.pop('pending_user', None)
        login_user(user)
        return redirect('/')

    return render_template('verify_otp.html', email=pending.get('email', ''))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect('/')
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not email or not password:
            return render_template('login.html', error="All fields are required.")

        if not validate_email(email):
            return render_template('login.html', error="Please enter a valid email address.")

        user = User.query.filter_by(email=email).first()

        if not user:
            return render_template('login.html', error="No account found with this email.")

        if not bcrypt.check_password_hash(user.password, password):
            return render_template('login.html', error="Incorrect password.")

        if not user.is_verified:
            return render_template('login.html',
                                   error="Please verify your email first.")

        login_user(user)
        return redirect('/')

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/login')

@app.context_processor
def inject_notifications():

    if current_user.is_authenticated:

        notifications = Notification.query.filter_by(
            user_id=current_user.id
        ).order_by(Notification.created_at.desc()).limit(5).all()

        unread_count = Notification.query.filter_by(
            user_id=current_user.id,
            is_read=False
        ).count()

        return dict(
            notifications=notifications,
            unread_count=unread_count
        )

    return dict(
        notifications=[],
        unread_count=0
    )

# =====================
# Profile Section
# =====================

from werkzeug.utils import secure_filename
import os

UPLOAD_FOLDER = "static/uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():

    if request.method == 'POST':

        current_user.full_name = request.form.get("full_name")
        current_user.bio = request.form.get("bio")
        current_user.college = request.form.get("college")
        current_user.phone = request.form.get("phone")

        file = request.files.get("profile_image")

        if file and file.filename != "":
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            file.save(filepath)

            current_user.profile_image = filename

        db.session.commit()

        add_notification(current_user.id, "Profile updated successfully")

        return redirect(url_for("profile"))

    return render_template("profile.html")
# =====================
# MAIN ROUTES
# =====================

@app.route('/')
def home():
    return render_template('new_home.html')

@app.route('/pdf_chat', methods=['GET', 'POST'])
@login_required
def pdf_chat():
    user_pdfs = PDFHistory.query.filter_by(
        user_id=current_user.id
    ).order_by(PDFHistory.uploaded_at.desc()).all()



    return render_template('new_pdf_chat.html', user_pdfs=user_pdfs)

@app.route('/process', methods=['POST'])
@login_required
def process_documents():
    global active_session, pdf_sessions
    try:
        pdf_docs = request.files.getlist('pdf_docs')
        if not pdf_docs or pdf_docs[0].filename == '':
            return render_template('new_pdf_chat.html',
                                   error="Please upload at least one PDF.")

        # ✅ Check file size - max 10MB
        pdf_docs[0].seek(0, 2)
        file_size = pdf_docs[0].tell()
        pdf_docs[0].seek(0)
        if file_size > 10 * 1024 * 1024:
            return render_template('new_pdf_chat.html',
                                   error="PDF too large. Please upload a PDF under 10MB.")

        session_name = pdf_docs[0].filename.replace('.pdf', '')
        raw_text = get_pdf_text(pdf_docs)

        if not raw_text.strip():
            return render_template('new_pdf_chat.html',
                                   error="Could not extract text from the PDF.")

        # ✅ Limit text size to prevent memory issues
        raw_text = raw_text[:50000]

        # ✅ Store plain text directly — no FAISS, no vectorstore
        pdf_sessions[session_name] = {
            'text': raw_text,
            'chain': raw_text  # kept for compatibility
        }
        active_session = session_name
        session['active_session'] = session_name

        pdf_record = PDFHistory(
            user_id=current_user.id,
            filename=pdf_docs[0].filename
        )
        db.session.add(pdf_record)
        db.session.commit()
        session['current_pdf_id'] = pdf_record.id

        return redirect('/chat')

    except Exception as e:
        return render_template('new_pdf_chat.html', error=f"Error: {str(e)}")


@app.route('/chat', methods=['GET', 'POST'])
@login_required
def chat():
    global active_session, pdf_sessions
    resources = None
    followups = []
    summary = None
    chat_history = []

    current_pdf_id = session.get('current_pdf_id')
    active = session.get('active_session', active_session)

    # ✅ Get stored plain text instead of vectorstore
    pdf_text = pdf_sessions.get(active, {}).get('text', '')

    if current_pdf_id:
        chat_history = ChatHistory.query.filter_by(
            user_id=current_user.id,
            pdf_id=current_pdf_id
        ).order_by(ChatHistory.timestamp).all()

    if request.method == 'POST':
        user_question = request.form.get('user_question', '').strip()

        if not user_question:
            return chat_render(chat_history=[], summary=None,
                               resources=None, followups=[])

        if not pdf_text:
            return chat_render(chat_history=[], summary=None,
                               resources=None, followups=[],
                               error="Please upload a PDF first.")

        try:
            # ✅ Build history list for context
            history_list = [{
                'type': msg.role,
                'content': msg.content
            } for msg in chat_history]

            # ✅ Use ask_groq instead of conversation_chain
            ai_answer = ask_groq(user_question, pdf_text, history_list)

            # ✅ Save both messages to DB
            if current_pdf_id:
                db.session.add(ChatHistory(
                    user_id=current_user.id,
                    pdf_id=current_pdf_id,
                    role='human',
                    content=user_question
                ))
                db.session.add(ChatHistory(
                    user_id=current_user.id,
                    pdf_id=current_pdf_id,
                    role='ai',
                    content=ai_answer
                ))
                db.session.commit()

            # ✅ Get followups every 2nd question to save API calls
            if len(chat_history) % 4 == 0:
                followups = get_followup_questions(user_question, ai_answer)

            # ✅ Get resources
            resources = get_external_resources(user_question)

            # ✅ Reload from DB after saving
            if current_pdf_id:
                chat_history = ChatHistory.query.filter_by(
                    user_id=current_user.id,
                    pdf_id=current_pdf_id
                ).order_by(ChatHistory.timestamp).all()

        except Exception as e:
            return chat_render(chat_history=chat_history, summary=None,
                               resources=None, followups=[],
                               error=f"Error: {str(e)}")

    # ✅ Format history for template
    formatted_history = []
    for msg in chat_history:
        role = msg.role if hasattr(msg, 'role') else msg.type
        content = msg.content
        time = (msg.timestamp.strftime("%I:%M %p")
                if hasattr(msg, 'timestamp')
                else datetime.datetime.now().strftime("%I:%M %p"))
        formatted_history.append({
            'type': role,
            'content': markdown.markdown(content) if role == 'ai' else content,
            'time': time
        })

    return chat_render(
        chat_history=formatted_history,
        resources=resources,
        followups=followups,
        summary=summary
    )

@app.route('/pdf_history')
@login_required
def pdf_history():
    pdfs = PDFHistory.query.filter_by(
        user_id=current_user.id
    ).order_by(PDFHistory.uploaded_at.desc()).all()
    return render_template('pdf_history.html', pdfs=pdfs)

@app.route('/delete_pdf/<int:pdf_id>', methods=['POST'])
@login_required
def delete_pdf(pdf_id):
    pdf = PDFHistory.query.get_or_404(pdf_id)
    if pdf.user_id != current_user.id:
        return redirect(url_for('pdf_history'))

    # Delete all chats for this PDF first
    ChatHistory.query.filter_by(pdf_id=pdf_id).delete()

    # Clear session if this was the active PDF
    if session.get('current_pdf_id') == pdf_id:
        session.pop('current_pdf_id', None)

    # Remove from pdf_sessions if loaded
    pdf_name = pdf.filename.replace('.pdf', '')
    if pdf_name in pdf_sessions:
        del pdf_sessions[pdf_name]

    db.session.delete(pdf)
    db.session.commit()
    return redirect(url_for('pdf_history'))

@app.route('/load_pdf_chat/<int:pdf_id>')
@login_required
def load_pdf_chat(pdf_id):
    pdf = PDFHistory.query.get_or_404(pdf_id)
    if pdf.user_id != current_user.id:
        return redirect('/pdf_history')
    session['current_pdf_id'] = pdf_id
    return redirect('/chat')

@app.route('/clear_chat', methods=['POST'])
@login_required
def clear_chat():
    current_pdf_id = session.get('current_pdf_id')
    if current_pdf_id:
        ChatHistory.query.filter_by(
            user_id=current_user.id,
            pdf_id=current_pdf_id
        ).delete()
        db.session.commit()
    return redirect('/chat')

@app.route('/summarize_chat', methods=['POST'])
@login_required
def summarize_chat():
    current_pdf_id = session.get('current_pdf_id')
    db_chats = ChatHistory.query.filter_by(
        user_id=current_user.id,
        pdf_id=current_pdf_id
    ).order_by(ChatHistory.timestamp).all() if current_pdf_id else []

    if not db_chats:
        return redirect('/chat')

    convo = ""
    for msg in db_chats:
        role = "User" if msg.role == 'human' else "AI"
        convo += f"{role}: {msg.content}\n\n"

    messages = [
        SystemMessage(content="""
            Summarize this conversation concisely:
            **Main Topics:** • topic
            **Key Points:** • point
            **Conclusions:** • conclusion
        """),
        HumanMessage(content=f"Summarize:\n\n{convo}")
    ]
    try:
        response = llm.invoke(messages)
        summary = markdown.markdown(response.content)
    except Exception as e:
        summary = f"<p class='text-red-500'>⚠ Error: {str(e)}</p>"

    formatted_history = [{
        'type': msg.role,
        'content': markdown.markdown(msg.content) if msg.role == 'ai' else msg.content,
        'time': msg.timestamp.strftime("%I:%M %p")
    } for msg in db_chats]

    return chat_render(
        chat_history=formatted_history,
        summary=summary,
        resources=None,
        followups=[]
    )

@app.route('/switch_session', methods=['POST'])
@login_required
def switch_session():
    global active_session
    session_name = request.form.get('session_name')
    if session_name in pdf_sessions:
        active_session = session_name
    return redirect('/chat')

import re as re_module

@app.route('/essay_grading', methods=['GET', 'POST'])
def essay_grading():
    result = None
    input_text = None
    score = None
    strengths = []
    weaknesses = []
    suggestions = []

    if request.method == 'POST':
        if request.form.get('essay_rubric', False):
            global rubric_text
            rubric_text = request.form.get('essay_rubric')
           
            return render_template('new_essay_grading.html')

        elif 'essay_rubric' in request.form and not request.form.get('essay_rubric'):
            return render_template('new_essay_rubric.html', error="Please enter a grading rubric.")

        try:
            uploaded_file = request.files.get('file')
            if uploaded_file and uploaded_file.filename:
                if not allowed_file(uploaded_file.filename):
                    result = "<p style='color:red;'>⚠ Unsupported file type.</p>"
                else:
                    input_text = extract_text_from_file(uploaded_file)
            else:
                input_text = request.form.get('essay_text', '').strip()

            if input_text:
                result = _grade_essay(input_text)

                # ✅ Parse score
                score_match = re_module.search(r'(\d+(?:\.\d+)?)\s*/\s*10', result or '')
                if score_match:
                    score = float(score_match.group(1))

                # ✅ Parse sections from markdown result
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(result or '', 'html.parser')
                all_li = soup.find_all('li')

                # Simple split by section headers
                raw = result or ''
                if 'STRENGTHS' in raw.upper():
                    parts = re_module.split(r'(?i)strengths|weaknesses|suggestions', raw)
                    strengths   = [li.get_text() for li in all_li[:3]]
                    weaknesses  = [li.get_text() for li in all_li[3:6]]
                    suggestions = [li.get_text() for li in all_li[6:]]

                # ✅ Save to essay history
                if current_user.is_authenticated and result and input_text:
                    essay_record = EssayHistory(
                        user_id=current_user.id,
                        essay_text=input_text[:500],
                        result=result
                    )
                    db.session.add(essay_record)
                    db.session.commit()
                    add_notification(current_user.id, "Essay graded successfully")

        except Exception as e:
            result = f"<p style='color:red;'>⚠ Error: {str(e)}</p>"

    return render_template('new_essay_grading.html',
                           result=result,
                           input_text=input_text,
                           score=score,
                           strengths=strengths,
                           weaknesses=weaknesses,
                           suggestions=suggestions)

@app.route('/essay_history')
@login_required
def essay_history():
    essays = EssayHistory.query.filter_by(
        user_id=current_user.id
    ).order_by(EssayHistory.timestamp.desc()).all()
    return render_template('essay_history.html', essays=essays)

@app.route('/essay_rubric', methods=['GET', 'POST'])
def essay_rubric():
    return render_template('new_essay_rubric.html')


# =====================
# SHARED CHAT FUNCTIONALITY
# =====================

import uuid
import datetime

class SharedChat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    share_id = db.Column(db.String(36), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    pdf_id = db.Column(db.Integer, db.ForeignKey('pdf_history.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.now)

from flask import request, redirect, url_for, flash
from flask_login import login_required, current_user

@app.route('/share_chat', methods=['POST'])
@login_required
def share_chat():
    print("SHARE CLICKED")
    current_pdf_id = session.get('current_pdf_id')
    
    # ✅ Check if a PDF is loaded
    if not current_pdf_id:
        return chat_render(
            chat_history=[],
            resources=None,
            followups=[],
            summary=None,
            error="Please upload and process a PDF before sharing."
        )

    existing = SharedChat.query.filter_by(
        user_id=current_user.id,
        pdf_id=current_pdf_id
    ).first()

    if existing:
        share_url = f"http://127.0.0.1:5000/shared/{existing.share_id}"
    else:
        import uuid
        share_id = str(uuid.uuid4())
        shared = SharedChat(
            share_id=share_id,
            user_id=current_user.id,
            pdf_id=current_pdf_id
        )
        db.session.add(shared)
        db.session.commit()
        # share_url = f"http://127.0.0.1:5000/shared/{share_id}"
        share_url = request.host_url + "shared/" + share_id

    # ✅ Reload history for display
    chat_history = ChatHistory.query.filter_by(
        user_id=current_user.id,
        pdf_id=current_pdf_id
    ).order_by(ChatHistory.timestamp).all()

    formatted_history = [{
        'type': msg.role,
        'content': markdown.markdown(msg.content) if msg.role == 'ai' else msg.content,
        'time': msg.timestamp.strftime("%I:%M %p")
    } for msg in chat_history]

    return chat_render(
        chat_history=formatted_history,
        resources=None,
        followups=[],
        summary=None,
        share_url=share_url
    )

@app.route('/shared/<share_id>')
def view_shared_chat(share_id):

    shared = SharedChat.query.filter_by(share_id=share_id).first_or_404()

    chats = ChatHistory.query.filter_by(
        pdf_id=shared.pdf_id
    ).order_by(ChatHistory.timestamp).all()

    pdf = PDFHistory.query.get(shared.pdf_id)
    owner = User.query.get(shared.user_id)

    formatted = []
    for msg in chats:
        formatted.append({
            'type': msg.role,
            'content': markdown.markdown(msg.content) if msg.role == 'ai' else msg.content,
            'time': msg.timestamp.strftime("%I:%M %p")
        })

    return render_template(
        'shared_chat.html',
        chat_history=formatted,
        pdf_name=pdf.filename if pdf else "Unknown",
        shared_by=owner.username if owner else "Unknown"
    )

# =====================
# CREATE DB AND RUN
# =====================

# ✅ At bottom of app1.py
with app.app_context():
    db.create_all()
    print("Database ready")

@app.route('/admin/users')
def admin_users():
    key = request.args.get('key')
    if key != 'chatscholar_admin_2024':
        return "Access denied", 403
    users = User.query.all()
    html = """<html><head><title>Admin</title>
    <style>
        body{font-family:Inter,sans-serif;background:#031427;color:#d3e4fe;padding:32px;}
        h1{color:#adc6ff;} table{width:100%;border-collapse:collapse;margin-top:24px;}
        th{background:#1b2b3f;padding:12px;text-align:left;color:#adc6ff;}
        td{padding:10px 12px;border-bottom:1px solid #26364a;}
        tr:hover{background:#0b1c30;}
        .yes{background:#00a572;color:#003824;padding:2px 8px;border-radius:999px;font-size:12px;}
        .no{background:#93000a;color:#ffdad6;padding:2px 8px;border-radius:999px;font-size:12px;}
    </style></head><body>
    <h1>Chat Scholar — Admin Panel</h1>
    <p>Total users: <strong style="color:#4edea3;">""" + str(len(users)) + """</strong></p>
    <table><tr>
        <th>#</th><th>Username</th><th>Full Name</th>
        <th>Email</th><th>Verified</th><th>Joined</th>
        <th>PDFs</th><th>Essays</th>
    </tr>"""
    for i, user in enumerate(users, 1):
        verified = '<span class="yes">✓ Yes</span>' if user.is_verified else '<span class="no">✗ No</span>'
        pdf_count = PDFHistory.query.filter_by(user_id=user.id).count()
        essay_count = EssayHistory.query.filter_by(user_id=user.id).count()
        joined = user.created_at.strftime('%b %d, %Y') if user.created_at else 'N/A'
        html += f"<tr><td>{i}</td><td><strong>{user.username}</strong></td><td>{user.full_name}</td><td>{user.email}</td><td>{verified}</td><td>{joined}</td><td>{pdf_count}</td><td>{essay_count}</td></tr>"
    html += "</table></body></html>"
    return html


@app.route('/admin/stats')
def admin_stats():
    key = request.args.get('key')
    if key != 'chatscholar_admin_2024':
        return "Access denied", 403
    import json
    data = {
        'total_users': User.query.count(),
        'verified_users': User.query.filter_by(is_verified=True).count(),
        'total_pdfs': PDFHistory.query.count(),
        'total_essays': EssayHistory.query.count(),
        'total_chats': ChatHistory.query.count(),
        'users': [{'id': u.id, 'username': u.username, 'email': u.email,
                   'verified': u.is_verified, 'joined': str(u.created_at)}
                  for u in User.query.all()]
    }
    return json.dumps(data, indent=2), 200, {'Content-Type': 'application/json'}

if __name__ == '__main__':
    app.run(debug=False, use_reloader=False, threaded=True)