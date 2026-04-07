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
import jwt
from dotenv import load_dotenv
import urllib.parse

# --- Dataverse Service Layer ---
from dataverse_service import (
    get_user_by_email,
    get_user_by_id,
    get_user_by_employeeid,
    create_user,
    update_user_face_encoding,
    update_user_password,
    update_user_fields,
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
app.secret_key = os.getenv("FLASK_SECRET", "default-fallback-secret-key-1234")

# Important for cross-site iframe/redirect context cookies
app.config.update(
    SESSION_COOKIE_SAMESITE='None',
    SESSION_COOKIE_SECURE=True
)

CORS(app) 

BREVO_API_KEY = os.getenv("BREVO_API_KEY")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
COMPANY_NAME = os.getenv("COMPANY_NAME")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL") # Ensure this is in your .env
JWT_SECRET = os.getenv("JWT_SECRET") # Shared secret for HR Tool JWTs
FACEAUTH_BASE_URL = os.getenv("FACEAUTH_BASE_URL", "https://biometric-attendance-system-tsca.onrender.com")  # Production URL

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
        'AllowMobile': dv_record.get('crc6f_allowmobile', True),
        'AllowDesktop': dv_record.get('crc6f_allowdesktop', True),
        'RequireGPS': dv_record.get('crc6f_requiregps', True),
    }

