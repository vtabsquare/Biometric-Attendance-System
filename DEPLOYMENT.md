# FaceAuth - Digital Ocean Deployment Guide

## Prerequisites
- Digital Ocean Droplet (Ubuntu 22.04+ recommended)
- Domain name (optional but recommended for SSL)
- SSH access to your droplet

---

## Quick Deploy Commands

### 1. Connect to Droplet
```bash
ssh root@your-droplet-ip
```

### 2. Install System Dependencies
```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv nginx git cmake build-essential libopenblas-dev liblapack-dev libx11-dev libgtk-3-dev
```

### 3. Create App Directory
```bash
mkdir -p /var/www/faceauth
cd /var/www/faceauth
```

### 4. Clone/Upload Your Code
```bash
# Option A: Git clone
git clone https://your-repo-url.git .

# Option B: SCP from local
# Run from your local machine:
# scp -r ./Biometric-Attendance-System/* root@your-droplet-ip:/var/www/faceauth/
```

### 5. Setup Python Virtual Environment
```bash
cd /var/www/faceauth
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn
```

### 6. Install face_recognition (may take 10-15 minutes)
```bash
pip install face_recognition
```

### 7. Configure Environment
```bash
cp .env.example .env
nano .env  # Edit with your actual values
```

**Critical .env values:**
```
FACEAUTH_BASE_URL=https://faceauth.yourdomain.com
JWT_SECRET=same-as-hr-tool
JWT_SECRET_KEY=same-as-hr-tool
PORT=8080
```

### 8. Setup Systemd Service
```bash
cp faceauth.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable faceauth
systemctl start faceauth
```

### 9. Setup Nginx
```bash
cp faceauth.nginx.conf /etc/nginx/sites-available/faceauth
# Edit the server_name in the config:
nano /etc/nginx/sites-available/faceauth

ln -s /etc/nginx/sites-available/faceauth /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
```

### 10. Setup SSL (Recommended)
```bash
apt install certbot python3-certbot-nginx -y
certbot --nginx -d faceauth.yourdomain.com
```

### 11. Set Permissions
```bash
chown -R www-data:www-data /var/www/faceauth
chmod 600 /var/www/faceauth/.env
```

---

## Verify Deployment

```bash
# Check service status
systemctl status faceauth

# Check logs
journalctl -u faceauth -f

# Test health endpoint
curl http://localhost:8080/health

# Test from outside
curl https://faceauth.yourdomain.com/health
```

---

## Port Configuration

| Service | Port | Binding |
|---------|------|---------|
| FaceAuth (Gunicorn) | 8080 | 127.0.0.1 (localhost only) |
| Nginx HTTP | 80 | 0.0.0.0 (all interfaces) |
| Nginx HTTPS | 443 | 0.0.0.0 (all interfaces) |

---

## HR Tool Integration

After deploying FaceAuth, update your HR Tool with the new URL:

1. Update HR Tool's environment variable:
   ```
   FACEAUTH_URL=https://faceauth.yourdomain.com
   ```

2. Ensure JWT secrets match:
   ```
   # Both apps must have the same value:
   JWT_SECRET=your-shared-secret
   ```

3. Update any hardcoded FaceAuth URLs in HR Tool code

4. Redeploy HR Tool

---

## Troubleshooting

### Service won't start
```bash
journalctl -u faceauth -n 50
```

### 502 Bad Gateway
```bash
# Check if gunicorn is running
ps aux | grep gunicorn

# Check port binding
ss -tuln | grep 8080
```

### Face recognition fails
```bash
# Check dlib installation
python3 -c "import dlib; print(dlib.DLIB_USE_CUDA)"
```

### Permission denied
```bash
chown -R www-data:www-data /var/www/faceauth
```

---

## Useful Commands

```bash
# Restart service
systemctl restart faceauth

# View live logs
journalctl -u faceauth -f

# Reload nginx config
systemctl reload nginx

# Check nginx config
nginx -t
```

---

## Security Checklist

- [ ] SSL certificate installed (certbot)
- [ ] .env file permissions set to 600
- [ ] Firewall configured (ufw)
- [ ] JWT_SECRET is strong and matches HR Tool
- [ ] FLASK_SECRET is unique and strong
- [ ] Debug mode disabled in production
