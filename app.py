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
from datetime import datetime, timedelta
import pytz
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

os.environ["DLIB_USE_CUDA"] = "0"

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
        # 1. Force email to lowercase for consistent searching
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()
        
        user_sheet, _ = get_sheets()
        
        try:
            # 2. Find the user by email in Column C
            cell = user_sheet.find(email, in_column=3)
            user_data = user_sheet.row_values(cell.row)
            
            # 3. Check the password at Index 4 (Column E)
            if check_password_hash(user_data[4], password):
                session.clear()
                session.permanent = True
                app.permanent_session_lifetime = timedelta(hours=2)
                
                session.update({
                    'user_row': cell.row, 
                    'first_name': user_data[0], 
                    'last_name': user_data[1], 
                    'role': user_data[5].lower(), 
                    'verified': False,
                    'last_auth': time.time() 
                })
                
                # A. Check for Temporary Password (Reset Flag "1" in Column H)
                if len(user_data) >= 8 and user_data[7] == "1":
                    return redirect(url_for('reset_password'))
                
                # B. If Admin
                if user_data[5].lower() == 'admin': 
                    session['verified'] = True
                    return redirect(url_for('admin_dashboard'))
                
                # C. Check Face Registration (Column G)
                if len(user_data) < 7 or not user_data[6]:
                    flash("Face not registered. Please check your invite email.", "error")
                    return redirect(url_for('login'))
                
                return redirect(url_for('verify_face'))
            else:
                flash("Invalid password. Please try again.", "error")
        except Exception as e:
            print(f"Login Error: {e}")
            flash("User not found or connection error.", "error")
            
    return render_template('login.html')

@app.route('/add_employee', methods=['POST'])
def add_employee():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    f = request.form.get('first_name')
    l = request.form.get('last_name')
    # Force email to lowercase when saving to match the login search
    e = request.form.get('email', '').strip().lower()
    d = request.form.get('department')
    
    temp_pass = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(8))
    user_sheet, _ = get_sheets()
    
    try:
        # Save 8 columns to match your A-H structure
        user_sheet.append_row([f, l, e, d, generate_password_hash(temp_pass), 'employee', '', '1'])
        
        # Find row for the link
        user_row = user_sheet.find(e).row
        reg_link = f"https://biometric-attendance-system-tsca.onrender.com/register_face/{user_row}"

        email_html = f"""
        <div style="background-color: #121212; color: #ffffff; padding: 40px; font-family: Arial; border-radius: 10px;">
            <h1 style="color: #4c8bf5;">Hello {f},</h1>
            <p>Your workspace account is ready.</p>
            <p><b>Temporary Password:</b> {temp_pass}</p>
            <p><b>Face Registration:</b> Required before first login.</p>
            <a href="{reg_link}" style="background: #4c8bf5; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; display: inline-block; margin-top: 10px;">Register Face Now</a>
        </div>
        """
        send_email_via_brevo(e, "Account Ready - Action Required", email_html)
        flash(f"Employee {f} added and invite sent!", "success")
    except Exception as err:
        flash(f"Error adding employee: {err}", "error")
    return redirect(url_for('admin_dashboard'))

# --- FACE PROCESSING ---

@app.route('/register_face/<int:user_id>')
def register_face(user_id): 
    session['registering_row'] = user_id
    return render_template('register_face.html', user_id=user_id)

@app.route('/process_registration', methods=['POST'])
def process_registration():
    row_id = session.get('registering_row')
    if not row_id: return jsonify({"success": False, "message": "Session expired"})
    data = request.get_json()
    img_data = base64.b64decode(data['image'].split(',')[1])
    img = cv2.imdecode(np.frombuffer(img_data, np.uint8), cv2.IMREAD_COLOR)
    small_img = cv2.resize(img, (0, 0), fx=0.25, fy=0.25)
    rgb_small_img = cv2.cvtColor(small_img, cv2.COLOR_BGR2RGB)
    encs = face_recognition.face_encodings(rgb_small_img)
    if len(encs) > 0:
        encoding_str = json.dumps(encs[0].tolist())
        user_sheet, _ = get_sheets()
        user_sheet.update_cell(row_id, 7, encoding_str)
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "No face detected"})