def _norm_attendance(dv_record: dict) -> dict:
    if not dv_record: return {}
    def _parse_dt(val):
        """Extract HH:MM:SS from an ISO datetime string like '2026-03-20T19:28:31Z'."""
        if not val: return ''
        try:
            if 'T' in str(val):
                return str(val).split('T')[1].replace('Z', '').split('+')[0][:8]
            return str(val)
        except:
            return str(val)
    return {
        'First Name': dv_record.get('crc6f_firstname', ''),
        'Last Name': dv_record.get('crc6f_lastname', ''),
        'Date': dv_record.get('crc6f_date', ''),
        'Login Time': _parse_dt(dv_record.get('crc6f_logintime', '')),
        'Logout Time': _parse_dt(dv_record.get('crc6f_logouttime', '')),
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
    
    if not BREVO_API_KEY:
        print("[EMAIL ERROR] BREVO_API_KEY is not set")
        return False
    if not SENDER_EMAIL:
        print("[EMAIL ERROR] SENDER_EMAIL is not set")
        return False
    if not COMPANY_NAME:
        print("[EMAIL ERROR] COMPANY_NAME is not set")
        return False
    
    headers = {"accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json"}
    payload = {
        "sender": {"name": COMPANY_NAME, "email": SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_content
    }
    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code in [200, 201, 202]:
            print(f"[EMAIL SUCCESS] Email sent to {to_email}")
            return True
        else:
            print(f"[EMAIL ERROR] Failed to send email to {to_email}. Status: {response.status_code}, Response: {response.text}")
            return False
    except Exception as e:
        print(f"[EMAIL ERROR] Exception sending email to {to_email}: {e}")
        return False

def _generate_employee_id(email: str) -> str:
    return email.lower()

# --- ROUTES ---

@app.route('/')
def index(): 
    return render_template('landing.html')

@app.route('/health')
def health_check():
    return jsonify({"status": "ok"}), 200

@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": str(e)}), 400

@app.route('/external-verify')
def external_verify():
    token = urllib.parse.unquote(request.args.get('token', ''))
    callback_url = urllib.parse.unquote(request.args.get('callback_url', ''))

    if not token or not callback_url:
        return jsonify({"error": "Invalid or expired token"}), 400

    try:
        SECRET_KEY = os.environ.get('JWT_SECRET_KEY', str(JWT_SECRET) if JWT_SECRET else 'fallback')
        
        # Decode JWT
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS512"])
        
        employee_id = payload.get("employee_id")
        if not employee_id:
            return jsonify({"error": "Invalid or expired token"}), 400
        
        # Fetch user from Dataverse to get correct name
        dv_user = get_user_by_employeeid(employee_id)
        if not dv_user:
            return jsonify({"error": f"User with Employee ID {employee_id} not found"}), 404
        
        user = _norm_user(dv_user)
        
        # Clear session and store correct user info
        session.clear()
        session['pending_token'] = token
        session['callback_url'] = callback_url
        session['employee_id'] = employee_id
        session['external_auth'] = True
        session['first_name'] = user['First Name']
        session['require_gps'] = user.get('RequireGPS', True)

        return redirect("/verify-face")

    except Exception as e:
        print("JWT ERROR:", str(e))
        return jsonify({"error": "Invalid or expired token"}), 400

@app.route('/magic-register')
def magic_register():
    token = urllib.parse.unquote(request.args.get('token', ''))
    
    if not token:
        return jsonify({"error": "Invalid or missing token"}), 400

    try:
        SECRET_KEY = os.environ.get('JWT_SECRET_KEY', str(JWT_SECRET) if JWT_SECRET else 'fallback')
        
        # Decode JWT
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS512"])
        
        employee_id = payload.get("employee_id")
        action = payload.get("action")
        
        if not employee_id or action != "register":
            return jsonify({"error": "Invalid token action"}), 400
            
        dv_user = get_user_by_employeeid(employee_id)
        if not dv_user:
            return jsonify({"error": "User not found"}), 404
            
        record_id = dv_user.get(USERS_ID_FIELD)
        
        # Grant one-time registration access
        session.clear()
        session['user_id'] = record_id
        session['employee_id'] = employee_id
        session['magic_register'] = True
        session['first_name'] = dv_user.get('crc6f_firstname', 'Employee')

        return redirect(url_for('register_face', record_id=record_id))

    except Exception as e:
        print("MAGIC LINK ERROR:", str(e))
        return jsonify({"error": "Invalid or expired token. Please request a new link if necessary."}), 400

@app.route('/admin-sso')
def admin_sso():
    """
    SSO endpoint for HR Tool admins to access FaceAuth admin dashboard
    without re-entering credentials.
    
    URL: /admin-sso?token=<jwt_token>
    """
    token = request.args.get('token')
    
    if not token:
        print("[ADMIN-SSO] No token provided")
        return redirect('/login?error=missing_token')
    
    try:
        # IMPORTANT: Use HS512 algorithm (same as HR Tool)
        # IMPORTANT: JWT_SECRET_KEY must match HR Tool's id.env JWT_SECRET value
        SECRET_KEY = os.environ.get('JWT_SECRET_KEY', str(JWT_SECRET) if JWT_SECRET else 'fallback')
        payload = jwt.decode(
            token, 
            SECRET_KEY,  # Must be same as HR Tool
            algorithms=['HS512']  # HR Tool uses HS512
        )
        
        # Debug logging
        print(f"[ADMIN-SSO] Token decoded successfully")
        print(f"[ADMIN-SSO] Payload: {payload}")
        
        # Extract admin-related fields from HR Tool token
        access_level = payload.get('access_level', '')
        role = payload.get('role', '')
        is_admin = payload.get('is_admin', False)
        
        print(f"[ADMIN-SSO] access_level={access_level}, role={role}, is_admin={is_admin}")
        
        # Check if user is admin (any of these conditions)
        is_authorized = (
            access_level == 'L3' or 
            role == 'L3' or 
            is_admin == True
        )
        
        if not is_authorized:
            print(f"[ADMIN-SSO] User not authorized - access_level={access_level}, role={role}, is_admin={is_admin}")
            return redirect('/login?error=not_authorized')
        
        # Get user info from token
        email = payload.get('email', '')
        employee_id = payload.get('employee_id', '')
        name = payload.get('name', '')
        
        print(f"[ADMIN-SSO] Authorizing admin: {name} ({email})")
        
        # Create session for this user (auto-login)
        session.clear()
        session.permanent = True
        app.permanent_session_lifetime = timedelta(minutes=120)
        
        session['logged_in'] = True
        session['user_email'] = email
        session['employee_id'] = employee_id
        session['user_name'] = name
        session['is_admin'] = True
        session['access_level'] = 'L3'
        
        # --- NATIVE FACEAUTH REQUIREMENTS ---
        session['role'] = 'admin'
        session['verified'] = True
        session['user_id'] = employee_id
        session['first_name'] = name.split(' ')[0] if name else 'Admin'
        session['last_auth'] = time.time()
        
        print(f"[ADMIN-SSO] Session created, redirecting to admin dashboard")
        
        # Redirect directly to admin dashboard
        return redirect('/admin/dashboard')
        
    except jwt.ExpiredSignatureError:
        print("[ADMIN-SSO] Token expired")
        return redirect('/login?error=token_expired')
    except jwt.InvalidTokenError as e:
        print(f"[ADMIN-SSO] Invalid token error: {e}")
        return redirect('/login?error=invalid_token')
    except Exception as e:
        print(f"[ADMIN-SSO] Unexpected error: {e}")
        return redirect('/login?error=sso_failed')


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
            
            if user['Role'].lower() == 'employee':
                flash("Employee login has moved. Please log in through the HR Portal.", "error")
                return redirect(url_for('login'))
            
            if check_password_hash(user['Password'], password):
                session.clear()
                session.permanent = True
                app.permanent_session_lifetime = timedelta(minutes=1) # TESTING: 1 min (change back to 120 for production)
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
    if 'user_id' not in session and not session.get('external_auth'): 
        return redirect(url_for('login'))
        
    print("SESSION IN VERIFY PAGE:", dict(session))
    
    display_name = session.get('first_name') or session.get('employee_id') or "User"
    mode = request.args.get('mode', 'login')
    require_gps = session.get('require_gps', True)
    return render_template('verify_face.html', name=display_name, mode=mode, require_gps=require_gps)

@app.route('/process_verification', methods=['POST'])
def process_verification():
    print("SESSION IN VERIFY:", dict(session))
    
    if "external_auth" not in session:
        print("WARNING: external_auth missing")
        
    data = request.get_json()
    record_id = session.get('user_id')
    
    if not record_id and not session.get('external_auth'):
        return jsonify({"success": False, "message": "Session expired. Please login again."})
    mode = data.get('mode', 'login')
    loc_text = data.get('detailed_location', 'Unknown')
    map_link = data.get('location', '#')
    full_loc_string = f"{loc_text} | {map_link}" 
    
    # Extract Raw Coordinates for Mismatch Check
    lat = data.get('lat')
    lon = data.get('lon')
    
    # --- DEVICE DETECTION & RESTRICTION ---
    device_type = data.get('device_type', 'Desktop')
    user_agent = data.get('user_agent', '')

    try:
        if session.get('external_auth'):
            emp_id = session.get('employee_id')
            if not emp_id:
                raise Exception("Employee ID missing from session")
            dv_user = get_user_by_employeeid(emp_id)
            if not dv_user:
                raise Exception(f"User with Employee ID {emp_id} not found in database")
        else:
            dv_user = get_user_by_id(record_id)
            if not dv_user:
                raise Exception("User record not found")
                
        user = _norm_user(dv_user)
        
        # --- CHECK DEVICE RESTRICTIONS ---
        allow_mobile = user.get('AllowMobile', True)
        allow_desktop = user.get('AllowDesktop', True)
        
        if device_type == 'Mobile' and not allow_mobile:
            print(f"[DEVICE BLOCKED] {user['First Name']} - Mobile not allowed")
            return jsonify({
                "success": False, 
                "blocked": True,
                "message": "Face verification is not allowed on mobile devices for your account."
            })
        
        if device_type == 'Desktop' and not allow_desktop:
            print(f"[DEVICE BLOCKED] {user['First Name']} - Desktop not allowed")
            return jsonify({
                "success": False,
                "blocked": True, 
                "message": "Face verification is not allowed on desktop devices for your account."
            })
        stored_enc = np.array(json.loads(user['FaceEncoding']))
        img_data = base64.b64decode(data['image'].split(',')[1])
        img = cv2.imdecode(np.frombuffer(img_data, np.uint8), cv2.IMREAD_COLOR)
        rgb_small = cv2.cvtColor(cv2.resize(img, (0,0), fx=0.25, fy=0.25), cv2.COLOR_BGR2RGB)
        live_enc = face_recognition.face_encodings(rgb_small)

        if len(live_enc) > 0 and face_recognition.compare_faces([stored_enc], live_enc[0], tolerance=0.5)[0]:
            IST = pytz.timezone('Asia/Kolkata')
            now = datetime.now(IST)
            today = now.strftime("%Y-%m-%d")
            cur_time_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            
            if session.get('external_auth'):
                try:
                    callback_url = session.get('callback_url')
                    pending_token = session.get('pending_token')

                    if not callback_url or not pending_token:
                        return jsonify({"error": "No callback URL configured"}), 400

                    # --- NATIVE ATTENDANCE TRACKING FOR EXTERNAL FLOW ---
                    if mode == 'logout':
                        open_rec = find_open_attendance(user['First Name'], today)
                        if open_rec:
                            login_loc_str = open_rec.get('crc6f_loginlocation', '')
                            login_lat, login_lon = None, None
                            if "q=" in login_loc_str:
                                try:
                                    coords = login_loc_str.split("q=")[-1].split(',')
                                    login_lat = float(coords[0])
                                    login_lon = float(coords[1])
                                except: pass
                            
                            if login_lat is not None and login_lon is not None:
                                distance = get_distance_meters(login_lat, login_lon, lat, lon)
                                if distance > 500:
                                    send_location_alert_email(
                                        user['Email'], user['First Name'], distance, 
                                        f"{login_lat},{login_lon}", f"{lat},{lon}"
                                    )

                            update_attendance(open_rec[ATTENDANCE_ID_FIELD], {
                                "crc6f_logouttime": cur_time_iso,
                                "crc6f_status": "Present",
                                "crc6f_logoutlocation": full_loc_string,
                            })
                    else: # Default login mode
                        create_attendance(
                            first_name=user['First Name'],
                            last_name=user['Last Name'],
                            date_str=today,
                            login_time=cur_time_iso,
                            status="Present",
                            login_location=full_loc_string,
                            employee_id=user['EmployeeID'],
                        )
                    # ----------------------------------------------------
                        
                    SECRET_KEY = os.environ.get('JWT_SECRET_KEY', str(JWT_SECRET) if JWT_SECRET else 'fallback')
                    original_claims = jwt.decode(pending_token, SECRET_KEY, algorithms=["HS512"], options={"verify_exp": False})
                    
                    new_payload = original_claims.copy()
                    new_payload['face_verified'] = True
                    new_payload['exp'] = datetime.utcnow() + timedelta(hours=12)
                    
                    new_token = jwt.encode(new_payload, SECRET_KEY, algorithm="HS512")
                    if isinstance(new_token, bytes):
                        new_token = new_token.decode('utf-8')
                        
                    encoded_new_token = urllib.parse.quote(new_token, safe='')
                    redirect_url = f"{callback_url}?token={encoded_new_token}&face_verified=true"

                    return jsonify({
                        "success": True,
                        "external": True,
                        "redirect_url": redirect_url
                    })

                except Exception as e:
                    print("ERROR IN VERIFICATION:", str(e))
                    return jsonify({
                        "success": False,
                        "message": str(e)
                    })
            
            elif mode == 'logout':
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
                        "crc6f_logouttime": cur_time_iso,
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
                    login_time=cur_time_iso,
                    status="Present",
                    login_location=full_loc_string,
                    employee_id=user['EmployeeID'],
                )
                session.update({'verified': True, 'last_auth': time.time()})
                return jsonify({"success": True, "external": False, "redirect": "/employee/dashboard"})
            
        return jsonify({"success": False, "message": "Face not recognized."})
    except Exception as e:
        print(f"Verification error: {e}")
        return jsonify({"success": False, "message": str(e)})

# --- SECURE LOGOUT PREPARATION ---
@app.route('/prepare_logout', methods=['POST'])
def prepare_logout():
    if 'user_id' not in session: return jsonify({"success": False})
    try:
        IST = pytz.timezone('Asia/Kolkata')
        now = datetime.now(IST)
        today = now.strftime("%Y-%m-%d")
        cur_time_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        meeting_rec = find_open_meeting_attendance(session.get('first_name'), today)
        if meeting_rec:
            update_attendance(meeting_rec[ATTENDANCE_ID_FIELD], {
                "crc6f_logouttime": cur_time_iso,
            })
        return jsonify({"success": True})
    except:
        return jsonify({"success": False})

@app.route('/auto_logout_record', methods=['POST'])
def auto_logout_record():
    if 'user_id' not in session: return jsonify({"success": False})
    try:
        # Parse location data from request body
        data = request.get_json(silent=True) or {}
        loc_text = data.get('detailed_location', 'Unknown')
        map_link = data.get('location', '#')
        full_loc_string = f"{loc_text} | {map_link}" if map_link != '#' else ""

        IST = pytz.timezone('Asia/Kolkata')
        now = datetime.now(IST)
        today = now.strftime("%Y-%m-%d")
        cur_time_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        open_rec = find_open_attendance(session.get('first_name'), today)
        if open_rec:
            update_data = {
                "crc6f_logouttime": cur_time_iso,
                "crc6f_status": "Logged Out (Timeout)",
            }
            if full_loc_string:
                update_data["crc6f_logoutlocation"] = full_loc_string
            update_attendance(open_rec[ATTENDANCE_ID_FIELD], update_data)
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
        today = now.strftime("%Y-%m-%d")
        start_time_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_time_iso = (now + timedelta(minutes=duration)).strftime("%Y-%m-%dT%H:%M:%SZ")

        open_rec = find_open_attendance(user['First Name'], today)
        if open_rec:
            update_attendance(open_rec[ATTENDANCE_ID_FIELD], {
                "crc6f_logouttime": start_time_iso,
                "crc6f_status": "Transition to Meeting",
            })
        
        create_attendance(
            first_name=user['First Name'],
            last_name=user['Last Name'],
            date_str=today,
            login_time=start_time_iso,
            status="In Meeting",
            login_location="Meeting Popup",
            employee_id=user['EmployeeID'],
            logout_time=end_time_iso,
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
    
    # Read requested date, fallback to today
    requested_date = request.args.get('date')
    if not requested_date:
        requested_date = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d")
        
    dv_attendance = get_attendance_by_date(requested_date)
    
    attendance_list = [_norm_attendance(a) for a in dv_attendance]
    
    employee_list = []
    for dv_emp in dv_employees:
        emp = _norm_user(dv_emp)
        emp['logs'] = [a for a in attendance_list if a['First Name'] == emp['First Name']]
        employee_list.append(emp)
    
    return render_template('admin_dashboard.html', employees=employee_list, selected_date=requested_date)

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
    emp_id = request.form.get('employee_id', '').strip()
    
    try:
        employee_id = emp_id if emp_id else _generate_employee_id(e)
        created_record = create_user(
            first_name=f,
            last_name=l,
            email=e,
            hashed_password=generate_password_hash("NO_LOGIN_REQUIRED"),
            role='employee',
            employee_id=employee_id,
            status=True,
        )
        
        # Generate Magic Link JWT for Registration
        SECRET_KEY = os.environ.get('JWT_SECRET_KEY', str(JWT_SECRET) if JWT_SECRET else 'fallback')
        magic_payload = {
            "employee_id": employee_id,
            "action": "register",
            "exp": datetime.utcnow() + timedelta(days=7)
        }
        magic_token = jwt.encode(magic_payload, SECRET_KEY, algorithm="HS512")
        if isinstance(magic_token, bytes):
            magic_token = magic_token.decode('utf-8')
        
        encoded_token = urllib.parse.quote(magic_token, safe='')
        reg_link = f"{FACEAUTH_BASE_URL}/magic-register?token={encoded_token}"
        
        email_html = f"""
        <div style="background-color: #121212; color: #ffffff; padding: 40px; font-family: sans-serif; border-radius: 10px;">
            <h1 style="color: #4c8bf5; font-size: 28px;">Hello {f},</h1>
            <p style="font-size: 18px; line-height: 1.6;">Your biometric workspace account is ready. Please follow these steps:</p>
            <div style="background-color: #1a1a1a; padding: 25px; border-radius: 12px; margin: 25px 0; border: 1px solid #333;">
                <p style="margin: 0;"><b>Secure Face Registration:</b> You must register your face securely before you can log in to the HR Portal.</p>
            </div>
            <a href="{reg_link}" style="display: inline-block; background-color: #4c8bf5; color: white; padding: 16px 32px; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 16px;">Register Face Now</a>
        </div>
        """
        email_sent = send_email_via_brevo(e, "Account Ready - Action Required", email_html)
        if email_sent:
            flash(f"Employee {f} added and invite email sent!", "success")
        else:
            flash(f"Employee {f} added but email failed to send. Check server logs.", "warning")
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

@app.route('/forgot-password')
def forgot_password():
    return render_template('forgot_password.html')

otp_store = {}

@app.route('/send-otp', methods=['POST'])
def send_otp():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    
    if not email:
        return jsonify({"success": False, "message": "Email required"})
    
    dv_user = get_user_by_email(email)
    if not dv_user:
        return jsonify({"success": False, "message": "Email not found"})
    
    otp = ''.join(random.choices(string.digits, k=6))
    otp_store[email] = {'otp': otp, 'expires': time.time() + 600}
    
    html_content = f"""
    <div style="font-family: sans-serif; padding: 20px;">
        <h2>Password Reset Code</h2>
        <p>Your verification code is:</p>
        <h1 style="color: #2563eb; letter-spacing: 8px;">{otp}</h1>
        <p>This code expires in 10 minutes.</p>
    </div>
    """
    
    if send_email_via_brevo(email, "Password Reset Code", html_content):
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Failed to send email"})

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    otp = data.get('otp', '')
    
    stored = otp_store.get(email)
    if not stored:
        return jsonify({"success": False, "message": "No OTP found. Please request again."})
    
    if time.time() > stored['expires']:
        del otp_store[email]
        return jsonify({"success": False, "message": "OTP expired. Please request again."})
    
    if stored['otp'] != otp:
        return jsonify({"success": False, "message": "Invalid OTP"})
    
    dv_user = get_user_by_email(email)
    if dv_user:
        session['user_id'] = dv_user.get(USERS_ID_FIELD)
        session['otp_verified'] = True
        del otp_store[email]
        return jsonify({"success": True})
    
    return jsonify({"success": False, "message": "User not found"})

# --- DEVICE CONTROL MODULE ---

@app.route('/admin/device-control')
def device_control():
    if session.get('role') != 'admin': 
        return redirect(url_for('login'))
    
    try:
        raw_employees = get_all_employees()
        employees = []
        for emp in raw_employees:
            normalized = _norm_user(emp)
            employees.append(normalized)
        
        employees.sort(key=lambda x: x.get('First Name', '').lower())
        return render_template('device_control.html', employees=employees)
    except Exception as e:
        print(f"Device control error: {e}")
        flash("Error loading device settings.", "error")
        return redirect(url_for('admin_dashboard'))

@app.route('/api/device-settings', methods=['GET'])
def get_device_settings():
    if session.get('role') != 'admin':
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    
    try:
        raw_employees = get_all_employees()
        settings = []
        for emp in raw_employees:
            normalized = _norm_user(emp)
            settings.append({
                "record_id": normalized['record_id'],
                "employee_id": normalized['EmployeeID'],
                "name": f"{normalized['First Name']} {normalized['Last Name']}",
                "email": normalized['Email'],
                "allowmobile": normalized.get('AllowMobile', True),
                "allowdesktop": normalized.get('AllowDesktop', True)
            })
        return jsonify({"success": True, "data": settings})
    except Exception as e:
        print(f"Get device settings error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/device-settings/update', methods=['POST'])
def update_device_settings():
    if session.get('role') != 'admin':
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    
    try:
        data = request.get_json()
        record_id = data.get('record_id')
        field = data.get('field')
        value = data.get('value')
        
        if not record_id or field not in ['allowmobile', 'allowdesktop', 'requiregps']:
            return jsonify({"success": False, "message": "Invalid parameters"}), 400
        
        # Map field to Dataverse column name
        dv_field = f"crc6f_{field}"
        
        # Update the user record
        update_user_fields(record_id, {dv_field: value})
        
        print(f"[DEVICE SETTINGS] Updated {field}={value} for record {record_id}")
        return jsonify({"success": True})
    except Exception as e:
        print(f"Update device settings error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='127.0.0.1', port=port)