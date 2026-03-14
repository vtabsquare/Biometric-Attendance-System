from werkzeug.security import generate_password_hash

# Choose your new admin password here
new_password = "Admin@123" 
print(generate_password_hash(new_password))