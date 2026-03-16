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

# --- GOOGLE SHEETS CONNECTION ---
def get_sheets():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    
    # Try to get credentials from Render Environment Variable first
    creds_json = os.getenv("GOOGLE_CREDS_JSON")
    
    if creds_json:
        # On Render: Use the JSON string from Environment Variables
        info = json.loads(creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
    else:
        # On Local: Use your local file
        creds = ServiceAccountCredentials.from_json_keyfile_name("google_creds.json", scope)
        
    client = gspread.authorize(creds)
    # Ensure this name matches your Google Sheet exactly
    spreadsheet = client.open("Office_Attendance_System")
    return spreadsheet.worksheet("users"), spreadsheet.worksheet("Attendance")

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
        user_sheet, _ = get_sheets()
        
        # Search for user by email in Column C (3)
        try:
            cell = user_sheet.find(email, in_column=3)
            user_data = user_sheet.row_values(cell.row)
            # Map values (A:1, B:2, C:3, D:4, E:5, F:6, G:7, H:8)
            user = {
                'row': cell.row,
                'first_name': user_data[0],
                'last_name': user_data[1],
                'email': user_data[2],
                'password': user_data[4],
                'role': user_data[5],
                'face_encoding': user_data[6] if len(user_data) > 6 else None,
                'is_temp': user_data[7] if len(user_data) > 7 else "0"
            }
            
            if check_password_hash(user['password'], password):
                session.update({'user_row': user['row'], 'first_name': user['first_name'], 'last_name': user['last_name'], 'role': user['role']})
                if user['is_temp'] == "1":
                    return redirect(url_for('reset_password'))
                if user['role'] == 'admin': 
                    return redirect(url_for('admin_dashboard'))
                
                if not user['face_encoding']:
                    flash("Face not registered.", "error")
                    return redirect(url_for('login'))
                return redirect(url_for('verify_face'))
        except:
            flash("Invalid credentials.", "error")
    return render_template('login.html')

@app.route('/forgot-password')
def forgot_password(): 
    return render_template('forgot_password.html')

@app.route('/send-otp', methods=['POST'])
def send_otp():
    email = request.get_json().get('email')
    user_sheet, _ = get_sheets()
    try:
        user_sheet.find(email, in_column=3)
        otp = random.randint(100000, 999999)
        otp_store[email] = {"otp": otp, "expiry": time.time() + 300}
        html = f"<div style='font-family:sans-serif;'><h2>OTP: {otp}</h2><p>Valid for 5 mins.</p></div>"
        if send_email_via_brevo(email, "Password Reset OTP", html):
            return jsonify({"success": True})
    except:
        return jsonify({"success": False, "message": "Email not found"})
    return jsonify({"success": False, "message": "Email service error"})

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
def reset_password(): 
    return render_template('reset_password.html')

@app.route('/update_password', methods=['POST'])
def update_password():
    row_id = session.get('user_row') or session.get('reset_user_row')
    if not row_id: return redirect(url_for('login'))
    
    new_pass = request.form.get('password')
    if not is_password_strong(new_pass):
        flash("Weak Password! 8+ chars, 1 Upper, 1 Special Required.", "error")
        return redirect(url_for('reset_password'))

    hashed = generate_password_hash(new_pass)
    user_sheet, _ = get_sheets()
    user_sheet.update_cell(row_id, 5, hashed) # Column E: Password
    user_sheet.update_cell(row_id, 8, "0")    # Column H: Status/is_temp
    session.pop('reset_user_row', None)
    flash("Password updated successfully!", "success")
    return redirect(url_for('login'))

@app.route('/admin/dashboard')
def admin_dashboard():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    user_sheet, attn_sheet = get_sheets()
    
    # Get all employees
    all_users = user_sheet.get_all_records()
    employees = [u for u in all_users if u['Role'] == 'employee']
    
    # Get today's logs
    today = datetime.now().strftime("%Y-%m-%d")
    all_logs = attn_sheet.get_all_records()
    
    for emp in employees:
        emp['daily_logs'] = [l for l in all_logs if l['First Name'] == emp['First Name'] and l['Date'] == today]
        emp['is_registered'] = True if emp['Face Encoding'] else False
    
    return render_template('admin_dashboard.html', employees=employees)

@app.route('/add_employee', methods=['POST'])
def add_employee():
    f, l, e, d = request.form.get('first_name'), request.form.get('last_name'), request.form.get('email'), request.form.get('department')
    temp_pass = "Welcome@123"
    hashed = generate_password_hash(temp_pass)
    
    user_sheet, _ = get_sheets()
    # A:First, B:Last, C:Email, D:Dept, E:Pass, F:Role, G:Encoding, H:is_temp
    user_sheet.append_row([f, l, e, d, hashed, 'employee', '', '1'])
    
    html = f"<h3>Welcome {f}!</h3><p>Your temp pass is: {temp_pass}</p>"
    send_email_via_brevo(e, "Welcome to FaceAuth", html)
    return redirect(url_for('admin_dashboard'))

@app.route('/verify-face')
def verify_face(): 
    return render_template('verify_face.html', name=session.get('first_name'), mode=request.args.get('mode', 'login'))

@app.route('/process_verification', methods=['POST'])
def process_verification():
    data = request.get_json()
    row_id, mode = session.get('user_row'), data.get('mode')
    
    user_sheet, attn_sheet = get_sheets()
    user_data = user_sheet.row_values(row_id)
    
    if len(user_data) < 7 or not user_data[6]:
        return jsonify({"success": False, "message": "No face data"})

    stored_enc = np.array(json.loads(user_data[6]))
    img_data = base64.b64decode(data['image'].split(',')[1])
    nparr = np.frombuffer(img_data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    live_enc = face_recognition.face_encodings(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    
    if len(live_enc) > 0 and face_recognition.compare_faces([stored_enc], live_enc[0], tolerance=0.5)[0]:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        current_time = now.strftime("%H:%M:%S")
        
        if mode == 'logout':
            # Find today's log for this user that doesn't have a logout time
            records = attn_sheet.get_all_records()
            for i, r in enumerate(records, start=2):
                if r['First Name'] == user_data[0] and r['Date'] == today and not r['Logout Time']:
                    attn_sheet.update_cell(i, 5, current_time) # Col E
                    break
        else:
            # Check if already logged in today
            records = attn_sheet.get_all_records()
            exists = any(r['First Name'] == user_data[0] and r['Date'] == today for r in records)
            if not exists:
                attn_sheet.append_row([user_data[0], user_data[1], today, current_time, "", "Present"])
        
        session['verified'] = True
        return jsonify({"success": True})
    
    return jsonify({"success": False})

@app.route('/employee/dashboard')
def employee_dashboard():
    if 'user_row' not in session or not session.get('verified'): return redirect(url_for('login'))
    _, attn_sheet = get_sheets()
    all_logs = attn_sheet.get_all_records()
    today = datetime.now().strftime("%Y-%m-%d")
    records = [l for l in all_logs if l['First Name'] == session['first_name'] and l['Date'] == today]
    
    return render_template('employee_dashboard.html', records=records, name=session.get('first_name'))

@app.route('/register_face/<int:user_id>')
def register_face(user_id): 
    # In Sheets version, we use the row number as the ID
    session['registering_row'] = user_id
    return render_template('register_face.html', user_id=user_id)

@app.route('/process_registration', methods=['POST'])
def process_registration():
    row_id = session.get('registering_row')
    data = request.get_json()
    img_data = base64.b64decode(data['image'].split(',')[1])
    nparr = np.frombuffer(img_data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    encs = face_recognition.face_encodings(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    
    if len(encs) > 0:
        encoding_str = json.dumps(encs[0].tolist())
        user_sheet, _ = get_sheets()
        user_sheet.update_cell(row_id, 7, encoding_str) # Column G
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