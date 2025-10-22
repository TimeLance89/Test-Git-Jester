#!/usr/bin/env python3

from app import create_app
from models import db, Employee, Department
from werkzeug.security import generate_password_hash

def init_database():
    app = create_app()
    
    with app.app_context():
        # Create all tables
        db.create_all()
        
        # Check if admin user already exists
        admin_user = Employee.query.filter_by(username='admin').first()
        if not admin_user:
            # Create a default department
            dept = Department.query.first()
            if not dept:
                dept = Department(name='Administration', color='#2563eb', area='Verwaltung')
                db.session.add(dept)
                db.session.commit()
            
            # Create admin user
            admin_user = Employee(
                name='Administrator',
                username='admin',
                password_hash=generate_password_hash('admin'),
                department_id=dept.id,
                monthly_hours=160,
                is_admin=True
            )
            db.session.add(admin_user)
            db.session.commit()
            print("✓ Admin user created successfully (username: admin, password: admin)")
        else:
            print("✓ Admin user already exists")
        
        print("✓ Database initialized successfully")

if __name__ == "__main__":
    init_database()

