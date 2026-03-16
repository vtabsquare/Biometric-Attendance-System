import os
import base64
import cv2
import face_recognition
import psycopg2
from psycopg2.extras import RealDictCursor
import numpy as np
import random
import time
import requests
import re
import json
import sys
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from dotenv import load_dotenv

# --- DLIB PATCH FOR RENDER MEMORY ---
try:
    import dlib
except ImportError:
    try:
        import dlib_bin as dlib
        sys.modules['dlib'] = dlib
    except ImportError:
        print("Warning: dlib not found. Face recognition will fail.")

# --- INITIALIZATION ---
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET")

# --- DATABASE CONNECTION (POSTGRESQL / SUPABASE) ---
def get_db():
    # Uses the single DATABASE_URL from your .env or Render Dashboard
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    return conn

BREVO_API_KEY = os.getenv("BREVO_API_KEY")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
COMPANY_NAME = os.getenv("COMPANY_NAME")
otp_store = {} 

# --- HELPER: PASSWORD STRENGTH ---
def is_password_strong(password):
    if len(password) < 8: return False
    if not re.search(r"[A-Z]", password): return False
    if not re.search(r"[@$!%*?&]", password): return False
    return True

# --- EMAIL ENGINE ---
def send_email_via_brevo(to_email, subject, html_content):
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json"
    }
    payload = {
        "sender": {"name": COMPANY_NAME, "email": SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_content
    }
    try:
        response = requests.post(url, json=payload, headers=headers)
        return response.status_code in [200, 201, 202]
    except Exception as e:
        print(f"Connection Error: {e}")
        return False

# --- ROUTES ---

@app.route('/')
def index(): 
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email, password = request.form.get('email'), request.form.get('password')
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        cur.close(); conn.close()
        
        if user and check_password_hash(user['password'], password):
            session.update({'user_id': user['id'], 'first_name': user['first_name'], 'role': user['role']})
            if user.get('is_temp_password') == 1:
                return redirect(url_for('reset_password'))
            if user['role'] == 'admin': 
                return redirect(url_for('admin_dashboard'))
            
            if not user['face_encoding']:
                flash("Face not registered.", "error")
                return redirect(url_for('login'))
            return redirect(url_for('verify_face'))
        flash("Invalid credentials.", "error")
    return render_template('login.html')

@app.route('/forgot-password')
def forgot_password(): 
    return render_template('forgot_password.html')

@app.route('/send-otp', methods=['POST'])
def send_otp():
    email = request.get_json().get('email')
    conn = get_db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM users WHERE email = %s", (email,))
    user = cur.fetchone()
    cur.close(); conn.close()
    
    if not user: return jsonify({"success": False, "message": "Email not found"})

    otp = random.randint(100000, 999999)
    otp_store[email] = {"otp": otp, "expiry": time.time() + 300}
    
    html = f"<div style='font-family:sans-serif;'><h2>OTP: {otp}</h2><p>Valid for 5 mins.</p></div>"
    if send_email_via_brevo(email, "Password Reset OTP", html):
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Email service error"})

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json()
    email, otp = data.get('email'), data.get('otp')
    if email in otp_store and str(otp_store[email]['otp']) == str(otp):
        if time.time() < otp_store[email]['expiry']:
            conn = get_db(); cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
            session['reset_user_id'] = user['id']
            cur.close(); conn.close()
            return jsonify({"success": True})
    return jsonify({"success": False, "message": "Invalid OTP"})

@app.route('/reset-password')
def reset_password(): 
    return render_template('reset_password.html')

@app.route('/update_password', methods=['POST'])
def update_password():
    user_id = session.get('user_id') or session.get('reset_user_id')
    if not user_id: return redirect(url_for('login'))
    
    new_pass = request.form.get('password')
    if not is_password_strong(new_pass):
        flash("Weak Password! 8+ chars, 1 Upper, 1 Special Required.", "error")
        return redirect(url_for('reset_password'))

    hashed = generate_password_hash(new_pass)
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE users SET password=%s, is_temp_password=0 WHERE id=%s", (hashed, user_id))
    conn.commit(); cur.close(); conn.close()
    session.pop('reset_user_id', None)
    flash("Password updated successfully!", "success")
    return redirect(url_for('login'))

@app.route('/admin/dashboard')
def admin_dashboard():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    conn = get_db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users WHERE role = 'employee'")
    employees = cur.fetchall()
    for emp in employees:
        cur.execute("""SELECT login_time, logout_time, status 
                       FROM attendance 
                       WHERE user_id = %s AND date = CURRENT_DATE 
                       ORDER BY login_time DESC""", (emp['id'],))
        emp['daily_logs'] = cur.fetchall()
        emp['is_registered'] = True if emp['face_encoding'] else False
    cur.close(); conn.close()
    return render_template('admin_dashboard.html', employees=employees)

@app.route('/add_employee', methods=['POST'])
def add_employee():
    f, l, e, d = request.form.get('first_name'), request.form.get('last_name'), request.form.get('email'), request.form.get('department')
    temp_pass = "Welcome@123"
    hashed = generate_password_hash(temp_pass)
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO users (first_name, last_name, email, department, password, role, is_temp_password, face_encoding) 
                   VALUES (%s,%s,%s,%s,%s,'employee',1, '')""", (f,l,e,d,hashed))
    conn.commit(); cur.close(); conn.close()
    
    html = f"<h3>Welcome {f}!</h3><p>Your temp pass is: {temp_pass}</p>"
    send_email_via_brevo(e, "Welcome to FaceAuth", html)
    return redirect(url_for('admin_dashboard'))

@app.route('/verify-face')
def verify_face(): 
    return render_template('verify_face.html', name=session.get('first_name'), mode=request.args.get('mode', 'login'))

@app.route('/process_verification', methods=['POST'])
def process_verification():
    data = request.get_json()
    user_id, mode, lat, lon = session['user_id'], data.get('mode'), data.get('lat'), data.get('lon')
    
    conn = get_db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT first_name, last_name, face_encoding FROM users WHERE id = %s", (user_id,))
    user = cur.fetchone()
    
    if not user or not user['face_encoding']:
        return jsonify({"success": False, "message": "No face data"})

    stored_enc = np.array(json.loads(user['face_encoding']))
    img_data = base64.b64decode(data['image'].split(',')[1])
    nparr = np.frombuffer(img_data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    live_enc = face_recognition.face_encodings(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    
    if len(live_enc) > 0 and face_recognition.compare_faces([stored_enc], live_enc[0], tolerance=0.5)[0]:
        now = datetime.now()
        if mode == 'logout':
            cur.execute("""UPDATE attendance SET logout_time=CURRENT_TIME 
                           WHERE user_id=%s AND date=CURRENT_DATE AND logout_time IS NULL""", (user_id,))
        else:
            cur.execute("""INSERT INTO attendance (user_id, first_name, last_name, date, login_time, status) 
                           VALUES (%s, %s, %s, CURRENT_DATE, CURRENT_TIME, 'Present') 
                           ON CONFLICT (user_id, date) DO NOTHING""", 
                        (user_id, user['first_name'], user['last_name']))
        conn.commit(); cur.close(); conn.close()
        session['verified'] = True
        return jsonify({"success": True})
    
    cur.close(); conn.close()
    return jsonify({"success": False})

@app.route('/employee/dashboard')
def employee_dashboard():
    if 'user_id' not in session or not session.get('verified'): return redirect(url_for('login'))
    conn = get_db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM attendance WHERE user_id=%s AND date=CURRENT_DATE ORDER BY login_time DESC", (session['user_id'],))
    records = cur.fetchall()
    cur.close(); conn.close()
    return render_template('employee_dashboard.html', records=records, name=session.get('first_name'))

@app.route('/register_face/<int:user_id>')
def register_face(user_id): 
    session['registering_id'] = user_id
    return render_template('register_face.html', user_id=user_id)

@app.route('/process_registration', methods=['POST'])
def process_registration():
    uid, data = session.get('registering_id'), request.get_json()
    img_data = base64.b64decode(data['image'].split(',')[1])
    nparr = np.frombuffer(img_data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    encs = face_recognition.face_encodings(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    
    if len(encs) > 0:
        encoding_str = json.dumps(encs[0].tolist())
        conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE users SET face_encoding = %s WHERE id = %s", (encoding_str, uid))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True})
    return jsonify({"success": False})

@app.route('/logout-request')
def logout_request(): 
    return redirect(url_for('verify_face', mode='logout'))

@app.route('/logout')
def logout(): 
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)