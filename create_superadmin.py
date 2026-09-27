"""
Run once to create the Super Admin user.
Usage: python create_superadmin.py
"""
import sys
from app import app, db
from models import Company, User, Branch
from werkzeug.security import generate_password_hash
from datetime import datetime

def create_superadmin(username="superadmin", password="ChangeMe123!"):
    with app.app_context():
        # Check if super admin company exists
        sa_comp = Company.query.filter_by(company_name="__SAAS_ADMIN__").first()
        if not sa_comp:
            sa_comp = Company(
                company_name="__SAAS_ADMIN__",
                master_email="admin@saas.internal",
                plan="enterprise",
                subscription_status="Active",
                expiry_date=None,
            )
            db.session.add(sa_comp)
            db.session.flush()
            branch = Branch(company_id=sa_comp.id, branch_name="HQ")
            db.session.add(branch)

        existing = User.query.filter_by(username=username).first()
        if existing:
            print(f"User '{username}' already exists.")
            return

        user = User(
            company_id=sa_comp.id,
            username=username,
            password_hash=generate_password_hash(password),
            role="Super Admin",
            status="Active",
        )
        db.session.add(user)
        db.session.commit()
        print(f"Super Admin created: username={username}, password={password}")
        print("⚠️  Change the password immediately after first login!")

if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "superadmin"
    p = sys.argv[2] if len(sys.argv) > 2 else "ChangeMe123!"
    create_superadmin(u, p)
