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
CORS(app) # CRITICAL: Allows Vercel to talk to Render

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
    if not re.search(r"[@$!%*?&]", password): return False
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
                session.update({
                    'user_row': cell.row, 
                    'first_name': user_data[0], 
                    'last_name': user_data[1], 
                    'role': user_data[5]
                })
                if len(user_data) > 7 and user_data[7] == "1":
                    return redirect(url_for('reset_password'))
                if user_data[5] == 'admin': 
                    return redirect(url_for('admin_dashboard'))
                
                if len(user_data) < 7 or not user_data[6]:
                    flash("Face not registered.", "error")
                    return redirect(url_for('login'))
                return redirect(url_for('verify_face'))
        except:
            flash("Invalid credentials.", "error")
    return render_template('login.html')

@app.route('/admin/dashboard')
def admin_dashboard():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    user_sheet, attn_sheet = get_sheets()
    all_users = user_sheet.get_all_records()
    
    employees = []
    # Loop and manually track row index starting from 2 (Row 1 is headers)
    for i, u in enumerate(all_users, start=2):
        if u.get('Role') == 'employee':
            u['row_id'] = i  # This is the ID passed to the HTML
            u['is_registered'] = True if u.get('Face Encoding') else False
            employees.append(u)
    
    return render_template('admin_dashboard.html', employees=employees)

@app.route('/add_employee', methods=['POST'])
def add_employee():
    if session.get('role') != 'admin': return redirect(url_for('login'))
    f, l, e, d = request.form.get('first_name'), request.form.get('last_name'), request.form.get('email'), request.form.get('department')
    temp_pass = "Welcome@123"
    hashed = generate_password_hash(temp_pass)
    user_sheet, _ = get_sheets()
    user_sheet.append_row([f, l, e, d, hashed, 'employee', '', '1'])
    send_email_via_brevo(e, "Welcome Staff", f"<h3>Welcome {f}!</h3><p>Your temporary password is: {temp_pass}</p>")
    flash(f"Employee {f} added successfully!", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/edit_employee/<int:row_id>', methods=['POST'])
def edit_employee(row_id):
    if session.get('role') != 'admin': return redirect(url_for('login'))
    f, l, d = request.form.get('first_name'), request.form.get('last_name'), request.form.get('department')
    user_sheet, _ = get_sheets()
    user_sheet.update_cell(row_id, 1, f) # Col A
    user_sheet.update_cell(row_id, 2, l) # Col B
    user_sheet.update_cell(row_id, 4, d) # Col D
    flash("Details updated successfully!", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/delete_employee/<int:row_id>')
def delete_employee(row_id):
    if session.get('role') != 'admin': return redirect(url_for('login'))
    user_sheet, _ = get_sheets()
    user_sheet.delete_rows(row_id)
    flash("Employee removed from system.", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/register_face/<int:user_id>')
def register_face(user_id): 
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
        user_sheet.update_cell(row_id, 7, encoding_str)
        return jsonify({"success": True})
    return jsonify({"success": False})

@app.route('/verify-face')
def verify_face(): 
    return render_template('verify_face.html', name=session.get('first_name'), mode=request.args.get('mode', 'login'))

@app.route('/process_verification', methods=['POST'])
def process_verification():
    data = request.get_json()
    row_id, mode = session.get('user_row'), data.get('mode')
    user_sheet, attn_sheet = get_sheets()
    user_data = user_sheet.row_values(row_id)
    
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
            records = attn_sheet.get_all_records()
            for i, r in enumerate(records, start=2):
                if r['First Name'] == user_data[0] and r['Date'] == today and not r['Logout Time']:
                    attn_sheet.update_cell(i, 5, current_time)
                    break
        else:
            attn_sheet.append_row([user_data[0], user_data[1], today, current_time, "", "Present"])
        
        session['verified'] = True
        return jsonify({"success": True})
    return jsonify({"success": False})

@app.route('/logout')
def logout(): 
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)