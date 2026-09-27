from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

# --------------------------------------------------
# SAAS PLAN TIERS
# --------------------------------------------------
PLAN_LIMITS = {
    "trial":      {"bookings": 500,      "users": 2,      "branches": 1,      "label": "Free Trial"},
    "starter":    {"bookings": 5000,     "users": 5,      "branches": 1,      "label": "Starter"},
    "pro":        {"bookings": 50000,    "users": 20,     "branches": 5,      "label": "Pro"},
    "enterprise": {"bookings": 999999,  "users": 999999, "branches": 999999, "label": "Enterprise"},
}


class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(150), nullable=False)
    registration_number = db.Column(db.String(150), nullable=True)
    master_email = db.Column(db.String(150))

    # SaaS fields
    plan = db.Column(db.String(50), default="trial")
    subscription_status = db.Column(db.String(50), default="Active")   # Active / Suspended / Cancelled
    trial_start = db.Column(db.DateTime, default=datetime.utcnow)
    expiry_date = db.Column(db.DateTime, nullable=True)
    billing_cycle = db.Column(db.String(20), default="monthly")
    notes = db.Column(db.Text, nullable=True)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def plan_label(self):
        return PLAN_LIMITS.get(self.plan, {}).get("label", self.plan)

    @property
    def is_active(self):
        if self.subscription_status != "Active":
            return False
        if self.expiry_date and datetime.now(timezone.utc) > self.expiry_date:
            return False
        return True

    @property
    def days_remaining(self):
        if not self.expiry_date:
            return 9999
        delta = self.expiry_date - datetime.now(timezone.utc)
        return max(0, delta.days)


class Branch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    branch_name = db.Column(db.String(150), nullable=False)
    location = db.Column(db.String(150))


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branch.id'), nullable=True)
    full_name = db.Column(db.String(150))
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50))   # Super Admin / Company Admin / Staff
    status = db.Column(db.String(20), default="Active")
    permissions = db.Column(db.Text)


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False, unique=True)
    phone = db.Column(db.String(100))
    alt_phone = db.Column(db.String(100), nullable=True)
    whatsapp = db.Column(db.String(100), nullable=True)
    email = db.Column(db.String(150), nullable=True)
    contact_person = db.Column(db.String(150), nullable=True)
    credit_limit = db.Column(db.Float, default=0.00)
    balance = db.Column(db.Float, default=0.00)


class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    contact_person = db.Column(db.String(150), nullable=True)
    contact_info = db.Column(db.String(150))
    bank_details = db.Column(db.Text, nullable=True)
    credit_limit = db.Column(db.Float, default=0.00)
    balance = db.Column(db.Float, default=0.00)


class BankAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    bank_name = db.Column(db.String(150), nullable=False)
    account_number = db.Column(db.String(100), nullable=True)
    card_ref = db.Column(db.String(100), nullable=True)
    opening_balance = db.Column(db.Float, default=0.00)
    balance = db.Column(db.Float, default=0.00)


class ServiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(100))


class Vehicle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    registration_number = db.Column(db.String(100))
    reg_expiry = db.Column(db.Date, nullable=True)
    ins_expiry = db.Column(db.Date, nullable=True)
    total_loan_balance = db.Column(db.Float, default=0.00)
    monthly_emi = db.Column(db.Float, default=0.00)
    status = db.Column(db.String(50), default="Available")


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    customer_id = db.Column(db.Integer)
    supplier_id = db.Column(db.Integer, nullable=True)
    bank_account_id = db.Column(db.Integer, nullable=True)
    service_type = db.Column(db.String(100))
    carrier_code = db.Column(db.String(50), nullable=True)
    pnr_ref = db.Column(db.String(100))
    pnr_status = db.Column(db.String(50), default="Confirmed")
    hold_release_time = db.Column(db.DateTime, nullable=True)
    travel_date = db.Column(db.Date, nullable=True)
    return_date = db.Column(db.Date, nullable=True)
    departure_time = db.Column(db.String(10), nullable=True)
    return_time = db.Column(db.String(10), nullable=True)
    arrival_place = db.Column(db.String(150), nullable=True)
    arrival_time = db.Column(db.String(10), nullable=True)
    pax_adults = db.Column(db.Integer, default=1)
    pax_child = db.Column(db.Integer, default=0)
    pax_infants = db.Column(db.Integer, default=0)
    rent_source = db.Column(db.String(50), nullable=True)
    vehicle_id = db.Column(db.Integer, nullable=True)
    pickup_datetime = db.Column(db.DateTime, nullable=True)
    dropoff_datetime = db.Column(db.DateTime, nullable=True)
    rent_type = db.Column(db.String(50), nullable=True)
    driving_license = db.Column(db.String(100), nullable=True)
    license_issued_at = db.Column(db.String(100), nullable=True)
    license_valid_upto = db.Column(db.Date, nullable=True)
    passport_id = db.Column(db.String(100), nullable=True)
    nationality = db.Column(db.String(100), nullable=True)
    visa_country = db.Column(db.String(150), nullable=True)
    train_number = db.Column(db.String(100), nullable=True)
    train_name = db.Column(db.String(150), nullable=True)
    km_out = db.Column(db.Integer, default=0)
    km_in = db.Column(db.Integer, default=0)
    km_allowed = db.Column(db.Integer, default=0)
    excess_km = db.Column(db.Integer, default=0)
    fuel_stage_out = db.Column(db.String(50), nullable=True)
    fuel_stage_in = db.Column(db.String(50), nullable=True)
    booking_finalized = db.Column(db.Boolean, default=False)
    remarks = db.Column(db.Text, nullable=True)
    net_cost = db.Column(db.Float, default=0.00)
    selling_price = db.Column(db.Float, default=0.00)
    passenger_details = db.Column(db.Text, nullable=True)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    customer_id = db.Column(db.Integer)
    total_amount = db.Column(db.Float, default=0.00)
    total_paid = db.Column(db.Float, default=0.00)
    balance_due = db.Column(db.Float, default=0.00)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    expense_type = db.Column(db.String(100))
    reference_id = db.Column(db.Integer, nullable=True)
    amount = db.Column(db.Float, default=0.00)
    month_applied = db.Column(db.String(50), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)


class Refund(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey('booking.id'), nullable=False)
    refund_amount_expected = db.Column(db.Float, default=0.00)
    refund_amount_received = db.Column(db.Float, default=0.00)
    customer_refund_amount = db.Column(db.Float, default=0.00)
    status = db.Column(db.String(50), default="Pending")
    date_requested = db.Column(db.DateTime, default=datetime.utcnow)


class Ledger(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    transaction_type = db.Column(db.String(100))
    amount = db.Column(db.Float, default=0.00)
    staff_id = db.Column(db.Integer, nullable=True)
    remarks = db.Column(db.Text, nullable=True)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)
