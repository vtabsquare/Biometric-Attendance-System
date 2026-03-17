import os
import base64
import cv2
import face_recognition
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import numpy as np
import random
import time
import requests
import re
import json
import string
import sys
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_cors import CORS
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
        print("Warning: dlib not found.")

# --- INITIALIZATION ---
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET")
CORS(app) 

# --- GOOGLE SHEETS CONNECTION ---
def get_sheets():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_json = os.getenv("GOOGLE_CREDS_JSON")
    
    if creds_json:
        info = json.loads(creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("google_creds.json", scope)
        
    client = gspread.authorize(creds)
    spreadsheet = client.open("Office_Attendance_System")
    return spreadsheet.worksheet("users"), spreadsheet.worksheet("Attendance")

BREVO_API_KEY = os.getenv("BREVO_API_KEY")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
COMPANY_NAME = os.getenv("COMPANY_NAME")
otp_store = {} 

# --- HELPERS ---
def is_password_strong(password):
    """Checks for 8+ chars, 1 Uppercase, 1 Number, and 1 Special Character."""
    if len(password) < 8: return False
    if not re.search(r"[A-Z]", password): return False
    if not re.search(r"\d", password): return False
    if not re.search(r"[@$!%*?&#]", password): return False
    return True

def send_email_via_brevo(to_email, subject, html_content):
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {"accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json"}
    payload = {
        "sender": {"name": COMPANY_NAME, "email": SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_content
    }
    try:
        response = requests.post(url, json=payload, headers=headers)
        return response.status_code in [200, 201, 202]
    except:
        return False

# --- ROUTES ---

@app.route('/')
def index(): 
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email, password = request.form.get('email'), request.form.get('password')
        user_sheet, _ = get_sheets()
        try:
            cell = user_sheet.find(email.strip(), in_column=3)
            user_data = user_sheet.row_values(cell.row)
            
            if check_password_hash(user_data[4], password):
                # Track 'last_auth' for the 2-hour re-verification rule
                session.update({
                    'user_row': cell.row, 
                    'first_name': user_data[0], 
                    'last_name': user_data[1], 
                    'role': user_data[5],
                    'verified': False,
                    'last_auth': time.time() 
                })
                
                # Check for Force Password Change (Column 8)
                if len(user_data) > 7 and user_data[7] == "1":
                    return redirect(url_for('reset_password'))
                
                if user_data[5] == 'admin': 
                    return redirect(url_for('admin_dashboard'))
                
                if len(user_data) < 7 or not user_data[6]:
                    flash("Face not registered. Please check the registration link in your email.", "error")
                    return redirect(url_for('login'))
                
                return redirect(url_for('verify_face'))
        except:
            flash("Invalid credentials.", "error")
    return render_template('login.html')

# --- FORGOT / RESET PASSWORD ---

@app.route('/send-otp', methods=['POST'])
def send_otp():
    email = request.get_json().get('email')
    user_sheet, _ = get_sheets()
    try:
        cell = user_sheet.find(email, in_column=3)
        otp = random.randint(100000, 999999)
        otp_store[email] = {"otp": otp, "expiry": time.time() + 300}
        html = f"<h2>Your OTP: {otp}</h2><p>This code is valid for 5 minutes.</p>"
        send_email_via_brevo(email, "Password Reset OTP", html)
        return jsonify({"success": True})
    except:
        return jsonify({"success": False, "message": "Email not found"})

@app.route('/update_password', methods=['POST'])
def update_password():
    row_id = session.get('user_row') or session.get('reset_user_row')
    if not row_id: return redirect(url_for('login'))
    
    new_pass = request.form.get('password')
    if not is_password_strong(new_pass):
        flash("Password too weak! Must include 8+ chars, Uppercase, Number, and Special Char.", "error")
        return redirect(url_for('reset_password'))

    hashed = generate_password_hash(new_pass)
    user_sheet, _ = get_sheets()
    user_sheet.update_cell(row_id, 5, hashed)
    user_sheet.update_cell(row_id, 8, "0") # Reset force-change flag
    session.pop('reset_user_row', None)
    flash("Success! Password updated.", "success")
    return redirect(url_for('login'))

# --- ADMIN DASHBOARD ---

@app.route('/admin/dashboard')
def admin_dashboard():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    user_sheet, attn_sheet = get_sheets()
    employees = user_sheet.get_all_records()
    attendance = attn_sheet.get_all_records()
    today = datetime.now().strftime("%Y-%m-%d")

    employee_list = []
    for i, u in enumerate(employees, start=2):
        if u.get('Role') == 'employee':
            u['row_id'] = i
            u['is_registered'] = bool(u.get('Face Encoding'))
            # Filter all activity logs for this employee for the scrollable list
            u['logs'] = [row for row in attendance if row['First Name'] == u['First Name'] and row['Date'] == today]
            employee_list.append(u)
            
    return render_template('admin_dashboard.html', employees=employee_list)

# --- EMPLOYEE DASHBOARD & SESSION ---

@app.route('/employee/dashboard')
def employee_dashboard():
    # Security: Ensure face is verified and session hasn't timed out
    if 'user_row' not in session or not session.get('verified'):
        return redirect(url_for('login'))
    
    # 2-Hour Re-verification check
    if time.time() - session.get('last_auth', 0) > 7200:
        session['verified'] = False
        flash("Session expired. Please re-verify your face.", "error")
        return redirect(url_for('verify_face'))

    _, attn_sheet = get_sheets()
    today = datetime.now().strftime("%Y-%m-%d")
    all_attendance = attn_sheet.get_all_records()
    my_logs = [row for row in all_attendance if row['First Name'] == session.get('first_name') and row['Date'] == today]
    my_logs.reverse() 

    return render_template('employee_dashboard.html', name=session.get('first_name'), records=my_logs)

@app.route('/check_session')
def check_session():
    """Helper for frontend to check if 2 hours have passed."""
    if time.time() - session.get('last_auth', 0) > 7200:
        return jsonify({"expired": True})
    return jsonify({"expired": False})

# --- FACE LOGIC ---

@app.route('/process_verification', methods=['POST'])
def process_verification():
    data = request.get_json()
    row_id, mode = session.get('user_row'), data.get('mode')
    location_url = data.get('location', 'Location Not Shared')

    user_sheet, attn_sheet = get_sheets()
    user_data = user_sheet.row_values(row_id)
    stored_enc = np.array(json.loads(user_data[6]))
    
    img_data = base64.b64decode(data['image'].split(',')[1])
    img = cv2.imdecode(np.frombuffer(img_data, np.uint8), cv2.IMREAD_COLOR)
    live_enc = face_recognition.face_encodings(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    
    if len(live_enc) > 0 and face_recognition.compare_faces([stored_enc], live_enc[0], tolerance=0.5)[0]:
        now = datetime.now()
        today, current_time = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        
        if mode == 'logout':
            records = attn_sheet.get_all_records()
            for i, r in enumerate(records, start=2):
                if r['First Name'] == user_data[0] and r['Date'] == today and not r['Logout Time']:
                    attn_sheet.update_cell(i, 5, current_time)
                    break
        else:
            # Login: Save GPS link in Column G
            attn_sheet.append_row([user_data[0], user_data[1], today, current_time, "", "Present", location_url])
        
        session.update({'verified': True, 'last_auth': time.time()})
        return jsonify({"success": True})
    return jsonify({"success": False})

# --- OTHER ROUTES (Add/Edit/Delete/OTP/Reset) ---

@app.route('/forgot-password')
def forgot_password(): return render_template('forgot_password.html')

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json()
    email, otp = data.get('email'), data.get('otp')
    if email in otp_store and str(otp_store[email]['otp']) == str(otp):
        if time.time() < otp_store[email]['expiry']:
            user_sheet, _ = get_sheets()
            cell = user_sheet.find(email, in_column=3)
            session['reset_user_row'] = cell.row 
            return jsonify({"success": True})
    return jsonify({"success": False, "message": "Invalid OTP"})

@app.route('/reset-password')
def reset_password(): return render_template('reset_password.html')

@app.route('/add_employee', methods=['POST'])
def add_employee():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    f, l, e, d = request.form.get('first_name'), request.form.get('last_name'), request.form.get('email'), request.form.get('department')
    temp_pass = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(8))
    user_sheet, _ = get_sheets()
    user_sheet.append_row([f, l, e, d, generate_password_hash(temp_pass), 'employee', '', '1']) 
    reg_link = f"https://biometric-attendance-system-tsca.onrender.com/register_face/{user_sheet.find(e).row}"
    html = f"<h3>Welcome to the team!</h3><p>Your temporary password: <b>{temp_pass}</b></p><a href='{reg_link}'>Register your face now</a>"
    send_email_via_brevo(e, "Onboarding: Face Registration & Login", html)
    flash("Employee added and email sent!", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/edit_employee/<int:row_id>', methods=['POST'])
def edit_employee(row_id):
    if session.get('role') != 'admin': return redirect(url_for('login'))
    f, l, d = request.form.get('first_name'), request.form.get('last_name'), request.form.get('department')
    user_sheet, _ = get_sheets()
    user_sheet.update_cell(row_id, 1, f); user_sheet.update_cell(row_id, 2, l); user_sheet.update_cell(row_id, 4, d)
    flash("Employee updated!", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/delete_employee/<int:row_id>')
def delete_employee(row_id):
    if session.get('role') != 'admin': return redirect(url_for('login'))
    user_sheet, _ = get_sheets()
    user_sheet.delete_rows(row_id)
    flash("Employee deleted!", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/register_face/<int:user_id>')
def register_face(user_id): 
    session['registering_row'] = user_id
    return render_template('register_face.html', user_id=user_id)

@app.route('/logout')
def logout(): 
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)