@app.route('/verify-face')
def verify_face():
    if 'user_row' not in session: return redirect(url_for('login'))
    mode = request.args.get('mode', 'login') 
    return render_template('verify_face.html', name=session.get('first_name'), mode=mode)

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
    rgb_small = cv2.cvtColor(cv2.resize(img, (0,0), fx=0.25, fy=0.25), cv2.COLOR_BGR2RGB)
    live_enc = face_recognition.face_encodings(rgb_small)
    
    if len(live_enc) > 0 and face_recognition.compare_faces([stored_enc], live_enc[0], tolerance=0.5)[0]:
        IST = pytz.timezone('Asia/Kolkata')
        now = datetime.now(IST)
        today, current_time = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        
        if mode == 'logout':
            records = attn_sheet.get_all_records()
            for i, r in enumerate(records, start=2):
                if r['First Name'] == user_data[0] and r['Date'] == today and not r.get('Logout Time'):
                    attn_sheet.update_cell(i, 5, current_time) # Column E
                    attn_sheet.update_cell(i, 6, "Present")    # Column F
                    break
        else:
            attn_sheet.append_row([user_data[0], user_data[1], today, current_time, "", "Present", location_url])
        
        session.permanent = True
        app.permanent_session_lifetime = timedelta(hours=2)
        session.update({'verified': True, 'last_auth': time.time()})
        return jsonify({"success": True})
    return jsonify({"success": False})

# --- MEETING MODE ---

@app.route('/start_meeting', methods=['POST'])
def start_meeting():
    if 'user_row' not in session: return jsonify({"success": False})
    try:
        _, attn_sheet = get_sheets()
        IST = pytz.timezone('Asia/Kolkata')
        today = datetime.now(IST).strftime("%Y-%m-%d")
        records = attn_sheet.get_all_records()
        for i, r in enumerate(records, start=2):
            if r['First Name'] == session.get('first_name') and r['Date'] == today and not r.get('Logout Time'):
                attn_sheet.update_cell(i, 6, "In Meeting") # Column F
                break
        return jsonify({"success": True})
    except:
        return jsonify({"success": False})

# --- DASHBOARDS ---

@app.route('/admin/dashboard')
def admin_dashboard():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    user_sheet, attn_sheet = get_sheets()
    employees = user_sheet.get_all_records()
    attendance = attn_sheet.get_all_records()
    today = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d")

    employee_list = []
    for i, u in enumerate(employees, start=2):
        if u.get('Role') == 'employee':
            u['row_id'] = i
            u['is_registered'] = bool(u.get('Face Encoding'))
            u['logs'] = [row for row in attendance if row['First Name'] == u['First Name'] and row['Date'] == today]
            employee_list.append(u)
    return render_template('admin_dashboard.html', employees=employee_list)

@app.route('/employee/dashboard')
def employee_dashboard():
    if 'user_row' not in session or not session.get('verified'):
        return redirect(url_for('login'))
    
    # Check session server-side (hard limit 2 hours)
    if time.time() - session.get('last_auth', 0) > 7200:
        session['verified'] = False
        return redirect(url_for('verify_face'))

    _, attn_sheet = get_sheets()
    today = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d")
    all_attendance = attn_sheet.get_all_records()
    my_logs = [row for row in all_attendance if row['First Name'] == session.get('first_name') and row['Date'] == today]
    my_logs.reverse() 
    return render_template('employee_dashboard.html', name=session.get('first_name'), records=my_logs)

@app.route('/check_session')
def check_session():
    # Helper for frontend warning system
    elapsed = time.time() - session.get('last_auth', 0)
    if elapsed > 7200:
        return jsonify({"expired": True})
    return jsonify({"expired": False, "remaining": 7200 - elapsed})

# --- MANAGEMENT & CLEANUP ---

