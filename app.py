import os
import math # Added for distance calculation
import base64
import cv2
import face_recognition
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

# --- Dataverse Service Layer ---
from dataverse_service import (
    get_user_by_email,
    get_user_by_id,
    get_user_by_employeeid,
    create_user,
    update_user_face_encoding,
    update_user_password,
    get_all_employees,
    delete_user,
    create_attendance,
    find_open_attendance,
    find_open_meeting_attendance,
    update_attendance,
    get_attendance_by_date,
    get_attendance_by_name_and_date,
    delete_attendance_by_employee,
    USERS_ID_FIELD,
    ATTENDANCE_ID_FIELD,
)

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

BREVO_API_KEY = os.getenv("BREVO_API_KEY")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
COMPANY_NAME = os.getenv("COMPANY_NAME")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL") # Ensure this is in your .env

# --- NEW: LOCATION MISMATCH HELPERS ---

def get_distance_meters(lat1, lon1, lat2, lon2):
    """Calculates distance between two points in meters using Haversine formula."""
    try:
        if None in [lat1, lon1, lat2, lon2]: return 0
        lat1, lon1, lat2, lon2 = map(float, [lat1, lon1, lat2, lon2])
        radius = 6371000  # Earth radius in meters
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2)
        return radius * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))
    except:
        return 0

def send_location_alert_email(user_email, name, dist, login_pos, logout_pos):
    """Sends a security alert email via Brevo if location mismatch occurs."""
    login_map = f"https://www.google.com/maps?q={login_pos}"
    logout_map = f"https://www.google.com/maps?q={logout_pos}"
    
    subject = f"🚨 Security Alert: Location Mismatch - {name}"
    html_content = f"""
    <div style="font-family: sans-serif; padding: 20px; border: 2px solid #ff4b2b; border-radius: 10px;">
        <h2 style="color: #ff4b2b;">Location Mismatch Detected</h2>
        <p><b>Employee:</b> {name} ({user_email})</p>
        <p><b>Distance Mismatch:</b> {round(dist, 2)} meters</p>
        <hr>
        <p>📍 <b>Login Location:</b> <a href="{login_map}">View on Google Maps</a></p>
        <p>📍 <b>Logout Location:</b> <a href="{logout_map}">View on Google Maps</a></p>
        <p style="color: #888; font-size: 12px; margin-top: 20px;">This is an automated security alert from {COMPANY_NAME}.</p>
    </div>
    """
    send_email_via_brevo(ADMIN_EMAIL, subject, html_content)

# --- NORMALIZATION HELPERS ---
def _norm_user(dv_record: dict) -> dict:
    if not dv_record: return {}
    return {
        'First Name': dv_record.get('crc6f_firstname', ''),
        'Last Name': dv_record.get('crc6f_lastname', ''),
        'Email': dv_record.get('crc6f_email', ''),
        'Password': dv_record.get('crc6f_password', ''),
        'Role': dv_record.get('crc6f_role', ''),
        'FaceEncoding': dv_record.get('crc6f_faceencoding1', ''),
        'Status': dv_record.get('crc6f_status', ''),
        'EmployeeID': dv_record.get('crc6f_employeeid', ''),
        'record_id': dv_record.get(USERS_ID_FIELD, ''),
    }

def _norm_attendance(dv_record: dict) -> dict:
    if not dv_record: return {}
    return {
        'First Name': dv_record.get('crc6f_firstname', ''),
        'Last Name': dv_record.get('crc6f_lastname', ''),
        'Date': dv_record.get('crc6f_date', ''),
        'Login Time': dv_record.get('crc6f_logintime', ''),
        'Logout Time': dv_record.get('crc6f_logouttime', ''),
        'Status': dv_record.get('crc6f_status', ''),
        'Login Location': dv_record.get('crc6f_loginlocation', ''),
        'Logout Location': dv_record.get('crc6f_logoutlocation', ''),
        'record_id': dv_record.get(ATTENDANCE_ID_FIELD, ''),
    }

# --- HELPERS (Existing) ---
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

def _generate_employee_id(email: str) -> str:
    return email.lower()