@app.route('/add_employee', methods=['POST'])
def add_employee():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    f = request.form.get('first_name')
    l = request.form.get('last_name')
    e = request.form.get('email').strip()
    d = request.form.get('department')
    
    temp_pass = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(8))
    user_sheet, _ = get_sheets()
    
    try:
        # Save to sheet
        user_sheet.append_row([f, l, e, d, generate_password_hash(temp_pass), 'employee', '', '1'])
        user_row = user_sheet.find(e).row
        reg_link = f"https://biometric-attendance-system-tsca.onrender.com/register_face/{user_row}"

        # DARK THEME EMAIL HTML (Matching your image)
        email_html = f"""
        <div style="background-color: #121212; color: #ffffff; padding: 40px; font-family: 'Inter', Arial, sans-serif; border-radius: 10px;">
            <h1 style="color: #4c8bf5; font-size: 28px;">Hello {f},</h1>
            <p style="font-size: 18px; color: #e0e0e0;">Your workspace account is ready. Please follow these steps:</p>
            
            <div style="margin: 25px 0;">
                <p style="font-size: 18px;"><b>1. Temporary Password:</b> <span style="color: #ffffff; background: #333; padding: 5px 10px; border-radius: 5px;">{temp_pass}</span></p>
                <p style="font-size: 18px;"><b>2. Face Registration:</b> You must register your face profile before logging in.</p>
            </div>

            <a href="{reg_link}" style="display: inline-block; background-color: #4c8bf5; color: white; padding: 15px 30px; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 18px; margin-top: 10px;">Register Face Now</a>
            
            <p style="margin-top: 30px; font-size: 14px; color: #999;">Note: You will be asked to change your password on first login.</p>
        </div>
        """
        
        send_email_via_brevo(e, "Account Ready - Action Required", email_html)
        flash(f"Employee {f} added and invite sent!", "success")
    except Exception as err:
        flash(f"Error adding employee: {err}", "error")

    return redirect(url_for('admin_dashboard'))

@app.route('/delete_employee/<int:row_id>')
def delete_employee(row_id):
    if session.get('role') != 'admin': return redirect(url_for('login'))
    user_sheet, attn_sheet = get_sheets()
    try:
        employee_data = user_sheet.row_values(row_id)
        first_name = employee_data[0]
        
        # 1. Delete from users
        user_sheet.delete_rows(row_id)
        
        # 2. Deep clean attendance (Reverse loop)
        attn_recs = attn_sheet.get_all_records()
        for i in range(len(attn_recs) + 1, 1, -1):
            if attn_sheet.cell(i, 1).value == first_name:
                attn_sheet.delete_rows(i)
                
        flash(f"Employee {first_name} and all logs deleted.", "success")
    except:
        flash("Deletion error.", "error")
    return redirect(url_for('admin_dashboard'))

# --- AUTH & PASSWORDS ---

@app.route('/send-otp', methods=['POST'])
def send_otp():
    email = request.get_json().get('email')
    user_sheet, _ = get_sheets()
    try:
        cell = user_sheet.find(email, in_column=3)
        otp = random.randint(100000, 999999)
        otp_store[email] = {"otp": otp, "expiry": time.time() + 300}
        send_email_via_brevo(email, "Password Reset OTP", f"<h2>OTP: {otp}</h2>")
        return jsonify({"success": True})
    except:
        return jsonify({"success": False})

@app.route('/update_password', methods=['POST'])
def update_password():
    row_id = session.get('user_row') or session.get('reset_user_row')
    if not row_id: return redirect(url_for('login'))
    new_pass = request.form.get('password')
    if not is_password_strong(new_pass):
        flash("Weak password!", "error")
        return redirect(url_for('reset_password'))
    user_sheet, _ = get_sheets()
    user_sheet.update_cell(row_id, 5, generate_password_hash(new_pass))
    user_sheet.update_cell(row_id, 8, "0")
    session.pop('reset_user_row', None)
    flash("Success! Please login.", "success")
    return redirect(url_for('login'))

@app.route('/logout')
def logout(): 
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)