# --- ROUTES ---

@app.route('/')
def index(): 
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()
        try:
            dv_user = get_user_by_email(email)
            if not dv_user:
                flash("User not found.", "error")
                return render_template('login.html')
            
            user = _norm_user(dv_user)
            
            if check_password_hash(user['Password'], password):
                session.clear()
                session.permanent = True
                app.permanent_session_lifetime = timedelta(minutes=120) # 2 Hours
                session.update({
                    'user_id': user['record_id'],
                    'first_name': user['First Name'], 
                    'last_name': user['Last Name'], 
                    'employee_id': user['EmployeeID'],
                    'role': user['Role'].lower(),
                    'verified': False,
                    'last_auth': time.time() 
                })
                
                if user['Status'] is True:
                    return redirect(url_for('reset_password_page'))
                
                if user['Role'].lower() == 'admin': 
                    session['verified'] = True
                    return redirect(url_for('admin_dashboard'))
                
                if not user['FaceEncoding']:
                    flash("Face not registered. Check your email.", "error")
                    return redirect(url_for('login'))
                
                return redirect(url_for('verify_face'))
            else:
                flash("Invalid credentials.", "error")
        except Exception as e:
            print(f"Login error: {e}")
            flash("User not found.", "error")
    return render_template('login.html')

@app.route('/reset-password')
def reset_password_page(): 
    return render_template('reset_password.html')

# --- FACE PROCESSING ---

@app.route('/register_face/<record_id>')
def register_face(record_id): 
    session['registering_id'] = record_id
    return render_template('register_face.html', user_id=record_id)

@app.route('/process_registration', methods=['POST'])
def process_registration():
    record_id = session.get('registering_id')
    if not record_id: return jsonify({"success": False, "message": "Session expired"})
    data = request.get_json()
    img_data = base64.b64decode(data['image'].split(',')[1])
    img = cv2.imdecode(np.frombuffer(img_data, np.uint8), cv2.IMREAD_COLOR)
    small_img = cv2.resize(img, (0, 0), fx=0.25, fy=0.25)
    rgb_small_img = cv2.cvtColor(small_img, cv2.COLOR_BGR2RGB)
    encs = face_recognition.face_encodings(rgb_small_img)
    if len(encs) > 0:
        encoding_str = json.dumps(encs[0].tolist())
        update_user_face_encoding(record_id, encoding_str)
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "No face detected"})

@app.route('/verify-face')
def verify_face():
    if 'user_id' not in session: return redirect(url_for('login'))
    mode = request.args.get('mode', 'login') 
    return render_template('verify_face.html', name=session.get('first_name'), mode=mode)

@app.route('/process_verification', methods=['POST'])
def process_verification():
    data = request.get_json()
    record_id = session.get('user_id')
    mode = data.get('mode', 'login')
    loc_text = data.get('detailed_location', 'Unknown')
    map_link = data.get('location', '#')
    full_loc_string = f"{loc_text} | {map_link}" 
    
    # Extract Raw Coordinates for Mismatch Check
    lat = data.get('lat')
    lon = data.get('lon')

    try:
        dv_user = get_user_by_id(record_id)
        user = _norm_user(dv_user)
        stored_enc = np.array(json.loads(user['FaceEncoding']))
        img_data = base64.b64decode(data['image'].split(',')[1])
        img = cv2.imdecode(np.frombuffer(img_data, np.uint8), cv2.IMREAD_COLOR)
        rgb_small = cv2.cvtColor(cv2.resize(img, (0,0), fx=0.25, fy=0.25), cv2.COLOR_BGR2RGB)
        live_enc = face_recognition.face_encodings(rgb_small)

        if len(live_enc) > 0 and face_recognition.compare_faces([stored_enc], live_enc[0], tolerance=0.5)[0]:
            IST = pytz.timezone('Asia/Kolkata')
            now = datetime.now(IST)
            today, cur_time = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
            
            if mode == 'logout':
                # --- LOCATION MISMATCH CHECK ---
                open_rec = find_open_attendance(user['First Name'], today)
                if open_rec:
                    login_loc_str = open_rec.get('crc6f_loginlocation', '')
                    login_lat, login_lon = None, None
                    if "q=" in login_loc_str:
                        try:
                            coords = login_loc_str.split("q=")[-1].split(',')
                            login_lat = float(coords[0])
                            login_lon = float(coords[1])
                        except:
                            pass
                    
                    if login_lat is not None and login_lon is not None:
                        distance = get_distance_meters(login_lat, login_lon, lat, lon)
                        if distance > 500:
                            send_location_alert_email(
                                user['Email'], 
                                user['First Name'], 
                                distance, 
                                f"{login_lat},{login_lon}", 
                                f"{lat},{lon}"
                            )

                    update_attendance(open_rec[ATTENDANCE_ID_FIELD], {
                        "crc6f_logouttime": cur_time,
                        "crc6f_status": "Present",
                        "crc6f_logoutlocation": full_loc_string,
                    })
                session.clear() 
                return jsonify({"success": True, "redirect": "/"})

            elif mode == 'login':
                create_attendance(
                    first_name=user['First Name'],
                    last_name=user['Last Name'],
                    date_str=today,
                    login_time=cur_time,
                    status="Present",
                    login_location=full_loc_string,
                    employee_id=user['EmployeeID'],
                )
                session.update({'verified': True, 'last_auth': time.time()})
                return jsonify({"success": True, "redirect": "/employee/dashboard"})
            
        return jsonify({"success": False, "message": "Face not recognized."})
    except Exception as e:
        print(f"Verification error: {e}")
        return jsonify({"success": False, "message": "Server error."})

# --- SECURE LOGOUT PREPARATION ---
@app.route('/prepare_logout', methods=['POST'])
def prepare_logout():
    if 'user_id' not in session: return jsonify({"success": False})
    try:
        IST = pytz.timezone('Asia/Kolkata')
        now = datetime.now(IST)
        today, cur_time = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        
        meeting_rec = find_open_meeting_attendance(session.get('first_name'), today)
        if meeting_rec:
            update_attendance(meeting_rec[ATTENDANCE_ID_FIELD], {
                "crc6f_logouttime": cur_time,
            })
        return jsonify({"success": True})
    except:
        return jsonify({"success": False})

@app.route('/auto_logout_record', methods=['POST'])
def auto_logout_record():
    if 'user_id' not in session: return jsonify({"success": False})
    try:
        IST = pytz.timezone('Asia/Kolkata')
        now = datetime.now(IST)
        today, cur_time = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        
        open_rec = find_open_attendance(session.get('first_name'), today)
        if open_rec:
            update_attendance(open_rec[ATTENDANCE_ID_FIELD], {
                "crc6f_logouttime": cur_time,
                "crc6f_status": "Logged Out (Timeout)",
            })
        return jsonify({"success": True})
    except:
        return jsonify({"success": False})

@app.route('/start_meeting', methods=['POST'])
def start_meeting():
    if 'user_id' not in session: return jsonify({"success": False})
    data = request.get_json()
    duration = int(data.get('duration', 10)) 
    try:
        dv_user = get_user_by_id(session.get('user_id'))
        user = _norm_user(dv_user)
        IST = pytz.timezone('Asia/Kolkata')
        now = datetime.now(IST)
        today, start_time = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        end_time = (now + timedelta(minutes=duration)).strftime("%H:%M:%S")

        open_rec = find_open_attendance(user['First Name'], today)
        if open_rec:
            update_attendance(open_rec[ATTENDANCE_ID_FIELD], {
                "crc6f_logouttime": start_time,
                "crc6f_status": "Transition to Meeting",
            })
        
        create_attendance(
            first_name=user['First Name'],
            last_name=user['Last Name'],
            date_str=today,
            login_time=start_time,
            status="In Meeting",
            login_location="Meeting Popup",
            employee_id=user['EmployeeID'],
            logout_time=end_time,
            logout_location="Meeting Popup",
        )
        
        session.permanent = True
        app.permanent_session_lifetime = timedelta(seconds=(duration * 60))
        session['last_auth'] = time.time()
        return jsonify({"success": True})
    except:
        return jsonify({"success": False})

# --- DASHBOARDS ---

@app.route('/admin/dashboard')
def admin_dashboard():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    
    dv_employees = get_all_employees()
    today = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d")
    dv_attendance = get_attendance_by_date(today)
    
    attendance_list = [_norm_attendance(a) for a in dv_attendance]
    
    employee_list = []
    for dv_emp in dv_employees:
        emp = _norm_user(dv_emp)
        emp['logs'] = [a for a in attendance_list if a['First Name'] == emp['First Name']]
        employee_list.append(emp)
    
    return render_template('admin_dashboard.html', employees=employee_list)

@app.route('/employee/dashboard')
def employee_dashboard():
    if 'user_id' not in session or not session.get('verified'):
        return redirect(url_for('login'))
    
    today = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d")
    dv_records = get_attendance_by_name_and_date(session.get('first_name'), today)
    my_logs = [_norm_attendance(r) for r in dv_records]
    my_logs.reverse() 
    return render_template('employee_dashboard.html', name=session.get('first_name'), records=my_logs)

# --- MANAGEMENT ---

@app.route('/add_employee', methods=['POST'])
def add_employee():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    f = request.form.get('first_name')
    l = request.form.get('last_name')
    e = request.form.get('email', '').strip().lower()
    temp_pass = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(8))
    
    try:
        employee_id = _generate_employee_id(e)
        created_record = create_user(
            first_name=f,
            last_name=l,
            email=e,
            hashed_password=generate_password_hash(temp_pass),
            role='employee',
            employee_id=employee_id,
            status=True,
        )
        
        new_record_id = created_record.get(USERS_ID_FIELD)
        reg_link = f"https://biometric-attendance-system-tsca.onrender.com/register_face/{new_record_id}"
        
        email_html = f"""
        <div style="background-color: #121212; color: #ffffff; padding: 40px; font-family: sans-serif; border-radius: 10px;">
            <h1 style="color: #4c8bf5; font-size: 28px;">Hello {f},</h1>
            <p style="font-size: 18px; line-height: 1.6;">Your workspace account is ready. Please follow these steps:</p>
            <div style="background-color: #1a1a1a; padding: 25px; border-radius: 12px; margin: 25px 0; border: 1px solid #333;">
                <p style="margin: 0 0 10px 0;"><b>1. Temporary Password:</b> <code style="background-color: #333; padding: 4px 8px; border-radius: 6px; color: #4c8bf5;">{temp_pass}</code></p>
                <p style="margin: 0;"><b>2. Face Registration:</b> You must register your face profile before logging in.</p>
            </div>
            <a href="{reg_link}" style="display: inline-block; background-color: #4c8bf5; color: white; padding: 16px 32px; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 16px;">Register Face Now</a>
        </div>
        """
        send_email_via_brevo(e, "Account Ready - Action Required", email_html)
        flash(f"Employee {f} added!", "success")
    except Exception as err:
        print(f"Add employee error: {err}")
        flash(f"Error: {err}", "error")
    return redirect(url_for('admin_dashboard'))

@app.route('/delete_employee/<record_id>')
def delete_employee(record_id):
    if session.get('role') != 'admin': return redirect(url_for('login'))
    try:
        dv_user = get_user_by_id(record_id)
        user = _norm_user(dv_user)
        first_name = user['First Name']
        delete_attendance_by_employee(first_name)
        delete_user(record_id)
        flash(f"Employee {first_name} and records deleted.", "success")
    except Exception as e:
        print(f"Delete error: {e}")
        flash("Error during deletion.", "error")
    return redirect(url_for('admin_dashboard'))

@app.route('/update_password', methods=['POST'])
def update_password():
    record_id = session.get('user_id')
    if not record_id: return redirect(url_for('login'))
    new_pass = request.form.get('password')
    if not is_password_strong(new_pass):
        flash("Weak password!", "error")
        return redirect(url_for('reset_password_page'))
    
    update_user_password(record_id, generate_password_hash(new_pass), status=False)
    flash("Success! Please login.", "success")
    return redirect(url_for('login'))

@app.route('/logout')
def logout(): 
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)