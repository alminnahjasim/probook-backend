from fileinput import filename

from flask import Flask, json, render_template, request, jsonify, redirect, url_for, session, flash, Response, render_template_string
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta, datetime, timezone, date
import math
from sqlalchemy import func, text, inspect, or_
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import defer, joinedload
from functools import wraps
import smtplib
import secrets
import hashlib
from email.mime.text import MIMEText
import base64
import os
import logging
from logging.handlers import RotatingFileHandler

# ==========================================
# SAAS IMPORTS
# ==========================================
from saas_middleware import register_saas_routes, super_admin_required, subscription_active

app = Flask(__name__)

# ==========================================
# LOGGING CONFIGURATION
# ==========================================
os.makedirs('logs', exist_ok=True)

file_handler = RotatingFileHandler('logs/app.log', maxBytes=10240, backupCount=10)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
))
file_handler.setLevel(logging.INFO)
app.logger.addHandler(file_handler)
app.logger.setLevel(logging.INFO)
app.logger.info('Flask app startup')

# ==========================================
# ENTERPRISE CONFIGURATION
# ==========================================

# app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://u340353639_jasim:Pa#p05689@srv1878.hstgr.io/u340353639_servicebookerp'
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://servicebook:jasim0568@localhost/u340353639_servicebookerp'
# app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://root:@localhost/u340353639_servicebookerp'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'servicebook_production_master_key_12345'
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_recycle': 280, 'pool_pre_ping': True}

# ---------------------------------------------------------------------------
# EMAIL (SMTP) SETTINGS  —  used for the "forgot password" OTP emails.
# Fill these in with a mailbox from your domain (e.g. a Hostinger email account).
# Environment variables of the same name override these values if set.
# Hostinger: host = smtp.hostinger.com, port 465 (SSL) or 587 (TLS),
#            user = the full email address, pass = that mailbox's password.
# Leave blank to disable email sending.
# ---------------------------------------------------------------------------
SMTP_CONFIG = {
    'host': 'smtp.hostinger.com',                       # e.g. 'smtp.hostinger.com'
    'port': '587',                    # 465 for SSL, or 587 for TLS
    'user': 'no-reply@servicebookerp.com',                       # e.g. 'no-reply@yourdomain.com'
    'password': 'OTPmailservice@404',                   # the mailbox password
    'from': 'no-reply@servicebookerp.com',                       # usually same as user; blank = use user
    'use_tls': '1',                   # '1' to STARTTLS on port 587; ignored for 465
}

os.environ['OTP_DEBUG'] = '1'  # Set to '1' to print OTPs in console instead of sending emails (for testing)

def _smtp(key):
    """Read an SMTP setting from the environment first, then SMTP_CONFIG."""
    env_key = 'SMTP_' + ('PASS' if key == 'password' else key.upper())
    return os.environ.get(env_key) or SMTP_CONFIG.get(key) or ''

db = SQLAlchemy(app)

def ensure_ledger_columns():
    try:
        insp = inspect(db.engine)
        cols = [c['name'] for c in insp.get_columns('ledger')]
        if 'staff_id' not in cols:
            db.session.execute(text('ALTER TABLE ledger ADD COLUMN staff_id INT NULL'))
        if 'remarks' not in cols:
            db.session.execute(text('ALTER TABLE ledger ADD COLUMN remarks TEXT NULL'))
        if 'receipt_number' not in cols:
            db.session.execute(text('ALTER TABLE ledger ADD COLUMN receipt_number VARCHAR(50) NULL'))
        if 'category' not in cols:
            db.session.execute(text('ALTER TABLE ledger ADD COLUMN category VARCHAR(50) NULL'))
        if 'allocations' not in cols:
            db.session.execute(text('ALTER TABLE ledger ADD COLUMN allocations TEXT NULL'))
        if 'capital_account_id' not in cols:
            db.session.execute(text('ALTER TABLE ledger ADD COLUMN capital_account_id INT NULL'))
        db.session.commit()
        # "Credit Card" was renamed to "POS Payment"; bring historic rows along so the
        # payment-mode breakdown and reports do not split across two labels.
        db.session.execute(text("UPDATE ledger SET payment_mode = 'POS Payment' WHERE payment_mode = 'Credit Card'"))
        db.session.commit()
    except Exception as e:
        app.logger.warning(f'Ledger schema migration skipped: {e}')

startup_schema_ensured = False

@app.before_request
def prepare_database():
    global startup_schema_ensured
    if not startup_schema_ensured:
        ensure_ledger_columns()
        startup_schema_ensured = True

# ==========================================
# DATABASE MODELS
# ==========================================
class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(150), nullable=False)
    registration_number = db.Column(db.String(150), nullable=True)
    master_email = db.Column(db.String(150))
    plan = db.Column(db.String(50), default='trial')
    subscription_status = db.Column(db.String(50), default='Active')
    expiry_date = db.Column(db.DateTime, nullable=True)
    billing_cycle = db.Column(db.String(20), default='monthly')
    notes = db.Column(db.Text, nullable=True)
    date_created = db.Column(db.DateTime)
    currency_code = db.Column(db.String(3), default='USD')
    currency_symbol = db.Column(db.String(8), default='$')
    currency_decimals = db.Column(db.Integer, default=2)
    timezone = db.Column(db.String(64), default='UTC')
    country_code = db.Column(db.String(2), default='US')
    phone = db.Column(db.String(40), default='')
    # Company-wide print settings + logo (JSON), stored server-side so they persist
    # and are shared across devices/users instead of living only in browser localStorage.
    print_settings = db.Column(db.Text().with_variant(LONGTEXT, 'mysql'))

    @property
    def is_active(self):
        from datetime import datetime as dt
        if self.subscription_status != 'Active':
            return False
        if self.expiry_date and dt.utcnow() > self.expiry_date:
            return False
        return True

    @property
    def plan_label(self):
        labels = {'trial': 'Free Trial', 'gold': 'Gold', 'platinum': 'Platinum'}
        return labels.get(self.plan or 'trial', self.plan or 'trial')

    @property
    def days_remaining(self):
        from datetime import datetime as dt
        if not self.expiry_date:
            return 9999
        delta = self.expiry_date - dt.utcnow()
        return max(0, delta.days)

class Branch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    branch_name = db.Column(db.String(150), nullable=False)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    full_name = db.Column(db.String(150))
    username = db.Column(db.String(150), unique=True, nullable=False, index=True)
    email = db.Column(db.String(150), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50))
    status = db.Column(db.String(20), default="Active")
    permissions = db.Column(db.Text)

class PasswordResetOTP(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    otp_hash = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)
    attempts = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False, unique=True, index=True)
    phone = db.Column(db.String(100))
    contact_person = db.Column(db.String(150))
    opening_balance = db.Column(db.Float, default=0.00)
    ob_remarks = db.Column(db.String(255), nullable=True)
    credit_limit = db.Column(db.Float, default=0.00)
    balance = db.Column(db.Float, default=0.00)
    # Req #4: identity capture (Passport / National ID / Driving License) + optional scanned doc
    id_type = db.Column(db.String(50))
    id_number = db.Column(db.String(100), index=True)
    id_document = db.Column(db.String(255))

class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    contact_info = db.Column(db.String(150))
    contact_person = db.Column(db.String(150))
    opening_balance = db.Column(db.Float, default=0.00)
    ob_remarks = db.Column(db.String(255), nullable=True)
    credit_limit = db.Column(db.Float, default=0.00)
    balance = db.Column(db.Float, default=0.00)
    __table_args__ = (db.UniqueConstraint('company_id', 'name', name='uq_supplier_company_name'),)

class BankAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    bank_name = db.Column(db.String(150), nullable=False)
    account_number = db.Column(db.String(100))
    card_ref = db.Column(db.String(100))
    opening_balance = db.Column(db.Float, default=0.00)
    balance = db.Column(db.Float, default=0.00)

class CapitalAccount(db.Model):
    """Capital / Loan account holder — a partner, investor or lender whose money
    funds the business. The capital deposit is the opening fund; every receipt or
    payment recorded with the 'Capital/Loan Account' payment mode moves that
    balance, exactly the way a bank account balance moves."""
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(100))
    account_type = db.Column(db.String(50), default='Capital')   # Capital | Loan
    capital_deposit = db.Column(db.Float, default=0.00)          # opening deposit
    balance = db.Column(db.Float, default=0.00)
    remarks = db.Column(db.String(255), nullable=True)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('company_id', 'name', name='uq_capital_company_name'),)

class ServiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(100))

class VisaType(db.Model):
    """Visa type master, shared by the Booking page and Visa Plus tracking.
    The name is the identity; default_days is the stay length that auto-fills
    whenever that name is picked."""
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    default_days = db.Column(db.Integer, default=30)
    __table_args__ = (db.UniqueConstraint('company_id', 'name', name='uq_visa_type_company_name'),)

class VisaHistory(db.Model):
    """Timeline entries for a Visa Plus application.

    Kept on the server on purpose: the timeline used to live in the browser's
    localStorage keyed on a guessed row number, so it vanished on another device,
    for another user, or as soon as the guessed key stopped matching the real
    booking id. One row here = one line in the Visa Timeline modal."""
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('booking.id'), nullable=False, index=True)
    entry_time = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    username = db.Column(db.String(150))
    description = db.Column(db.Text, nullable=False)

class Vehicle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    registration_number = db.Column(db.String(100))
    reg_expiry = db.Column(db.Date)
    ins_expiry = db.Column(db.Date)
    insurance_provider = db.Column(db.String(150))
    bank_name = db.Column(db.String(150))
    loan_account_no = db.Column(db.String(100))
    total_loan_balance = db.Column(db.Float, default=0.00) 
    monthly_emi = db.Column(db.Float, default=0.00)
    status = db.Column(db.String(50), default="Available") 
    # ---- External-supplier fleet (car rented FROM a supplier and re-rented to customers) ----
    is_external = db.Column(db.Boolean, default=False)      # True => car is sourced from an external supplier
    supplier_id = db.Column(db.Integer, nullable=True)      # the Supplier we pay rent to (must exist in Suppliers/Vendors)

class VehicleRentAgreement(db.Model):
    """A dated supplier-rent agreement for an external vehicle. A vehicle can have
    several over time (renewals / rate changes); the one whose [start_date, end_date]
    range contains a booking's pickup date decides how much we owe the supplier.
    If no agreement covers the pickup date, the agreement is treated as expired."""
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    vehicle_id = db.Column(db.Integer, nullable=False, index=True)
    supplier_id = db.Column(db.Integer, nullable=True)
    rent_amount = db.Column(db.Float, default=0.00)
    rent_period = db.Column(db.String(20), default='daily')   # daily / monthly / yearly
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)

class Building(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(150), nullable=False)

class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    building_id = db.Column(db.Integer, nullable=False, index=True)
    room_number = db.Column(db.String(50), nullable=False)
    room_type = db.Column(db.String(100))
    status = db.Column(db.String(50), default="Available")
    electricity_acc = db.Column(db.String(100))
    water_acc = db.Column(db.String(100))
    current_reading = db.Column(db.Float, default=0.0) 
    opening_balance = db.Column(db.Float, default=0.0) 
    reading_date = db.Column(db.Date, nullable=True)   
    elec_latest_bill = db.Column(db.Float, default=0.00)
    water_latest_bill = db.Column(db.Float, default=0.00)
    current_elec_reading = db.Column(db.Float, default=0.0)
    current_water_reading = db.Column(db.Float, default=0.0)

class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    customer_id = db.Column(db.Integer, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoice.id'), nullable=True, index=True)
    supplier_id = db.Column(db.Integer, nullable=True, index=True)
    bank_account_id = db.Column(db.Integer, nullable=True, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    service_type = db.Column(db.String(100), index=True)
    carrier_code = db.Column(db.String(50)) 
    # Visa: carrier_code holds the visa TYPE NAME only (e.g. "Tourist"); the stay length
    # is kept separately so the overstay deadline can be counted from a real number
    # instead of being parsed out of a combined label like "Tourist 30 Days".
    visa_duration_days = db.Column(db.Integer)
    pnr_ref = db.Column(db.String(100), index=True)
    pnr_status = db.Column(db.String(50), default="Confirmed")
    hold_release_time = db.Column(db.DateTime, nullable=True)
    travel_date = db.Column(db.Date, nullable=True)
    return_date = db.Column(db.Date, nullable=True)
    rent_source = db.Column(db.String(50)) 
    vehicle_id = db.Column(db.Integer, nullable=True, index=True)
    room_id = db.Column(db.Integer, nullable=True, index=True) 
    pickup_datetime = db.Column(db.DateTime, nullable=True)
    dropoff_datetime = db.Column(db.DateTime, nullable=True)
    pickup_location = db.Column(db.String(200), nullable=True)
    drop_location = db.Column(db.String(200), nullable=True)
    rent_type = db.Column(db.String(50)) 
    rent_amount = db.Column(db.Float, default=0.00)
    driving_license = db.Column(db.String(100))
    passport_id = db.Column(db.String(100))
    nationality = db.Column(db.String(100))
    license_issued_at = db.Column(db.String(100))
    license_valid_upto = db.Column(db.Date, nullable=True)
    km_out = db.Column(db.Integer, default=0)
    km_in = db.Column(db.Integer, default=0)
    km_allowed = db.Column(db.Integer, default=0)
    extra_km_price = db.Column(db.Float, default=0.00)
    excess_km = db.Column(db.Integer, default=0)
    fuel_stage_out = db.Column(db.String(50))
    fuel_stage_in = db.Column(db.String(50))
    booking_finalized = db.Column(db.Boolean, default=False)
    
    elec_reading_in = db.Column(db.Float, default=0.0)
    elec_reading_out = db.Column(db.Float, default=0.0)
    elec_unit_price = db.Column(db.Float, default=0.0)
    water_reading_in = db.Column(db.Float, default=0.0)
    water_reading_out = db.Column(db.Float, default=0.0)
    water_unit_price = db.Column(db.Float, default=0.0)
    
    photo1 = db.Column(db.String(255), nullable=True)
    photo2 = db.Column(db.String(255), nullable=True)
    photo3 = db.Column(db.String(255), nullable=True)
    photo4 = db.Column(db.String(255), nullable=True)
    damage_charge = db.Column(db.Float, default=0.0)
    maintenance_charge = db.Column(db.Float, default=0.0)
    overstay_charge = db.Column(db.Float, default=0.0)
    advance_paid = db.Column(db.Float, default=0.0)
    supplier_paid = db.Column(db.Float, default=0.0)

    driver_name = db.Column(db.String(150), nullable=True)
    driver_phone = db.Column(db.String(50), nullable=True)
    driver_email = db.Column(db.String(100), nullable=True)
    driver_id_card = db.Column(db.String(255), nullable=True)
    additional_driver_details = db.Column(db.Text, nullable=True)
    add_driver_name = db.Column(db.String(150), nullable=True)
    add_driver_phone = db.Column(db.String(50), nullable=True)
    add_driver_email = db.Column(db.String(100), nullable=True)
    add_driver_license = db.Column(db.String(100), nullable=True)
    add_driver_license_country = db.Column(db.String(100), nullable=True)
    add_driver_license_valid = db.Column(db.Date, nullable=True)
    add_driver_passport = db.Column(db.String(100), nullable=True)
    add_driver_nationality = db.Column(db.String(100), nullable=True)
    add_driver_license_issued_at = db.Column(db.String(100), nullable=True)
    rent_location = db.Column(db.String(150), nullable=True)
    license_issued_country = db.Column(db.String(100), nullable=True)
    rent_description = db.Column(db.Text, nullable=True)
    security_deposit = db.Column(db.Float, default=0.0)
    fuel_charge = db.Column(db.Float, default=0.0)
    vat_added = db.Column(db.String(10), nullable=True)
    vat_amount = db.Column(db.Float, default=0.0)

    remarks = db.Column(db.Text)
    net_cost = db.Column(db.Float, default=0.00)
    selling_price = db.Column(db.Float, default=0.00)
    passenger_details = db.Column(db.Text)
    pax_adults = db.Column(db.Integer, default=1)
    pax_child = db.Column(db.Integer, default=0)
    pax_infants = db.Column(db.Integer, default=0)
    sector_details = db.Column(db.Text, nullable=True)
    departure_time = db.Column(db.DateTime, nullable=True)
    return_time = db.Column(db.String(10), nullable=True)
    arrival_place = db.Column(db.String(150), nullable=True)
    departure_place = db.Column(db.String(150), nullable=True)
    flight_number = db.Column(db.String(50), nullable=True)
    arrival_time = db.Column(db.DateTime, nullable=True)
    visa_country = db.Column(db.String(150), nullable=True)
    train_number = db.Column(db.String(50), nullable=True)
    train_name = db.Column(db.String(100), nullable=True)
    # Tour Booking extras. tour_carrier (carrier_code) stays the operator/agency name;
    # tour_package_name is the tour's own title. tour_itinerary is a JSON-encoded list of
    # day objects: [{title, description, breakfast, lunch, dinner, hotel, city_tour}, ...],
    # built by the Day-by-Day Schedule Builder in booking.html. Inclusions/exclusions are
    # stored newline-separated, one bullet per line, matching the passenger_details pattern.
    tour_package_name = db.Column(db.String(200), nullable=True)
    tour_hotel_name = db.Column(db.String(200), nullable=True)
    tour_itinerary = db.Column(db.Text, nullable=True)
    tour_inclusions = db.Column(db.Text, nullable=True)
    tour_exclusions = db.Column(db.Text, nullable=True)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)


class BookingSupplierCost(db.Model):
    """One supplier-cost line on a booking. Tour bookings often need this: transport from
    one supplier, the airline from another, the hotel from a third. A Tour booking that
    uses this "split costing" keeps Booking.net_cost as the SUM of these lines (so P&L /
    COGS reporting, which only ever reads the booking total, needs no change) and clears
    Booking.supplier_id to None (so the single-supplier payable scan below doesn't also
    count the same cost a second time). auto-created by db.create_all(), same as
    CapitalAccount.

    NOTE (scope of this pass): `paid` is tracked here for forward compatibility, but no
    payment screen writes to it yet. Receipt & Payment's supplier-bill picker
    (get_supplier_payables/save_supplier_payment) and the Balance Sheet's Supplier.balance
    rollback still key off single-supplier bookings only; they do not yet apply a payment
    against, or roll back, an individual split line. Wiring that up touches
    receipt_payment.html / accounts.html, which weren't part of this change set."""
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('booking.id'), nullable=False, index=True)
    supplier_id = db.Column(db.Integer, nullable=False, index=True)
    component = db.Column(db.String(100))   # e.g. "Transport", "Airline", "Hotel", or free text
    amount = db.Column(db.Float, default=0.0)
    paid = db.Column(db.Float, default=0.0)
    remarks = db.Column(db.String(255))
    date_created = db.Column(db.DateTime, default=datetime.utcnow)


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    customer_id = db.Column(db.Integer, index=True)
    total_amount = db.Column(db.Float, default=0.00)
    total_paid = db.Column(db.Float, default=0.00)
    balance_due = db.Column(db.Float, default=0.00)
    tax_rate = db.Column(db.Float, default=0.0)
    tax_amount = db.Column(db.Float, default=0.0)
    remarks = db.Column(db.Text, nullable=True)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)
    items = db.relationship('Booking', backref='invoice_ref', lazy=True)

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    expense_type = db.Column(db.String(100)) 
    amount = db.Column(db.Float, default=0.00)
    month_applied = db.Column(db.String(50))
    notes = db.Column(db.Text)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)

class Refund(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('booking.id'), nullable=False, index=True)
    refund_amount_expected = db.Column(db.Float, default=0.00)
    refund_amount_received = db.Column(db.Float, default=0.00)
    customer_refund_amount = db.Column(db.Float, default=0.00)
    status = db.Column(db.String(50), default="Pending")
    date_requested = db.Column(db.DateTime, default=datetime.utcnow)

class Ledger(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    transaction_type = db.Column(db.String(100), index=True)
    amount = db.Column(db.Float, default=0.00)
    customer_id = db.Column(db.Integer, nullable=True, index=True) 
    supplier_id = db.Column(db.Integer, nullable=True, index=True) 
    room_id = db.Column(db.Integer, nullable=True, index=True)      
    vehicle_id = db.Column(db.Integer, nullable=True, index=True)
    bank_account_id = db.Column(db.Integer, nullable=True, index=True)
    capital_account_id = db.Column(db.Integer, nullable=True, index=True)
    staff_id = db.Column(db.Integer, nullable=True, index=True)
    payment_mode = db.Column(db.String(50))
    remarks = db.Column(db.Text, nullable=True)
    cheque_number = db.Column(db.String(50),nullable=True)
    cheque_date = db.Column(db.Date, nullable=True)
    receipt_number = db.Column(db.String(50), nullable=True, index=True)
    category = db.Column(db.String(50), nullable=True)
    allocations = db.Column(db.Text, nullable=True)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)

class PrintLayout(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    document_type = db.Column(db.String(100)) 
    layout_name = db.Column(db.String(150))
    file_type = db.Column(db.String(50)) 
    file_name = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True)
    date_uploaded = db.Column(db.DateTime, default=datetime.utcnow)

class RefundTrack(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False)
    request_date = db.Column(db.String(50))
    supplier_id = db.Column(db.Integer)
    customer_id = db.Column(db.Integer)
    expected_amount = db.Column(db.Float, default=0.00)
    payable_amount = db.Column(db.Float, default=0.00)
    approx_date = db.Column(db.String(50))
    remarks = db.Column(db.Text)
    status = db.Column(db.String(50), default="Pending")

class CompanyStorageConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, unique=True, index=True)
    photo_storage_path = db.Column(db.String(500), nullable=False)
    date_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

def ensure_booking_columns():
    inspector = inspect(db.engine)
    if not inspector.has_table('booking'): return ['Booking table does not exist.']
    existing_cols = {col['name'] for col in inspector.get_columns('booking')}
    upgrade_queries = [
        ('sector_details', 'ALTER TABLE booking ADD COLUMN sector_details TEXT'),
        ('departure_time', 'ALTER TABLE booking ADD COLUMN departure_time DATETIME'),
        ('return_time', 'ALTER TABLE booking ADD COLUMN return_time VARCHAR(10)'),
        ('arrival_place', 'ALTER TABLE booking ADD COLUMN arrival_place VARCHAR(150)'),
        ('arrival_time', 'ALTER TABLE booking ADD COLUMN arrival_time DATETIME'),
        ('departure_place', 'ALTER TABLE booking ADD COLUMN departure_place VARCHAR(150)'),
        ('flight_number', 'ALTER TABLE booking ADD COLUMN flight_number VARCHAR(50)'),
        ('pickup_location', 'ALTER TABLE booking ADD COLUMN pickup_location VARCHAR(200)'),
        ('drop_location', 'ALTER TABLE booking ADD COLUMN drop_location VARCHAR(200)'),
        ('rent_amount', 'ALTER TABLE booking ADD COLUMN rent_amount FLOAT DEFAULT 0.00'),
        ('extra_km_price', 'ALTER TABLE booking ADD COLUMN extra_km_price FLOAT DEFAULT 0.00'),
        ('visa_country', 'ALTER TABLE booking ADD COLUMN visa_country VARCHAR(150)'),
        ('train_number', 'ALTER TABLE booking ADD COLUMN train_number VARCHAR(100)'),
        ('train_name', 'ALTER TABLE booking ADD COLUMN train_name VARCHAR(150)'),
        ('tour_package_name', 'ALTER TABLE booking ADD COLUMN tour_package_name VARCHAR(200)'),
        ('tour_hotel_name', 'ALTER TABLE booking ADD COLUMN tour_hotel_name VARCHAR(200)'),
        ('tour_itinerary', 'ALTER TABLE booking ADD COLUMN tour_itinerary TEXT'),
        ('tour_inclusions', 'ALTER TABLE booking ADD COLUMN tour_inclusions TEXT'),
        ('tour_exclusions', 'ALTER TABLE booking ADD COLUMN tour_exclusions TEXT'),
        ('elec_reading_in', 'ALTER TABLE booking ADD COLUMN elec_reading_in FLOAT DEFAULT 0.0'),
        ('elec_reading_out', 'ALTER TABLE booking ADD COLUMN elec_reading_out FLOAT DEFAULT 0.0'),
        ('elec_unit_price', 'ALTER TABLE booking ADD COLUMN elec_unit_price FLOAT DEFAULT 0.0'),
        ('water_reading_in', 'ALTER TABLE booking ADD COLUMN water_reading_in FLOAT DEFAULT 0.0'),
        ('water_reading_out', 'ALTER TABLE booking ADD COLUMN water_reading_out FLOAT DEFAULT 0.0'),
        ('water_unit_price', 'ALTER TABLE booking ADD COLUMN water_unit_price FLOAT DEFAULT 0.0'),
        ('damage_charge', 'ALTER TABLE booking ADD COLUMN damage_charge FLOAT DEFAULT 0.0'),
        ('maintenance_charge', 'ALTER TABLE booking ADD COLUMN maintenance_charge FLOAT DEFAULT 0.0'),
        ('overstay_charge', 'ALTER TABLE booking ADD COLUMN overstay_charge FLOAT DEFAULT 0.0'),
        ('photo1', 'ALTER TABLE booking ADD COLUMN photo1 LONGBLOB'),
        ('photo2', 'ALTER TABLE booking ADD COLUMN photo2 LONGBLOB'),
        ('photo3', 'ALTER TABLE booking ADD COLUMN photo3 LONGBLOB'),
        ('photo4', 'ALTER TABLE booking ADD COLUMN photo4 LONGBLOB'),
        ('driver_name', 'ALTER TABLE booking ADD COLUMN driver_name VARCHAR(150)'),
        ('driver_phone', 'ALTER TABLE booking ADD COLUMN driver_phone VARCHAR(50)'),
        ('driver_email', 'ALTER TABLE booking ADD COLUMN driver_email VARCHAR(100)'),
        ('driver_id_card', 'ALTER TABLE booking ADD COLUMN driver_id_card LONGBLOB'),
        ('additional_driver_details', 'ALTER TABLE booking ADD COLUMN additional_driver_details TEXT'),
        ('rent_location', 'ALTER TABLE booking ADD COLUMN rent_location VARCHAR(150)'),
        ('license_issued_country', 'ALTER TABLE booking ADD COLUMN license_issued_country VARCHAR(100)'),
        ('rent_description', 'ALTER TABLE booking ADD COLUMN rent_description TEXT'),
        ('security_deposit', 'ALTER TABLE booking ADD COLUMN security_deposit FLOAT DEFAULT 0.0'),
        ('fuel_charge', 'ALTER TABLE booking ADD COLUMN fuel_charge FLOAT DEFAULT 0.0'),
        ('vat_added', 'ALTER TABLE booking ADD COLUMN vat_added VARCHAR(10)'),
        ('vat_amount', 'ALTER TABLE booking ADD COLUMN vat_amount FLOAT DEFAULT 0.0'),
        ('add_driver_name', 'ALTER TABLE booking ADD COLUMN add_driver_name VARCHAR(150)'),
        ('add_driver_phone', 'ALTER TABLE booking ADD COLUMN add_driver_phone VARCHAR(50)'),
        ('add_driver_email', 'ALTER TABLE booking ADD COLUMN add_driver_email VARCHAR(100)'),
        ('add_driver_license', 'ALTER TABLE booking ADD COLUMN add_driver_license VARCHAR(100)'),
        ('add_driver_license_country', 'ALTER TABLE booking ADD COLUMN add_driver_license_country VARCHAR(100)'),
        ('add_driver_license_valid', 'ALTER TABLE booking ADD COLUMN add_driver_license_valid DATE'),
        ('add_driver_license_issued_at', 'ALTER TABLE booking ADD COLUMN add_driver_license_issued_at VARCHAR(100)'),
        ('add_driver_passport', 'ALTER TABLE booking ADD COLUMN add_driver_passport VARCHAR(100)'),
         ('add_driver_nationality', 'ALTER TABLE booking ADD COLUMN add_driver_nationality VARCHAR(100)'),
        ('supplier_paid', 'ALTER TABLE booking ADD COLUMN supplier_paid FLOAT DEFAULT 0.0'),
        ('visa_duration_days', 'ALTER TABLE booking ADD COLUMN visa_duration_days INT')
    ]
    results = []
    for col_name, query in upgrade_queries:
        if col_name in existing_cols: continue
        try:
            db.session.execute(text(query))
            db.session.commit()
            results.append(f'Added column {col_name}.')
        except Exception as e:
            db.session.rollback()
            results.append(f'Failed to add {col_name}: {str(e)}')
    return results

with app.app_context():
    db.create_all()
    _col_results = ensure_booking_columns()
    # One-time backfill: every visa booking that predates the type/duration split becomes
    # "Tourist" for 30 days. Guarded on the column having just been created so a later
    # restart never overwrites what a user has since edited.
    if any('Added column visa_duration_days' in r for r in (_col_results or [])):
        try:
            db.session.execute(text(
                "UPDATE booking SET carrier_code = 'Tourist', visa_duration_days = 30 "
                "WHERE service_type = 'visa'"))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f'Legacy visa backfill skipped: {e}')
    try:
        _insp = inspect(db.engine)
        if _insp.has_table('company'):
            _ccols = {c['name'] for c in _insp.get_columns('company')}
            if 'print_settings' not in _ccols:
                db.session.execute(text('ALTER TABLE company ADD COLUMN print_settings LONGTEXT'))
                db.session.commit()
            if 'currency_decimals' not in _ccols:
                db.session.execute(text('ALTER TABLE company ADD COLUMN currency_decimals INT DEFAULT 2'))
                db.session.commit()
            if 'phone' not in _ccols:
                db.session.execute(text('ALTER TABLE company ADD COLUMN phone VARCHAR(40)'))
                db.session.commit()
        if _insp.has_table('user'):
            _ucols = {c['name'] for c in _insp.get_columns('user')}
            if 'email' not in _ucols:
                db.session.execute(text('ALTER TABLE user ADD COLUMN email VARCHAR(150)'))
                db.session.commit()
        if _insp.has_table('vehicle'):
            _vcols = {c['name'] for c in _insp.get_columns('vehicle')}
            if 'is_external' not in _vcols:
                db.session.execute(text('ALTER TABLE vehicle ADD COLUMN is_external BOOLEAN DEFAULT 0'))
                db.session.commit()
            if 'supplier_id' not in _vcols:
                db.session.execute(text('ALTER TABLE vehicle ADD COLUMN supplier_id INT NULL'))
                db.session.commit()
        if _insp.has_table('customer'):
            _custcols = {c['name'] for c in _insp.get_columns('customer')}
            for _cn, _cq in [
                ('id_type',     'ALTER TABLE customer ADD COLUMN id_type VARCHAR(50)'),
                ('id_number',   'ALTER TABLE customer ADD COLUMN id_number VARCHAR(100)'),
                ('id_document', 'ALTER TABLE customer ADD COLUMN id_document VARCHAR(255)'),
            ]:
                if _cn not in _custcols:
                    db.session.execute(text(_cq)); db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.warning(f'Company schema migration skipped: {e}')

# ==========================================
# FILE STORAGE FOR BOOKING PHOTOS 
# ==========================================

import os
import re
import shutil
import tempfile
from werkzeug.utils import secure_filename

def _photo_storage_base():
    """Base directory for company photo storage. Override with the PHOTO_STORAGE_BASE
    environment variable (useful on shared hosting to point at a writable, non-public
    directory). Defaults to a 'company_data' folder beside the app."""
    return os.environ.get('PHOTO_STORAGE_BASE') or os.path.join(app.root_path, 'company_data')

def _default_company_photo_path(company_id):
    return os.path.join(_photo_storage_base(), f'company_{company_id}', 'bookings')

def is_os_compatible_path(p):
    """Validate a storage path against the OS the server actually runs on, so the
    'change path' feature works on both Windows and Linux/Unix hosts. On Windows it
    accepts drive-letter paths (like D: followed by a folder), UNC paths and plain or
    relative folder names, and rejects a POSIX-style absolute path (a leading slash with
    no drive) that was almost certainly pasted from a Linux box. On POSIX it accepts an
    absolute /path or a relative name and rejects Windows drive-letter / backslash paths.
    """
    if not p:
        return False
    p = p.strip()
    if not p:
        return False
    backslash = chr(92)
    looks_windows = (backslash in p) or bool(re.match(r"^[A-Za-z]:", p))
    if os.name == "nt":
        # a leading single "/" with no drive is a Linux path pasted onto a Windows host
        if p.startswith("/") and not p.startswith("//") and not looks_windows:
            return False
        return True
    # POSIX host: reject Windows drive-letter / backslash paths
    if looks_windows:
        return False
    return True

def ensure_writable_dir(path):
    """Create the directory if needed and confirm we can actually write to it."""
    try:
        os.makedirs(path, exist_ok=True)
        test_file = os.path.join(path, '.write_test')
        with open(test_file, 'w') as f:
            f.write('ok')
        os.remove(test_file)
        return True
    except Exception:
        return False

def get_company_photo_path(company_id):
    config = CompanyStorageConfig.query.filter_by(company_id=company_id).first()
    desired = config.photo_storage_path if config else None

    # Use the configured path only if it is valid for THIS operating system and is
    # actually writable. Otherwise self-heal to a safe default under the app folder.
    # This fixes a Windows path carried over to Linux hosting, or any path the server
    # cannot write to, and persists the correction so it is consistent afterwards.
    if desired and is_os_compatible_path(desired) and ensure_writable_dir(desired):
        return desired

    fallback = _default_company_photo_path(company_id)
    if not ensure_writable_dir(fallback):
        # Last resort: a guaranteed-writable temp location so uploads never crash.
        fallback = os.path.join(tempfile.gettempdir(), 'erp_photos', f'company_{company_id}')
        os.makedirs(fallback, exist_ok=True)

    try:
        if config:
            config.photo_storage_path = fallback
        else:
            config = CompanyStorageConfig(company_id=company_id, photo_storage_path=fallback)
            db.session.add(config)
        db.session.commit()
    except Exception:
        db.session.rollback()
    return fallback

def save_booking_photo(file_obj, company_id, booking_id, photo_field):
    if not file_obj or not file_obj.filename:
        return None
    storage_dir = get_company_photo_path(company_id)
    original_name = secure_filename(file_obj.filename)
    ext = os.path.splitext(original_name)[1].lower() or '.jpg'
    if ext not in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
        ext = '.jpg'
    filename = f'booking_{booking_id}_{photo_field}{ext}'
    full_path = os.path.join(storage_dir, filename)
    file_obj.save(full_path)
    return filename

def get_booking_photo_full_path(company_id, filename):
    if not filename: return None
    storage_dir = get_company_photo_path(company_id)
    full_path = os.path.join(storage_dir, filename)
    return full_path if os.path.exists(full_path) else None

def delete_booking_photos(company_id, booking_id):
    storage_dir = get_company_photo_path(company_id)
    if not os.path.isdir(storage_dir): return
    prefix = f'booking_{booking_id}_'
    for f in os.listdir(storage_dir):
        if f.startswith(prefix):
            try: os.remove(os.path.join(storage_dir, f))
            except Exception as e: app.logger.warning(f"Could not delete photo {f}: {e}")

# ==========================================
# AUTHENTICATION & CONTEXT
# ==========================================
@app.before_request
def check_auth():
    open_routes = {'login', 'check_setup', 'register', 'static', 'fix_db',
                   'signup_page', 'signup', 'subscription_expired',
                   'forgot_password', 'reset_password'}
    if not request.endpoint or request.endpoint in open_routes: return
    if request.endpoint == 'static': return
    if 'user_id' not in session or 'company_id' not in session: return redirect(url_for('login'))
    
    if request.endpoint != 'logout':
        is_super = session.get('role') == 'Super Admin'
        if is_super:
            user = User.query.get(session['user_id'])
            if not user or (user.status and user.status.lower() != 'active'):
                session.clear()
                return redirect(url_for('login'))
        else:
            result = db.session.query(User, Company).join(Company, User.company_id == Company.id)\
                .filter(User.id == session['user_id']).first()
            if not result:
                session.clear()
                return redirect(url_for('login'))
            user, comp = result
            if user.status and user.status.lower() != 'active':
                session.clear()
                return redirect(url_for('login'))
            if not comp.is_active:
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'success': False, 'message': 'Subscription expired or suspended.'}), 403
                return redirect(url_for('subscription_expired'))

from flask import g

@app.context_processor
def inject_company():
    comp = getattr(g, 'current_company', None)
    if comp is None and 'company_id' in session:
        comp = Company.query.get(session['company_id'])
        g.current_company = comp
    is_impersonating = 'sa_return_user_id' in session
    is_super_admin = session.get('role') == 'Super Admin'
    return dict(current_company=comp, is_impersonating=is_impersonating, is_super_admin=is_super_admin)

# ==========================================
# PERMISSION SYSTEM
# ==========================================
def get_user_permissions():
    if session.get('role') == 'Super Admin': return ['Admin']
    user = getattr(g, 'current_user', None)
    if user is None:
        user_id = session.get('user_id')
        if not user_id: return []
        user = User.query.get(user_id)
        g.current_user = user
    if not user or not user.permissions: return []
    try: return json.loads(user.permissions)
    except (ValueError, TypeError): return []

def has_permission(perm):
    perms = get_user_permissions()
    return 'Admin' in perms or perm in perms

def require_permission(perm):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not has_permission(perm):
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'success': False, 'message': f'You do not have permission ({perm}) to perform this action.'}), 403
                return render_template_string("""
                    <div style="text-align:center; padding: 60px; font-family: sans-serif;">
                        <h1 style="color:#ef4444;">🚫 Access Denied</h1>
                        <p>You don't have permission to access this page.</p>
                        <p style="color:#6b7280; font-size:13px;">Required: <code>{{ perm }}</code></p>
                        <a href="/" style="color:#6366f1;">← Back to Dashboard</a>
                    </div>
                """, perm=perm), 403
            return f(*args, **kwargs)
        return wrapped
    return decorator

@app.context_processor
def inject_permissions():
    return dict(has_permission=has_permission, user_permissions=get_user_permissions())

def decode_image_field(data):
    if not data or not isinstance(data, str): return None
    if data.startswith('data:') and ',' in data:
        payload = data.split(',', 1)[1]
        try: return base64.b64decode(payload)
        except Exception: return None
    return None

def save_photo_to_disk(booking_id, photo_name, data, company_id=None):
    if not data: return None
    try:
        if company_id is None: company_id = session.get('company_id')
        if company_id: upload_dir = get_company_photo_path(company_id)
        else:
            upload_dir = os.path.join(app.root_path, 'static', 'uploads')
            os.makedirs(upload_dir, exist_ok=True)
        filename = f"booking_{booking_id}_{photo_name}.jpg"
        filepath = os.path.join(upload_dir, filename)
        with open(filepath, 'wb') as f:
            f.write(data)
        return filename
    except Exception as e:
        app.logger.error(f"Error saving photo to disk: {e}")
        return None

def parse_datetime_local(value):
    if not value: return None
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M'):
        try: return datetime.strptime(value, fmt)
        except ValueError: continue
    return None

def get_last_nonempty_form_value(key):
    values = request.form.getlist(key)
    for value in reversed(values):
        if value and value.strip():
            return value.strip()
    return None

def _save_tour_supplier_costs(booking, comp_id):
    """Replace booking's BookingSupplierCost rows from the tour_supplier_costs[] JSON
    field (written by the Split Cost builder in booking.html, only when its checkbox is
    ticked) and return their total. The hidden field is present on every booking form,
    but left blank unless split costing was actually used, so this returns None (no
    override — the normal single buying_price/supplier_id fields apply, as before) for
    every ordinary booking, and only returns a total when there are real split rows."""
    raw = (request.form.get('tour_supplier_costs') or '').strip()
    BookingSupplierCost.query.filter_by(booking_id=booking.id).delete()
    if not raw:
        return None
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        rows = []
    total = 0.0
    for r in (rows if isinstance(rows, list) else []):
        try:
            sup_id = int(r.get('supplier_id'))
            amt = float(r.get('amount') or 0)
        except (TypeError, ValueError):
            continue
        if not sup_id or amt <= 0:
            continue
        db.session.add(BookingSupplierCost(
            company_id=comp_id, booking_id=booking.id, supplier_id=sup_id,
            component=(r.get('component') or '').strip()[:100],
            amount=amt, remarks=(r.get('remarks') or '').strip()[:255]
        ))
        total += amt
    return round(total, 2) if total > 0 else None

# ---- External-supplier vehicle-rent helpers ----
def _parse_date(value):
    """Parse a 'YYYY-MM-DD' string into a date, tolerating empties/None."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None

def _clean_period(value):
    """Normalise a rent period to one of daily/monthly/yearly."""
    v = (str(value or '').strip().lower())
    return v if v in ('daily', 'monthly', 'yearly') else 'daily'

def _applicable_agreement(vehicle_id, comp_id, on_date=None):
    """The supplier-rent agreement that governs `on_date` for a vehicle.
    Picks the agreement whose [start_date, end_date] range contains the date
    (open-ended bounds count as always-valid on that side). If several match,
    the most recently created one wins. Returns None when nothing covers the
    date, which the caller treats as 'agreement expired'."""
    on_date = on_date or date.today()
    ags = VehicleRentAgreement.query.filter_by(company_id=comp_id, vehicle_id=vehicle_id)\
        .order_by(VehicleRentAgreement.id.desc()).all()
    for a in ags:
        if a.start_date and on_date < a.start_date:
            continue
        if a.end_date and on_date > a.end_date:
            continue
        return a
    return None

def _compute_supplier_rent(rent_amount, period, pickup_dt, dropoff_dt):
    """Estimate what we owe the supplier for a rental window. Units are rounded
    UP (a part day/month/year counts as one) and floored at 1, matching how
    rental rates are normally billed. The result is only a default — the user
    can still override the net cost on the booking form."""
    rent_amount = rent_amount or 0.0
    period = _clean_period(period)
    if not pickup_dt:
        return round(rent_amount, 2)
    end = dropoff_dt or pickup_dt
    total_days = (end - pickup_dt).total_seconds() / 86400.0
    if period == 'monthly':
        units = max(1, math.ceil(total_days / 30.0))
    elif period == 'yearly':
        units = max(1, math.ceil(total_days / 365.0))
    else:
        units = max(1, math.ceil(total_days)) if total_days > 0 else 1
    return round(rent_amount * units, 2)

# ---- Ledger balance helpers (apply/reverse are exact inverses, so create/edit/delete stay consistent) ----
def _apply_ledger_effect(ledger, comp_id):
    """Apply a ledger row's effect on customer / supplier / bank balances."""
    amt = ledger.amount or 0.0
    if ledger.customer_id:
        c = Customer.query.filter_by(id=ledger.customer_id, company_id=comp_id).first()
        if c: c.balance = (c.balance or 0.0) - amt
    if ledger.supplier_id:
        s = Supplier.query.filter_by(id=ledger.supplier_id, company_id=comp_id).first()
        if s: s.balance = (s.balance or 0.0) + amt
    if ledger.bank_account_id:
        b = BankAccount.query.filter_by(id=ledger.bank_account_id, company_id=comp_id).first()
        if b: b.balance = (b.balance or 0.0) + amt
    if ledger.capital_account_id:
        cap = CapitalAccount.query.filter_by(id=ledger.capital_account_id, company_id=comp_id).first()
        if cap: cap.balance = (cap.balance or 0.0) + amt

def _reverse_ledger_effect(ledger, comp_id):
    """Reverse a ledger row's effect on customer / supplier / bank balances."""
    amt = ledger.amount or 0.0
    if ledger.customer_id:
        c = Customer.query.filter_by(id=ledger.customer_id, company_id=comp_id).first()
        if c: c.balance = (c.balance or 0.0) + amt
    if ledger.supplier_id:
        s = Supplier.query.filter_by(id=ledger.supplier_id, company_id=comp_id).first()
        if s: s.balance = (s.balance or 0.0) - amt
    if ledger.bank_account_id:
        b = BankAccount.query.filter_by(id=ledger.bank_account_id, company_id=comp_id).first()
        if b: b.balance = (b.balance or 0.0) - amt
    if ledger.capital_account_id:
        cap = CapitalAccount.query.filter_by(id=ledger.capital_account_id, company_id=comp_id).first()
        if cap: cap.balance = (cap.balance or 0.0) - amt

# ---- Invoice / bill allocation helpers (record what a receipt paid so edit & delete can undo it) ----
_BOOKING_DEFER = [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]

def _alloc_list(ledger):
    """Return the stored allocations for a ledger row as a list of dicts."""
    if not ledger or not getattr(ledger, 'allocations', None):
        return []
    try:
        data = json.loads(ledger.allocations)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _reverse_invoice_allocations(allocs, comp_id):
    """Give the balance back to every invoice this receipt had paid. Returns total reversed."""
    total = 0.0
    for a in allocs:
        try:
            iid = int(a.get('invoice_id')); amt = float(a.get('amount') or 0)
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        inv = Invoice.query.filter_by(id=iid, company_id=comp_id).first()
        if inv:
            inv.total_paid = (inv.total_paid or 0.0) - amt
            inv.balance_due = (inv.balance_due or 0.0) + amt
            total += amt
    return total

def _reverse_bill_allocations(allocs, comp_id):
    """Undo supplier-bill payments this receipt had made. Returns total reversed."""
    total = 0.0
    for a in allocs:
        try:
            bid = int(a.get('bill_id')); amt = float(a.get('amount') or 0)
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        bill = Booking.query.filter_by(id=bid, company_id=comp_id).options(*[defer(c) for c in _BOOKING_DEFER]).first()
        if bill:
            bill.supplier_paid = max(0.0, (bill.supplier_paid or 0.0) - amt)
            total += amt
    return total

def _apply_invoice_allocations(form, comp_id, max_total=None):
    """Apply apply_amount[] against invoice_id[]; reduce each invoice's balance.
    max_total (optional) caps the cumulative amount applied to the receipt total,
    so allocations can never exceed the money actually received.
    Returns (allocations_list, total_applied)."""
    ids = form.getlist('invoice_id[]'); amts = form.getlist('apply_amount[]')
    out, total = [], 0.0
    for i in range(len(ids)):
        try:
            iid = int(ids[i]); amt = float(amts[i]) if i < len(amts) and amts[i] else 0.0
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        inv = Invoice.query.filter_by(id=iid, company_id=comp_id).first()
        if not inv:
            continue
        actual = min(amt, inv.balance_due or 0.0)
        if max_total is not None:
            actual = min(actual, max(0.0, round(max_total - total, 2)))
        if actual <= 0:
            continue
        inv.total_paid = (inv.total_paid or 0.0) + actual
        inv.balance_due = (inv.balance_due or 0.0) - actual
        out.append({'invoice_id': iid, 'amount': round(actual, 2)})
        total += actual
    return out, total

def _apply_bill_allocations(form, comp_id, max_total=None):
    """Apply apply_amount[] against bill_id[]; reduce each supplier bill's outstanding.
    max_total (optional) caps the cumulative amount applied to the payment total.
    Returns (allocations_list, total_applied)."""
    ids = form.getlist('bill_id[]'); amts = form.getlist('apply_amount[]')
    out, total = [], 0.0
    for i in range(len(ids)):
        try:
            bid = int(ids[i]); amt = float(amts[i]) if i < len(amts) and amts[i] else 0.0
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        bill = Booking.query.filter_by(id=bid, company_id=comp_id).options(*[defer(c) for c in _BOOKING_DEFER]).first()
        if not bill:
            continue
        outstanding = (bill.net_cost or 0.0) - (bill.supplier_paid or 0.0)
        actual = min(amt, outstanding)
        if max_total is not None:
            actual = min(actual, max(0.0, round(max_total - total, 2)))
        if actual <= 0:
            continue
        bill.supplier_paid = (bill.supplier_paid or 0.0) + actual
        out.append({'bill_id': bid, 'amount': round(actual, 2)})
        total += actual
    return out, total

def _reverse_ledger_effects(rec, comp_id):
    """Undo every balance / allocation effect of a single ledger row.

    Shared by delete_receipt (delete) and save_receipt (edit = reverse-then-recreate)
    so the two paths stay perfectly symmetric and balances never drift."""
    if not rec:
        return
    if rec.customer_id:
        cust = Customer.query.get(rec.customer_id)
        if cust: cust.balance = (cust.balance or 0.0) + rec.amount
    if rec.supplier_id:
        sup = Supplier.query.get(rec.supplier_id)
        if sup: sup.balance = (sup.balance or 0.0) - rec.amount
    if rec.bank_account_id:
        bank = BankAccount.query.get(rec.bank_account_id)
        if bank: bank.balance = (bank.balance or 0.0) - rec.amount
    if rec.capital_account_id:
        cap = CapitalAccount.query.get(rec.capital_account_id)
        if cap: cap.balance = (cap.balance or 0.0) - rec.amount

    allocs = _alloc_list(rec)
    if rec.category in ('receipt', 'adjustment_invoice'):
        restored = _reverse_invoice_allocations(allocs, comp_id)
        if rec.category == 'adjustment_invoice' and rec.customer_id:
            c = Customer.query.filter_by(id=rec.customer_id, company_id=comp_id).first()
            if c: c.balance = (c.balance or 0.0) - restored
    elif rec.category == 'payment':
        _reverse_bill_allocations(allocs, comp_id)

    # Req #3: a vehicle EMI/loan payment reduced the vehicle's loan balance — restore it
    # here so delete and edit-as-reverse-then-recreate stay perfectly symmetric.
    if rec.category == 'vehicle_emi' and rec.vehicle_id:
        veh = Vehicle.query.filter_by(id=rec.vehicle_id, company_id=comp_id).first()
        if veh:
            veh.total_loan_balance = (veh.total_loan_balance or 0.0) + abs(rec.amount or 0.0)

@app.route('/booking_photo/<int:booking_id>/<photo_name>')
def booking_photo(booking_id, photo_name):
    if photo_name not in ['photo1', 'photo2', 'photo3', 'photo4', 'driver_id_card']: return "Invalid photo field", 400
    comp_id = session.get('company_id')
    booking = Booking.query.filter_by(id=booking_id, company_id=comp_id).first()
    if not booking: return "Not found", 404
    filename_bytes = getattr(booking, photo_name)
    if not filename_bytes: return "No photo", 404
    try:
        if isinstance(filename_bytes, bytes): filename = filename_bytes.decode('utf-8')
        else: filename = filename_bytes
    except UnicodeDecodeError:
        return Response(filename_bytes, mimetype='image/jpeg')
    storage_dir = get_company_photo_path(comp_id)
    full_path = os.path.join(storage_dir, filename)
    if os.path.exists(full_path):
        from flask import send_file
        return send_file(full_path, mimetype='image/jpeg')
    old_dir = os.path.join(app.root_path, 'static', 'uploads')
    old_path = os.path.join(old_dir, filename)
    if os.path.exists(old_path):
        from flask import send_from_directory
        return send_from_directory(old_dir, filename)
    return "Photo file missing", 404

@app.route('/')
def index(): 
    return render_template('index.html', username=session.get('username', 'Admin'))

@app.route('/login', methods=['GET', 'POST'])
def login(): 
    if request.method == 'POST':
        username = request.form.get('username')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, request.form.get('password')):
            if user.status and user.status.lower() != 'active':
                return render_template('login.html', error=f"Your account is {user.status}. Please contact an administrator.")
            if user.role != 'Super Admin':
                company = Company.query.get(user.company_id)
                if company and not company.is_active:
                    return render_template('login.html', error="Your company's subscription is not active. Please contact your administrator.")
            session.permanent = True
            session['user_id'] = user.id
            session['username'] = user.username
            session['company_id'] = user.company_id
            session['role'] = user.role
            return redirect(url_for('index'))
        return render_template('login.html', error="Invalid Username or Password")
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

# ---------------------------------------------------------------------------
# Password reset via OTP (email) + authenticated password change
# ---------------------------------------------------------------------------
def send_email(to_email, subject, body):
    """Send a plain-text email via SMTP. Settings come from SMTP_CONFIG (top of file)
    or environment variables of the same name. Returns True on success, False if not
    configured or on error."""
    host = _smtp('host')
    if not host or not to_email:
        return False
    port = int(_smtp('port') or '587')
    smtp_user = _smtp('user')
    smtp_pass = _smtp('password')
    sender = _smtp('from') or smtp_user or 'no-reply@localhost'
    use_tls = (_smtp('use_tls') or '1') != '0'
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = to_email
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if use_tls:
                server.starttls()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.sendmail(sender, [to_email], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {repr(e)}", flush=True)
        return False

def _hash_otp(otp):
    return hashlib.sha256((otp or '').encode('utf-8')).hexdigest()

def _mask_email(addr):
    try:
        local, domain = addr.split('@', 1)
        shown = local[0] + '***' if len(local) > 1 else '*'
        return f"{shown}@{domain}"
    except Exception:
        return "your registered email"

@app.route('/api/forgot_password', methods=['POST'])
def forgot_password():
    data = request.get_json(silent=True) or {}
    identifier = (data.get('identifier') or '').strip()
    if not identifier:
        return jsonify({"success": False, "message": "Please enter your username or email."})
    user = User.query.filter((User.username == identifier) | (User.email == identifier)).first()
    if not user:
        # Generic response to avoid revealing whether an account exists.
        return jsonify({"success": True, "message": "If an account matches, a verification code has been sent."})
    company = Company.query.get(user.company_id)
    target_email = (user.email or (company.master_email if company else None) or '').strip()
    if not target_email:
        return jsonify({"success": False, "message": "No email is on file for this account. Please contact your administrator."})

    # Invalidate any earlier unused codes, then issue a fresh one.
    PasswordResetOTP.query.filter_by(user_id=user.id, used=False).update({'used': True})
    otp = f"{secrets.randbelow(900000) + 100000}"
    rec = PasswordResetOTP(user_id=user.id, otp_hash=_hash_otp(otp), expires_at=datetime.utcnow() + timedelta(minutes=10))
    db.session.add(rec)
    db.session.commit()

    body = (f"Your password reset code is: {otp}\n\n"
            f"This code expires in 10 minutes. If you didn't request a reset, you can ignore this email.")
    sent = send_email(target_email, "Your password reset code", body)
    print(f"[OTP DEBUG] user={user.username} email={target_email} sent={sent} code={otp}", flush=True)
    return jsonify({"success": True, "message": f"A verification code has been sent to {_mask_email(target_email)}.",
                    "email_hint": _mask_email(target_email)})

@app.route('/api/reset_password', methods=['POST'])
def reset_password():
    data = request.get_json(silent=True) or {}
    identifier = (data.get('identifier') or '').strip()
    otp = (data.get('otp') or '').strip()
    new_password = data.get('new_password') or ''
    if not identifier or not otp or not new_password:
        return jsonify({"success": False, "message": "All fields are required."})
    if len(new_password) < 6:
        return jsonify({"success": False, "message": "Password must be at least 6 characters."})
    user = User.query.filter((User.username == identifier) | (User.email == identifier)).first()
    if not user:
        return jsonify({"success": False, "message": "Invalid code or account."})
    rec = PasswordResetOTP.query.filter_by(user_id=user.id, used=False).order_by(PasswordResetOTP.id.desc()).first()
    if not rec or rec.expires_at < datetime.utcnow():
        return jsonify({"success": False, "message": "The code has expired. Please request a new one."})
    if (rec.attempts or 0) >= 5:
        rec.used = True
        db.session.commit()
        return jsonify({"success": False, "message": "Too many incorrect attempts. Please request a new code."})
    if rec.otp_hash != _hash_otp(otp):
        rec.attempts = (rec.attempts or 0) + 1
        db.session.commit()
        return jsonify({"success": False, "message": "Invalid code. Please try again."})
    user.password_hash = generate_password_hash(new_password)
    rec.used = True
    db.session.commit()
    return jsonify({"success": True, "message": "Password reset successful. You can now sign in."})

@app.route('/api/change_password', methods=['POST'])
def change_password():
    if not session.get('user_id'):
        return jsonify({"success": False, "message": "Not authenticated."}), 401
    data = request.get_json(silent=True) or {}
    current = data.get('current_password') or ''
    new_password = data.get('new_password') or ''
    if len(new_password) < 6:
        return jsonify({"success": False, "message": "New password must be at least 6 characters."})
    user = User.query.get(session.get('user_id'))
    if not user or not check_password_hash(user.password_hash, current):
        return jsonify({"success": False, "message": "Current password is incorrect."})
    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({"success": True, "message": "Password changed successfully."})

@app.errorhandler(500)
def internal_error(error): return "500 Internal Server Error", 500
@app.errorhandler(404)
def not_found_error(error): return "404 Not Found", 404

# ==========================================
# MASTER ACCOUNTS
# ==========================================
@app.route('/accounts')
@require_permission('Accounts_View')
def accounts(): 
    comp_id = session.get('company_id')
    return render_template('accounts.html', 
                           customers=Customer.query.filter_by(company_id=comp_id).all(), 
                           suppliers=Supplier.query.filter_by(company_id=comp_id).all(), 
                           bank_accounts=BankAccount.query.filter_by(company_id=comp_id).all(),
                           capital_accounts=CapitalAccount.query.filter_by(company_id=comp_id).order_by(CapitalAccount.name.asc()).all(),
                           services=ServiceItem.query.filter_by(company_id=comp_id).all(),
                           vehicles=Vehicle.query.filter_by(company_id=comp_id).all(), 
                           buildings=Building.query.filter_by(company_id=comp_id).all(), 
                           rooms=Room.query.filter_by(company_id=comp_id).all(), 
                           layouts=PrintLayout.query.filter_by(company_id=comp_id).all())

# ==========================================
# BOOKING ENGINE
# ==========================================
def build_calendar_events(comp_id):
    """Active (non-terminated) car & room bookings that are current or upcoming, for the booking calendar."""
    today = datetime.now().date()
    cust_map = {c.id: c.name for c in Customer.query.filter_by(company_id=comp_id).all()}
    veh_map = {v.id: {'name': v.name, 'reg': v.registration_number} for v in Vehicle.query.filter_by(company_id=comp_id).all()}
    bld_map = {b.id: b.name for b in Building.query.filter_by(company_id=comp_id).all()}
    room_map = {r.id: r for r in Room.query.filter_by(company_id=comp_id).all()}
    photo_cols = [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]
    rows = Booking.query.filter(
        Booking.company_id == comp_id,
        Booking.booking_finalized == False,
        Booking.service_type.in_(['rentacar', 'room_rent'])
    ).options(*[defer(col) for col in photo_cols]).all()
    events = []
    for b in rows:
        start_dt, end_dt = b.pickup_datetime, b.dropoff_datetime
        if not start_dt:
            continue  # no usable date range to place on the calendar
        if end_dt and end_dt.date() < today:
            continue  # already ended — not current/upcoming
        ev = {
            'id': b.id,
            'type': 'car' if b.service_type == 'rentacar' else 'room',
            'customer': cust_map.get(b.customer_id, 'Unknown'),
            'start': start_dt.strftime('%Y-%m-%d'),
            'end': (end_dt or start_dt).strftime('%Y-%m-%d'),
            'from': start_dt.strftime('%Y-%m-%d %H:%M'),
            'upto': end_dt.strftime('%Y-%m-%d %H:%M') if end_dt else 'Open',
        }
        if b.service_type == 'rentacar':
            v = veh_map.get(b.vehicle_id, {})
            ev.update({'car_name': v.get('name', 'Unknown Vehicle'), 'car_number': v.get('reg') or '-',
                       'driver_name': b.driver_name or '-', 'title': v.get('name', 'Vehicle')})
        else:
            rm = room_map.get(b.room_id)
            ev.update({'room_number': rm.room_number if rm else '-',
                       'building': bld_map.get(rm.building_id, 'Unknown') if rm else '-',
                       'title': 'Room ' + (rm.room_number if rm else '?')})
        events.append(ev)
    return events

# Pages a form may ask to return to after saving. Whitelisted on purpose: the value
# comes from the request body, so it maps to an endpoint name, never a raw URL.
_RETURN_TO_ROUTES = {'visaplus': 'visaplus', 'booking': 'booking'}

# ==========================================
# VISA TYPES (shared by Booking + Visa Plus)
# ==========================================
# Starter templates every tenant gets. Anything beyond these is created by the user
# from the Booking page with its own duration.
VISA_TYPE_TEMPLATES = [('Tourist', 30), ('Business', 30), ('Visit', 30)]

def get_visa_types(company_id):
    """Visa types for a tenant, seeding the starter templates on first use so a new
    company (and any company from before this feature) is never left with an empty list."""
    if not company_id:
        return []
    types = VisaType.query.filter_by(company_id=company_id).order_by(VisaType.name.asc()).all()
    if not types:
        try:
            for _n, _d in VISA_TYPE_TEMPLATES:
                db.session.add(VisaType(company_id=company_id, name=_n, default_days=_d))
            db.session.commit()
        except Exception:
            # A concurrent request may have seeded first — fall through and re-read.
            db.session.rollback()
        types = VisaType.query.filter_by(company_id=company_id).order_by(VisaType.name.asc()).all()
    return types

def _visa_days(value, default=None):
    """Parse a duration from a form field. An empty string must become NULL, not 0 —
    MySQL strict mode rejects '' against an INT column."""
    try:
        v = (value or '').strip() if isinstance(value, str) else value
        if v in (None, ''):
            return default
        d = int(float(v))
        return d if d > 0 else default
    except (TypeError, ValueError):
        return default

# ==========================================
# VISA TIMELINE HISTORY
# ==========================================
# Statuses the follow-up modal is allowed to set. Anything else is ignored rather
# than written, so a hand-crafted request cannot invent a status the rest of the
# page (row colours, filters, the arrival countdown) does not understand.
VISA_NOTE_STATUSES = {'Processing', 'Action Required', 'Approved', 'Rejected'}
VISA_RENEWAL_STATUSES = {'Under Renewal Processing', 'Renewal Approved'}

def log_visa_history(booking_id, company_id, description, username=None, when=None):
    """Append one timeline entry for a visa. Adds to the session; the caller commits.

    Deliberately never raises: a booking, an arrival or an exit must still save even
    if its log line cannot be written."""
    try:
        desc = (description or '').strip()
        if not booking_id or not company_id or not desc:
            return None
        entry = VisaHistory(company_id=company_id, booking_id=booking_id,
                            description=desc[:2000],
                            username=(username if username is not None else session.get('username')),
                            entry_time=when or datetime.utcnow())
        db.session.add(entry)
        return entry
    except Exception as e:
        app.logger.warning(f'Visa history not logged for booking {booking_id}: {e}')
        return None

def _visa_history_rows(company_id, booking_ids):
    """Newest first, so the modal reads top-down like the old client-side log did."""
    if not company_id or not booking_ids:
        return []
    return VisaHistory.query.filter(
        VisaHistory.company_id == company_id,
        VisaHistory.booking_id.in_(list(booking_ids))
    ).order_by(VisaHistory.entry_time.desc(), VisaHistory.id.desc()).all()

def purge_visa_history(booking_id, company_id):
    """Drop a visa's timeline when the booking itself is deleted, so no orphan rows
    are left pointing at an id that no longer exists (and could be reused)."""
    try:
        if not booking_id or not company_id:
            return
        VisaHistory.query.filter_by(company_id=company_id, booking_id=booking_id).delete(synchronize_session=False)
    except Exception as e:
        app.logger.warning(f'Visa history not purged for booking {booking_id}: {e}')

def _visa_history_payload(entries):
    out = []
    for h in entries:
        stamp = h.entry_time or datetime.utcnow()
        out.append({'id': h.id,
                    'time': stamp.strftime('%d %b %Y, %H:%M'),
                    'user': h.username or '',
                    'desc': h.description or ''})
    return out

@app.route('/booking')
@require_permission('Booking_View')
def booking(): 
    comp_id = session.get('company_id')
    page = request.args.get('page', 1, type=int)
    active_rooms = {b.room_id: b.id for b in Booking.query.filter_by(company_id=comp_id, booking_finalized=False).filter(Booking.room_id.isnot(None)).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).all()}
    active_vehicles = {b.vehicle_id: b.id for b in Booking.query.filter_by(company_id=comp_id, booking_finalized=False).filter(Booking.vehicle_id.isnot(None)).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).all()}

    photo_cols = [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]
    bookings = Booking.query.filter_by(company_id=comp_id).options(*[defer(col) for col in photo_cols]).order_by(Booking.id.desc()).paginate(page=page, per_page=10, error_out=False)

    all_customers = Customer.query.filter_by(company_id=comp_id).all()
    all_vehicles = Vehicle.query.filter_by(company_id=comp_id).all()
    all_rooms = Room.query.filter_by(company_id=comp_id).all()
    all_buildings = Building.query.filter_by(company_id=comp_id).all()
    
    customers_dict = {c.id: c.name for c in all_customers}
    vehicles_dict = {v.id: {'name': v.name, 'reg': v.registration_number} for v in all_vehicles}
    buildings_dict = {b.id: b.name for b in Building.query.filter_by(company_id=comp_id).all()}
    rooms_dict = {r.id: f"{buildings_dict.get(r.building_id, 'Unknown')} - Room {r.room_number}" for r in all_rooms}
    
    page_ids = [b.id for b in bookings.items]
    photo_status = {bid: {'photo1':False, 'photo2':False, 'photo3':False, 'photo4':False, 'driver_id_card':False} for bid in page_ids}
    if page_ids:
        for p_col in ['photo1', 'photo2', 'photo3', 'photo4', 'driver_id_card']:
            has_photo = db.session.query(Booking.id).filter(Booking.id.in_(page_ids), getattr(Booking, p_col).isnot(None)).all()
            for (bid,) in has_photo: photo_status[bid][p_col] = True

    return render_template('booking.html', customers=all_customers, customers_dict=customers_dict, suppliers=Supplier.query.filter_by(company_id=comp_id).all(), bank_accounts=BankAccount.query.filter_by(company_id=comp_id).all(), vehicles=all_vehicles, vehicles_dict=vehicles_dict, active_vehicles=active_vehicles, services=ServiceItem.query.filter_by(company_id=comp_id).all(), visa_types=get_visa_types(comp_id), buildings=all_buildings, buildings_dict=buildings_dict, rooms=all_rooms, rooms_dict=rooms_dict, active_rooms=active_rooms, bookings=bookings, photo_status=photo_status, calendar_events=build_calendar_events(comp_id))

@app.route('/save_booking', methods=['POST'])
@require_permission('Booking_Add')
def save_booking(): 
    comp_id = session.get('company_id')
    service_type = request.form.get('service_type')
    # Rent a Car: driver name, phone and license validity are mandatory.
    if service_type == 'rentacar':
        _missing = []
        if not (request.form.get('driver_name') or '').strip(): _missing.append('Driver / User Name')
        if not (request.form.get('driver_phone') or '').strip(): _missing.append('Phone Number')
        if not (request.form.get('license_valid_upto') or '').strip(): _missing.append('License Valid Upto')
        if _missing:
            return f"<script>alert('Please fill the required Rent a Car fields: {', '.join(_missing)}.'); window.history.back();</script>"
    carrier = request.form.get(f'{service_type}_carrier')
    pnr = request.form.get(f'{service_type}_pnr') or request.form.get(f'{service_type}_ticket') or "N/A"
    
    if pnr != "N/A" and pnr.strip() != "":
        existing = Booking.query.filter_by(company_id=comp_id, pnr_ref=pnr).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first()
        if existing: return f"<script>alert('ERROR: Duplicate Data! The PNR or Reference {pnr} already exists.'); window.history.back();</script>"

    if service_type == 'flight': date_str = request.form.get('departure_date') or request.form.get('flight_date')
    else: date_str = request.form.get(f'{service_type}_date')
    date_str = date_str or request.form.get('visa_expiry') or request.form.get('other_date')
    travel_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else None
    return_date_str = request.form.get('return_date')
    return_date = datetime.strptime(return_date_str, '%Y-%m-%d').date() if return_date_str else None
    departure_time_str = request.form.get('departure_time')
    return_time = request.form.get('return_time')
    arrival_place = request.form.get(f'{service_type}_arrival_place') or request.form.get('arrival_place')
    departure_place = request.form.get(f'{service_type}_departure_place') or request.form.get('departure_place')
    arrival_time_str = request.form.get(f'{service_type}_arrival_time')
    arrival_date_str = request.form.get(f'{service_type}_arrival_date') or date_str
    visa_country = request.form.get('visa_country')
    train_number = request.form.get('train_number')
    flight_number = request.form.get('flight_number')
    train_name = request.form.get('train_name')
    
    departure_time = None
    arrival_time = None
    if departure_time_str:
        try: departure_time = datetime.strptime(f"{travel_date} {departure_time_str}", '%Y-%m-%d %H:%M') if travel_date else datetime.strptime(departure_time_str, '%H:%M')
        except ValueError: departure_time = None
    if arrival_time_str:
        try: arrival_time = datetime.strptime(f"{arrival_date_str} {arrival_time_str}", '%Y-%m-%d %H:%M') if arrival_date_str else datetime.strptime(arrival_time_str, '%H:%M')
        except ValueError: arrival_time = None
        
    hold_time_str = request.form.get('hold_release_time')
    hold_time = datetime.strptime(hold_time_str, '%Y-%m-%dT%H:%M') if hold_time_str else None

    veh_id = request.form.get('vehicle_id')
    room_id = request.form.get('room_id')
    pickup_str = get_last_nonempty_form_value('pickup_datetime')
    dropoff_str = get_last_nonempty_form_value('dropoff_datetime')
    lic_val = request.form.get('license_valid_upto')
    bp = float(request.form.get('buying_price') or 0.0)
    sp = float(request.form.get('selling_price') or 0.0)

    new_booking = Booking(
        company_id=comp_id, customer_id=request.form.get('customer_id'), supplier_id=request.form.get('supplier_id') or None, 
        bank_account_id=request.form.get('bank_account_id') or None, service_type=service_type, carrier_code=carrier, pnr_ref=pnr, 
        pnr_status=request.form.get('pnr_status', 'Confirmed'), hold_release_time=hold_time, travel_date=travel_date, return_date=return_date, 
        departure_time=departure_time, return_time=return_time, arrival_place=arrival_place, departure_place=departure_place, 
        flight_number=flight_number, arrival_time=arrival_time, rent_source=request.form.get('rent_source'), vehicle_id=veh_id or None, 
room_id=room_id or None, pickup_datetime=parse_datetime_local(pickup_str), dropoff_datetime=parse_datetime_local(dropoff_str),
        pickup_location=request.form.get('pickup_location'), drop_location=request.form.get('drop_location'),
        rent_type=request.form.get('rent_type'), driving_license=request.form.get('driving_license'), passport_id=request.form.get('passport_id'), 
        nationality=request.form.get('nationality'), visa_country=visa_country, train_number=train_number, train_name=train_name,
        visa_duration_days=(_visa_days(request.form.get('visa_duration_days')) if service_type == 'visa' else None),
        license_issued_at=request.form.get('license_issued_at'), license_valid_upto=datetime.strptime(lic_val, '%Y-%m-%d').date() if lic_val else None,
        fuel_stage_out=request.form.get('fuel_stage_out'), km_out=int(request.form.get('km_out') or 0), km_allowed=int(request.form.get('km_allowed') or 0), 
        extra_km_price=float(request.form.get('extra_km_price') or 0.0), rent_amount=float(request.form.get('hotel_rent_amount') or request.form.get('room_rent_amount') or request.form.get('car_rent_amount') or request.form.get('rent_amount') or 0.0),
        remarks=request.form.get('remarks'), net_cost=bp, selling_price=sp, passenger_details=request.form.get('passenger_details', ''),
        advance_paid=float(request.form.get('advance_paid') or 0.0), pax_adults=int(request.form.get('pax_adults') or 1), pax_child=int(request.form.get('pax_child') or 0), 
        pax_infants=int(request.form.get('pax_infants') or 0), elec_reading_in=float(request.form.get('elec_reading_in') or 0.0), water_reading_in=float(request.form.get('water_reading_in') or 0.0), 
        elec_unit_price=float(request.form.get('elec_unit_price') or 0.0), water_unit_price=float(request.form.get('water_unit_price') or 0.0),
        driver_name=request.form.get('driver_name'), driver_phone=request.form.get('driver_phone'), driver_email=request.form.get('driver_email'),
        additional_driver_details=request.form.get('additional_driver_details'), rent_location=request.form.get('rent_location'),
        license_issued_country=request.form.get('license_issued_country'), rent_description=request.form.get('rent_description'),
        add_driver_name=request.form.get('add_driver_name'), add_driver_phone=request.form.get('add_driver_phone'), add_driver_email=request.form.get('add_driver_email'), 
        add_driver_license=request.form.get('add_driver_license'), add_driver_license_country=request.form.get('add_driver_license_country'),
        add_driver_license_valid=datetime.strptime(request.form.get('add_driver_license_valid'), '%Y-%m-%d').date() if request.form.get('add_driver_license_valid') else None,
        add_driver_license_issued_at=request.form.get('add_driver_license_issued_at'), add_driver_passport=request.form.get('add_driver_passport'), 
        add_driver_nationality=request.form.get('add_driver_nationality'),
        tour_package_name=request.form.get('tour_package_name'), tour_hotel_name=request.form.get('tour_hotel_name'),
        tour_itinerary=request.form.get('tour_itinerary'), tour_inclusions=request.form.get('tour_inclusions'),
        tour_exclusions=request.form.get('tour_exclusions'),
        created_by=session.get('user_id')
    )
    db.session.add(new_booking)
    db.session.flush()

    # Tour: cost split across multiple suppliers (transport / airline / hotel / ...).
    # When the builder was used, the split lines' total replaces the single buying price
    # and the single-supplier field is cleared so it isn't counted twice.
    split_total = _save_tour_supplier_costs(new_booking, comp_id)
    if split_total is not None:
        new_booking.net_cost = split_total
        new_booking.supplier_id = None

    for field in ['photo1', 'photo2', 'photo3', 'photo4', 'driver_id_card']:
        if request.form.get(f'{field}_remove') == '1':
            continue
        file_obj = request.files.get(f'{field}_file')
        photo_data = file_obj.read() if file_obj and file_obj.filename else decode_image_field(request.form.get(f'{field}_data'))
        if photo_data:
            filename = save_photo_to_disk(new_booking.id, field, photo_data, comp_id)
            if filename:
                setattr(new_booking, field, filename.encode('utf-8'))

    if service_type == 'rentacar' and veh_id:
        veh = Vehicle.query.get(veh_id)
        if veh: veh.status = "Rented"
    if service_type == 'room_rent' and room_id:
        rm = Room.query.get(room_id)
        if rm: rm.status = "Rented"
    # Opening line of the Visa Plus timeline. Written here, against the real id the
    # flush just produced, instead of being guessed in the browser.
    if service_type == 'visa':
        _created = 'Visa application created.'
        if visa_country: _created += f' Destination: {visa_country}.'
        if carrier: _created += f' Type: {carrier}.'
        if new_booking.visa_duration_days: _created += f' Duration: {new_booking.visa_duration_days} days.'
        if pnr and pnr != 'N/A': _created += f' Visa No: {pnr}.'
        log_visa_history(new_booking.id, comp_id, _created)
    try:
        db.session.commit()
    except Exception as _e:
        db.session.rollback()
        _msg = (str(_e).replace(chr(39), ' ').replace(chr(34), ' ').replace(chr(10), ' ').replace(chr(13), ' '))[:300]
        return f"<script>alert('Could not save the booking:\n\n{_msg}\n\nYour entries are still here \u2014 press OK to go back, fix the issue, and save again.'); window.history.back();</script>"
    if 'save_and_invoice' in request.form: return redirect(url_for('invoice', booking_id=new_booking.id))
    # A form can ask to be returned to the page it was opened from (e.g. the Visa Plus
    # tracker). Resolved against a whitelist — never redirect to a caller-supplied URL.
    _dest = _RETURN_TO_ROUTES.get((request.form.get('return_to') or '').strip())
    if _dest: return redirect(url_for(_dest))
    return redirect(url_for('booking'))

@app.route('/edit_booking/<int:id>', methods=['GET', 'POST'])
@require_permission('Booking_Edit')
def edit_booking(id): 
    comp_id = session.get('company_id')
    existing_booking = Booking.query.filter_by(id=id, company_id=comp_id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first()
    if not existing_booking: return redirect(url_for('booking'))

    if request.method == 'GET':
        all_customers = Customer.query.filter_by(company_id=comp_id).all()
        all_vehicles = Vehicle.query.filter_by(company_id=comp_id).all()
        all_rooms = Room.query.filter_by(company_id=comp_id).all()
        all_buildings = Building.query.filter_by(company_id=comp_id).all()
        
        customers_dict = {c.id: c.name for c in all_customers}
        vehicles_dict = {v.id: {'name': v.name, 'reg': v.registration_number} for v in all_vehicles}
        b_dict = {b.id: b.name for b in all_buildings}
        rooms_dict = {r.id: f"{b_dict.get(r.building_id, 'Unknown')} - Room {r.room_number}" for r in all_rooms}
        
        p_status = {existing_booking.id: {'photo1':False, 'photo2':False, 'photo3':False, 'photo4':False, 'driver_id_card':False}}
        for p_col in ['photo1', 'photo2', 'photo3', 'photo4', 'driver_id_card']:
            exists = db.session.query(Booking.id).filter(Booking.id == existing_booking.id, getattr(Booking, p_col).isnot(None)).scalar()
            if exists: p_status[existing_booking.id][p_col] = True

        tour_supplier_costs_json = json.dumps([
            {'supplier_id': sc.supplier_id, 'component': sc.component or '', 'amount': sc.amount or 0.0, 'remarks': sc.remarks or ''}
            for sc in BookingSupplierCost.query.filter_by(booking_id=existing_booking.id).all()
        ])

        return render_template('booking.html', customers=all_customers, customers_dict=customers_dict, suppliers=Supplier.query.filter_by(company_id=comp_id).all(), bank_accounts=BankAccount.query.filter_by(company_id=comp_id).all(), vehicles=all_vehicles, vehicles_dict=vehicles_dict, active_vehicles={b.vehicle_id: b.id for b in Booking.query.filter_by(company_id=comp_id, booking_finalized=False).filter(Booking.vehicle_id.isnot(None)).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).all()}, services=ServiceItem.query.filter_by(company_id=comp_id).all(), visa_types=get_visa_types(comp_id), buildings=all_buildings, buildings_dict=b_dict, rooms=all_rooms, rooms_dict=rooms_dict, active_rooms={b.room_id: b.id for b in Booking.query.filter_by(company_id=comp_id, booking_finalized=False).filter(Booking.room_id.isnot(None)).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).all()}, bookings=Booking.query.filter_by(company_id=comp_id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).order_by(Booking.id.desc()).all(), edit_booking=existing_booking, photo_status=p_status, tour_supplier_costs_json=tour_supplier_costs_json, calendar_events=build_calendar_events(comp_id))

    service_type = request.form.get('service_type')
    pnr = request.form.get(f'{service_type}_pnr') or request.form.get(f'{service_type}_ticket') or "N/A"
    # Rent a Car: driver name, phone and license validity are mandatory.
    if service_type == 'rentacar':
        _missing = []
        if not (request.form.get('driver_name') or '').strip(): _missing.append('Driver / User Name')
        if not (request.form.get('driver_phone') or '').strip(): _missing.append('Phone Number')
        if not (request.form.get('license_valid_upto') or '').strip(): _missing.append('License Valid Upto')
        if _missing:
            return f"<script>alert('Please fill the required Rent a Car fields: {', '.join(_missing)}.'); window.history.back();</script>"
    if pnr != "N/A" and pnr.strip() != "":
        duplicate = Booking.query.filter(Booking.company_id==comp_id, Booking.pnr_ref==pnr, Booking.id != id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first()
        if duplicate: return f"<script>alert('ERROR: Duplicate Data! The PNR or Reference {pnr} already exists.'); window.history.back();</script>"

    existing_booking.customer_id = request.form.get('customer_id')
    existing_booking.service_type = service_type
    existing_booking.pnr_ref = pnr
    existing_booking.pnr_status = request.form.get('pnr_status', 'Confirmed')
    existing_booking.supplier_id = request.form.get('supplier_id') or None
    existing_booking.bank_account_id = request.form.get('bank_account_id') or None
    
    if service_type == 'flight': date_str = request.form.get('departure_date') or request.form.get('flight_date')
    else: date_str = request.form.get(f'{service_type}_date')
    date_str = date_str or request.form.get('visa_expiry') or request.form.get('other_date')
    existing_booking.travel_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else None
    
    return_date_str = request.form.get('return_date')
    existing_booking.return_date = datetime.strptime(return_date_str, '%Y-%m-%d').date() if return_date_str else None
    
    departure_time_str = request.form.get('departure_time')
    if departure_time_str:
        try: existing_booking.departure_time = datetime.strptime(f"{existing_booking.travel_date} {departure_time_str}", '%Y-%m-%d %H:%M') if existing_booking.travel_date else datetime.strptime(departure_time_str, '%H:%M')
        except ValueError: pass

    existing_booking.return_time = request.form.get('return_time')
    existing_booking.arrival_place = request.form.get(f'{service_type}_arrival_place') or request.form.get('arrival_place')
    existing_booking.departure_place = request.form.get(f'{service_type}_departure_place') or request.form.get('departure_place')
    
    arrival_time_str = request.form.get(f'{service_type}_arrival_time')
    arrival_date_str = request.form.get(f'{service_type}_arrival_date') or date_str
    if arrival_time_str:
        try: existing_booking.arrival_time = datetime.strptime(f"{arrival_date_str} {arrival_time_str}", '%Y-%m-%d %H:%M') if arrival_date_str else datetime.strptime(arrival_time_str, '%H:%M')
        except ValueError: pass

    hold_time_str = request.form.get('hold_release_time')
    existing_booking.hold_release_time = datetime.strptime(hold_time_str, '%Y-%m-%dT%H:%M') if hold_time_str else None

    if service_type == 'visa':
        existing_booking.visa_duration_days = _visa_days(request.form.get('visa_duration_days'),
                                                         existing_booking.visa_duration_days)
    existing_booking.vehicle_id = request.form.get('vehicle_id') or None
    existing_booking.room_id = request.form.get('room_id') or None
    existing_booking.rent_source = request.form.get('rent_source')
    pickup_str = get_last_nonempty_form_value('pickup_datetime')
    dropoff_str = get_last_nonempty_form_value('dropoff_datetime')
    existing_booking.pickup_datetime = parse_datetime_local(pickup_str)
    existing_booking.dropoff_datetime = parse_datetime_local(dropoff_str)
    existing_booking.pickup_location = request.form.get('pickup_location')
    existing_booking.drop_location = request.form.get('drop_location')
    existing_booking.rent_type = request.form.get('rent_type')
    existing_booking.rent_amount = float(request.form.get('hotel_rent_amount') or request.form.get('room_rent_amount') or request.form.get('car_rent_amount') or request.form.get('rent_amount') or 0.0)
    existing_booking.driving_license = request.form.get('driving_license')
    existing_booking.passport_id = request.form.get('passport_id')
    existing_booking.nationality = request.form.get('nationality')
    existing_booking.visa_country = request.form.get('visa_country')
    existing_booking.train_number = request.form.get('train_number')
    existing_booking.train_name = request.form.get('train_name')
    existing_booking.flight_number = request.form.get('flight_number')
    lic_val = request.form.get('license_valid_upto')
    existing_booking.license_issued_at = request.form.get('license_issued_at')
    existing_booking.license_valid_upto = datetime.strptime(lic_val, '%Y-%m-%d').date() if lic_val else None
    existing_booking.passenger_details = request.form.get('passenger_details', '')
    existing_booking.pax_adults = int(request.form.get('pax_adults') or 1)
    existing_booking.pax_child = int(request.form.get('pax_child') or 0)
    existing_booking.pax_infants = int(request.form.get('pax_infants') or 0)
    existing_booking.additional_driver_details = request.form.get('additional_driver_details')
    existing_booking.driver_name = request.form.get('driver_name')
    existing_booking.driver_phone = request.form.get('driver_phone')
    existing_booking.driver_email = request.form.get('driver_email')
    existing_booking.license_issued_country = request.form.get('license_issued_country')
    existing_booking.rent_location = request.form.get('rent_location')
    existing_booking.rent_description = request.form.get('rent_description')
    existing_booking.add_driver_name = request.form.get('add_driver_name')
    existing_booking.add_driver_phone = request.form.get('add_driver_phone')
    existing_booking.add_driver_email = request.form.get('add_driver_email')
    existing_booking.add_driver_license = request.form.get('add_driver_license')
    existing_booking.add_driver_license_country = request.form.get('add_driver_license_country')
    add_lic_val = request.form.get('add_driver_license_valid')
    existing_booking.add_driver_license_valid = datetime.strptime(add_lic_val, '%Y-%m-%d').date() if add_lic_val else None
    existing_booking.add_driver_license_issued_at = request.form.get('add_driver_license_issued_at')
    existing_booking.add_driver_passport = request.form.get('add_driver_passport')
    existing_booking.add_driver_nationality = request.form.get('add_driver_nationality')
    existing_booking.tour_package_name = request.form.get('tour_package_name')
    existing_booking.tour_hotel_name = request.form.get('tour_hotel_name')
    existing_booking.tour_itinerary = request.form.get('tour_itinerary')
    existing_booking.tour_inclusions = request.form.get('tour_inclusions')
    existing_booking.tour_exclusions = request.form.get('tour_exclusions')

    existing_booking.net_cost = float(request.form.get('buying_price') or 0.0)
    existing_booking.selling_price = float(request.form.get('selling_price') or 0.0)
    existing_booking.advance_paid = float(request.form.get('advance_paid') or 0.0)
    existing_booking.remarks = request.form.get('remarks')
    existing_booking.fuel_stage_out = request.form.get('fuel_stage_out')
    existing_booking.km_out = int(request.form.get('km_out') or 0)
    existing_booking.km_allowed = int(request.form.get('km_allowed') or 0)
    existing_booking.extra_km_price = float(request.form.get('extra_km_price') or 0.0)
    existing_booking.elec_reading_in = float(request.form.get('elec_reading_in') or 0.0)
    existing_booking.water_reading_in = float(request.form.get('water_reading_in') or 0.0)
    existing_booking.elec_unit_price = float(request.form.get('elec_unit_price') or 0.0)
    existing_booking.water_unit_price = float(request.form.get('water_unit_price') or 0.0)

    split_total = _save_tour_supplier_costs(existing_booking, comp_id)
    if split_total is not None:
        existing_booking.net_cost = split_total
        existing_booking.supplier_id = None

    for p in ['photo1', 'photo2', 'photo3', 'photo4', 'driver_id_card']:
        # Explicit removal: user clicked the remove (×) button on the image.
        if request.form.get(f'{p}_remove') == '1':
            setattr(existing_booking, p, None)
            try:
                sdir = get_company_photo_path(existing_booking.company_id)
                for fn in os.listdir(sdir):
                    if fn.startswith(f'booking_{existing_booking.id}_{p}'):
                        os.remove(os.path.join(sdir, fn))
            except Exception as e:
                app.logger.warning(f"Could not delete removed photo {p}: {e}")
            continue
        file = request.files.get(f'{p}_file')
        photo_data = file.read() if file and file.filename else decode_image_field(request.form.get(f'{p}_data'))
        if photo_data:
            filename = save_photo_to_disk(existing_booking.id, p, photo_data, existing_booking.company_id)
            if filename: setattr(existing_booking, p, filename.encode('utf-8'))

    try:
        db.session.commit()
    except Exception as _e:
        db.session.rollback()
        _msg = (str(_e).replace(chr(39), ' ').replace(chr(34), ' ').replace(chr(10), ' ').replace(chr(13), ' '))[:300]
        return f"<script>alert('Could not update the booking:\n\n{_msg}\n\nYour entries are still here \u2014 press OK to go back, fix the issue, and save again.'); window.history.back();</script>"
    return redirect(url_for('booking'))

@app.route('/finalize_rental/<int:id>', methods=['POST'])
@require_permission('Booking_Edit')
def finalize_rental(id):
    booking = Booking.query.filter_by(id=id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first()
    if not booking or booking.company_id != session.get('company_id'): return redirect(url_for('booking'))
    
    booking.selling_price = float(get_last_nonempty_form_value('final_rent_total') or booking.selling_price or 0.0)
    # Bug fix: persist the actual check-out (return) date/time entered at finalize back
    # onto the booking, so the main booking record reflects the finalized return date —
    # not just the originally expected return.
    _date_in = parse_datetime_local(request.form.get('date_in'))
    if _date_in:
        booking.dropoff_datetime = _date_in
        booking.return_date = _date_in.date()
    # Req #12/#13: persist the advance actually collected at check-out. Without this the
    # advance was dropped, so the amount due (selling_price - advance) could never be
    # reconciled on the booking list or when an invoice was raised for the rental.
    booking.advance_paid = float(request.form.get('advance_paid') or booking.advance_paid or 0.0)
    booking.km_in = int(request.form.get('km_in') or booking.km_out)
    booking.excess_km = int(request.form.get('extra_km') or 0)
    booking.fuel_stage_in = request.form.get('fuel_stage_in')
    booking.fuel_charge = float(request.form.get('fuel_charge') or 0.0)
    booking.damage_charge = float(request.form.get('damage_charge') or 0.0)
    booking.security_deposit = float(request.form.get('security_deposit') or 0.0)
    booking.vat_added = request.form.get('vat_added')
    booking.booking_finalized = True

    if booking.vehicle_id:
        veh = Vehicle.query.get(booking.vehicle_id)
        if veh: veh.status = "Available"

    if booking.security_deposit > 0:
        sd_entry = Ledger(
            company_id=session.get('company_id'), transaction_type="Income (Security Deposit)", amount=booking.security_deposit,
            customer_id=booking.customer_id, vehicle_id=booking.vehicle_id, payment_mode="Cash", remarks=f"Security Deposit for Car Rental Booking #{booking.id}"
        )
        db.session.add(sd_entry)

    due_amount = float(request.form.get('due_amount') or 0.0)
    if due_amount > 0:
        settlement_entry = Ledger(
            company_id=session.get('company_id'), transaction_type="Income (Rental Settlement)", amount=due_amount,
            customer_id=booking.customer_id, vehicle_id=booking.vehicle_id, payment_mode="Cash", remarks=f"Final Settlement for Car Rental Booking #{booking.id}"
        )
        db.session.add(settlement_entry)
    db.session.commit()
    return redirect(url_for('booking'))

@app.route('/finalize_property/<int:id>', methods=['POST'])
@require_permission('Booking_Edit')
def finalize_property(id):
    comp_id = session.get('company_id')
    b = Booking.query.filter_by(id=id, company_id=comp_id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first_or_404()
    base_rent = float(request.form.get('num_period', 1)) * float(request.form.get('rent_rate', 0))
    elec_total = max(0, float(request.form.get('elec_out', 0)) - float(request.form.get('elec_in', 0))) * float(request.form.get('elec_unit', 0))
    water_total = max(0, float(request.form.get('water_out', 0)) - float(request.form.get('water_in', 0))) * float(request.form.get('water_unit', 0))
    grand_total = base_rent + elec_total + water_total + float(request.form.get('damage_charge', 0)) + float(request.form.get('overstay_charge', 0)) + float(request.form.get('maintenance_charge', 0))
    b.selling_price = grand_total
    b.booking_finalized = True
    if b.room_id:
        room = Room.query.get(b.room_id)
        if room: room.status = 'Available'
    db.session.commit()
    flash('Property rental finalized successfully!', 'success')
    return redirect(url_for('booking'))

@app.route('/delete_booking/<int:id>', methods=['POST'])
@require_permission('Booking_Delete')
def delete_booking(id):
    comp_id = session.get('company_id')
    booking = Booking.query.filter_by(id=id, company_id=comp_id).first()
    if not booking: return redirect(url_for('booking'))
    # Reverse balance effects before deleting
    if booking.booking_finalized:
        if booking.customer_id:
            cust = Customer.query.filter_by(id=booking.customer_id, company_id=comp_id).first()
            if cust and booking.selling_price:
                cust.balance -= float(booking.selling_price or 0)
        if booking.supplier_id:
            sup = Supplier.query.filter_by(id=booking.supplier_id, company_id=comp_id).first()
            if sup and booking.net_cost:
                sup.balance -= float(booking.net_cost or 0)
    delete_booking_photos(comp_id, id)
    if booking.service_type == 'visa': purge_visa_history(id, comp_id)
    db.session.delete(booking)
    db.session.commit()
    return redirect(url_for('booking'))

# ==========================================
# Photo Storage Management (Super Admin controlled)
# ==========================================
# The storage location is no longer editable by the tenant. These helpers are called
# by the Super Admin panel (saas_middleware.py) with an explicit company_id.

def get_storage_info(company_id):
    """Resolved photo path for a company plus a small usage summary."""
    path = get_company_photo_path(company_id)
    total_bytes = 0
    file_count = 0
    if os.path.isdir(path):
        for f in os.listdir(path):
            try:
                fp = os.path.join(path, f)
                if os.path.isfile(fp):
                    total_bytes += os.path.getsize(fp)
                    file_count += 1
            except Exception:
                pass
    return {
        'path': path,
        'size_mb': round(total_bytes / (1024 * 1024), 2),
        'file_count': file_count,
        'os': ('windows' if os.name == 'nt' else 'linux'),
        'sep': os.sep,
        'default_path': _default_company_photo_path(company_id),
        'example_path': ('D:' + chr(92) + 'company_photos' if os.name == 'nt' else '/home/youruser/company_photos'),
    }

def apply_storage_path_change(company_id, new_path, migrate_existing=True):
    """Validate, create and persist a new photo storage path for one company,
    optionally moving that company's existing files across. Returns a dict that
    can be jsonify'd directly."""
    new_path = (new_path or '').strip()
    if not new_path:
        return {"success": False, "message": "Path is required"}
    # Reject a path written for the wrong operating system (common when a setting is
    # carried from a local Windows machine to Linux hosting).
    if not is_os_compatible_path(new_path):
        if os.name == 'nt':
            msg = ("That looks like a Linux path. This server runs on Windows, so use a Windows path, "
                   "e.g. D:\\company_photos, or just a folder name like company_photos (created next to the app).")
        else:
            msg = ("That looks like a Windows path. This server runs on Linux, so use a Linux path, "
                   "e.g. /home/youruser/company_photos, or just a folder name like company_photos (created next to the app).")
        return {"success": False, "message": msg}
    # Resolve relative paths against the app folder, not the unpredictable working
    # directory the hosting process may run from.
    if not os.path.isabs(new_path):
        new_path = os.path.join(app.root_path, new_path)
    new_path = os.path.normpath(new_path)
    try:
        os.makedirs(new_path, exist_ok=True)
    except Exception as e:
        return {"success": False, "message": f"Cannot create directory: {e}"}

    test_file = os.path.join(new_path, '.write_test')
    try:
        with open(test_file, 'w') as f:
            f.write('ok')
        os.remove(test_file)
    except Exception as e:
        return {"success": False, "message": f"Cannot write to directory: {e}"}

    config = CompanyStorageConfig.query.filter_by(company_id=company_id).first()
    old_path = config.photo_storage_path if config else None
    default_uploads_path = os.path.join(app.root_path, 'static', 'uploads')

    source_paths = []
    if old_path and old_path != new_path and os.path.isdir(old_path):
        source_paths.append(old_path)
    if default_uploads_path != new_path and os.path.isdir(default_uploads_path) and default_uploads_path not in source_paths:
        source_paths.append(default_uploads_path)

    moved_count = 0
    if migrate_existing and source_paths:
        # Only this tenant's own files are moved — a shared source folder must not leak
        # another company's photos into this company's directory.
        company_filenames = set()
        for b in Booking.query.filter_by(company_id=company_id).all():
            for field_name in ['photo1', 'photo2', 'photo3', 'photo4', 'driver_id_card']:
                val = getattr(b, field_name, None)
                if val:
                    try:
                        fname = val.decode('utf-8') if isinstance(val, bytes) else val
                        if isinstance(fname, str) and len(fname) < 300:
                            company_filenames.add(fname)
                    except (UnicodeDecodeError, AttributeError):
                        pass
        for source_path in source_paths:
            try:
                listing = os.listdir(source_path)
            except Exception:
                continue
            for filename in listing:
                src = os.path.join(source_path, filename)
                dst = os.path.join(new_path, filename)
                if not os.path.isfile(src) or filename not in company_filenames or os.path.exists(dst):
                    continue
                try:
                    shutil.move(src, dst)
                    moved_count += 1
                except Exception as e:
                    app.logger.warning(f"Could not move {filename}: {e}")

    if config:
        config.photo_storage_path = new_path
    else:
        config = CompanyStorageConfig(company_id=company_id, photo_storage_path=new_path)
        db.session.add(config)
    db.session.commit()
    return {"success": True, "path": new_path,
            "message": f"Path updated. {moved_count} file(s) moved to new location." if moved_count else "Path updated."}

# ==========================================
# CAR STATUS API
# ==========================================
@app.route('/api/car_status')
@require_permission('Booking_View')
def api_car_status():
    comp_id = session.get('company_id')
    if not comp_id: return jsonify({'success': False, 'message': 'Not logged in'})
    all_vehicles = Vehicle.query.filter_by(company_id=comp_id).all()
    statuses = []
    _sup_names = {s.id: s.name for s in Supplier.query.filter_by(company_id=comp_id).all()}
    for v in all_vehicles:
        status, details = "Available", "Ready"
        active_booking = Booking.query.filter(Booking.vehicle_id == v.id, Booking.company_id == comp_id, Booking.pickup_datetime <= datetime.now(timezone.utc), Booking.dropoff_datetime >= datetime.now(timezone.utc)).first()
        if active_booking:
            status, details = "Occupied", f"Rented till {active_booking.dropoff_datetime.strftime('%Y-%m-%d %H:%M')}"
        else:
            future_booking = Booking.query.filter(Booking.vehicle_id == v.id, Booking.company_id == comp_id, Booking.pickup_datetime > datetime.now(timezone.utc)).order_by(Booking.pickup_datetime.asc()).first()
            if future_booking:
                status, details = "Future", f"Reserved from {future_booking.pickup_datetime.strftime('%Y-%m-%d %H:%M')}"
        # External supplier rent-agreement expiry note (shown on the availability view).
        agreement_expired = False
        if v.is_external:
            has_any = VehicleRentAgreement.query.filter_by(company_id=comp_id, vehicle_id=v.id).count() > 0
            cur = _applicable_agreement(v.id, comp_id) if has_any else None
            if has_any and not cur:
                agreement_expired = True
                sup_nm = _sup_names.get(v.supplier_id, 'supplier')
                details = (details + " · " if details and details != "Ready" else "") + f"<span style='color:#dc2626; font-weight:bold;'>⚠ Rent agreement from {sup_nm} is expired</span>"
        statuses.append({"id": v.id, "name": v.name, "reg": v.registration_number, "status": status, "details": details, "agreement_expired": agreement_expired})
    return jsonify({'success': True, 'statuses': statuses})

# ==========================================
# INVOICING ENGINE
# ==========================================
@app.route('/invoice')
@require_permission('Invoice_View')
def invoice(): 
    comp_id = session.get('company_id')
    current_company = Company.query.get(comp_id)
    booking_ids = request.args.get('booking_ids')
    suppliers=Supplier.query.filter_by(company_id=comp_id).all()
    
    prefill_bookings = []
    prefill = None
    if booking_ids:
        id_list = [int(x) for x in booking_ids.split(',') if x.isdigit()]
        prefill_bookings = Booking.query.filter(Booking.id.in_(id_list), Booking.company_id == comp_id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).all()
        if prefill_bookings: prefill = prefill_bookings[0]
    elif request.args.get('booking_id'):
        b = Booking.query.filter_by(id=request.args.get('booking_id'), company_id=comp_id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first()
        if b: 
            prefill_bookings.append(b)
            prefill = b
    # Req #13: advance already collected on the prefilled booking(s), so the invoice's
    # Advance/Credit field starts populated and the Balance Due is the true amount owed.
    prefill_advance = sum(float(getattr(b, 'advance_paid', 0.0) or 0.0) for b in prefill_bookings)
    current_date = datetime.now().strftime('%Y-%m-%d')
    customers = Customer.query.filter_by(company_id=comp_id).all()
    for c in customers: c.advance_amount = abs(c.balance) if c.balance < 0 else 0.0
    
    search = request.args.get('search', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    
    query = Invoice.query.filter_by(company_id=comp_id)
    if start_date: query = query.filter(func.date(Invoice.date_created) >= start_date)
    if end_date: query = query.filter(func.date(Invoice.date_created) <= end_date)
    if search:
        search_term = f"%{search}%"
        query = query.outerjoin(Customer, Invoice.customer_id == Customer.id).filter(
            or_(Invoice.id.cast(db.String).ilike(search_term), Invoice.remarks.ilike(search_term), Customer.name.ilike(search_term))
        )
        
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    
    if per_page == -1:
        invoices = query.order_by(Invoice.id.desc()).all()
        pagination = None
    else:
        pagination = query.order_by(Invoice.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
        invoices = pagination.items

    # Filter-aware totals: sum across EVERY invoice matching the current search + date range
    # (not just the current page), so the footer reflects the active filter.
    _tot = query.with_entities(
        func.coalesce(func.sum(Invoice.total_amount), 0.0),
        func.coalesce(func.sum(Invoice.total_paid), 0.0),
        func.coalesce(func.sum(Invoice.balance_due), 0.0)
    ).first()
    inv_tot_amount = float(_tot[0] or 0.0)
    inv_tot_paid = float(_tot[1] or 0.0)
    inv_tot_balance = float(_tot[2] or 0.0)

    all_customers = Customer.query.filter_by(company_id=comp_id).all()
    customers_dict = {c.id: c.name for c in all_customers}

    return render_template('invoice.html', current_date=current_date, customers=all_customers, customers_dict=customers_dict, invoices=invoices, pagination=pagination, search=search, start_date=start_date, end_date=end_date, per_page=per_page, prefill_bookings=prefill_bookings, prefill=prefill, prefill_advance=prefill_advance, current_company=current_company, suppliers=suppliers, bank_accounts=BankAccount.query.filter_by(company_id=comp_id).all(), inv_tot_amount=inv_tot_amount, inv_tot_paid=inv_tot_paid, inv_tot_balance=inv_tot_balance)

import csv
from io import StringIO
from flask import Response, stream_with_context

@app.route('/export_invoices')
@require_permission('Invoice_View')
def export_invoices():
    comp_id = session.get('company_id')
    search = request.args.get('search', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    query = Invoice.query.filter_by(company_id=comp_id)
    if start_date: query = query.filter(func.date(Invoice.date_created) >= start_date)
    if end_date: query = query.filter(func.date(Invoice.date_created) <= end_date)
    if search:
        search_term = f"%{search}%"
        query = query.outerjoin(Customer, Invoice.customer_id == Customer.id).filter(
            or_(Invoice.id.cast(db.String).ilike(search_term), Invoice.remarks.ilike(search_term), Customer.name.ilike(search_term))
        )
    invoices = query.options(joinedload(Invoice.items)).order_by(Invoice.id.desc()).all()
    customers = {c.id: c.name for c in Customer.query.filter_by(company_id=comp_id).all()}

    def generate():
        data = StringIO()
        writer = csv.writer(data)
        writer.writerow(['Invoice #', 'Date', 'Customer Name', 'Cost', 'Selling Price', 'Profit', 'Balance Due'])
        yield data.getvalue()
        data.seek(0); data.truncate(0)
        
        for inv in invoices:
            cust_name = customers.get(inv.customer_id, "Unknown")
            cost = sum([(i.net_cost or 0.0) for i in inv.items]) if inv.items else (sum([(b.net_cost or 0.0) for b in inv.bookings]) if hasattr(inv, 'bookings') and inv.bookings else 0.0)
            selling_price = inv.total_amount
            profit = selling_price - cost
            writer.writerow([f"INV-{inv.id}", inv.date_created.strftime('%Y-%m-%d') if inv.date_created else '', cust_name, "{:.2f}".format(cost), "{:.2f}".format(selling_price), "{:.2f}".format(profit), "{:.2f}".format(inv.balance_due)])
            yield data.getvalue()
            data.seek(0); data.truncate(0)
    return Response(stream_with_context(generate()), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=Invoice_History.csv'})

# ==========================================
# REPORT / PRINT / EXPORT HELPERS & ROUTES
# ==========================================
def _booking_report_data(comp_id, search='', service='', start_date='', end_date=''):
    """Build filtered booking rows for the printable report / Excel export.
    Scoped to company_id. Filters mirror the on-screen booking toolbar
    (search text, service type, and travel-date range). Returns (rows, totals)."""
    photo_cols = [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]
    bookings = Booking.query.filter_by(company_id=comp_id).options(*[defer(c) for c in photo_cols]).order_by(Booking.id.desc()).all()
    cust_names = {c.id: c.name for c in Customer.query.filter_by(company_id=comp_id).all()}

    search_l = (search or '').strip().lower()
    rows, tot_cost, tot_sell = [], 0.0, 0.0
    for b in bookings:
        # Effective booking date: travel_date, else pickup date, else created date
        if b.travel_date:
            d = b.travel_date
        elif b.pickup_datetime:
            d = b.pickup_datetime.date()
        elif b.date_created:
            d = b.date_created.date()
        else:
            d = None
        date_str = d.strftime('%Y-%m-%d') if d else ''

        if service and b.service_type != service:
            continue
        if start_date and (not date_str or date_str < start_date):
            continue
        if end_date and (not date_str or date_str > end_date):
            continue

        # passenger_details is stored newline-separated -> join names with commas
        names_list = [n.strip() for n in (b.passenger_details or '').replace('\r', '\n').split('\n') if n.strip()]
        travelers = ', '.join(names_list)

        cust_name = cust_names.get(b.customer_id, 'Unknown')
        pnr = b.pnr_ref or ''
        frm = b.departure_place or ''
        dest = b.arrival_place or ''
        cost = float(b.net_cost or 0.0)
        sell = float(b.selling_price or 0.0)

        if search_l:
            hay = ' '.join([cust_name, travelers, pnr, frm, dest, b.service_type or '']).lower()
            if search_l not in hay:
                continue

        tot_cost += cost
        tot_sell += sell
        rows.append({'date': date_str, 'customer': cust_name, 'travelers': travelers,
                     'pnr': pnr, 'frm': frm, 'dest': dest, 'cost': cost, 'sell': sell})

    return rows, {'cost': tot_cost, 'sell': tot_sell, 'count': len(rows)}


_BOOKING_REPORT_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Booking Report</title>
<style>
  *{box-sizing:border-box;} body{font-family:'Segoe UI',Arial,sans-serif;margin:24px;color:#1e293b;}
  .rpt-head{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:3px solid #1e3a8a;padding-bottom:12px;margin-bottom:16px;}
  .rpt-title{font-size:22px;font-weight:800;color:#1e3a8a;margin:0;}
  .rpt-sub{font-size:12px;color:#64748b;margin-top:4px;}
  .rpt-company{text-align:right;font-size:15px;font-weight:800;color:#0f172a;}
  table{width:100%;border-collapse:collapse;font-size:12px;}
  th{background:#1e3a8a;color:#fff;text-align:left;padding:8px 10px;font-size:11px;text-transform:uppercase;letter-spacing:.3px;}
  td{padding:7px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;}
  tbody tr:nth-child(even){background:#f8fafc;}
  .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}
  tfoot td{font-weight:800;background:#eef2ff;border-top:2px solid #1e3a8a;font-size:13px;}
  .toolbar{margin-bottom:14px;}
  .btn{background:#1e3a8a;color:#fff;border:none;padding:9px 18px;border-radius:6px;font-weight:700;cursor:pointer;font-size:13px;}
  .empty{text-align:center;padding:30px;color:#94a3b8;}
  @media print{.toolbar{display:none;} body{margin:0;}}
</style></head><body>
  <div class="toolbar"><button class="btn" onclick="window.print()">&#128424; Print</button></div>
  <div class="rpt-head">
    <div>
      <h1 class="rpt-title">Booking Report</h1>
      <div class="rpt-sub">Generated {{ generated }}{% if start_date or end_date %} &bull; Period: {{ start_date or '&hellip;' }} to {{ end_date or '&hellip;' }}{% endif %}{% if service %} &bull; Service: {{ service }}{% endif %}{% if search %} &bull; Search: "{{ search }}"{% endif %} &bull; {{ totals.count }} booking(s)</div>
    </div>
    <div class="rpt-company">{{ company.company_name if company else 'Company' }}</div>
  </div>
  <table>
    <thead><tr>
      <th>Date</th><th>Traveler Names</th><th>PNR</th><th>From</th><th>To</th>
      <th class="num">Cost ({{ sym }})</th><th class="num">Sell Amount ({{ sym }})</th>
    </tr></thead>
    <tbody>
      {% for r in rows %}
      <tr>
        <td>{{ r.date }}</td><td>{{ r.travelers }}</td><td>{{ r.pnr }}</td>
        <td>{{ r.frm }}</td><td>{{ r.dest }}</td>
        <td class="num">{{ "{:,.2f}".format(r.cost) }}</td>
        <td class="num">{{ "{:,.2f}".format(r.sell) }}</td>
      </tr>
      {% else %}
      <tr><td colspan="7" class="empty">No bookings match the selected filters.</td></tr>
      {% endfor %}
    </tbody>
    <tfoot><tr>
      <td colspan="5" style="text-align:right;">TOTAL</td>
      <td class="num">{{ "{:,.2f}".format(totals.cost) }}</td>
      <td class="num">{{ "{:,.2f}".format(totals.sell) }}</td>
    </tr></tfoot>
  </table>
  <script>window.addEventListener('load',function(){setTimeout(function(){window.print();},350);});</script>
</body></html>"""


@app.route('/booking_report_print')
@require_permission('Booking_View')
def booking_report_print():
    comp_id = session.get('company_id')
    company = Company.query.get(comp_id)
    sym = (company.currency_symbol if company and company.currency_symbol else '$')
    search = request.args.get('search', '')
    service = request.args.get('service', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    rows, totals = _booking_report_data(comp_id, search, service, start_date, end_date)
    return render_template_string(_BOOKING_REPORT_TEMPLATE, rows=rows, totals=totals, sym=sym,
        company=company, search=search, service=service, start_date=start_date, end_date=end_date,
        generated=datetime.now().strftime('%Y-%m-%d %H:%M'))


@app.route('/booking_report_export')
@require_permission('Booking_View')
def booking_report_export():
    comp_id = session.get('company_id')
    search = request.args.get('search', '')
    service = request.args.get('service', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    rows, totals = _booking_report_data(comp_id, search, service, start_date, end_date)

    data = StringIO()
    writer = csv.writer(data)
    writer.writerow(['Date', 'Traveler Names', 'PNR', 'From', 'To', 'Cost', 'Sell Amount'])
    for r in rows:
        writer.writerow([r['date'], r['travelers'], r['pnr'], r['frm'], r['dest'],
                         "{:.2f}".format(r['cost']), "{:.2f}".format(r['sell'])])
    writer.writerow([])
    writer.writerow(['', '', '', '', 'TOTAL', "{:.2f}".format(totals['cost']), "{:.2f}".format(totals['sell'])])
    return Response(data.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=Booking_Report.csv'})


_INVOICE_REPORT_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Invoice Records</title>
<style>
  *{box-sizing:border-box;} body{font-family:'Segoe UI',Arial,sans-serif;margin:24px;color:#1e293b;}
  .rpt-head{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:3px solid #1e3a8a;padding-bottom:12px;margin-bottom:16px;}
  .rpt-title{font-size:22px;font-weight:800;color:#1e3a8a;margin:0;}
  .rpt-sub{font-size:12px;color:#64748b;margin-top:4px;}
  .rpt-company{text-align:right;font-size:15px;font-weight:800;color:#0f172a;}
  table{width:100%;border-collapse:collapse;font-size:12px;}
  th{background:#1e3a8a;color:#fff;text-align:left;padding:8px 10px;font-size:11px;text-transform:uppercase;letter-spacing:.3px;}
  td{padding:7px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;}
  tbody tr:nth-child(even){background:#f8fafc;}
  .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}
  .paid{color:#059669;} .bal{color:#dc2626;}
  tfoot td{font-weight:800;background:#eef2ff;border-top:2px solid #1e3a8a;font-size:13px;}
  .toolbar{margin-bottom:14px;}
  .btn{background:#1e3a8a;color:#fff;border:none;padding:9px 18px;border-radius:6px;font-weight:700;cursor:pointer;font-size:13px;}
  .empty{text-align:center;padding:30px;color:#94a3b8;}
  @media print{.toolbar{display:none;} body{margin:0;}}
</style></head><body>
  <div class="toolbar"><button class="btn" onclick="window.print()">&#128424; Print</button></div>
  <div class="rpt-head">
    <div>
      <h1 class="rpt-title">Invoice Records &amp; History</h1>
      <div class="rpt-sub">Generated {{ generated }}{% if start_date or end_date %} &bull; Period: {{ start_date or '&hellip;' }} to {{ end_date or '&hellip;' }}{% endif %}{% if search %} &bull; Search: "{{ search }}"{% endif %} &bull; {{ totals.count }} invoice(s)</div>
    </div>
    <div class="rpt-company">{{ company.company_name if company else 'Company' }}</div>
  </div>
  <table>
    <thead><tr>
      <th>Invoice #</th><th>Date</th><th>Customer Name</th><th>Service / Item</th><th>Description / Details</th>
      <th class="num">Selling Amount ({{ sym }})</th><th class="num">Paid Amount ({{ sym }})</th><th class="num">Balance Due ({{ sym }})</th>
    </tr></thead>
    <tbody>
      {% for r in rows %}
      <tr>
        <td style="font-weight:700;color:#1e3a8a;">INV-{{ r.inv }}</td>
        <td>{{ r.date }}</td><td style="font-weight:600;">{{ r.customer }}</td>
        <td>{{ r.service }}</td><td>{{ r.description }}</td>
        <td class="num">{{ "{:,.2f}".format(r.sell) }}</td>
        <td class="num paid">{{ "{:,.2f}".format(r.paid) }}</td>
        <td class="num bal">{{ "{:,.2f}".format(r.balance) }}</td>
      </tr>
      {% else %}
      <tr><td colspan="8" class="empty">No invoices match the selected filters.</td></tr>
      {% endfor %}
    </tbody>
    <tfoot><tr>
      <td colspan="5" style="text-align:right;">TOTAL</td>
      <td class="num">{{ "{:,.2f}".format(totals.sell) }}</td>
      <td class="num paid">{{ "{:,.2f}".format(totals.paid) }}</td>
      <td class="num bal">{{ "{:,.2f}".format(totals.balance) }}</td>
    </tr></tfoot>
  </table>
  <script>window.addEventListener('load',function(){setTimeout(function(){window.print();},350);});</script>
</body></html>"""


@app.route('/print_invoices')
@require_permission('Invoice_View')
def print_invoices():
    comp_id = session.get('company_id')
    company = Company.query.get(comp_id)
    sym = (company.currency_symbol if company and company.currency_symbol else '$')
    search = request.args.get('search', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    query = Invoice.query.filter_by(company_id=comp_id)
    if start_date: query = query.filter(func.date(Invoice.date_created) >= start_date)
    if end_date: query = query.filter(func.date(Invoice.date_created) <= end_date)
    if search:
        st = f"%{search}%"
        query = query.outerjoin(Customer, Invoice.customer_id == Customer.id).filter(
            or_(Invoice.id.cast(db.String).ilike(st), Invoice.remarks.ilike(st), Customer.name.ilike(st)))
    invoices = query.options(joinedload(Invoice.items)).order_by(Invoice.id.desc()).all()
    cust_names = {c.id: c.name for c in Customer.query.filter_by(company_id=comp_id).all()}

    rows, tot_sell, tot_paid, tot_bal = [], 0.0, 0.0, 0.0
    for inv in invoices:
        items = inv.items or []
        services = ', '.join(sorted({(it.service_type or '').strip() for it in items if (it.service_type or '').strip()})) or '-'
        descs = []
        for it in items:
            d = (it.passenger_details or '').replace('\r', ' ').replace('\n', ' ').strip()
            pnr = (it.pnr_ref or '').strip()
            piece = (pnr + ' | ' + d).strip(' |') if (pnr or d) else ''
            if piece:
                descs.append(piece)
        description = '; '.join(descs) or (inv.remarks or '-')
        sell = float(inv.total_amount or 0.0)
        paid = float(inv.total_paid or 0.0)
        bal = float(inv.balance_due or 0.0)
        rows.append({'inv': inv.id,
                     'date': inv.date_created.strftime('%Y-%m-%d') if inv.date_created else '',
                     'customer': cust_names.get(inv.customer_id, 'Unknown'),
                     'service': services, 'description': description,
                     'sell': sell, 'paid': paid, 'balance': bal})
        tot_sell += sell; tot_paid += paid; tot_bal += bal

    totals = {'sell': tot_sell, 'paid': tot_paid, 'balance': tot_bal, 'count': len(rows)}
    return render_template_string(_INVOICE_REPORT_TEMPLATE, rows=rows, totals=totals, sym=sym,
        company=company, search=search, start_date=start_date, end_date=end_date,
        generated=datetime.now().strftime('%Y-%m-%d %H:%M'))

# ==========================================
# READ-ONLY RECORD VIEW ENDPOINTS
# Return a saved record as label/value rows (+ optional line-item table) for a
# view-only popup. No editing happens here; these are strictly for inspection.
# All are scoped by company_id so tenants can never read each other's records.
# ==========================================
def _view_money(sym, v):
    return "{} {:,.2f}".format(sym, float(v or 0.0))


@app.route('/api/booking_view/<int:id>')
@require_permission('Booking_View')
def api_booking_view(id):
    comp_id = session.get('company_id')
    photo_cols = [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]
    b = Booking.query.filter_by(id=id, company_id=comp_id).options(*[defer(c) for c in photo_cols]).first()
    if not b:
        return jsonify({'success': False, 'message': 'Booking not found.'}), 404

    company = Company.query.get(comp_id)
    sym = company.currency_symbol if company and company.currency_symbol else '$'
    cust = Customer.query.filter_by(id=b.customer_id, company_id=comp_id).first() if b.customer_id else None
    sup = Supplier.query.filter_by(id=b.supplier_id, company_id=comp_id).first() if b.supplier_id else None
    veh = Vehicle.query.filter_by(id=b.vehicle_id, company_id=comp_id).first() if b.vehicle_id else None
    room = Room.query.filter_by(id=b.room_id, company_id=comp_id).first() if b.room_id else None
    bank = BankAccount.query.filter_by(id=b.bank_account_id, company_id=comp_id).first() if b.bank_account_id else None

    rows = []

    def add(label, val):
        if val is None:
            return
        s = val if isinstance(val, str) else str(val)
        if s.strip() == '' or s.strip().lower() == 'none':
            return
        rows.append([label, s])

    def dt(v, fmt='%Y-%m-%d %H:%M'):
        return v.strftime(fmt) if v else ''

    svc = (b.service_type or '').replace('other_', '').replace('_', ' ').title() or 'Booking'
    add('Service Type', svc)
    add('Status', b.pnr_status)
    add('Customer', cust.name if cust else None)
    add('Supplier', sup.name if sup else None)
    add('Paid via Bank/Card', (bank.bank_name + (' (' + (bank.card_ref or bank.account_number or '') + ')' if (bank.card_ref or bank.account_number) else '')) if bank else None)

    add('PNR / Reference', b.pnr_ref)
    add('Tour Name', b.tour_package_name)
    add('Hotel', b.tour_hotel_name)
    add('Carrier', b.carrier_code)
    add('Flight Number', b.flight_number)
    add('Travel Date', dt(b.travel_date, '%Y-%m-%d'))
    add('Return Date', dt(b.return_date, '%Y-%m-%d'))
    add('From', b.departure_place)
    add('To', b.arrival_place)
    add('Departure Time', dt(b.departure_time))
    add('Arrival Time', dt(b.arrival_time))
    names = ', '.join([n.strip() for n in (b.passenger_details or '').replace('\r', '\n').split('\n') if n.strip()])
    add('Traveler Names', names)
    pax_bits = []
    if b.pax_adults:
        pax_bits.append('{} adult(s)'.format(b.pax_adults))
    if b.pax_child:
        pax_bits.append('{} child'.format(b.pax_child))
    if b.pax_infants:
        pax_bits.append('{} infant(s)'.format(b.pax_infants))
    add('Passengers', ', '.join(pax_bits))
    add('Sector Details', b.sector_details)

    if veh:
        add('Vehicle', '{} ({})'.format(veh.name, veh.registration_number or ''))
    if room:
        bld = Building.query.filter_by(id=room.building_id, company_id=comp_id).first()
        add('Room', '{} - Room {}'.format(bld.name if bld else 'Unknown', room.room_number))
    add('Pickup', dt(b.pickup_datetime))
    add('Drop-off', dt(b.dropoff_datetime))
    add('Rent Type', b.rent_type)
    if b.km_out or b.km_in or b.km_allowed:
        add('KM (Out / In / Allowed)', '{} / {} / {}'.format(b.km_out or 0, b.km_in or 0, b.km_allowed or 0))
    add('Driver Name', b.driver_name)
    add('Driver Phone', b.driver_phone)
    add('Driving License', b.driving_license)
    add('Passport ID', b.passport_id)
    add('Nationality', b.nationality)
    add('Rent Location', b.rent_location)
    add('Rent Description', b.rent_description)

    rows.append(['Net Cost', _view_money(sym, b.net_cost)])
    rows.append(['Selling Price', _view_money(sym, b.selling_price)])
    if b.rent_amount:
        add('Rent Amount', _view_money(sym, b.rent_amount))
    if b.security_deposit:
        add('Security Deposit', _view_money(sym, b.security_deposit))
    if b.advance_paid:
        add('Advance Paid', _view_money(sym, b.advance_paid))
    if b.damage_charge:
        add('Damage Charge', _view_money(sym, b.damage_charge))
    if b.overstay_charge:
        add('Overstay Charge', _view_money(sym, b.overstay_charge))
    if b.fuel_charge:
        add('Fuel Charge', _view_money(sym, b.fuel_charge))
    if b.vat_amount:
        add('VAT Amount', _view_money(sym, b.vat_amount))
    sc_rows = BookingSupplierCost.query.filter_by(booking_id=b.id).all()
    if sc_rows:
        sup_names = {s.id: s.name for s in Supplier.query.filter_by(company_id=comp_id).all()}
        for sc in sc_rows:
            label = (sc.component or 'Supplier Cost')
            add(f'Cost — {label} ({sup_names.get(sc.supplier_id, "Supplier")})', _view_money(sym, sc.amount))
    add('Remarks', b.remarks)
    add('Linked Invoice', ('INV-' + str(b.invoice_id)) if b.invoice_id else None)
    add('Created', dt(b.date_created))

    return jsonify({'success': True, 'title': 'Booking #{} — {}'.format(b.id, svc), 'rows': rows})


@app.route('/api/invoice_view/<int:id>')
@require_permission('Invoice_View')
def api_invoice_view(id):
    comp_id = session.get('company_id')
    inv = Invoice.query.filter_by(id=id, company_id=comp_id).options(joinedload(Invoice.items)).first()
    if not inv:
        return jsonify({'success': False, 'message': 'Invoice not found.'}), 404

    company = Company.query.get(comp_id)
    sym = company.currency_symbol if company and company.currency_symbol else '$'
    cust = Customer.query.filter_by(id=inv.customer_id, company_id=comp_id).first() if inv.customer_id else None

    items = {'columns': ['Service / Item', 'Description / Details', 'Cost ({})'.format(sym), 'Selling ({})'.format(sym)], 'data': []}
    gross = 0.0
    for it in (inv.items or []):
        desc = (((it.pnr_ref + ' | ') if it.pnr_ref else '') + (it.passenger_details or '')).strip(' |') or '-'
        items['data'].append([it.service_type or '-', desc,
                              "{:,.2f}".format(it.net_cost or 0.0), "{:,.2f}".format(it.selling_price or 0.0)])
        gross += float(it.selling_price or 0.0)

    tax_amt = float(inv.tax_amount or 0.0)
    total = float(inv.total_amount or 0.0)
    discount = round(gross + tax_amt - total, 2)

    rows = []
    rows.append(['Customer', cust.name if cust else 'Unknown'])
    rows.append(['Invoice Date', inv.date_created.strftime('%Y-%m-%d') if inv.date_created else ''])
    rows.append(['Line Items Total', _view_money(sym, gross)])
    rows.append(['Tax / VAT', '{}%  =  {}'.format(inv.tax_rate or 0, _view_money(sym, tax_amt))])
    if abs(discount) > 0.001:
        rows.append(['Discount', _view_money(sym, discount)])
    rows.append(['Invoice Total', _view_money(sym, total)])
    rows.append(['Amount Paid', _view_money(sym, inv.total_paid)])
    rows.append(['Balance Due', _view_money(sym, inv.balance_due)])
    if inv.remarks:
        rows.append(['Remarks', inv.remarks])

    return jsonify({'success': True, 'title': 'Invoice INV-{}'.format(inv.id), 'rows': rows, 'items': items})


@app.route('/api/receipt_view/<int:id>')
@require_permission('Ledger_View')
def api_receipt_view(id):
    comp_id = session.get('company_id')
    r = Ledger.query.filter_by(id=id, company_id=comp_id).first()
    if not r:
        return jsonify({'success': False, 'message': 'Transaction not found.'}), 404

    company = Company.query.get(comp_id)
    sym = company.currency_symbol if company and company.currency_symbol else '$'
    cust = Customer.query.filter_by(id=r.customer_id, company_id=comp_id).first() if r.customer_id else None
    sup = Supplier.query.filter_by(id=r.supplier_id, company_id=comp_id).first() if r.supplier_id else None
    bank = BankAccount.query.filter_by(id=r.bank_account_id, company_id=comp_id).first() if r.bank_account_id else None
    cap = CapitalAccount.query.filter_by(id=r.capital_account_id, company_id=comp_id).first() if r.capital_account_id else None

    rows = []

    def add(label, val):
        if val is None:
            return
        s = val if isinstance(val, str) else str(val)
        if s.strip() == '' or s.strip().lower() == 'none':
            return
        rows.append([label, s])

    docnum = r.receipt_number if r.receipt_number else ('RCP-' + str(r.id))
    amt = float(r.amount or 0.0)
    direction = 'Money IN' if amt > 0 else ('Money OUT' if amt < 0 else '—')

    add('Receipt / Voucher No', docnum)
    add('Transaction ID', '#{}'.format(r.id))
    add('Date', r.date_created.strftime('%Y-%m-%d %H:%M') if r.date_created else '')
    add('Transaction Type', r.transaction_type)
    add('Category', r.category)
    if cust:
        add('Party', '👤 ' + cust.name)
    elif sup:
        add('Party', '🏢 ' + sup.name)
    else:
        add('Party', 'General')
    rows.append(['Amount ({})'.format(direction), _view_money(sym, abs(amt))])
    add('Payment Mode', r.payment_mode)
    add('Cheque Number', r.cheque_number)
    add('Cheque Date', r.cheque_date.strftime('%Y-%m-%d') if r.cheque_date else '')
    if bank:
        add('Bank / Card', bank.bank_name + (' (' + (bank.card_ref or bank.account_number or '') + ')' if (bank.card_ref or bank.account_number) else ''))
    if cap:
        add('Capital / Loan Account', cap.name + (' — ' + cap.phone if cap.phone else ''))
    add('Remarks', r.remarks)

    # Invoice/bill allocations recorded against this transaction (view-only)
    allocs = _alloc_list(r)
    items = None
    if allocs:
        data = []
        for a in allocs:
            try:
                iid = int(a.get('invoice_id'))
            except (TypeError, ValueError):
                continue
            amt_a = float(a.get('amount') or 0.0)
            data.append(['INV-{}'.format(iid), "{:,.2f}".format(amt_a)])
        if data:
            items = {'columns': ['Applied To', 'Amount ({})'.format(sym)], 'data': data}

    result = {'success': True, 'title': 'Transaction {}'.format(docnum), 'rows': rows}
    if items:
        result['items'] = items
    return jsonify(result)

@app.route('/edit_invoice/<int:id>', methods=['GET'])
@require_permission('Invoice_Edit')
def edit_invoice(id):
    comp_id = session.get('company_id')
    invoice = Invoice.query.filter_by(id=id, company_id=comp_id).first()
    if not invoice: return redirect(url_for('invoice'))
    prefill_bookings = Booking.query.filter_by(invoice_id=id, company_id=comp_id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).all()
    current_date = invoice.date_created.strftime('%Y-%m-%d') if invoice.date_created else datetime.now().strftime('%Y-%m-%d')
    customers = Customer.query.filter_by(company_id=comp_id).all()
    for c in customers: c.advance_amount = abs(c.balance) if c.balance < 0 else 0.0
    customers_dict = {c.id: c.name for c in customers}

    # The Invoice Records table footer prints these three totals. If they are not passed,
    # Jinja formats an Undefined value and the edit page 500s (which made Edit "not work").
    # Compute company-wide totals so the footer renders correctly on the edit screen too.
    current_company = Company.query.get(comp_id)
    _tot = Invoice.query.filter_by(company_id=comp_id).with_entities(
        func.coalesce(func.sum(Invoice.total_amount), 0.0),
        func.coalesce(func.sum(Invoice.total_paid), 0.0),
        func.coalesce(func.sum(Invoice.balance_due), 0.0)
    ).first()
    inv_tot_amount = float(_tot[0] or 0.0)
    inv_tot_paid = float(_tot[1] or 0.0)
    inv_tot_balance = float(_tot[2] or 0.0)

    return render_template('invoice.html', current_date=current_date, customers=customers, customers_dict=customers_dict, suppliers=Supplier.query.filter_by(company_id=comp_id).all(), bank_accounts=BankAccount.query.filter_by(company_id=comp_id).all(), invoices=Invoice.query.filter_by(company_id=comp_id).order_by(Invoice.id.desc()).limit(10).all(), pagination=None, per_page=10, search='', start_date='', end_date='', prefill_bookings=prefill_bookings, prefill=invoice, edit_mode=True, current_company=current_company, inv_tot_amount=inv_tot_amount, inv_tot_paid=inv_tot_paid, inv_tot_balance=inv_tot_balance, discount=(sum([(b.selling_price or 0) for b in prefill_bookings]) - (invoice.total_amount or 0) + (invoice.tax_amount or 0)) if prefill_bookings else 0)

def _purge_invoice_auto_ledger(inv_id, comp_id):
    """Remove auto-posted bank ledger rows for an invoice (card-sourced line costs and
    the upfront advance) and reverse their effect on bank balances. Called before re-saving
    an edited invoice and when deleting one, so bank balances never drift on re-edit."""
    rows = Ledger.query.filter(Ledger.company_id == comp_id, Ledger.receipt_number.like(f"INVAUTO-{inv_id}-%")).all()
    for r in rows:
        if r.bank_account_id:
            bank = BankAccount.query.filter_by(id=r.bank_account_id, company_id=comp_id).first()
            if bank:
                bank.balance = (bank.balance or 0.0) - (r.amount or 0.0)
        db.session.delete(r)

@app.route('/save_invoice', methods=['POST'])
@require_permission('Invoice_Edit')
def save_invoice(): 
    comp_id = session.get('company_id')
    subt = float(request.form.get('subtotal') or 0.0)
    disc = float(request.form.get('discount') or 0.0)
    adv = float(request.form.get('advance') or 0.0)
    cust_id = request.form.get('customer_id')
    invoice_date_str = request.form.get('invoice_date')
    payment_remarks = request.form.get('payment_remarks', '').strip()
    
    tax_rate = float(request.form.get('tax_rate') or request.form.get('vat_rate_hidden') or 0.0)
    tax_amount = float(request.form.get('tax_amount') or request.form.get('vat_amount_hidden') or 0.0)
    edit_id = request.form.get('edit_invoice_id')
    
    net_total = subt - disc + tax_amount
    balance = net_total - adv
    
    if edit_id:
        new_inv = Invoice.query.filter_by(id=edit_id, company_id=comp_id).first()
        if new_inv:
            cust = Customer.query.get(new_inv.customer_id)
            if cust: cust.balance -= new_inv.balance_due
            
            new_inv.customer_id = cust_id
            new_inv.total_amount = net_total
            new_inv.total_paid = adv
            new_inv.balance_due = balance
            new_inv.tax_rate = tax_rate
            new_inv.tax_amount = tax_amount
            new_inv.remarks = payment_remarks if payment_remarks else None
            if invoice_date_str: new_inv.date_created = datetime.strptime(invoice_date_str, '%Y-%m-%d')
                
            old_bookings = Booking.query.filter_by(invoice_id=new_inv.id, company_id=comp_id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).all()
            posted_b_ids = [int(b) for b in request.form.getlist('booking_id[]') if b]
            
            for ob in old_bookings:
                if ob.supplier_id and ob.net_cost > 0:
                    osup = Supplier.query.get(ob.supplier_id)
                    if osup: osup.balance -= ob.net_cost
                if ob.id not in posted_b_ids:
                    if not ob.pnr_ref and not ob.travel_date and not ob.rent_type:
                        if ob.service_type == 'visa': purge_visa_history(ob.id, comp_id)
                        db.session.delete(ob)
                    else:
                        if ob.service_type == 'visa':
                            log_visa_history(ob.id, comp_id,
                                             f'Removed from invoice INV-{new_inv.id}. The visa can be invoiced again.')
                        ob.invoice_id = None; ob.booking_finalized = False
    else:
        new_inv = Invoice(company_id=comp_id, customer_id=cust_id, total_amount=net_total, total_paid=adv, balance_due=balance, tax_rate=tax_rate, tax_amount=tax_amount, remarks=payment_remarks if payment_remarks else None)
        if invoice_date_str: new_inv.date_created = datetime.strptime(invoice_date_str, '%Y-%m-%d')
        db.session.add(new_inv)
        
    db.session.flush() 
    # On edit, wipe the previous auto-posted bank rows (and reverse their balances)
    # before re-posting fresh ones below, so repeated edits never drift the bank balance.
    if edit_id and new_inv:
        _purge_invoice_auto_ledger(new_inv.id, comp_id)
    
    cust = Customer.query.get(cust_id)
    if cust: cust.balance += balance
        
    service_types = request.form.getlist('service_type[]')
    descriptions = request.form.getlist('description[]')
    supplier_ids = request.form.getlist('supplier_id[]')
    bank_account_ids = request.form.getlist('bank_account_id[]')
    source_types = request.form.getlist('source_type[]')
    buying_prices = request.form.getlist('buying_price[]')
    selling_prices = request.form.getlist('selling_price[]')
    booking_ids = request.form.getlist('booking_id[]')
    # Visa lines typed straight onto the invoice have no id until the flush below, so
    # they are collected here and logged once they do.
    _new_visa_lines = []

    for i in range(len(service_types)):
        srv = service_types[i]
        desc = descriptions[i]
        raw_sup_id = supplier_ids[i] if i < len(supplier_ids) and supplier_ids[i] else None
        raw_bank_id = bank_account_ids[i] if i < len(bank_account_ids) and bank_account_ids[i] else None
        # The per-line source toggle decides whether the buying price was owed to a supplier
        # or paid on a company bank/card. A hidden dropdown still posts its stale value, so we
        # trust source_type[] and fall back to whichever id is actually present.
        src = source_types[i] if i < len(source_types) and source_types[i] else ('bank' if raw_bank_id else ('supplier' if raw_sup_id else 'none'))
        if src == 'bank':
            sup_id, bank_id_line = None, raw_bank_id
        elif src == 'supplier':
            sup_id, bank_id_line = raw_sup_id, None
        else:
            sup_id, bank_id_line = None, None
        bp = float(buying_prices[i]) if i < len(buying_prices) and buying_prices[i] else 0.0
        sp = float(selling_prices[i]) if i < len(selling_prices) and selling_prices[i] else 0.0
        b_id = booking_ids[i] if i < len(booking_ids) and booking_ids[i] else None
        
        if b_id:
            existing_booking = Booking.query.filter_by(id=b_id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first()
            if existing_booking:
                # Only a change of invoice is worth a timeline line — re-saving the same
                # invoice (a price tweak, a supplier swap) must not repeat the entry.
                _prev_inv_id = existing_booking.invoice_id
                if srv == 'visa' and _prev_inv_id != new_inv.id:
                    log_visa_history(existing_booking.id, comp_id,
                                     f'Invoiced. Invoice No: INV-{new_inv.id}. Amount: {sp:.2f}.')
                existing_booking.invoice_id = new_inv.id
                existing_booking.booking_finalized = True
                existing_booking.service_type = srv
                existing_booking.passenger_details = desc  
                existing_booking.supplier_id = sup_id
                existing_booking.bank_account_id = bank_id_line
                existing_booking.net_cost = bp
                existing_booking.selling_price = sp
                if sup_id and bp > 0:
                    sup = Supplier.query.get(sup_id)
                    if sup: sup.balance += bp
        else:
            new_booking = Booking(company_id=comp_id, customer_id=cust_id, supplier_id=sup_id, bank_account_id=bank_id_line, service_type=srv, passenger_details=desc, net_cost=bp, selling_price=sp, invoice_id=new_inv.id, booking_finalized=True, created_by=session.get('user_id'))
            db.session.add(new_booking)
            if srv == 'visa':
                _new_visa_lines.append((new_booking, sp))
            if sup_id and bp > 0:
                sup = Supplier.query.get(sup_id)
                if sup: sup.balance += bp 
                # FIXED: Removed the erroneous db.session.add(Ledger(amount=-bp)) here so it remains a pending payable!

        # If this line's buying price was put on a company bank/card, record it as money OUT
        # of that card so it appears on the bank statement. Tagged "Direct Invoice Cost" so
        # analytics skips it for the P&L expense total (the cost is already counted as COGS
        # via the booking's net_cost), which avoids double counting.
        if src == 'bank' and bank_id_line and bp > 0:
            card_bank = BankAccount.query.filter_by(id=bank_id_line, company_id=comp_id).first()
            if card_bank:
                db.session.add(Ledger(company_id=comp_id, transaction_type=f"Direct Invoice Cost (Card) - INV-{new_inv.id}", amount=-bp, bank_account_id=card_bank.id, payment_mode='Card', category='card_purchase', receipt_number=f"INVAUTO-{new_inv.id}-CARD-{i}", remarks=desc))
                card_bank.balance = (card_bank.balance or 0.0) - bp

    # A visa created on the invoice itself starts its timeline here: it never passed
    # through the Visa Plus form, so this is its first entry.
    if _new_visa_lines:
        db.session.flush()
        for _nb, _sp in _new_visa_lines:
            log_visa_history(_nb.id, comp_id,
                             f'Visa line created on invoice INV-{new_inv.id}. Amount: {_sp:.2f}.')

    db.session.commit()
    
    # FIXED: Send customer ID and balance to the receipt page for auto-filling
    if 'save_and_receipt' in request.form: 
        return redirect(url_for('receipts', invoice_id=new_inv.id, cust_id=cust_id, amount=balance, remarks=payment_remarks))
    
    return redirect(url_for('invoice'))

@app.route('/delete_invoice/<int:id>', methods=['POST'])
@require_permission('Invoice_Delete')
def delete_invoice(id):
    inv = Invoice.query.get(id)
    if inv and inv.company_id == session.get('company_id'):
        _purge_invoice_auto_ledger(inv.id, inv.company_id)
        # Deleting the invoice releases its visas for invoicing again, so each one says so.
        for _vb in Booking.query.with_entities(Booking.id).filter_by(
                company_id=inv.company_id, invoice_id=inv.id, service_type='visa').all():
            log_visa_history(_vb[0], inv.company_id,
                             f'Invoice INV-{inv.id} deleted. The visa can be invoiced again.')
        cust = Customer.query.get(inv.customer_id)
        if cust: cust.balance -= inv.balance_due
        db.session.delete(inv)
        db.session.commit()
    return redirect(url_for('invoice'))

# ==========================================
# RECEIPTS & CASH BOOK
# ==========================================
@app.route('/receipt')
@app.route('/receipts')
@app.route('/receipt_payment')
@require_permission('Ledger_View')
def receipts(): 
    comp_id = session.get('company_id')
    prefill_inv = Invoice.query.filter_by(id=request.args.get('invoice_id'), company_id=comp_id).first() if request.args.get('invoice_id') else None

    # Refund tracking rows: attach display-only names + completion flag the template expects
    _cust_names = {c.id: c.name for c in Customer.query.filter_by(company_id=comp_id).all()}
    _sup_names = {s.id: s.name for s in Supplier.query.filter_by(company_id=comp_id).all()}
    refund_tracks = RefundTrack.query.filter_by(company_id=comp_id).order_by(RefundTrack.id.desc()).all()
    for rf in refund_tracks:
        rf.supplier_name = _sup_names.get(rf.supplier_id, '—')
        rf.customer_name = _cust_names.get(rf.customer_id, '—')
        rf.is_completed = (rf.status or '').lower() in ('completed', 'done', 'closed')

    return render_template('receipt_payment.html', 
        prefill=prefill_inv, default_remarks=request.args.get('remarks', ''), form_error=request.args.get('err', ''),
        customers=Customer.query.filter_by(company_id=comp_id).all(), 
        suppliers=Supplier.query.filter_by(company_id=comp_id).all(), 
        staff_members=User.query.filter_by(company_id=comp_id).all(),
        rooms=Room.query.filter_by(company_id=comp_id).all(), 
        buildings=Building.query.filter_by(company_id=comp_id).all(), 
        vehicles=Vehicle.query.filter_by(company_id=comp_id).all(),
        receipts=Ledger.query.filter_by(company_id=comp_id).order_by(Ledger.id.desc()).limit(100).all(),
        refund_tracks=refund_tracks,
        capital_accounts=CapitalAccount.query.filter_by(company_id=comp_id).order_by(CapitalAccount.name.asc()).all(),
        bank_accounts=BankAccount.query.filter_by(company_id=comp_id).all())

@app.route('/api/booking_calendar', methods=['GET'])
@require_permission('Booking_View')
def booking_calendar():
    comp_id = session.get('company_id')
    now = datetime.now()
    # Active (not terminated/finalized) car & room bookings that are current or upcoming.
    # A "terminated" booking is one that has been finalized, so those are excluded; we also
    # skip bookings whose period is entirely in the past.
    q = Booking.query.filter(
        Booking.company_id == comp_id,
        Booking.booking_finalized == False,
        Booking.service_type.in_(['rentacar', 'room_rent']),
        Booking.pickup_datetime.isnot(None),
        Booking.dropoff_datetime.isnot(None),
        Booking.dropoff_datetime >= now,
    ).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]])
    bookings = q.order_by(Booking.pickup_datetime.asc()).all()

    cust_map = {c.id: c.name for c in Customer.query.filter_by(company_id=comp_id).all()}
    veh_map = {v.id: v for v in Vehicle.query.filter_by(company_id=comp_id).all()}
    room_map = {r.id: r for r in Room.query.filter_by(company_id=comp_id).all()}
    bldg_map = {b.id: b.name for b in Building.query.filter_by(company_id=comp_id).all()}

    events = []
    for b in bookings:
        start, end = b.pickup_datetime, b.dropoff_datetime
        cust_name = cust_map.get(b.customer_id, 'N/A')
        ev = {
            'id': b.id,
            'start': start.isoformat(),
            'end': end.isoformat(),
            'from': start.strftime('%Y-%m-%d %H:%M'),
            'upto': end.strftime('%Y-%m-%d %H:%M'),
            'customer_name': cust_name,
            'status': 'Current' if start <= now <= end else ('Upcoming' if start > now else 'Active'),
        }
        if b.service_type == 'rentacar':
            v = veh_map.get(b.vehicle_id)
            ev.update({
                'type': 'car',
                'title': (v.name if v else 'Vehicle'),
                'car_name': (v.name if v else 'N/A'),
                'car_number': ((v.registration_number or 'N/A') if v else 'N/A'),
                'driver_name': (b.driver_name or cust_name or 'N/A'),
                'pickup_location': (b.pickup_location or 'N/A'),
                'drop_location': (b.drop_location or 'N/A'),
            })
        else:
            r = room_map.get(b.room_id)
            ev.update({
                'type': 'room',
                'title': ('Room ' + r.room_number) if r else 'Room',
                'room_number': (r.room_number if r else 'N/A'),
                'building': (bldg_map.get(r.building_id, 'N/A') if r else 'N/A'),
            })
        events.append(ev)
    return jsonify(events)

@app.route('/api/asset_availability', methods=['GET'])
@require_permission('Booking_View')
def asset_availability():
    """Full fleet + room inventory with their current/upcoming bookings.

    Unlike /api/booking_calendar (which only returns booked periods), this lists
    EVERY registered vehicle and room so the availability view can show assets that
    are free as well as booked. Availability is derived from actual booking
    date-ranges, not the static Vehicle.status / Room.status flags.
    """
    comp_id = session.get('company_id')
    now = datetime.now()

    vehicles = Vehicle.query.filter_by(company_id=comp_id).all()
    rooms = Room.query.filter_by(company_id=comp_id).all()
    bldg_map = {b.id: b.name for b in Building.query.filter_by(company_id=comp_id).all()}
    cust_map = {c.id: c.name for c in Customer.query.filter_by(company_id=comp_id).all()}

    # Active (not finalized) car/room bookings that are ongoing or upcoming.
    # Open-ended rentals (no drop-off yet) are kept so a car that is currently out
    # still reads as booked.
    photo_cols = [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]
    bq = Booking.query.filter(
        Booking.company_id == comp_id,
        Booking.booking_finalized == False,
        Booking.service_type.in_(['rentacar', 'room_rent']),
        Booking.pickup_datetime.isnot(None),
        or_(Booking.dropoff_datetime.is_(None), Booking.dropoff_datetime >= now),
    ).options(*[defer(col) for col in photo_cols]).order_by(Booking.pickup_datetime.asc()).all()

    veh_bookings, room_bookings = {}, {}
    for b in bq:
        rec = {
            'from': b.pickup_datetime.isoformat(),
            'upto': b.dropoff_datetime.isoformat() if b.dropoff_datetime else None,
            'from_disp': b.pickup_datetime.strftime('%Y-%m-%d %H:%M'),
            'upto_disp': b.dropoff_datetime.strftime('%Y-%m-%d %H:%M') if b.dropoff_datetime else 'Open (no return set)',
            'customer_name': cust_map.get(b.customer_id, 'N/A'),
            # Req #14 Car List: booking identity + renter/driver contact + advance
            'booking_id': b.id,
            'ref': b.pnr_ref or f'BK-{b.id}',
            'driver_name': b.driver_name or '',
            'driver_phone': b.driver_phone or '',
            'advance_paid': float(b.advance_paid or 0.0),
        }
        if b.service_type == 'rentacar' and b.vehicle_id:
            veh_bookings.setdefault(b.vehicle_id, []).append(rec)
        elif b.service_type == 'room_rent' and b.room_id:
            room_bookings.setdefault(b.room_id, []).append(rec)

    sup_names = {s.id: s.name for s in Supplier.query.filter_by(company_id=comp_id).all()}
    def _veh_agreement_info(v):
        if not v.is_external:
            return {'is_external': False, 'agreement_expired': False, 'supplier_name': '', 'agreement_end': ''}
        has_any = VehicleRentAgreement.query.filter_by(company_id=comp_id, vehicle_id=v.id).count() > 0
        cur = _applicable_agreement(v.id, comp_id) if has_any else None
        return {
            'is_external': True,
            'agreement_expired': bool(has_any and not cur),
            'supplier_name': sup_names.get(v.supplier_id, '') if v.supplier_id else '',
            'agreement_end': cur.end_date.isoformat() if (cur and cur.end_date) else '',
        }

    cars = [dict({
        'id': v.id,
        'name': v.name or 'Vehicle',
        'reg': v.registration_number or '',
        'reg_expiry': v.reg_expiry.isoformat() if v.reg_expiry else '',
        'ins_expiry': v.ins_expiry.isoformat() if v.ins_expiry else '',
        'bookings': veh_bookings.get(v.id, []),
    }, **_veh_agreement_info(v)) for v in vehicles]
    cars.sort(key=lambda c: (c['name'] or '').lower())

    rooms_out = [{
        'id': r.id,
        'room_number': r.room_number or '',
        'room_type': r.room_type or '',
        'building': bldg_map.get(r.building_id, ''),
        'bookings': room_bookings.get(r.id, []),
    } for r in rooms]
    rooms_out.sort(key=lambda x: ((x['building'] or '').lower(), x['room_number'] or ''))

    return jsonify({'cars': cars, 'rooms': rooms_out, 'now': now.isoformat()})

@app.route('/api/extend_booking', methods=['POST'])
@require_permission('Booking_Edit')
def extend_booking():
    """Req #14: extend a rental's return date/time straight from the Car List.
    Updates the booking's dropoff so it flows through to the booking document."""
    comp_id = session.get('company_id')
    data = request.json or {}
    b = Booking.query.filter_by(id=data.get('booking_id'), company_id=comp_id).options(
        *[defer(c) for c in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first()
    if not b:
        return jsonify({'success': False, 'message': 'Booking not found'}), 404
    dt = parse_datetime_local(data.get('dropoff_datetime'))
    if not dt:
        return jsonify({'success': False, 'message': 'Invalid date/time'}), 400
    if b.pickup_datetime and dt <= b.pickup_datetime:
        return jsonify({'success': False, 'message': 'Return must be after the pickup date/time.'}), 400
    b.dropoff_datetime = dt
    db.session.commit()
    return jsonify({'success': True, 'dropoff_disp': dt.strftime('%Y-%m-%d %H:%M'),
                    'dropoff_iso': dt.isoformat()})

@app.route('/api/update_vehicle_docs', methods=['POST'])
@require_permission('Accounts_Add')
def update_vehicle_docs():
    """Req #14: update a vehicle's registration / insurance expiry from the Car List;
    persists onto the vehicle master."""
    comp_id = session.get('company_id')
    data = request.json or {}
    v = Vehicle.query.filter_by(id=data.get('vehicle_id'), company_id=comp_id).first()
    if not v:
        return jsonify({'success': False, 'message': 'Vehicle not found'}), 404
    reg = data.get('reg_expiry')
    ins = data.get('ins_expiry')
    if reg is not None:
        v.reg_expiry = _parse_date(reg) if str(reg).strip() else None
    if ins is not None:
        v.ins_expiry = _parse_date(ins) if str(ins).strip() else None
    db.session.commit()
    return jsonify({'success': True,
                    'reg_expiry': v.reg_expiry.isoformat() if v.reg_expiry else '',
                    'ins_expiry': v.ins_expiry.isoformat() if v.ins_expiry else ''})

@app.route('/api/save_print_settings', methods=['POST'])
@require_permission('Accounts_View')
def save_print_settings():
    comp_id = session.get('company_id')
    company = Company.query.get(comp_id)
    if not company:
        return jsonify({"success": False, "message": "Company not found"}), 404
    data = request.get_json(silent=True) or {}
    settings = data.get('settings', data)  # accept either {settings:{...}} or the object directly
    try:
        company.print_settings = json.dumps(settings)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    
@app.route('/api/get_supplier_payables/<int:supplier_id>', methods=['GET'])
@require_permission('Ledger_View')
def get_supplier_payables(supplier_id):
    comp_id = session.get('company_id')
    supplier = Supplier.query.filter_by(id=supplier_id, company_id=comp_id).first()
    if not supplier: return jsonify([])

    # When editing a payment, add back what THIS receipt already paid so the bill is editable again
    prior = {}
    edit_id = request.args.get('edit_id')
    if edit_id:
        led = Ledger.query.filter_by(id=edit_id, company_id=comp_id).first()
        for a in _alloc_list(led):
            try:
                bid = int(a.get('bill_id')); amt = float(a.get('amount') or 0)
                if bid: prior[bid] = prior.get(bid, 0.0) + amt
            except (TypeError, ValueError):
                continue

    bills = Booking.query.filter(Booking.company_id == comp_id, Booking.supplier_id == supplier_id, Booking.net_cost > 0).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).order_by(Booking.date_created.asc()).all()
    result = []
    for b in bills:
        paid = b.supplier_paid or 0.0
        already = prior.get(b.id, 0.0)
        outstanding = round((b.net_cost or 0.0) - paid + already, 2)  # add back this receipt's prior payment
        if outstanding <= 0.001 and already <= 0: continue
        ref = b.pnr_ref if (b.pnr_ref and b.pnr_ref != 'N/A') else f'BK-{b.id}'
        desc = b.service_type or 'Booking'
        if b.passenger_details: desc = f"{desc} — {b.passenger_details[:40]}"
        result.append({'booking_id': b.id, 'date': b.date_created.strftime('%Y-%m-%d') if b.date_created else '', 'ref': ref, 'description': desc, 'invoice_id': b.invoice_id, 'total': float(b.net_cost or 0.0), 'paid': float(paid), 'outstanding': outstanding, 'applied': round(already, 2)})
    return jsonify(result)

@app.route('/api/get_customer_invoices/<int:customer_id>', methods=['GET'])
@require_permission('Ledger_View')
def get_customer_invoices(customer_id):
    comp_id = session.get('company_id')

    # When editing a receipt, add back what THIS receipt already applied so the invoice is editable again
    prior = {}
    edit_id = request.args.get('edit_id')
    if edit_id:
        led = Ledger.query.filter_by(id=edit_id, company_id=comp_id).first()
        for a in _alloc_list(led):
            try:
                iid = int(a.get('invoice_id')); amt = float(a.get('amount') or 0)
                if iid: prior[iid] = prior.get(iid, 0.0) + amt
            except (TypeError, ValueError):
                continue

    invoices = Invoice.query.filter(Invoice.customer_id == customer_id, Invoice.company_id == comp_id).order_by(Invoice.date_created.asc()).all()
    result = []
    for inv in invoices:
        already = prior.get(inv.id, 0.0)
        avail = round((inv.balance_due or 0.0) + already, 2)  # add back this receipt's prior allocation
        if avail <= 0.001 and already <= 0: continue
        result.append({'id': inv.id, 'date': inv.date_created.strftime('%Y-%m-%d') if inv.date_created else '', 'total': float(inv.total_amount or 0), 'balance_due': avail, 'applied': round(already, 2), 'paid': float(inv.total_paid or 0)})
    return jsonify(result)

@app.route('/save_supplier_payment', methods=['POST'])
@require_permission('Ledger_Add')
def save_supplier_payment():
    comp_id = session.get('company_id')
    supplier = Supplier.query.filter_by(id=request.form.get('supplier_id'), company_id=comp_id).first()
    if not supplier: return jsonify({'success': False, 'message': 'Supplier not found'}), 404

    amount = float(request.form.get('amount') or 0.0)
    if amount <= 0: return jsonify({'success': False, 'message': 'Amount must be greater than zero'}), 400

    payment_mode = request.form.get('payment_mode')
    remarks = request.form.get('remarks', '')
    bank_account_id = request.form.get('bank_account_id') if payment_mode == 'Bank Transfer' else None
    cheque_number = request.form.get('cheque_number')
    cheque_date_str = request.form.get('cheque_date')
    cheque_date = datetime.strptime(cheque_date_str, '%Y-%m-%d').date() if cheque_date_str else None

    db.session.add(Ledger(company_id=comp_id, transaction_type=f"Supplier Payment - {payment_mode} - {remarks}", amount=-amount, supplier_id=supplier.id, payment_mode=payment_mode, bank_account_id=bank_account_id, cheque_number=cheque_number, cheque_date=cheque_date, remarks=remarks))
    supplier.balance = (supplier.balance or 0.0) - amount
    if bank_account_id:
        bank = BankAccount.query.filter_by(id=bank_account_id, company_id=comp_id).first()
        if bank: bank.balance = (bank.balance or 0.0) - amount

    bill_ids = request.form.getlist('bill_id[]')
    apply_amts = request.form.getlist('apply_amount[]')
    for i in range(len(bill_ids)):
        try:
            bid = int(bill_ids[i])
            apply_amt = float(apply_amts[i]) if i < len(apply_amts) and apply_amts[i] else 0.0
        except (ValueError, TypeError): continue
        if apply_amt <= 0: continue
        bill = Booking.query.filter_by(id=bid, company_id=comp_id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first()
        if bill:
            outstanding = (bill.net_cost or 0.0) - (bill.supplier_paid or 0.0)
            bill.supplier_paid = (bill.supplier_paid or 0.0) + min(apply_amt, outstanding)
    db.session.commit()
    return jsonify({'success': True})
    
# Human-readable label for each receipt category (used in transaction_type & statements)
RECEIPT_CATEGORY_LABELS = {
    'receipt': 'Customer Payment',
    'advance_receipt': 'Customer Advance',
    'refund_supplier': 'Refund from Supplier',
    'room_rent_col': 'Room Rent Collection',
    'utility_water': 'Water Bill Collection',
    'utility_elec': 'Electricity Bill Collection',
    'misc_income': 'Miscellaneous Income',
    'adjustment_invoice': 'Adjustment Invoice',
    'payment': 'Supplier Payment',
    'advance_payment': 'Supplier Advance',
    'refund_customer': 'Refund to Customer',
    'vehicle_emi': 'Vehicle EMI Payment',
    'car_maintenance': 'Vehicle Maintenance Expense',
    'registration_expense': 'Registration Expense',
    'misc_expense': 'Miscellaneous Expense',
    'room_water_payment': 'Room Water Payment',
    'room_elec_payment': 'Room Electricity Payment',
    'room_wifi_payment': 'Room WiFi Payment',
    'room_tax_payment': 'Room Tax Payment',
    'salary_payment': 'Salary Payment',
    'payout': 'Payout (Non-Expense)',
    'money_receipt': 'Money Receipt (Non-Income)',
    'cash_to_bank': 'Contra (Cash to Bank)',
    'bank_to_cash': 'Contra (Bank to Cash)',
}
# Categories that represent money going OUT (stored as a negative amount)
# Payment-mode labels that carry extra behaviour. "Credit Card" was renamed to
# "POS Payment"; "Capital/Loan Account" additionally requires an account holder.
CAPITAL_PAYMENT_MODE = 'Capital/Loan Account'
POS_PAYMENT_MODE = 'POS Payment'
# A Money Receipt is cash physically handed in — not income, and not something that
# arrives by bank, card or cheque. Only these modes make sense for it.
MONEY_RECEIPT_MODES = ('Cash', 'Petty Cash', CAPITAL_PAYMENT_MODE)

# Grouped heads on the Receipt & Payment form. The form posts the head as txn_category and
# the real category as txn_sub_category; save_receipt swaps the real one in before any
# accounting logic runs, so every rule below stays keyed on the leaf categories.
TXN_GROUP_MEMBERS = {
    'occupancy_billing': {'room_rent_col', 'utility_water', 'utility_elec', 'misc_income'},
    'fleet_expenses':    {'car_maintenance', 'vehicle_emi', 'registration_expense'},
    'property_opex':     {'room_water_payment', 'room_elec_payment', 'room_wifi_payment', 'room_tax_payment'},
}

RECEIPT_OUT_CATEGORIES = {
    'payment', 'advance_payment', 'refund_customer', 'vehicle_emi', 'car_maintenance',
    'registration_expense', 'misc_expense', 'room_water_payment', 'room_elec_payment',
    'room_wifi_payment', 'room_tax_payment', 'salary_payment',
    # Payout: money physically leaves the business through the chosen payment mode but is
    # NOT an expense — it never reaches the P&L expense total (see NON_OPERATING_OUT).
    'payout',
}

def _safe_float(v, default=0.0):
    """Parse a money field without ever raising — an empty or malformed amount
    would otherwise 500 the whole transaction save."""
    try:
        s = (v or '').strip() if isinstance(v, str) else v
        if s in (None, ''):
            return default
        return float(s)
    except (TypeError, ValueError):
        return default

def _safe_id(v):
    """Coerce a form-supplied id to int or None. Strings like '', 'None', 'null'
    become None instead of being passed to a query where MySQL (unlike SQLite)
    rejects them against an integer column and raises a 500."""
    try:
        s = (v or '').strip() if isinstance(v, str) else v
        if s in (None, '', 'None', 'null', 'undefined', '0'):
            return None
        return int(s)
    except (TypeError, ValueError):
        return None

@app.route('/save_receipt', methods=['POST'])
@require_permission('Ledger_Add')
def save_receipt():
    comp_id = session.get('company_id')
    txn_category = request.form.get('txn_category')
    # A grouped head (Occupancy Billing / Fleet Expenses / Property Operating Expenses)
    # resolves to the sub-type picked beneath it — that is the category actually recorded.
    if txn_category in TXN_GROUP_MEMBERS:
        _sub = request.form.get('txn_sub_category')
        if _sub not in TXN_GROUP_MEMBERS[txn_category]:
            _msg = "Please choose the type under the selected heading."
            return jsonify({"success": False, "message": _msg}) if request.is_json else redirect(url_for('receipts', err=_msg))
        txn_category = _sub
    amount = _safe_float(request.form.get('amount'))
    payment_mode = request.form.get('payment_mode')

    # User-entered transaction date. Ledger.date_created is already used by the
    # receipts screen as the transaction date/time, so keep the existing schema
    # and apply the entered date to newly-created Ledger rows.
    #
    # _entered_dt is built here, before any branch, and passed to every Ledger
    # created below as date_created. It must NOT be applied afterwards by sweeping
    # db.session.new: any query run after the row is added (the bank balance lookup
    # for Bank Transfer, the Capital/Loan holder lookup, the vehicle lookup for an
    # EMI payment) autoflushes the pending Ledger, which drops it out of
    # db.session.new — and the entered date was silently lost with it.
    _transaction_date_raw = (request.form.get('transaction_date') or '').strip()
    try:
        transaction_date = (datetime.strptime(_transaction_date_raw, '%d/%m/%Y').date() if _transaction_date_raw else datetime.utcnow().date())
    except ValueError:
        transaction_date = datetime.utcnow().date()
    _entered_dt = datetime.combine(transaction_date, datetime.now().time())
    remarks = request.form.get('remarks', '')
    bank_account_id = _safe_id(request.form.get('bank_account_id')) if payment_mode == 'Bank Transfer' else None

    # Capital / Loan account holder — only when the money moved through that account.
    capital_account_id = _safe_id(request.form.get('capital_account_id')) if payment_mode == CAPITAL_PAYMENT_MODE else None
    if payment_mode == CAPITAL_PAYMENT_MODE and not capital_account_id:
        return jsonify({"success": False, "message": "Select the Capital / Loan account holder"}) if request.is_json else redirect(url_for('receipts'))

    # A Money Receipt can only arrive as Cash, Petty Cash or into a Capital / Loan account.
    if txn_category == 'money_receipt' and payment_mode not in MONEY_RECEIPT_MODES:
        _msg = f"'{payment_mode}' is not an appropriate payment mode for a Money Receipt. Use Cash, Petty Cash or Capital / Loan Account."
        return jsonify({"success": False, "message": _msg}) if request.is_json else redirect(url_for('receipts', err=_msg))

    # Cheque details (only relevant when paying by cheque)
    cheque_number = request.form.get('cheque_number') if payment_mode == 'Cheque' else None
    _cd = request.form.get('cheque_date')
    cheque_date = datetime.strptime(_cd, '%Y-%m-%d').date() if (payment_mode == 'Cheque' and _cd) else None

    # Editing an existing transaction: reverse the old row's effects, delete it, then
    # fall through and recreate it fresh. Keeps balances symmetric (uses the exact same
    # reversal as delete) and prevents the duplicate rows a blind re-insert would create.
    edit_id = request.form.get('edit_id')
    if edit_id:
        _old = Ledger.query.filter_by(id=edit_id, company_id=comp_id).first()
        if _old:
            _reverse_ledger_effects(_old, comp_id)
            db.session.delete(_old)
            db.session.flush()

    if txn_category == 'adjustment_invoice':
        cust = Customer.query.get(_safe_id(request.form.get('customer_id')))
        if cust: cust.balance += amount
        db.session.add(Ledger(company_id=comp_id, capital_account_id=capital_account_id, transaction_type=f"Adjustment Invoice - {payment_mode} - {remarks}", amount=0.0, customer_id=cust.id if cust else None, date_created=_entered_dt))
        
        invoice_ids, apply_amts = request.form.getlist('invoice_id[]'), request.form.getlist('apply_amount[]')
        for i in range(len(invoice_ids)):
            try:
                inv_id, apply_amt = int(invoice_ids[i]), float(apply_amts[i]) if i < len(apply_amts) else 0.0
                if apply_amt > 0:
                    inv = Invoice.query.get(inv_id)
                    if inv: 
                        actual = min(apply_amt, inv.balance_due)
                        inv.total_paid += actual
                        inv.balance_due -= actual
            except: continue
    
    elif txn_category == 'receipt':
        # Customer payment against existing invoices
        cust_id = _safe_id(request.form.get('customer_id'))
        cust = Customer.query.get(cust_id) if cust_id else None

        # Apply to invoices AND record the allocations so a later edit/delete can restore them
        allocs, _applied = _apply_invoice_allocations(request.form, comp_id, max_total=amount)
        db.session.add(Ledger(company_id=comp_id, capital_account_id=capital_account_id, transaction_type=f"Customer Payment - {payment_mode} - {remarks}", amount=amount, customer_id=cust.id if cust else None, payment_mode=payment_mode, bank_account_id=bank_account_id, cheque_number=cheque_number, cheque_date=cheque_date, remarks=remarks, category='receipt', allocations=json.dumps(allocs), date_created=_entered_dt))
        if cust: cust.balance -= amount
    
    elif txn_category in ('advance_receipt', 'customer_advance', 'customer_deposit', 'advance', 'deposit'):
        # Customer paid in advance (no specific invoice to apply to yet)
        cust_id = _safe_id(request.form.get('customer_id'))
        cust = Customer.query.get(cust_id) if cust_id else None
        
        if not cust:
            return jsonify({"success": False, "message": "Customer is required for advance/deposit"}) if request.is_json else redirect(url_for('receipts'))
        
        db.session.add(Ledger(
            date_created=_entered_dt,
            company_id=comp_id, capital_account_id=capital_account_id,
            transaction_type=f"Customer Advance - {payment_mode} - {remarks}",
            amount=amount,
            customer_id=cust.id,
            payment_mode=payment_mode,
            bank_account_id=bank_account_id,
            category='advance_receipt',
            remarks=remarks
        ))
        cust.balance -= amount   # negative balance = customer has credit with us
    
    elif txn_category in ('advance_payment', 'supplier_advance', 'supplier_deposit'):
        # We paid a supplier in advance
        sup_id = _safe_id(request.form.get('supplier_id'))
        sup = Supplier.query.get(sup_id) if sup_id else None
        
        if not sup:
            return redirect(url_for('receipts'))
        
        db.session.add(Ledger(
            date_created=_entered_dt,
            company_id=comp_id, capital_account_id=capital_account_id,
            transaction_type=f"Supplier Advance - {payment_mode} - {remarks}",
            amount=-amount,  # outgoing money
            supplier_id=sup.id,
            payment_mode=payment_mode,
            bank_account_id=bank_account_id,
            category='advance_payment',
            remarks=remarks
        ))
        sup.balance -= amount   # we owe them less now

    elif txn_category in ('cash_to_bank', 'bank_to_cash'):
        # Contra entry — internal transfer between cash in hand and a bank account.
        # No customer/supplier involved. The ledger amount is signed to match the
        # bank-balance delta, so the bank statement and delete-reversal stay consistent.
        contra_bank_id = _safe_id(request.form.get('bank_account_id'))
        bank = BankAccount.query.filter_by(id=contra_bank_id, company_id=comp_id).first() if contra_bank_id else None
        if not bank:
            return jsonify({"success": False, "message": "A bank account is required for a contra transfer"}) if request.is_json else redirect(url_for('receipts'))

        if txn_category == 'cash_to_bank':
            signed = amount          # cash deposited into the bank -> bank balance up
            direction = "Cash to Bank"
        else:
            signed = -amount         # cash withdrawn from the bank -> bank balance down
            direction = "Bank to Cash"

        db.session.add(Ledger(
            date_created=_entered_dt,
            company_id=comp_id, capital_account_id=capital_account_id,
            transaction_type=f"Contra ({direction}) - {remarks}",
            amount=signed,
            payment_mode="Contra",
            bank_account_id=bank.id,
            category=txn_category,
            remarks=remarks
        ))
        bank.balance += signed

    elif txn_category == 'refund_supplier':
        # Refund RECEIVED from a supplier (money in). Persist supplier_id so it shows on the
        # supplier statement and pre-fills when the row is edited.
        sup_id = _safe_id(request.form.get('supplier_id'))
        sup = Supplier.query.filter_by(id=sup_id, company_id=comp_id).first() if sup_id else None
        db.session.add(Ledger(
            date_created=_entered_dt,
            company_id=comp_id, capital_account_id=capital_account_id,
            transaction_type=f"Refund from Supplier - {payment_mode} - {remarks}",
            amount=amount,  # positive = money in
            supplier_id=sup.id if sup else None,
            payment_mode=payment_mode,
            bank_account_id=bank_account_id,
            cheque_number=cheque_number,
            cheque_date=cheque_date,
            category='refund_supplier',
            remarks=remarks
        ))
        if sup: sup.balance = (sup.balance or 0.0) + amount

    elif txn_category == 'payment':
        # Supplier payment (money out). Persist supplier_id + apply to outstanding bills so
        # the supplier statement reflects it and the balance/bill allocations stay correct.
        sup_id = _safe_id(request.form.get('supplier_id'))
        sup = Supplier.query.filter_by(id=sup_id, company_id=comp_id).first() if sup_id else None

        allocs, _applied = _apply_bill_allocations(request.form, comp_id, max_total=amount)
        db.session.add(Ledger(
            date_created=_entered_dt,
            company_id=comp_id, capital_account_id=capital_account_id,
            transaction_type=f"Supplier Payment - {payment_mode} - {remarks}",
            amount=-amount,
            supplier_id=sup.id if sup else None,
            payment_mode=payment_mode,
            bank_account_id=bank_account_id,
            cheque_number=cheque_number,
            cheque_date=cheque_date,
            category='payment',
            remarks=remarks,
            allocations=json.dumps(allocs)
        ))
        if sup: sup.balance = (sup.balance or 0.0) - amount

    elif txn_category == 'refund_customer':
        # Refund paid to a customer (money out). Persist customer_id so it shows on the
        # customer statement and pre-fills correctly when the row is edited.
        cust_id = _safe_id(request.form.get('customer_id'))
        cust = Customer.query.filter_by(id=cust_id, company_id=comp_id).first() if cust_id else None
        db.session.add(Ledger(
            date_created=_entered_dt,
            company_id=comp_id, capital_account_id=capital_account_id,
            transaction_type=f"Refund to Customer - {payment_mode} - {remarks}",
            amount=-amount,
            customer_id=cust.id if cust else None,
            payment_mode=payment_mode,
            bank_account_id=bank_account_id,
            cheque_number=cheque_number,
            cheque_date=cheque_date,
            category='refund_customer',
            remarks=remarks
        ))
        if cust: cust.balance = (cust.balance or 0.0) + amount

    else:
        # Generic ledger entry (income / expense not tied to a customer or supplier).
        # Money-out categories are stored as a NEGATIVE amount so they register as
        # expenses, appear in the Money OUT column, and reduce cash/bank balances —
        # consistent with how the rest of the app reads the ledger.
        signed = -abs(amount) if txn_category in RECEIPT_OUT_CATEGORIES else amount

        # Associate the asset / staff this entry relates to (no balance side-effect, but it
        # lets the row surface in the relevant statement and cost-center reports).
        _veh_cats = {'advance_receipt', 'misc_income', 'vehicle_emi', 'car_maintenance', 'registration_expense', 'misc_expense'}
        _room_cats = {'advance_receipt', 'room_rent_col', 'utility_water', 'utility_elec', 'misc_income', 'room_water_payment', 'room_elec_payment', 'room_wifi_payment', 'room_tax_payment'}
        staff_id = _safe_id(request.form.get('staff_id'))
        vehicle_id = _safe_id(request.form.get('vehicle_id')) if txn_category in _veh_cats else None
        room_id = _safe_id(request.form.get('room_id')) if txn_category in _room_cats else None

        # A payout is a standalone money-out movement, so give it a readable label in the
        # ledger instead of the raw category code.
        _tt = {'payout': 'Payout', 'money_receipt': 'Money Receipt'}.get(txn_category, txn_category)

        db.session.add(Ledger(
            date_created=_entered_dt,
            company_id=comp_id, capital_account_id=capital_account_id,
            transaction_type=f"{_tt} - {remarks}",
            amount=signed,
            payment_mode=payment_mode,
            bank_account_id=bank_account_id,
            staff_id=staff_id,
            vehicle_id=vehicle_id,
            room_id=room_id,
            category=txn_category,
            remarks=remarks
        ))
        # Req #3: a vehicle EMI/loan payment reduces that vehicle's outstanding loan balance.
        if txn_category == 'vehicle_emi' and vehicle_id:
            _veh = Vehicle.query.filter_by(id=vehicle_id, company_id=comp_id).first()
            if _veh:
                _veh.total_loan_balance = (_veh.total_loan_balance or 0.0) - amount
    
    # Update bank balance if applicable
    if bank_account_id:
        bank = BankAccount.query.get(bank_account_id)
        if bank and txn_category in ('receipt', 'advance_receipt', 'customer_advance', 'customer_deposit', 'advance', 'deposit', 'refund_supplier'):
            bank.balance += amount
        elif bank and txn_category in ('advance_payment', 'supplier_advance', 'supplier_deposit', 'payment'):
            bank.balance -= amount
        elif bank and txn_category in RECEIPT_OUT_CATEGORIES:
            bank.balance -= amount

    # Capital / Loan account behaves like a bank account: money received into it raises
    # the holder's balance, money paid out of it lowers it.
    if capital_account_id:
        cap = CapitalAccount.query.filter_by(id=capital_account_id, company_id=comp_id).first()
        if cap:
            if txn_category in ('receipt', 'advance_receipt', 'customer_advance', 'customer_deposit', 'advance', 'deposit', 'refund_supplier'):
                cap.balance = (cap.balance or 0.0) + amount
            elif txn_category in ('advance_payment', 'supplier_advance', 'supplier_deposit', 'payment'):
                cap.balance = (cap.balance or 0.0) - amount
            elif txn_category in RECEIPT_OUT_CATEGORIES:
                cap.balance = (cap.balance or 0.0) - amount
            else:
                cap.balance = (cap.balance or 0.0) + amount

    # Backstop for a Ledger added above without date_created (a future branch, say).
    # It only reaches rows still pending, which is exactly why it cannot be the primary
    # mechanism: an intervening query flushes a row out of db.session.new. Every Ledger
    # in this route is therefore built with date_created=_entered_dt directly.
    for _obj in list(db.session.new):
        if isinstance(_obj, Ledger) and _obj.date_created is None:
            _obj.date_created = _entered_dt

    db.session.commit()
    return redirect(url_for('receipts'))

# FIXED: Added delete_receipt route
@app.route('/delete_receipt/<int:id>', methods=['POST'])
@require_permission('Ledger_View')
def delete_receipt(id):
    comp_id = session.get('company_id')
    rec = Ledger.query.filter_by(id=id, company_id=comp_id).first()
    if rec:
        # Reverse every balance / allocation effect this row had, then remove it.
        _reverse_ledger_effects(rec, comp_id)
        db.session.delete(rec)
        db.session.commit()
    return redirect(url_for('receipts'))

@app.route('/save_refund_track', methods=['POST'])
@require_permission('Ledger_Add')
def save_refund_track():
    comp_id = session.get('company_id')
    try:
        expected = float(request.form.get('expected_amount') or 0)
    except (TypeError, ValueError):
        expected = 0.0
    try:
        payable = float(request.form.get('payable_amount') or 0)
    except (TypeError, ValueError):
        payable = 0.0

    db.session.add(RefundTrack(
        company_id=comp_id,
        request_date=request.form.get('request_date') or '',
        supplier_id=(request.form.get('supplier_id') or None),
        customer_id=(request.form.get('customer_id') or None),
        expected_amount=expected,
        payable_amount=payable,
        approx_date=request.form.get('approx_date') or '',
        remarks=request.form.get('remarks') or '',
        status='Pending'
    ))
    db.session.commit()
    return redirect(url_for('receipts'))

# ==========================================
# THE FULL ANALYTICS / REPORTS ROUTE
# ==========================================
# ==========================================
# PROFIT & LOSS ENGINE  +  BALANCE SHEET
# ==========================================

# Money-IN ledger categories that are NOT income:
#   receipt / advance* / deposit*  -> settle or create a customer balance (balance-sheet only)
#   cash_to_bank / bank_to_cash    -> internal transfers
#   adjustment_invoice             -> zero-value credit note
#   refund_supplier                -> handled separately as Other Income
# Everything else that brought money in IS income. Recognising it matters: the cash it
# produced sits on the Balance Sheet, so unless an equal amount of income reaches equity
# the two sides of the Balance Sheet cannot agree.
NON_INCOME_IN_CATEGORIES = {
    'receipt', 'advance_receipt', 'customer_advance', 'customer_deposit', 'advance',
    'deposit', 'cash_to_bank', 'bank_to_cash', 'adjustment_invoice', 'refund_supplier',
    # money_receipt is the money-in twin of payout: cash comes in, but it is a
    # contribution to the business, not something it earned — it goes to equity.
    'money_receipt',
}

# Same list for rows saved before the `category` column was populated: the category is
# then read back from the transaction_type prefix, which spells the label out in words.
NON_INCOME_IN_PREFIXES = (
    'Customer Payment', 'Customer Advance', 'Customer Deposit', 'Supplier Advance',
    'Contra', 'Adjustment', 'Refund from Supplier', 'Money Receipt',
)


def _pl_core(comp_id, pl_start=None, pl_end=None):
    """Profit & Loss figures for one company over an optional date window.

    Returns every number the P&L statement needs. It is deliberately callable a second
    time with no date window, which is how the Balance Sheet obtains the cumulative
    Accumulated Profit / (Loss) it carries into equity — a balance sheet states an
    all-time position, so it can never use a period-filtered profit figure.
    """
    def apply_dates(query, model, date_col='date_created'):
        col = getattr(model, date_col)
        if pl_start: query = query.filter(col >= pl_start)
        if pl_end: query = query.filter(col < pl_end)
        return query

    inc_q = db.session.query(Booking.service_type, func.sum(Booking.selling_price).label('revenue'), func.sum(Booking.net_cost).label('cogs')).filter(Booking.company_id == comp_id, Booking.invoice_id.isnot(None))
    inc_q = apply_dates(inc_q, Booking)
    income_by_service = inc_q.group_by(Booking.service_type).all()

    total_revenue = sum([item.revenue for item in income_by_service]) if income_by_service else 0.0
    total_cogs = sum([item.cogs for item in income_by_service]) if income_by_service else 0.0

    l_q = Ledger.query.filter(Ledger.company_id == comp_id)
    all_ledger = apply_dates(l_q, Ledger).all()

    e_q = Expense.query.filter_by(company_id=comp_id)
    db_expenses = apply_dates(e_q, Expense).all()

    operating_expenses_dict = {}
    detailed_expenses = []
    total_expenses = 0.0

    # Money-out categories that are settlements / prepayments. They appear in the detailed
    # expense list (so it shows all money out of the receipt & payment page) but are excluded
    # from the P&L expense total to avoid double-counting against COGS.
    # NOTE: refunds are NOT in this set — per the accounting rule used here, a refund received
    # from a supplier is income and a refund paid to a customer is an expense.
    # 'payout' belongs here too: it is money out by design, but the user records it precisely
    # because it is NOT an expense, so it must never reach the P&L expense total.
    NON_OPERATING_OUT = {'payment', 'advance_payment', 'payout'}

    # Lines that sit outside the booking-driven revenue / COGS figures
    refund_income_total = 0.0    # refunds from suppliers  -> income
    refund_expense_total = 0.0   # refunds to customers    -> direct cost
    other_income_dict = {}       # rent / utility / deposit / misc collections -> other income

    for le in all_ledger:
        tt = le.transaction_type or ''
        # Internal cash <-> bank transfers are not expenses
        if le.payment_mode == 'Contra' or tt.startswith('Contra'):
            continue
        # Skip pure ledger adjustments and cost-of-sale postings
        if any(x in tt for x in ["Direct Invoice Cost", "Adjustment"]):
            continue
        # Resolve the category code from the stored column, else the transaction_type prefix
        code = (le.category or '').strip() or tt.split(' - ')[0].strip()

        # Refund RECEIVED from a supplier = money in = INCOME (not an expense row at all).
        # The tt fallback catches legacy rows saved before the category column existed.
        if code == 'refund_supplier' or tt.startswith('Refund from Supplier'):
            refund_income_total += abs(le.amount)
            continue

        # A row is money-out if it's stored negative OR its category is a known money-out type
        # (the OR clause also catches legacy rows that were saved with a positive amount)
        is_money_out = (le.amount < 0) or (code in RECEIPT_OUT_CATEGORIES)

        if not is_money_out:
            # Money IN. Anything that is not a settlement, an advance or a transfer is income.
            if (le.amount or 0) > 0 and code not in NON_INCOME_IN_CATEGORIES \
                    and not tt.startswith(NON_INCOME_IN_PREFIXES):
                _lbl = RECEIPT_CATEGORY_LABELS.get(code) or (code.replace('_', ' ').title() if code else 'Other Receipts')
                other_income_dict[_lbl] = other_income_dict.get(_lbl, 0.0) + float(le.amount or 0.0)
            continue

        label = RECEIPT_CATEGORY_LABELS.get(code) or (code.replace('_', ' ').title() if code else 'Other')
        amt = abs(le.amount)
        detailed_expenses.append({'date': le.date_created.strftime('%Y-%m-%d'), 'category': label, 'description': tt, 'paid_via': le.payment_mode or '-', 'amount': amt})

        # Refund PAID to a customer = DIRECT expense. It reverses a sale, so it is charged
        # against Cost of Sales rather than sitting with the indirect/operating overheads.
        if code == 'refund_customer' or tt.startswith('Refund to Customer'):
            refund_expense_total += amt
            continue

        # Operating-expense total (feeds P&L) excludes settlements / advances
        non_operating = (code in NON_OPERATING_OUT) or any(x in tt for x in ["Supplier Payment", "Supplier Advance", "Customer Advance"])
        if not non_operating:
            operating_expenses_dict[label] = operating_expenses_dict.get(label, 0.0) + amt
            total_expenses += amt

    for e in db_expenses:
        cat = e.expense_type or "Other Expenses"
        operating_expenses_dict[cat] = operating_expenses_dict.get(cat, 0.0) + abs(e.amount)
        total_expenses += abs(e.amount)
        detailed_expenses.append({'date': e.date_created.strftime('%Y-%m-%d'), 'category': cat, 'description': e.notes, 'paid_via': 'System', 'amount': abs(e.amount)})

    detailed_expenses.sort(key=lambda x: x['date'], reverse=True)
    expense_list = [{'category': k, 'amount': v} for k, v in operating_expenses_dict.items()]

    # Other income shown under the Income head, above TOTAL GROSS REVENUE
    other_income = []
    if refund_income_total:
        other_income.append({'label': RECEIPT_CATEGORY_LABELS.get('refund_supplier', 'Refund from Supplier'),
                             'amount': refund_income_total})
    for _lbl in sorted(other_income_dict):
        other_income.append({'label': _lbl, 'amount': other_income_dict[_lbl]})
    total_other_income = sum(o['amount'] for o in other_income)
    total_income = total_revenue + total_other_income

    # Direct costs that aren't a booking's own COGS, listed under Cost of Sales
    direct_expenses = []
    if refund_expense_total:
        direct_expenses.append({'label': RECEIPT_CATEGORY_LABELS.get('refund_customer', 'Refund to Customer'),
                                'amount': refund_expense_total})
    total_cogs = total_cogs + sum(d['amount'] for d in direct_expenses)

    net_profit = total_income - total_cogs - total_expenses

    return {
        'income_by_service': income_by_service, 'total_revenue': total_revenue,
        'other_income': other_income, 'total_other_income': total_other_income,
        'total_income': total_income, 'direct_expenses': direct_expenses,
        'total_cogs': total_cogs, 'expense_list': expense_list,
        'detailed_expenses': detailed_expenses, 'total_expenses': total_expenses,
        'net_profit': net_profit, 'refund_income_total': refund_income_total,
        'refund_expense_total': refund_expense_total,
    }


def _balance_sheet(comp_id, bs_start=None, bs_end=None):
    """Balance Sheet as at a date, classified under standard accounting heads.

    A balance sheet is a statement of POSITION, so it is drawn up "as at" a single date —
    never "between" two dates. `bs_end` is that date (today when omitted). `bs_start` is
    optional and does not change any asset or liability: it only splits the accumulated
    profit into the amount brought forward and the profit earned during the period, which
    is how a financial-period balance sheet is normally presented.

    Historical positions
    --------------------
    The masters carry live running balances, not dated history, so a position as at an
    earlier date is obtained by taking each live balance and rolling it BACK through every
    transaction recorded after that date — the ledger rows, the invoices raised and the
    supplier costs on them. Doing it this way guarantees the important property: with no
    date (or a date of today or later) there is nothing to roll back, so the statement is
    exactly the live position.

        ASSETS  =  LIABILITIES  +  EQUITY

    Where each figure comes from, and why it sits on that side:

      Assets
        Cash in Hand / Unbanked Funds  every ledger amount not attributed to a bank or a
                                       capital account, less the contra transfers that
                                       moved cash into a bank (those raised the bank side)
        Bank & Card Balances           BankAccount.balance, positive accounts only
        Capital / Loan Account Funds   CapitalAccount.balance — money of the holder's that
                                       the business still holds; the matching contribution
                                       sits in Equity (capital) or Liabilities (loan)
        Trade Receivables              customers with a debit balance — they owe us
        Advances to Suppliers          suppliers with a debit balance — we have prepaid

      Liabilities
        Trade Payables                 suppliers with a credit balance — we owe them
        Customer Advances & Credits    customers with a credit balance — we owe them
        Bank Overdraft / Card O/S      bank or card accounts that are overdrawn
        Vehicle / Asset Loans          Vehicle.total_loan_balance still outstanding
        Loans from A/C Holders         capital accounts recorded with type "Loan"

      Equity
        Capital Introduced             capital accounts recorded with type "Capital"
        Opening Balance Equity         opening balances seeded on customers, suppliers and
                                       banks — they were entered without a double entry, so
                                       equity is what carries them
        Accumulated Profit / (Loss)    cumulative P&L result
        Add: Money Receipts            "Money Receipt" rows — cash put in, not income
        Less: Drawings / Payouts       "Payout" rows — money withdrawn, not an expense
        Refund basis adjustment        refunds hit the P&L AND the party's balance in this
                                       system; equity is adjusted so they are not counted twice
        Unreconciled Difference        whatever the single-entry data cannot explain, shown
                                       openly instead of being buried in another head
    """
    _f = lambda x: float(x or 0.0)

    # Everything dated on or after `after` is "in the future" for this statement and gets
    # rolled back. None means today's live position, and nothing is rolled back at all.
    after = datetime.combine(bs_end + timedelta(days=1), datetime.min.time()) if bs_end else None

    customers = Customer.query.filter_by(company_id=comp_id).all()
    suppliers = Supplier.query.filter_by(company_id=comp_id).all()
    banks = BankAccount.query.filter_by(company_id=comp_id).all()
    caps = CapitalAccount.query.filter_by(company_id=comp_id).all()
    vehicles = Vehicle.query.filter_by(company_id=comp_id).all()

    # A capital / loan account opened after the as-at date did not exist yet.
    if after:
        caps = [k for k in caps if not k.date_created or k.date_created < after]

    # ---- Movements recorded AFTER the as-at date, to be unwound from the live balances ----
    cust_after, sup_after, bank_after, cap_after = {}, {}, {}, {}
    if after:
        _bump = lambda d, k, v: d.__setitem__(k, d.get(k, 0.0) + v)
        for le in Ledger.query.filter(Ledger.company_id == comp_id, Ledger.date_created >= after).all():
            amt = _f(le.amount)
            # Forward effects, mirrored from _apply_ledger_effect / _reverse_ledger_effects
            if le.customer_id: _bump(cust_after, le.customer_id, -amt)
            if le.supplier_id: _bump(sup_after, le.supplier_id, amt)
            if le.bank_account_id: _bump(bank_after, le.bank_account_id, amt)
            if le.capital_account_id: _bump(cap_after, le.capital_account_id, amt)

        # Raising an invoice adds the amount left unpaid at that moment to the customer, and
        # each supplier-sourced line on it adds its cost to that supplier. The unpaid amount
        # is the invoice total less any advance typed on the invoice screen, which is
        # recovered as total_paid minus everything later allocated to it by a receipt.
        alloc_by_inv = {}
        for le in Ledger.query.filter(Ledger.company_id == comp_id, Ledger.allocations.isnot(None)).all():
            for a in _alloc_list(le):
                try:
                    alloc_by_inv[int(a.get('invoice_id'))] = alloc_by_inv.get(int(a.get('invoice_id')), 0.0) + float(a.get('amount') or 0.0)
                except (TypeError, ValueError):
                    continue
        _defer = [defer(col) for col in _BOOKING_DEFER]
        for inv in Invoice.query.filter(Invoice.company_id == comp_id, Invoice.date_created >= after).all():
            adv = max(0.0, _f(inv.total_paid) - alloc_by_inv.get(inv.id, 0.0))
            if inv.customer_id:
                _bump(cust_after, inv.customer_id, _f(inv.total_amount) - adv)
            for b in Booking.query.filter_by(company_id=comp_id, invoice_id=inv.id).options(*_defer).all():
                if b.supplier_id and _f(b.net_cost) > 0:
                    _bump(sup_after, b.supplier_id, _f(b.net_cost))

    cbal = lambda c: _f(c.balance) - cust_after.get(c.id, 0.0)
    sbal = lambda s: _f(s.balance) - sup_after.get(s.id, 0.0)
    bbal = lambda b: _f(b.balance) - bank_after.get(b.id, 0.0)
    kbal = lambda k: _f(k.balance) - cap_after.get(k.id, 0.0)

    def _upto(q):
        """Restrict a ledger aggregate to rows dated on or before the as-at date."""
        return q.filter(Ledger.date_created < after) if after else q

    # ---- Cash: everything the ledger moved that no bank or capital account absorbed ----
    unbanked = _upto(db.session.query(func.sum(Ledger.amount)).filter(
        Ledger.company_id == comp_id,
        Ledger.bank_account_id.is_(None),
        Ledger.capital_account_id.is_(None))).scalar() or 0.0
    contra = _upto(db.session.query(func.sum(Ledger.amount)).filter(
        Ledger.company_id == comp_id, Ledger.payment_mode == 'Contra')).scalar() or 0.0
    cash_in_hand = _f(unbanked) - _f(contra)

    trade_receivables = sum(cbal(c) for c in customers if cbal(c) > 0)
    customer_credits = sum(-cbal(c) for c in customers if cbal(c) < 0)
    trade_payables = sum(sbal(s) for s in suppliers if sbal(s) > 0)
    supplier_advances = sum(-sbal(s) for s in suppliers if sbal(s) < 0)

    bank_funds = sum(bbal(b) for b in banks if bbal(b) > 0)
    bank_overdrawn = sum(-bbal(b) for b in banks if bbal(b) < 0)

    capital_funds = sum(kbal(c) for c in caps if kbal(c) > 0)
    capital_overdrawn = sum(-kbal(c) for c in caps if kbal(c) < 0)
    capital_introduced = sum(_f(c.capital_deposit) for c in caps if (c.account_type or 'Capital') != 'Loan')
    holder_loans = sum(_f(c.capital_deposit) for c in caps if (c.account_type or '') == 'Loan')

    vehicle_loans = sum(_f(v.total_loan_balance) for v in vehicles if _f(v.total_loan_balance) > 0)

    opening_equity = (sum(_f(c.opening_balance) for c in customers)
                      - sum(_f(s.opening_balance) for s in suppliers)
                      + sum(_f(b.opening_balance) for b in banks))

    drawings = abs(_f(_upto(db.session.query(func.sum(Ledger.amount)).filter(
        Ledger.company_id == comp_id, Ledger.category == 'payout')).scalar()))
    # Money Receipts are the mirror image: cash put INTO the business that is not income,
    # so they are a contribution to equity, exactly as a payout is a withdrawal from it.
    money_receipts = _f(_upto(db.session.query(func.sum(Ledger.amount)).filter(
        Ledger.company_id == comp_id, Ledger.category == 'money_receipt')).scalar())

    # Accumulated profit up to the as-at date. When a period start is given the same total
    # is split into what was brought forward and what the period itself earned.
    pl = _pl_core(comp_id, None, after)
    retained = pl['net_profit']
    refund_adj = pl['refund_expense_total'] - pl['refund_income_total']
    retained_bf = _pl_core(comp_id, None, bs_start)['net_profit'] if bs_start else None
    retained_period = (retained - retained_bf) if bs_start else None

    # A head that is nil is left off the face of the statement, the way a printed
    # balance sheet does; the totals are unaffected either way.
    def _live(rows):
        return [r for r in rows if abs(r['amount']) > 0.005]

    current_assets = _live([
        {'label': 'Cash in Hand / Unbanked Funds', 'amount': cash_in_hand},
        {'label': 'Bank & Card Account Balances', 'amount': bank_funds},
        {'label': 'Funds Held in Capital / Loan Accounts', 'amount': capital_funds},
        {'label': 'Trade Receivables (Customers Owing)', 'amount': trade_receivables},
        {'label': 'Advances Paid to Suppliers', 'amount': supplier_advances},
    ])
    fixed_assets = [
        {'label': 'Vehicles, Property & Equipment (at cost)', 'amount': 0.0,
         'note': 'Not valued in the system — see note below'},
    ]
    total_current_assets = sum(r['amount'] for r in current_assets)
    total_fixed_assets = sum(r['amount'] for r in fixed_assets)
    total_assets = total_current_assets + total_fixed_assets

    current_liabilities = _live([
        {'label': 'Trade Payables (Owed to Suppliers)', 'amount': trade_payables},
        {'label': 'Customer Advances & Credit Balances', 'amount': customer_credits},
        {'label': 'Bank Overdraft / Card Outstanding', 'amount': bank_overdrawn},
        {'label': 'Capital / Loan Accounts Overdrawn', 'amount': capital_overdrawn},
    ])
    long_term_liabilities = _live([
        {'label': 'Vehicle / Asset Loans Outstanding', 'amount': vehicle_loans},
        {'label': 'Loans from Capital Account Holders', 'amount': holder_loans},
    ])
    total_current_liabilities = sum(r['amount'] for r in current_liabilities)
    total_long_term_liabilities = sum(r['amount'] for r in long_term_liabilities)
    total_liabilities = total_current_liabilities + total_long_term_liabilities

    _profit_rows = ([{'label': 'Accumulated Profit / (Loss) Brought Forward', 'amount': retained_bf},
                     {'label': 'Profit / (Loss) for the Period', 'amount': retained_period}]
                    if bs_start else
                    [{'label': 'Accumulated Profit / (Loss)', 'amount': retained}])
    equity = _live([
        {'label': "Owner's / Partners' Capital Introduced", 'amount': capital_introduced},
        {'label': 'Opening Balance Equity', 'amount': opening_equity},
    ] + _profit_rows + [
        {'label': 'Add: Money Receipts (Contributions, Non-Income)', 'amount': money_receipts},
        {'label': 'Less: Drawings / Payouts', 'amount': -drawings},
    ])
    if abs(refund_adj) > 0.005:
        equity.append({'label': 'Refunds already reflected in party balances', 'amount': refund_adj})

    equity_before_diff = sum(r['amount'] for r in equity)
    difference = total_assets - total_liabilities - equity_before_diff
    if abs(difference) > 0.005:
        equity.append({'label': 'Unreconciled Difference (Suspense)', 'amount': difference,
                       'suspense': True})
    total_equity = equity_before_diff + (difference if abs(difference) > 0.005 else 0.0)

    return {
        'current_assets': current_assets, 'total_current_assets': total_current_assets,
        'fixed_assets': fixed_assets, 'total_fixed_assets': total_fixed_assets,
        'total_assets': total_assets,
        'current_liabilities': current_liabilities, 'total_current_liabilities': total_current_liabilities,
        'long_term_liabilities': long_term_liabilities, 'total_long_term_liabilities': total_long_term_liabilities,
        'total_liabilities': total_liabilities,
        'equity': equity, 'total_equity': total_equity,
        'total_liab_equity': total_liabilities + total_equity,
        'difference': difference,
        'working_capital': total_current_assets - total_current_liabilities,
        'net_worth': total_assets - total_liabilities,
        'vehicle_loans': vehicle_loans,
        'balanced': abs(difference) <= 0.005,
        'as_at': bs_end, 'period_start': bs_start,
        'retained_bf': retained_bf, 'retained_period': retained_period,
        'is_historic': bool(after and after.date() <= datetime.utcnow().date()),
    }

@app.route('/analytics')
@require_permission('Reports_View')
def analytics(): 
    comp_id = session.get('company_id')
    today = datetime.now(timezone.utc).date()
    
    pl_start_str = request.args.get('pl_start')
    pl_end_str = request.args.get('pl_end')
    
    pl_start = datetime.strptime(pl_start_str, '%Y-%m-%d').date() if pl_start_str else None
    pl_end = datetime.strptime(pl_end_str, '%Y-%m-%d').date() + timedelta(days=1) if pl_end_str else None

    def apply_dates(query, model, date_col='date_created'):
        col = getattr(model, date_col)
        if pl_start: query = query.filter(col >= pl_start)
        if pl_end: query = query.filter(col < pl_end)
        return query

    # ---- Profit & Loss for the selected period, and the Balance Sheet position ----
    _pl = _pl_core(comp_id, pl_start, pl_end)
    income_by_service   = _pl['income_by_service']
    total_revenue       = _pl['total_revenue']
    other_income        = _pl['other_income']
    total_other_income  = _pl['total_other_income']
    total_income        = _pl['total_income']
    direct_expenses     = _pl['direct_expenses']
    total_cogs          = _pl['total_cogs']
    expense_list        = _pl['expense_list']
    detailed_expenses   = _pl['detailed_expenses']
    total_expenses      = _pl['total_expenses']
    net_profit          = _pl['net_profit']

    # ---- Balance Sheet period. Independent of the P&L filter: the balance sheet is drawn
    # up "as at" bs_end, and bs_start only splits the profit into brought-forward and
    # for-the-period. fy_start is remembered so the financial-year presets stay put. ----
    bs_start_str = request.args.get('bs_start')
    bs_end_str = request.args.get('bs_end')
    fy_start_month = request.args.get('fy_start') or '1'
    try:
        bs_start = datetime.strptime(bs_start_str, '%Y-%m-%d').date() if bs_start_str else None
    except ValueError:
        bs_start, bs_start_str = None, ''
    try:
        bs_end = datetime.strptime(bs_end_str, '%Y-%m-%d').date() if bs_end_str else None
    except ValueError:
        bs_end, bs_end_str = None, ''
    balance_sheet = _balance_sheet(comp_id, bs_start, bs_end)
    
    total_receivables = db.session.query(func.sum(Customer.balance)).filter_by(company_id=comp_id).scalar() or 0.0
    total_payables = db.session.query(func.sum(Supplier.balance)).filter_by(company_id=comp_id).scalar() or 0.0
    
    pending_invoices = db.session.query(Invoice, Customer).join(Customer, Invoice.customer_id == Customer.id).filter(Invoice.company_id == comp_id, Invoice.balance_due > 0).order_by(Invoice.date_created.desc()).all()
    
    # FIXED: Proper joining and mapping for Supplier Payables
    pending_payables_raw = db.session.query(Booking, Supplier).join(Supplier, Booking.supplier_id == Supplier.id).filter(Booking.company_id == comp_id, Booking.net_cost > 0).all()
    pending_payables = []
    for b, s in pending_payables_raw:
        paid = b.supplier_paid or 0.0
        bal = b.net_cost - paid
        if bal > 0.001:
            b.total_amount = b.net_cost 
            b.total_paid = paid
            b.balance_due = bal
            pending_payables.append((b, s))
    
    tax_reports = []
    all_invs_with_cust = db.session.query(Invoice, Customer).outerjoin(Customer, Invoice.customer_id == Customer.id).filter(Invoice.company_id == comp_id, Invoice.tax_amount > 0).all()
    for inv, cust in all_invs_with_cust:
        tax_reports.append({'invoice_id': inv.id, 'date': inv.date_created.strftime('%Y-%m-%d') if inv.date_created else '', 'customer': cust.name if cust else 'Unknown', 'gross_amount': inv.total_amount - (inv.tax_amount or 0), 'tax_rate': inv.tax_rate or 0, 'tax_amount': inv.tax_amount or 0, 'total_amount': inv.total_amount})

    all_vehicles = Vehicle.query.filter_by(company_id=comp_id).all()
    vehicle_stats, cost_centers, car_statuses, car_emis, doc_expiries = [], [], [], [], []
    for v in all_vehicles:
        v_rev_q = db.session.query(func.sum(Booking.selling_price)).filter(Booking.company_id == comp_id, Booking.vehicle_id == v.id, Booking.invoice_id.isnot(None))
        v_rev = apply_dates(v_rev_q, Booking).scalar() or 0.0
        v_exp_q = db.session.query(func.sum(Ledger.amount)).filter_by(company_id=comp_id, vehicle_id=v.id).filter(Ledger.amount < 0)
        v_exp = apply_dates(v_exp_q, Ledger).scalar() or 0.0
        cost_centers.append({'type': 'Vehicle', 'name': f"{v.name} ({v.registration_number})", 'revenue': v_rev, 'costs': abs(v_exp), 'profit': v_rev - abs(v_exp)})
        status, details = "Available", "Ready"
        active_booking = Booking.query.filter(Booking.vehicle_id == v.id, Booking.pickup_datetime <= datetime.now(timezone.utc), Booking.dropoff_datetime >= datetime.now(timezone.utc)).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first()
        if active_booking: status, details = "Occupied", f"Rented till {active_booking.dropoff_datetime.strftime('%Y-%m-%d %H:%M')}"
        else:
            future_booking = Booking.query.filter(Booking.vehicle_id == v.id, Booking.pickup_datetime > datetime.now(timezone.utc)).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).order_by(Booking.pickup_datetime.asc()).first()
            if future_booking: status, details = "Future", f"Reserved from {future_booking.pickup_datetime.strftime('%Y-%m-%d %H:%M')}"
        car_statuses.append({"name": v.name, "reg": v.registration_number, "status": status, "details": details})
        if v.total_loan_balance > 0: car_emis.append({"car_name": v.name, "reg": v.registration_number, "bank": v.bank_name, "acc_no": v.loan_account_no, "balance": v.total_loan_balance, "emi": v.monthly_emi})
        if v.ins_expiry and (v.ins_expiry - today).days <= 30: doc_expiries.append({'type': 'Insurance', 'entity': v.name, 'ref': v.registration_number, 'expiry': v.ins_expiry.strftime('%Y-%m-%d'), 'days_left': (v.ins_expiry - today).days})
        if v.reg_expiry and (v.reg_expiry - today).days <= 30: doc_expiries.append({'type': 'Registration', 'entity': v.name, 'ref': v.registration_number, 'expiry': v.reg_expiry.strftime('%Y-%m-%d'), 'days_left': (v.reg_expiry - today).days})

    all_rooms = Room.query.filter_by(company_id=comp_id).all()
    room_statuses = []
    for r in all_rooms:
        bldg = Building.query.get(r.building_id)
        bldg_name = bldg.name if bldg else "Unknown"
        r_rev_q = db.session.query(func.sum(Booking.selling_price)).filter(Booking.company_id == comp_id, Booking.room_id == r.id, Booking.invoice_id.isnot(None))
        r_rev = apply_dates(r_rev_q, Booking).scalar() or 0.0
        r_exp_q = db.session.query(func.sum(Ledger.amount)).filter_by(company_id=comp_id, room_id=r.id).filter(Ledger.amount < 0)
        r_exp = apply_dates(r_exp_q, Ledger).scalar() or 0.0
        cost_centers.append({'type': 'Property/Room', 'name': f"{bldg_name} - Room {r.room_number}", 'revenue': r_rev, 'costs': abs(r_exp), 'profit': r_rev - abs(r_exp)})
        status, details = "Available", "Ready"
        active_booking = Booking.query.filter(Booking.room_id == r.id, Booking.pickup_datetime <= datetime.now(timezone.utc), Booking.dropoff_datetime >= datetime.now(timezone.utc)).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first()
        if active_booking: status, details = "Occupied", f"Occupied till {active_booking.dropoff_datetime.strftime('%Y-%m-%d %H:%M')}"
        else:
            future_booking = Booking.query.filter(Booking.room_id == r.id, Booking.pickup_datetime > datetime.now(timezone.utc)).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).order_by(Booking.pickup_datetime.asc()).first()
            if future_booking: status, details = "Future", f"Reserved from {future_booking.pickup_datetime.strftime('%Y-%m-%d %H:%M')}"
        room_statuses.append({"building": bldg_name, "room_no": r.room_number, "type": r.room_type, "status": status, "details": details})

    loss_bills_q = Booking.query.filter(Booking.company_id == comp_id, Booking.net_cost > Booking.selling_price, Booking.invoice_id.isnot(None)).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]])
    loss_bills_data = apply_dates(loss_bills_q, Booking).all()
    customers_dict = {c.id: c.name for c in Customer.query.filter_by(company_id=comp_id).all()}
    loss_bills = [{"id": b.id, "date": b.date_created.strftime('%Y-%m-%d'), "customer": customers_dict.get(b.customer_id, "Unknown"), "cost": b.net_cost, "sold": b.selling_price, "loss_amount": b.net_cost - b.selling_price} for b in loss_bills_data]

    cashbook_data = Ledger.query.filter_by(company_id=comp_id).order_by(Ledger.date_created.asc()).limit(500).all()
    cashbook_entries = []
    run_bal = 0.0
    for cb in cashbook_data:
        run_bal += cb.amount
        cashbook_entries.append({"id": cb.id, "date": cb.date_created.strftime('%Y-%m-%d'), "reference": cb.transaction_type, "cash_in": cb.amount if cb.amount > 0 else 0, "cash_out": abs(cb.amount) if cb.amount < 0 else 0, "balance": run_bal})

    bank_accounts = BankAccount.query.filter_by(company_id=comp_id).all()
    bank_reports = []
    for b in bank_accounts:
        total_in = db.session.query(func.sum(Ledger.amount)).filter_by(company_id=comp_id, bank_account_id=b.id).filter(Ledger.amount > 0).scalar() or 0.0
        total_out = db.session.query(func.sum(Ledger.amount)).filter_by(company_id=comp_id, bank_account_id=b.id).filter(Ledger.amount < 0).scalar() or 0.0
        bank_reports.append({'id': b.id, 'bank_name': b.bank_name, 'acc_ref': b.account_number or b.card_ref or '-', 'opening': b.opening_balance, 'total_in': total_in, 'total_out': abs(total_out), 'closing': b.balance})

    service_reports = []
    for inc in income_by_service:
        b_cnt_q = Booking.query.filter(Booking.company_id == comp_id, Booking.service_type == inc.service_type, Booking.invoice_id.isnot(None)).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]])
        b_count = apply_dates(b_cnt_q, Booking).count()
        margin = inc.revenue - inc.cogs
        service_reports.append({'category': inc.service_type, 'count': b_count, 'revenue': inc.revenue, 'costs': inc.cogs, 'margin': margin})
        
    pdc_data = Ledger.query.filter_by(company_id=comp_id, payment_mode='Cheque').order_by(Ledger.cheque_date.asc()).all()
    pdc_cheques = []
    for chq in pdc_data:
        entity_name = "Unknown"
        if chq.customer_id:
            c = Customer.query.get(chq.customer_id)
            if c: entity_name = c.name
        elif chq.supplier_id:
            s = Supplier.query.get(chq.supplier_id)
            if s: entity_name = s.name
        b_name = "N/A"
        if chq.bank_account_id:
            bb = BankAccount.query.get(chq.bank_account_id)
            if bb: b_name = bb.bank_name
        pdc_cheques.append({'cheque_no': chq.cheque_number or "N/A", 'bank_name': b_name, 'entity_name': entity_name, 'due_date': chq.cheque_date.strftime('%Y-%m-%d') if chq.cheque_date else "N/A", 'amount': abs(chq.amount)})

    upcoming_travels = []
    next_week = today + timedelta(days=7)
    travels = Booking.query.filter(Booking.company_id == comp_id, or_(Booking.travel_date.between(today, next_week), func.date(Booking.pickup_datetime).between(today, next_week))).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).all()
    for t in travels:
        target_date = t.travel_date or (t.pickup_datetime.date() if t.pickup_datetime else None)
        if target_date:
            days_left = (target_date - today).days
            if 0 <= days_left <= 7:
                upcoming_travels.append({'id': t.id, 'pax': t.passenger_details or 'Unknown', 'service': t.service_type, 'pnr': t.pnr_ref or '-', 'date': target_date.strftime('%Y-%m-%d'), 'days_left': days_left})
                
    cust_id = request.args.get('customer_id')
    sup_id = request.args.get('supplier_id')
    bank_id = request.args.get('bank_id')
    transactions, entity_name, total_debit, total_credit, final_balance, is_customer = [], "", 0, 0, 0, False
    
    if cust_id:
        is_customer = True
        cust = Customer.query.get(cust_id)
        entity_name = cust.name if cust else "Unknown"
        # Opening Balance as first transaction
        if cust and (cust.opening_balance or 0) != 0:
            ob = float(cust.opening_balance or 0)
            ob_desc = cust.ob_remarks or 'Opening Balance'
            transactions.append({'date': datetime(2000, 1, 1), 'type': 'Opening Balance', 'ref': ob_desc, 'desc': '', 'debit': ob if ob > 0 else 0, 'credit': abs(ob) if ob < 0 else 0, 'is_ob': True})
        for inv in Invoice.query.filter_by(company_id=comp_id, customer_id=cust_id).all():
            _linked = Booking.query.filter_by(invoice_id=inv.id, company_id=comp_id).options(*[defer(c) for c in _BOOKING_DEFER]).all()
            _desc = '; '.join(_booking_desc(b) for b in _linked) if _linked else (inv.remarks or '')
            transactions.append({'date': inv.date_created, 'type': 'Invoice / Billing', 'ref': f'INV-{inv.id}', 'desc': _desc, 'debit': inv.total_amount, 'credit': 0})
        for rec in Ledger.query.filter_by(company_id=comp_id, customer_id=cust_id).all():
            _rcptno = rec.receipt_number or f'RCP-{rec.id}'
            _rbase = (rec.remarks or '').strip() or (rec.payment_mode or '')
            _rdesc = f"Receipt No: {_rcptno}" + (f" — {_rbase}" if _rbase else "")
            if rec.amount > 0: transactions.append({'date': rec.date_created, 'type': 'Receipt / Collection', 'ref': rec.transaction_type, 'desc': _rdesc, 'debit': 0, 'credit': rec.amount})
            else: transactions.append({'date': rec.date_created, 'type': 'Refund Given', 'ref': rec.transaction_type, 'desc': _rdesc, 'debit': 0, 'credit': abs(rec.amount)})
    elif sup_id:
        sup = Supplier.query.get(sup_id)
        entity_name = sup.name if sup else "Unknown"
        # Opening Balance as first transaction
        if sup and (sup.opening_balance or 0) != 0:
            ob = float(sup.opening_balance or 0)
            ob_desc = sup.ob_remarks or 'Opening Balance'
            transactions.append({'date': datetime(2000, 1, 1), 'type': 'Opening Balance', 'ref': ob_desc, 'desc': '', 'debit': 0 if ob > 0 else abs(ob), 'credit': ob if ob > 0 else 0, 'is_ob': True})
        # Bills payable: every booking sourced to this supplier (this includes direct-invoice
        # line costs, since those are stored as bookings with supplier_id + net_cost). Posted as
        # CREDIT so the supplier statement shows WHAT we owe, not just the payments against it.
        for bk in Booking.query.filter(Booking.company_id == comp_id, Booking.supplier_id == sup_id, Booking.net_cost > 0).options(*[defer(c) for c in _BOOKING_DEFER]).all():
            transactions.append({'date': bk.date_created or datetime(2000, 1, 1), 'type': 'Bill / Purchase', 'ref': f'BILL-{bk.id}', 'desc': _booking_desc(bk), 'debit': 0, 'credit': bk.net_cost or 0})
        for pay in Ledger.query.filter_by(company_id=comp_id, supplier_id=sup_id).all():
            _pcptno = pay.receipt_number or f'RCP-{pay.id}'
            _pbase = (pay.remarks or '').strip() or (pay.payment_mode or '')
            _pdesc = f"Receipt No: {_pcptno}" + (f" — {_pbase}" if _pbase else "")
            if pay.amount < 0: transactions.append({'date': pay.date_created, 'type': 'Payment to Supplier', 'ref': pay.transaction_type, 'desc': _pdesc, 'debit': abs(pay.amount), 'credit': 0})
            else: transactions.append({'date': pay.date_created, 'ref': pay.transaction_type, 'type': 'Refund from Supplier', 'desc': _pdesc, 'debit': pay.amount, 'credit': 0})
    elif bank_id:
        bank = BankAccount.query.get(bank_id)
        entity_name = f"{bank.bank_name} ({bank.account_number or bank.card_ref})" if bank else "Unknown Bank"
        if bank and bank.opening_balance:
            transactions.append({'date': datetime(2000, 1, 1), 'type': 'Opening Balance', 'ref': 'Account Initialization', 'desc': '', 'debit': bank.opening_balance if bank.opening_balance > 0 else 0, 'credit': abs(bank.opening_balance) if bank.opening_balance < 0 else 0})
        for rec in Ledger.query.filter_by(company_id=comp_id, bank_account_id=bank_id).all():
            _bcptno = rec.receipt_number or f'RCP-{rec.id}'
            _bbase = (rec.remarks or '').strip() or (rec.payment_mode or '')
            _bdesc = f"Receipt No: {_bcptno}" + (f" — {_bbase}" if _bbase else "")
            if rec.amount > 0: transactions.append({'date': rec.date_created, 'type': 'Deposit', 'ref': rec.transaction_type, 'desc': _bdesc, 'debit': rec.amount, 'credit': 0})
            else: transactions.append({'date': rec.date_created, 'type': 'Withdrawal', 'ref': rec.transaction_type, 'desc': _bdesc, 'debit': 0, 'credit': abs(rec.amount)})

    if transactions:
        transactions.sort(key=lambda x: x['date'])
        final_balance = 0
        for t in transactions:
            if not t.get('is_ob'):  # OB already reflected in balance column
                total_debit += t['debit']
                total_credit += t['credit']
            if cust_id: final_balance += (t['debit'] - t['credit'])
            elif sup_id: final_balance += (t['credit'] - t['debit'])
            elif bank_id: final_balance += (t['debit'] - t['credit'])
            t['balance'] = final_balance
    return render_template('reports.html', balance_sheet=balance_sheet, bs_start_date=bs_start_str, bs_end_date=bs_end_str, fy_start_month=fy_start_month, rev=total_revenue, other_income=other_income, total_other_income=total_other_income, total_income=total_income, cogs=total_cogs, direct_expenses=direct_expenses, exp=total_expenses, profit=net_profit, rec=total_receivables, pay=total_payables, pending_invoices=pending_invoices, pending_payables=pending_payables, tax_reports=tax_reports, customers=Customer.query.filter_by(company_id=comp_id).all(), suppliers=Supplier.query.filter_by(company_id=comp_id).all(), income_by_service=income_by_service, expense_list=expense_list, detailed_expenses=detailed_expenses, upcoming_travels=upcoming_travels, bank_accounts=bank_accounts, room_statuses=room_statuses, car_statuses=car_statuses, car_emis=car_emis, loss_bills=loss_bills, cashbook_entries=cashbook_entries, pdc_cheques=pdc_cheques, bank_reports=bank_reports, cost_centers=cost_centers, service_reports=service_reports, staff_reports=[], doc_expiries=doc_expiries, transactions=transactions, entity_name=entity_name, total_debit=total_debit, total_credit=total_credit, final_balance=final_balance, is_customer=is_customer, stmt_active=bool(transactions), today=today, start_date=pl_start_str, end_date=pl_end_str)

# ==========================================
# STATIC MODULES & API
# ==========================================
@app.route('/refunds', methods=['GET'])
@require_permission('Reports_View')
def refunds(): return render_template('refunds.html')

@app.route('/visaplus')
@require_permission('Visa_View')
def visaplus(): 
    comp_id = session.get('company_id')
    visas = Booking.query.filter_by(company_id=comp_id, service_type='visa').options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).order_by(Booking.id.desc()).all()
    customers = Customer.query.filter_by(company_id=comp_id).all()
    suppliers = Supplier.query.filter_by(company_id=comp_id).all()
    bank_accounts = BankAccount.query.filter_by(company_id=comp_id).all()
    today = datetime.now(timezone.utc).date()
    # Timeline for every visa on the page in one query, handed to the template as JSON
    # so opening the history modal needs no round trip.
    visa_history = {str(v.id): [] for v in visas}
    for h in _visa_history_rows(comp_id, [v.id for v in visas]):
        visa_history.setdefault(str(h.booking_id), []).extend(_visa_history_payload([h]))
    return render_template('visaplus.html', visas=visas, customers=customers, suppliers=suppliers, bank_accounts=bank_accounts, visa_types=get_visa_types(comp_id), today=today, visa_history=visa_history)

def _get_visa(id, comp_id):
    """A visa of this company, or None. Every visa API below goes through this so a
    booking id from another tenant (or a non-visa booking) can never be touched."""
    return Booking.query.filter_by(id=id, company_id=comp_id, service_type='visa').options(
        *[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]
    ).first()

@app.route('/api/visa_history/<int:id>', methods=['GET'])
@require_permission('Visa_View')
def api_visa_history(id):
    comp_id = session.get('company_id')
    if not _get_visa(id, comp_id):
        return jsonify({'success': False, 'message': 'Visa not found'}), 404
    return jsonify({'success': True, 'history': _visa_history_payload(_visa_history_rows(comp_id, [id]))})

@app.route('/api/visa_note/<int:id>', methods=['POST'])
@require_permission('Visa_Edit')
def api_visa_note(id):
    """Follow-up note: the log line, the status change and the approved visa number
    are one transaction, so the timeline can never disagree with the row."""
    comp_id = session.get('company_id')
    visa = _get_visa(id, comp_id)
    if not visa:
        return jsonify({'success': False, 'message': 'Visa not found'}), 404
    data = request.json or {}
    note = (data.get('note') or '').strip()
    status = (data.get('status') or '').strip()
    visa_number = (data.get('visa_number') or '').strip()[:100]
    if status and status not in VISA_NOTE_STATUSES:
        return jsonify({'success': False, 'message': 'Unknown status'}), 400
    if not note and not status:
        return jsonify({'success': False, 'message': 'Enter a note or change the status.'}), 400

    msg = 'Update: ' + (note or 'Status changed')
    if status:
        msg += f' [Status -> {status}]'
        visa.pnr_status = status
    if status == 'Approved' and visa_number:
        # The approved number is the booking's reference from here on, so it shows on
        # the row, in search, and on the invoice — not only in this browser.
        dup = Booking.query.with_entities(Booking.id).filter(
            Booking.company_id == comp_id, Booking.pnr_ref == visa_number, Booking.id != visa.id).first()
        if dup:
            return jsonify({'success': False, 'message': f'Visa number {visa_number} is already used on booking #{dup[0]}.'}), 400
        visa.pnr_ref = visa_number
        msg += f' [Visa No: {visa_number}]'
    try:
        log_visa_history(visa.id, comp_id, msg)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    return jsonify({'success': True, 'status': visa.pnr_status, 'visa_number': visa.pnr_ref or '',
                    'history': _visa_history_payload(_visa_history_rows(comp_id, [id]))})

@app.route('/api/visa_renew/<int:id>', methods=['POST'])
@require_permission('Visa_Edit')
def api_visa_renew(id):
    comp_id = session.get('company_id')
    visa = _get_visa(id, comp_id)
    if not visa:
        return jsonify({'success': False, 'message': 'Visa not found'}), 404
    data = request.json or {}
    try:
        amount = float(data.get('amount') or 0)
    except (TypeError, ValueError):
        amount = 0.0
    try:
        cost = float(data.get('cost') or 0)
    except (TypeError, ValueError):
        cost = 0.0
    if amount <= 0:
        return jsonify({'success': False, 'message': 'Enter the renewal selling price.'}), 400
    status = (data.get('status') or '').strip()
    if status and status not in VISA_RENEWAL_STATUSES:
        return jsonify({'success': False, 'message': 'Unknown renewal status'}), 400
    source = (data.get('source') or 'None').strip()[:150]
    visa.pnr_status = status or visa.pnr_status
    try:
        log_visa_history(visa.id, comp_id,
                         f'Visa renewal processed. Sell price: {amount:.2f}. Cost: {cost:.2f}. '
                         f'Paid via: {source}. Status: {visa.pnr_status}')
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    return jsonify({'success': True, 'status': visa.pnr_status,
                    'history': _visa_history_payload(_visa_history_rows(comp_id, [id]))})

@app.route('/api/visa_history/import', methods=['POST'])
@require_permission('Visa_Edit')
def api_visa_history_import():
    """One-time rescue of timelines written by the old browser-only version.

    Only ids that really belong to this company are taken, and only where the visa has
    no server history yet, so running it twice cannot duplicate anything."""
    comp_id = session.get('company_id')
    entries = (request.json or {}).get('entries') or {}
    if not isinstance(entries, dict) or not entries:
        return jsonify({'success': True, 'imported': 0})
    wanted = []
    for k in entries.keys():
        try: wanted.append(int(k))
        except (TypeError, ValueError): continue
    if not wanted:
        return jsonify({'success': True, 'imported': 0})
    valid = {b[0] for b in Booking.query.with_entities(Booking.id).filter(
        Booking.company_id == comp_id, Booking.service_type == 'visa', Booking.id.in_(wanted)).all()}
    already = {h[0] for h in VisaHistory.query.with_entities(VisaHistory.booking_id).filter(
        VisaHistory.company_id == comp_id, VisaHistory.booking_id.in_(list(valid) or [0])).distinct().all()} if valid else set()
    imported = 0
    for bid in valid - already:
        items = entries.get(str(bid)) or entries.get(bid) or []
        if not isinstance(items, list):
            continue
        for item in reversed(items[:100]):   # stored newest-first; insert oldest-first
            if not isinstance(item, dict):
                continue
            desc = (item.get('desc') or '').strip()
            if not desc:
                continue
            when = None
            try:
                when = datetime.strptime((item.get('time') or '').strip(), '%d %b %Y, %H:%M')
            except (TypeError, ValueError):
                when = None
            log_visa_history(bid, comp_id, desc, username=(item.get('user') or 'imported'), when=when)
            imported += 1
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    return jsonify({'success': True, 'imported': imported})

@app.route('/visa_action/<int:id>', methods=['POST'])
@require_permission('Visa_Edit')
def visa_action(id):
    comp_id = session.get('company_id')
    visa = Booking.query.filter_by(id=id, company_id=comp_id).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]).first()
    if visa:
        action = request.form.get('action')
        if action == 'arrived':
            arr_date_str = request.form.get('arrival_date')
            if arr_date_str:
                arr_date = datetime.strptime(arr_date_str, '%Y-%m-%d')
                visa.pickup_datetime = arr_date
                visa.pnr_status = "Arrived"
                # The countdown runs on the duration recorded against this visa. Legacy rows
                # that only ever had a combined label ("Tourist 60 Days") fall back to the
                # number inside it, and 30 days only if there is nothing to read at all.
                days = visa.visa_duration_days
                if not days:
                    _m = re.search(r'(\d+)', str(visa.carrier_code or ''))
                    days = int(_m.group(1)) if _m else 30
                    visa.visa_duration_days = days
                visa.dropoff_datetime = arr_date + timedelta(days=days)
                log_visa_history(visa.id, comp_id,
                                 f'Passenger arrived. Airport stamp date: {arr_date.strftime("%d %b %Y")}. '
                                 f'Stay of {days} days tracked to {visa.dropoff_datetime.strftime("%d %b %Y")}.')
        elif action == 'exited':
            visa.pnr_status = "Exit Completed"
            log_visa_history(visa.id, comp_id, 'Exit confirmed. Passenger has left the country.')
        db.session.commit()
    return redirect(url_for('visaplus'))

@app.route('/api/staff_kpi')
@require_permission('Admin')
def staff_kpi():
    comp_id = session.get('company_id')
    is_super = session.get('role') == 'Super Admin'
    if is_super:
        return jsonify({'success': False, 'message': 'KPI not available for Super Admin'})

    period = request.args.get('period', 'all')
    now = datetime.utcnow()
    date_from = None
    if period == 'this_month':
        date_from = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == 'last_month':
        first_this = now.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        date_from = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        now = first_this
    elif period == 'this_year':
        date_from = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    users = User.query.filter_by(company_id=comp_id).all()
    kpi_data = []

    for u in users:
        q = Booking.query.filter_by(company_id=comp_id, created_by=u.id)
        if date_from:
            q = q.filter(Booking.date_created >= date_from)
        if period == 'last_month':
            q = q.filter(Booking.date_created < now)
        bookings = q.all()
        total_bookings = len(bookings)
        invoice_ids = set(b.invoice_id for b in bookings if b.invoice_id)
        total_invoices = len(invoice_ids)
        total_revenue = sum(float(b.selling_price or 0) for b in bookings)
        total_cost = sum(float(b.net_cost or 0) for b in bookings)
        total_profit = total_revenue - total_cost
        service_breakdown = {}
        for b in bookings:
            stype = b.service_type or 'other'
            service_breakdown[stype] = service_breakdown.get(stype, 0) + 1
        kpi_data.append({
            'user_id': u.id,
            'name': u.full_name or u.username,
            'username': u.username,
            'role': u.role or '-',
            'status': u.status,
            'total_bookings': total_bookings,
            'total_invoices': total_invoices,
            'total_revenue': round(total_revenue, 2),
            'total_cost': round(total_cost, 2),
            'total_profit': round(total_profit, 2),
            'service_breakdown': service_breakdown,
        })

    return jsonify({'success': True, 'kpi': kpi_data})


@app.route('/staffmanagement')
@require_permission('Admin')
def staffmanagement(): 
    is_super = session.get('role') == 'Super Admin'
    comp_id = session.get('company_id')
    
    all_companies = []
    user_count = 0
    user_limit = 0
    plan_label = ''
    
    if is_super:
        users = User.query.order_by(User.company_id.asc(), User.id.asc()).all()
        companies_dict = {c.id: c.company_name for c in Company.query.all()}
        for u in users:
            u.company_name = companies_dict.get(u.company_id, 'Unknown')
        all_companies = Company.query.filter(Company.company_name != '__SAAS_ADMIN__').order_by(Company.company_name.asc()).all()
    else:
        users = User.query.filter_by(company_id=comp_id).all()
        for u in users:
            u.company_name = None
        
        # Compute current usage and limit
        plan_limits = {'trial': 3, 'gold': 3, 'platinum': 999999}
        comp = Company.query.get(comp_id)
        if comp:
            plan_label = (comp.plan or 'trial').title()
            user_limit = plan_limits.get(comp.plan, 3)
        user_count = len(users)
    
    return render_template('staffmanagement.html', 
                           users=users, 
                           is_super=is_super, 
                           all_companies=all_companies,
                           user_count=user_count,
                           user_limit=user_limit,
                           plan_label=plan_label)

@app.route('/crm')
@require_permission('CRM_View')
def crm(): return render_template('crm.html')

@app.route('/api/check_setup')
def check_setup(): return jsonify({"status": "ready", "company_name": "ServiceBook SaaS"})

# Maps an ISO currency code to a display symbol so the currency a user picks at
# account creation is stored and shown consistently across invoices, receipts and reports.
CURRENCY_SYMBOLS = {
    'USD': '$', 'EUR': '€', 'GBP': '£', 'INR': '₹', 'JPY': '¥', 'CNY': '¥',
    'AED': 'د.إ', 'SAR': 'ر.س', 'OMR': 'ر.ع.', 'QAR': 'ر.ق', 'KWD': 'د.ك',
    'BHD': 'ب.د', 'JOD': 'د.ا', 'EGP': 'E£', 'PKR': '₨', 'BDT': '৳',
    'LKR': 'Rs', 'NPR': '₨', 'AUD': 'A$', 'CAD': 'C$', 'SGD': 'S$',
    'MYR': 'RM', 'PHP': '₱', 'IDR': 'Rp', 'THB': '฿', 'ZAR': 'R',
    'NGN': '₦', 'KES': 'KSh', 'TRY': '₺', 'RUB': '₽', 'BRL': 'R$', 'CHF': 'CHF',
}

def _resolve_currency(data):
    """Pull a currency choice from a request payload and return (code, symbol).
    Accepts 'currency_code' or 'currency'; an explicit 'currency_symbol' wins if sent."""
    code = ((data.get('currency_code') or data.get('currency') or 'USD') or 'USD').upper().strip()[:3]
    symbol = (data.get('currency_symbol') or CURRENCY_SYMBOLS.get(code) or '$')
    return code, symbol

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    from datetime import timedelta
    try:
        expiry = datetime.now(timezone.utc) + timedelta(days=14)
        _ccode, _csym = _resolve_currency(data)
        new_comp = Company(company_name=data.get('company_name'), registration_number="CR-12345678", master_email=data.get('email'), plan='trial', expiry_date=expiry, currency_code=_ccode, currency_symbol=_csym, country_code=((data.get('country_code') or 'US') or 'US').upper()[:2], timezone=data.get('timezone') or 'UTC')
        db.session.add(new_comp); db.session.commit()
        new_branch = Branch(company_id=new_comp.id, branch_name="Main Branch")
        db.session.add(new_branch); db.session.commit()
        new_user = User(company_id=new_comp.id, username=data.get('username'), password_hash=generate_password_hash(data.get('password')), role="Company Admin")
        db.session.add(new_user); db.session.commit()
        session.permanent = True
        session['user_id'] = new_user.id
        session['username'] = new_user.username
        session['company_id'] = new_comp.id
        session['role'] = new_user.role
        return jsonify({"success": True})
    except Exception as e: return jsonify({"success": False, "message": str(e)})

@app.route('/api/currency_list', methods=['GET'])
def currency_list():
    """Supported currencies so a settings/signup dropdown can be populated."""
    return jsonify({"success": True, "currencies": [{"code": c, "symbol": s} for c, s in sorted(CURRENCY_SYMBOLS.items())]})

@app.route('/api/set_currency', methods=['POST'])
def set_currency():
    """Set/change the company currency. Lets companies that were created before a
    currency was captured (and so default to '$') switch to their real currency."""
    comp_id = session.get('company_id')
    if not comp_id:
        return jsonify({"success": False, "message": "Not logged in"}), 401
    if not (has_permission('Admin') or 'admin' in (session.get('role') or '').lower()):
        return jsonify({"success": False, "message": "Admin access required"}), 403
    data = request.json or {}
    if not (data.get('currency_code') or data.get('currency') or data.get('currency_symbol')):
        return jsonify({"success": False, "message": "currency_code is required"})
    comp = Company.query.get(comp_id)
    if not comp:
        return jsonify({"success": False, "message": "Company not found"})
    code, symbol = _resolve_currency(data)
    comp.currency_code = code
    comp.currency_symbol = symbol
    db.session.commit()
    return jsonify({"success": True, "currency_code": comp.currency_code, "currency_symbol": comp.currency_symbol})

def _customer_duplicate(comp_id, name=None, phone=None, id_number=None, exclude_id=None):
    """Req #4 duplicate rule: block if the ID number, phone, OR name already exists
    for this company. Returns (customer, message) for the first field that collides,
    else (None, None) so callers can raise a clear, specific error."""
    base = Customer.query.filter(Customer.company_id == comp_id)
    if exclude_id:
        base = base.filter(Customer.id != exclude_id)
    idn = (id_number or '').strip()
    if idn:
        m = base.filter(Customer.id_number == idn).first()
        if m: return m, f"ID number '{idn}' already belongs to customer '{m.name}' (#{m.id})."
    ph = (phone or '').strip()
    if ph:
        m = base.filter(Customer.phone == ph).first()
        if m: return m, f"Phone '{ph}' already belongs to customer '{m.name}' (#{m.id})."
    nm = (name or '').strip()
    if nm:
        m = base.filter(db.func.lower(Customer.name) == nm.lower()).first()
        if m: return m, f"A customer named '{m.name}' (#{m.id}) already exists."
    return None, None

def _save_customer_document(customer_id, data_url, comp_id):
    """Persist an optional ID document (base64 data URL) to the company photo dir; return filename."""
    raw = decode_image_field(data_url)
    if not raw:
        return None
    try:
        head = (data_url or '')[:40].lower()
        ext = 'png' if 'png' in head else ('pdf' if 'pdf' in head else 'jpg')
        upload_dir = get_company_photo_path(comp_id)
        filename = f"customer_{customer_id}_iddoc.{ext}"
        with open(os.path.join(upload_dir, filename), 'wb') as f:
            f.write(raw)
        return filename
    except Exception as e:
        app.logger.error(f"Customer document save failed: {e}")
        return None

@app.route('/api/search_customer')
def search_customer():
    """Look up customers by name, phone, or ID number. Shared by the "Search existing"
    box in Add Customer AND the searchable customer dropdowns on the Booking and
    Invoice pages. A short/blank query returns a starter list so the dropdown isn't
    empty when first opened."""
    comp_id = session.get('company_id')
    if not comp_id:
        return jsonify([])
    q = (request.args.get('q') or '').strip()
    base = Customer.query.filter(Customer.company_id == comp_id)
    if len(q) < 2:
        rows = base.order_by(Customer.name.asc()).limit(30).all()
    else:
        like = f"%{q}%"
        rows = base.filter(
            db.or_(Customer.name.ilike(like), Customer.phone.ilike(like), Customer.id_number.ilike(like))
        ).order_by(Customer.name.asc()).limit(25).all()
    def _adv(c):
        b = c.balance or 0.0
        return abs(b) if b < 0 else 0.0
    return jsonify([{'id': c.id, 'name': c.name, 'phone': c.phone or '',
                     'id_number': c.id_number or '', 'id_type': c.id_type or '',
                     'contact_person': c.contact_person or '',
                     'credit_limit': c.credit_limit or 0.0,
                     'balance': c.balance or 0.0,
                     'advance_amount': _adv(c)} for c in rows])

@app.route('/customer_document/<int:customer_id>')
def customer_document(customer_id):
    comp_id = session.get('company_id')
    c = Customer.query.filter_by(id=customer_id, company_id=comp_id).first()
    if not c or not c.id_document:
        return "No document", 404
    full = os.path.join(get_company_photo_path(comp_id), c.id_document)
    if os.path.exists(full):
        from flask import send_file
        return send_file(full)
    return "Not found", 404

@app.route('/api/add_customer', methods=['POST'])
@require_permission('Accounts_Add')
def add_customer():
    try:
        comp_id = session['company_id']
        name = (request.json.get('name') or '').strip()
        if not name:
            return jsonify({"success": False, "message": "Customer name cannot be empty"})
        phone = (request.json.get('phone') or '').strip()
        id_number = (request.json.get('id_number') or '').strip()
        id_type = (request.json.get('id_type') or '').strip()
        dup, msg = _customer_duplicate(comp_id, name=name, phone=phone, id_number=id_number)
        if dup:
            return jsonify({"success": False, "message": msg, "existing_id": dup.id})
        ob = float(request.json.get('opening_balance') or 0.0)
        ob_remarks = (request.json.get('ob_remarks') or '').strip()
        cust = Customer(company_id=comp_id, name=name, phone=(phone or None),
                        contact_person=request.json.get('contact_person'),
                        opening_balance=ob, ob_remarks=(ob_remarks or None),
                        credit_limit=float(request.json.get('credit_limit') or 0.0), balance=ob,
                        id_type=(id_type or None), id_number=(id_number or None))
        db.session.add(cust)
        db.session.flush()
        doc = request.json.get('id_document_data')
        if doc:
            cust.id_document = _save_customer_document(cust.id, doc, comp_id)
        db.session.commit()
        return jsonify({"success": True, "id": cust.id, "name": cust.name})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/add_supplier', methods=['POST'])
@require_permission('Accounts_Add')
def add_supplier():
    try:
        comp_id = session['company_id']
        name = (request.json.get('name') or '').strip()
        if not name: return jsonify({"success": False, "message": "Supplier name cannot be empty"})
        existing = Supplier.query.filter(Supplier.company_id == comp_id, db.func.lower(Supplier.name) == name.lower()).first()
        if existing: return jsonify({"success": False, "message": f"A supplier named '{existing.name}' already exists"})
        ob = float(request.json.get('opening_balance') or 0.0)
        ob_remarks = (request.json.get('ob_remarks') or '').strip()
        db.session.add(Supplier(company_id=comp_id, name=name, contact_info=request.json.get('contact'), contact_person=request.json.get('contact_person'), opening_balance=ob, ob_remarks=(ob_remarks or None), credit_limit=float(request.json.get('credit_limit') or 0.0), balance=ob))
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback(); return jsonify({"success": False, "message": str(e)})

@app.route('/api/add_bank_account', methods=['POST'])
@require_permission('Accounts_Add')
def add_bank_account():
    try:
        ob = float(request.json.get('opening_balance') or 0)
        db.session.add(BankAccount(company_id=session['company_id'], bank_name=request.json.get('bank_name'), account_number=request.json.get('account_number'), card_ref=request.json.get('card_ref'), opening_balance=ob, balance=ob))
        db.session.commit(); return jsonify({"success": True})
    except Exception as e: return jsonify({"success": False, "message": str(e)})

@app.route('/api/add_capital_account', methods=['POST'])
@require_permission('Accounts_Add')
def add_capital_account():
    """Create a Capital / Loan account holder. The capital deposit is the opening
    fund, so the live balance starts there."""
    try:
        comp_id = session['company_id']
        name = (request.json.get('name') or '').strip()
        if not name:
            return jsonify({"success": False, "message": "Account holder name cannot be empty"})
        existing = CapitalAccount.query.filter(CapitalAccount.company_id == comp_id,
                                               db.func.lower(CapitalAccount.name) == name.lower()).first()
        if existing:
            return jsonify({"success": False, "message": f"A capital/loan account named '{existing.name}' already exists"})
        deposit = float(request.json.get('capital_deposit') or 0.0)
        db.session.add(CapitalAccount(
            company_id=comp_id, name=name,
            phone=(request.json.get('phone') or '').strip() or None,
            account_type=(request.json.get('account_type') or 'Capital'),
            capital_deposit=deposit, balance=deposit,
            remarks=(request.json.get('remarks') or '').strip() or None))
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback(); return jsonify({"success": False, "message": str(e)})

@app.route('/api/visa_types', methods=['GET'])
@require_permission('Booking_View')
def api_visa_types():
    comp_id = session.get('company_id')
    return jsonify([{'id': t.id, 'name': t.name, 'default_days': t.default_days or 30}
                    for t in get_visa_types(comp_id)])

@app.route('/api/add_visa_type', methods=['POST'])
@require_permission('Booking_Add')
def add_visa_type():
    comp_id = session.get('company_id')
    data = request.json or {}
    name = (data.get('name') or '').strip()[:100]
    days = _visa_days(data.get('default_days'))
    if not name:
        return jsonify({"success": False, "message": "Visa type name is required"})
    if not days:
        return jsonify({"success": False, "message": "Duration in days is required"})
    # The name is the identity: re-adding an existing one returns it rather than creating
    # a duplicate, so picking that name always resolves to one known duration.
    existing = VisaType.query.filter(VisaType.company_id == comp_id,
                                     func.lower(VisaType.name) == name.lower()).first()
    if existing:
        return jsonify({"success": True, "existing": True, "id": existing.id,
                        "name": existing.name, "default_days": existing.default_days or 30,
                        "message": f"'{existing.name}' already exists ({existing.default_days or 30} days)."})
    try:
        vt = VisaType(company_id=comp_id, name=name, default_days=days)
        db.session.add(vt)
        db.session.commit()
        return jsonify({"success": True, "existing": False, "id": vt.id,
                        "name": vt.name, "default_days": vt.default_days})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/add_service', methods=['POST'])
@require_permission('Accounts_Add')
def add_service():
    try:
        db.session.add(ServiceItem(company_id=session['company_id'], name=request.json.get('name'), category=request.json.get('category')))
        db.session.commit(); return jsonify({"success": True})
    except Exception as e: return jsonify({"success": False, "message": str(e)})

@app.route('/api/add_vehicle', methods=['POST'])
@require_permission('Accounts_Add')
def add_vehicle():
    data = request.json
    try:
        comp_id = session['company_id']
        reg_exp_str = data.get('reg_expiry')
        ins_exp_str = data.get('insurance_expiry')
        reg_exp = datetime.strptime(reg_exp_str, '%Y-%m-%d').date() if reg_exp_str else None
        ins_exp = datetime.strptime(ins_exp_str, '%Y-%m-%d').date() if ins_exp_str else None

        is_external = str(data.get('is_external')) in ('1', 'true', 'True', 'yes', 'on')
        sup_id = _safe_id(data.get('supplier_id')) if is_external else None
        if is_external and not sup_id:
            return jsonify({"success": False, "message": "Pick the external supplier first (add it under Suppliers/Vendors if it is missing)."})

        veh = Vehicle(company_id=comp_id, name=data.get('name'), registration_number=data.get('reg_number'),
                      reg_expiry=reg_exp, ins_expiry=ins_exp, insurance_provider=data.get('insurance_provider'),
                      bank_name=data.get('bank_name'), loan_account_no=data.get('loan_account_no'),
                      total_loan_balance=float(data.get('loan_balance') or 0.0), monthly_emi=float(data.get('monthly_emi') or 0.0),
                      status="Available", is_external=is_external, supplier_id=sup_id)
        db.session.add(veh)
        db.session.flush()

        # If external and an opening rent rate was supplied, record the first agreement.
        if is_external:
            rent_amount = float(data.get('supplier_rent_amount') or 0.0)
            if rent_amount > 0:
                db.session.add(VehicleRentAgreement(
                    company_id=comp_id, vehicle_id=veh.id, supplier_id=sup_id,
                    rent_amount=rent_amount, rent_period=_clean_period(data.get('supplier_rent_period')),
                    start_date=_parse_date(data.get('agreement_start')),
                    end_date=_parse_date(data.get('agreement_end'))))
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e: return jsonify({"success": False, "message": str(e)})

@app.route('/api/vehicle_agreements')
@require_permission('Accounts_View')
def vehicle_agreements():
    """List every supplier-rent agreement for a vehicle (newest first)."""
    comp_id = session.get('company_id')
    veh_id = _safe_id(request.args.get('vehicle_id'))
    veh = Vehicle.query.filter_by(id=veh_id, company_id=comp_id).first()
    if not veh:
        return jsonify({"success": False, "message": "Vehicle not found"})
    sup_names = {s.id: s.name for s in Supplier.query.filter_by(company_id=comp_id).all()}
    ags = VehicleRentAgreement.query.filter_by(company_id=comp_id, vehicle_id=veh_id)\
        .order_by(VehicleRentAgreement.id.desc()).all()
    current = _applicable_agreement(veh_id, comp_id)
    return jsonify({
        "success": True,
        "vehicle_name": veh.name,
        "is_external": bool(veh.is_external),
        "supplier_id": veh.supplier_id,
        "supplier_name": sup_names.get(veh.supplier_id, '') if veh.supplier_id else '',
        "current_agreement_id": current.id if current else None,
        "agreements": [{
            "id": a.id,
            "supplier_id": a.supplier_id,
            "supplier_name": sup_names.get(a.supplier_id, '—'),
            "rent_amount": float(a.rent_amount or 0.0),
            "rent_period": a.rent_period or 'daily',
            "start_date": a.start_date.isoformat() if a.start_date else '',
            "end_date": a.end_date.isoformat() if a.end_date else '',
        } for a in ags]
    })

@app.route('/api/add_vehicle_agreement', methods=['POST'])
@require_permission('Accounts_Add')
def add_vehicle_agreement():
    """Record a new/updated supplier-rent agreement (rate + period + date range)
    for an external vehicle."""
    data = request.json or {}
    comp_id = session.get('company_id')
    try:
        veh_id = _safe_id(data.get('vehicle_id'))
        veh = Vehicle.query.filter_by(id=veh_id, company_id=comp_id).first()
        if not veh:
            return jsonify({"success": False, "message": "Vehicle not found"})
        sup_id = _safe_id(data.get('supplier_id')) or veh.supplier_id
        if not sup_id:
            return jsonify({"success": False, "message": "Pick the external supplier first (add it under Suppliers/Vendors if it is missing)."})
        rent_amount = float(data.get('rent_amount') or 0.0)
        if rent_amount <= 0:
            return jsonify({"success": False, "message": "Enter the rent payable amount."})
        db.session.add(VehicleRentAgreement(
            company_id=comp_id, vehicle_id=veh_id, supplier_id=sup_id,
            rent_amount=rent_amount, rent_period=_clean_period(data.get('rent_period')),
            start_date=_parse_date(data.get('start_date')), end_date=_parse_date(data.get('end_date'))))
        # Keep the vehicle flagged external and pointed at this supplier.
        veh.is_external = True
        veh.supplier_id = sup_id
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/delete_vehicle_agreement', methods=['POST'])
@require_permission('Accounts_Add')
def delete_vehicle_agreement():
    data = request.json or {}
    comp_id = session.get('company_id')
    try:
        ag = VehicleRentAgreement.query.filter_by(id=_safe_id(data.get('id')), company_id=comp_id).first()
        if not ag:
            return jsonify({"success": False, "message": "Agreement not found"})
        db.session.delete(ag)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/vehicle_booking_info')
@require_permission('Booking_View')
def vehicle_booking_info():
    """Everything the booking form needs when a vehicle is picked:
    the external supplier + governing rent agreement for the pickup date (so the
    net cost can auto-calculate), whether that agreement has expired, and the last
    closing odometer / fuel stage from this vehicle's most recent finalized rental
    (so the new OUT reading pre-fills to where the car came back)."""
    comp_id = session.get('company_id')
    veh_id = _safe_id(request.args.get('vehicle_id'))
    veh = Vehicle.query.filter_by(id=veh_id, company_id=comp_id).first()
    if not veh:
        return jsonify({"success": False, "message": "Vehicle not found"})

    on_date = _parse_date(request.args.get('on_date')) or date.today()
    ag = _applicable_agreement(veh_id, comp_id, on_date) if veh.is_external else None
    has_any = VehicleRentAgreement.query.filter_by(company_id=comp_id, vehicle_id=veh_id).count() > 0

    supplier = Supplier.query.filter_by(id=veh.supplier_id, company_id=comp_id).first() if veh.supplier_id else None

    # Last closing reading for this vehicle (most recent finalized rental).
    last = Booking.query.filter(
        Booking.company_id == comp_id, Booking.vehicle_id == veh_id,
        Booking.service_type == 'rentacar', Booking.booking_finalized == True
    ).options(*[defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]])\
     .order_by(Booking.id.desc()).first()

    return jsonify({
        "success": True,
        "is_external": bool(veh.is_external),
        "supplier_id": veh.supplier_id,
        "supplier_name": supplier.name if supplier else '',
        "agreement": ({
            "id": ag.id,
            "rent_amount": float(ag.rent_amount or 0.0),
            "rent_period": ag.rent_period or 'daily',
            "start_date": ag.start_date.isoformat() if ag.start_date else '',
            "end_date": ag.end_date.isoformat() if ag.end_date else '',
        } if ag else None),
        # Expired only makes sense for an external car that has agreement(s) but none cover the date.
        "agreement_expired": bool(veh.is_external and has_any and not ag),
        "has_agreement": bool(ag),
        "last_km_in": int(last.km_in) if (last and last.km_in) else None,
        "last_fuel_in": (last.fuel_stage_in or None) if last else None,
    })

@app.route('/api/add_room', methods=['POST'])
@require_permission('Accounts_Add')
def add_room():
    data = request.json
    try:
        db.session.add(Room(company_id=session['company_id'], building_id=int(data.get('building_id')), room_number=data.get('room_number'), room_type=data.get('room_type'), electricity_acc=data.get('electricity_acc'), elec_latest_bill=float(data.get('elec_latest_bill') or 0.0), water_acc=data.get('water_acc'), water_latest_bill=float(data.get('water_latest_bill') or 0.0), status="Available"))
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e: return jsonify({"success": False, "message": str(e)})

@app.route('/api/add_building', methods=['POST'])
def add_building():
    data = request.json
    try:
        db.session.add(Building(company_id=session['company_id'], name=data.get('name')))
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e: return jsonify({"success": False, "message": str(e)})

@app.route('/api/save_staff', methods=['POST'])
@require_permission('Admin')
def save_staff():
    data = request.json
    is_super = session.get('role') == 'Super Admin'
    comp_id = session.get('company_id')
    
    try:
        import json
        perms_json = json.dumps(data.get('perms', []))
        
        user = None
        if data.get('db_id'):
            # Edit existing user
            q = User.query.filter_by(id=data.get('db_id'))
            if not is_super:
                q = q.filter_by(company_id=comp_id)
            user = q.first()
            if not user:
                return jsonify({"success": False, "message": "User not found or access denied"})
            target_comp_id = user.company_id
        else:
            # Creating new user — Super Admin can pick the target company, others use their own
            if is_super and data.get('target_company_id'):
                target_comp_id = int(data.get('target_company_id'))
                if not Company.query.get(target_comp_id):
                    return jsonify({"success": False, "message": "Selected company does not exist"})
            else:
                target_comp_id = comp_id
        
        # ────────────────────────────────────────────────────
        # Enforce staff limit based on company's plan
        # Only when CREATING a new user, and not for Super Admin
        # ────────────────────────────────────────────────────
        if not user and not is_super:
            target_company = Company.query.get(target_comp_id)
            if target_company:
                plan_limits = {
                    'trial':     3,
                    'gold':     3,
                    'platinum': 999999,
                }
                limit = plan_limits.get(target_company.plan, 3)
                current_count = User.query.filter_by(company_id=target_comp_id).count()
                
                if current_count >= limit:
                    plan_label = (target_company.plan or 'trial').title()
                    return jsonify({
                        "success": False,
                        "message": f"User limit reached. Your {plan_label} plan allows up to {limit} users. Currently you have {current_count}. Please upgrade your plan to add more staff."
                    })
        
        # Username uniqueness check within the target company
        existing = User.query.filter(
            User.username == data.get('username'),
            User.company_id == target_comp_id,
            User.id != (user.id if user else -1)
        ).first()
        if existing:
            return jsonify({"success": False, "message": "Username already taken in this company"})
        
        if user:
            user.full_name = data.get('name')
            user.username = data.get('username')
            user.email = (data.get('email') or '').strip() or None
            user.role = data.get('role')
            user.status = data.get('status')
            user.permissions = perms_json
            if data.get('password'):
                user.password_hash = generate_password_hash(data.get('password'))
        else:
            if not data.get('password'):
                return jsonify({"success": False, "message": "Password is required for new users"})
            user = User(
                company_id=target_comp_id,
                full_name=data.get('name'),
                username=data.get('username'),
                email=(data.get('email') or '').strip() or None,
                password_hash=generate_password_hash(data.get('password')),
                role=data.get('role'),
                status=data.get('status', 'Active'),
                permissions=perms_json
            )
            db.session.add(user)
        
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/delete_staff/<int:id>', methods=['POST'])
@require_permission('Admin')
def delete_staff(id):
    is_super = session.get('role') == 'Super Admin'
    comp_id = session.get('company_id')
    q = User.query.filter_by(id=id)
    if not is_super: q = q.filter_by(company_id=comp_id)
    user = q.first()
    if not user: return jsonify({"success": False, "message": "Staff not found or access denied"})
    if user.username == 'admin' or user.role == 'Super Admin': return jsonify({"success": False, "message": "Cannot delete primary/Super Admin accounts"})
    db.session.delete(user)
    db.session.commit()
    return jsonify({"success": True})

@app.route('/api/add_company', methods=['POST'])
def add_company():
    if session.get('role') != 'Super Admin':
        return jsonify({"success": False, "message": "Super Admin access required"}), 403
    
    data = request.json or {}
    company_name = (data.get('company_name') or '').strip()
    email = (data.get('master_email') or '').strip()
    plan = data.get('plan') or 'free'
    trial_days = int(data.get('trial_days') or 14)
    
    if not company_name:
        return jsonify({"success": False, "message": "Company name is required"})
    
    if Company.query.filter(db.func.lower(Company.company_name) == company_name.lower()).first():
        return jsonify({"success": False, "message": f"A company named '{company_name}' already exists"})
    
    try:
        # Set expiry based on plan
        if plan == 'trial':
            expiry = datetime.now(timezone.utc) + timedelta(days=trial_days)  # trial expires
        elif plan in ('gold', 'platinum'):
            expiry = datetime.now(timezone.utc) + timedelta(days=365)  # 1-year subscription
        else:
            expiry = datetime.now(timezone.utc) + timedelta(days=trial_days)
        
        comp = Company(
            company_name=company_name,
            master_email=email,
            plan=plan,
            subscription_status='Active',
            expiry_date=expiry,
            currency_code=_resolve_currency(data)[0],
            currency_symbol=_resolve_currency(data)[1],
            date_created=datetime.now(timezone.utc)
        )
        db.session.add(comp)
        db.session.flush()
        
        branch = Branch(company_id=comp.id, branch_name='Main Branch')
        db.session.add(branch)
        
        db.session.commit()
        return jsonify({"success": True, "company_id": comp.id, "company_name": comp.company_name})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)})

@app.route('/api/update_master', methods=['POST'])
def update_master():
    data = request.json
    try:
        action, item_type, item_id, payload = data.get('action'), data.get('type'), data.get('id'), data.get('payload', {})
        comp_id = session.get('company_id')
        model = None
        if item_type == 'customer': model = Customer
        elif item_type == 'supplier': model = Supplier
        elif item_type == 'bank': model = BankAccount
        elif item_type == 'capital': model = CapitalAccount
        elif item_type == 'vehicle': model = Vehicle
        elif item_type == 'room': model = Room
        if not model: return jsonify({"success": False, "message": "Invalid action or type"})
        record = model.query.filter_by(id=item_id, company_id=comp_id).first()
        if not record: return jsonify({"success": False, "message": "Record not found"})
        if action == 'delete':
            # A capital/loan account that already funded transactions must not vanish —
            # deleting it would orphan those ledger rows and break the statement.
            if item_type == 'capital':
                used = Ledger.query.filter_by(capital_account_id=record.id, company_id=comp_id).count()
                if used:
                    return jsonify({"success": False, "message": f"This capital/loan account is used by {used} transaction(s) and cannot be deleted."})
            db.session.delete(record)
        elif action == 'edit':
            # Changing the opening deposit shifts the live balance by the same delta.
            if item_type == 'capital' and 'capital_deposit' in payload:
                try:
                    _new_dep = float(payload.pop('capital_deposit') or 0.0)
                    record.balance = (record.balance or 0.0) + (_new_dep - (record.capital_deposit or 0.0))
                    record.capital_deposit = _new_dep
                except (TypeError, ValueError):
                    payload.pop('capital_deposit', None)
            if item_type == 'customer':
                _dup, _m = _customer_duplicate(comp_id, name=payload.get('name'), phone=payload.get('phone'),
                                               id_number=payload.get('id_number'), exclude_id=record.id)
                if _dup:
                    return jsonify({"success": False, "message": _m})
                _doc = payload.pop('id_document_data', None)
                if _doc:
                    _saved = _save_customer_document(record.id, _doc, comp_id)
                    if _saved: record.id_document = _saved
            for key, value in payload.items():
                if hasattr(record, key):
                    if value == "": value = None
                    if key in ['reg_expiry', 'insurance_expiry', 'ins_expiry'] and value:
                        try: value = datetime.strptime(str(value), '%Y-%m-%d').date()
                        except: value = None
                    if key == 'insurance_expiry': setattr(record, 'ins_expiry', value)
                    elif key == 'reg_number': setattr(record, 'registration_number', value)
                    elif key in ['loan_balance', 'total_loan_balance']: setattr(record, 'total_loan_balance', float(value) if value is not None else 0.0)
                    elif key == 'is_external': setattr(record, 'is_external', str(value) in ('1', 'true', 'True', 'yes', 'on'))
                    elif key == 'supplier_id': setattr(record, 'supplier_id', _safe_id(value))
                    else: setattr(record, key, value)
            # An external vehicle with no supplier makes no sense; keep the two consistent.
            if item_type == 'vehicle' and 'is_external' in payload and not record.is_external:
                record.supplier_id = None
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e: return jsonify({"success": False, "message": str(e)})

def _booking_desc(b):
    """Short human-readable description of a booking, used in account statements."""
    ref = b.pnr_ref if (b.pnr_ref and b.pnr_ref != 'N/A') else f'BK-{b.id}'
    part = (b.service_type or 'Booking').replace('_', ' ').title()
    if b.passenger_details:
        part = f"{part} — {b.passenger_details[:40]}"
    elif b.remarks:
        part = f"{part} — {b.remarks[:40]}"
    return f"{ref}: {part}"

@app.route('/api/account_history')
def account_history():
    try:
        item_type = request.args.get('type')
        item_id = request.args.get('item_id')
        comp_id = session.get('company_id')
        if not item_type or not item_id:
            return jsonify({"success": False, "message": "Invalid request"})

        defer_opts = [defer(col) for col in [Booking.photo1, Booking.photo2, Booking.photo3, Booking.photo4, Booking.driver_id_card]]

        # ---- Capital / Loan account: opening deposit + every transaction funded by it ----
        if item_type == 'capital':
            cap = CapitalAccount.query.filter_by(id=item_id, company_id=comp_id).first()
            if not cap:
                return jsonify({"success": False, "message": "Capital/Loan account not found"})
            leds = Ledger.query.filter_by(capital_account_id=item_id, company_id=comp_id).order_by(Ledger.id.asc()).limit(300).all()
            return jsonify({
                "success": True, "item_type": "capital", "name": cap.name,
                "history": [], "invoices": [], "bills": [],
                "capital": {
                    "name": cap.name, "phone": cap.phone or '',
                    "account_type": cap.account_type or 'Capital',
                    "capital_deposit": float(cap.capital_deposit or 0.0),
                    "balance": float(cap.balance or 0.0),
                    "remarks": cap.remarks or '',
                    "opened_on": cap.date_created.strftime('%Y-%m-%d') if cap.date_created else '',
                },
                "payments": [{
                    'date': l.date_created.strftime('%Y-%m-%d') if l.date_created else '',
                    'description': l.transaction_type or (l.category or 'Transaction'),
                    'amount': float(l.amount or 0), 'mode': l.payment_mode or '',
                    'ref': l.receipt_number or '', 'remarks': l.remarks or ''} for l in leds]
            })

        # ---- Bookings (kept for every account type) ----
        bq = Booking.query.filter_by(company_id=comp_id).options(*defer_opts)
        if item_type == 'customer': bq = bq.filter_by(customer_id=item_id)
        elif item_type == 'supplier': bq = bq.filter_by(supplier_id=item_id)
        elif item_type == 'vehicle': bq = bq.filter_by(vehicle_id=item_id)
        elif item_type == 'room': bq = bq.filter_by(room_id=item_id)
        else: return jsonify({"success": False, "message": "Unsupported history type"})
        bookings = bq.order_by(Booking.id.desc()).limit(50).all()
        history = [{'booking_id': b.id, 'service_type': b.service_type, 'pnr_ref': b.pnr_ref, 'travel_date': b.travel_date.isoformat() if b.travel_date else '', 'return_date': b.return_date.isoformat() if b.return_date else '', 'train_number': b.train_number or '', 'train_name': b.train_name or '', 'visa_country': b.visa_country or '', 'status': 'Finalized' if b.booking_finalized else 'Open', 'remarks': b.remarks or ''} for b in bookings]

        payload = {"success": True, "item_type": item_type, "history": history,
                   "invoices": [], "bills": [], "payments": []}

        # ---- Customer: invoices raised + receipts/refunds recorded ----
        if item_type == 'customer':
            cust = Customer.query.filter_by(id=item_id, company_id=comp_id).first()
            payload['name'] = cust.name if cust else f'Customer #{item_id}'
            if cust and (cust.opening_balance or 0) != 0:
                payload['opening_balance'] = float(cust.opening_balance or 0)
                payload['ob_remarks'] = cust.ob_remarks or 'Opening Balance'
            invoices = Invoice.query.filter_by(customer_id=item_id, company_id=comp_id).order_by(Invoice.date_created.desc()).limit(100).all()
            for inv in invoices:
                linked = Booking.query.filter_by(invoice_id=inv.id, company_id=comp_id).options(*defer_opts).all()
                desc = '; '.join(_booking_desc(b) for b in linked) if linked else (inv.remarks or '—')
                payload['invoices'].append({
                    'invoice_id': inv.id,
                    'date': inv.date_created.strftime('%Y-%m-%d') if inv.date_created else '',
                    'total': float(inv.total_amount or 0), 'paid': float(inv.total_paid or 0),
                    'balance_due': float(inv.balance_due or 0),
                    'description': desc, 'remarks': inv.remarks or ''})
            leds = Ledger.query.filter_by(customer_id=item_id, company_id=comp_id).order_by(Ledger.id.desc()).limit(100).all()
            payload['payments'] = [{
                'date': l.date_created.strftime('%Y-%m-%d') if l.date_created else '',
                'description': l.transaction_type or (l.category or 'Transaction'),
                'amount': float(l.amount or 0), 'mode': l.payment_mode or '',
                'ref': l.receipt_number or '', 'remarks': l.remarks or ''} for l in leds]

        # ---- Supplier: bills payable + payments/refunds recorded ----
        elif item_type == 'supplier':
            sup = Supplier.query.filter_by(id=item_id, company_id=comp_id).first()
            payload['name'] = sup.name if sup else f'Supplier #{item_id}'
            if sup and (sup.opening_balance or 0) != 0:
                payload['opening_balance'] = float(sup.opening_balance or 0)
                payload['ob_remarks'] = sup.ob_remarks or 'Opening Balance'
            bills = Booking.query.filter(Booking.company_id == comp_id, Booking.supplier_id == item_id, Booking.net_cost > 0).options(*defer_opts).order_by(Booking.date_created.desc()).limit(100).all()
            for b in bills:
                paid = b.supplier_paid or 0.0
                payload['bills'].append({
                    'booking_id': b.id,
                    'date': b.date_created.strftime('%Y-%m-%d') if b.date_created else '',
                    'total': float(b.net_cost or 0), 'paid': float(paid),
                    'outstanding': round((b.net_cost or 0.0) - paid, 2),
                    'description': _booking_desc(b), 'remarks': b.remarks or ''})
            leds = Ledger.query.filter_by(supplier_id=item_id, company_id=comp_id).order_by(Ledger.id.desc()).limit(100).all()
            payload['payments'] = [{
                'date': l.date_created.strftime('%Y-%m-%d') if l.date_created else '',
                'description': l.transaction_type or (l.category or 'Transaction'),
                'amount': float(l.amount or 0), 'mode': l.payment_mode or '',
                'ref': l.receipt_number or '', 'remarks': l.remarks or ''} for l in leds]

        # ---- Vehicle: loan summary + the loan/EMI ledger (Req #3 Statement of Account) ----
        elif item_type == 'vehicle':
            veh = Vehicle.query.filter_by(id=item_id, company_id=comp_id).first()
            payload['name'] = (veh.name if veh else f'Vehicle #{item_id}')
            if veh:
                payload['loan'] = {
                    'bank_name': veh.bank_name or '',
                    'loan_account_no': veh.loan_account_no or '',
                    'registration_number': veh.registration_number or '',
                    'total_loan_balance': float(veh.total_loan_balance or 0.0),
                    'monthly_emi': float(veh.monthly_emi or 0.0),
                }
            leds = Ledger.query.filter_by(vehicle_id=item_id, company_id=comp_id).order_by(Ledger.id.desc()).limit(100).all()
            payload['payments'] = [{
                'date': l.date_created.strftime('%Y-%m-%d') if l.date_created else '',
                'description': l.transaction_type or (l.category or 'Transaction'),
                'amount': float(l.amount or 0), 'mode': l.payment_mode or '',
                'ref': l.receipt_number or '', 'remarks': l.remarks or ''} for l in leds]

        return jsonify(payload)
    except Exception as e: return jsonify({"success": False, "message": str(e)})

@app.route('/fix_db')
def fix_db():
    queries = [
        "ALTER TABLE vehicle ADD COLUMN insurance_provider VARCHAR(150);",
        "ALTER TABLE vehicle ADD COLUMN bank_name VARCHAR(150);",
        "ALTER TABLE vehicle ADD COLUMN loan_account_no VARCHAR(100);",
        "ALTER TABLE vehicle ADD COLUMN total_loan_balance FLOAT DEFAULT 0.0;",
        "ALTER TABLE vehicle ADD COLUMN monthly_emi FLOAT DEFAULT 0.0;",
        "ALTER TABLE room ADD COLUMN elec_latest_bill FLOAT DEFAULT 0.0;",
        "ALTER TABLE room ADD COLUMN water_latest_bill FLOAT DEFAULT 0.0;",
        "ALTER TABLE user ADD COLUMN full_name VARCHAR(150);",
        "ALTER TABLE user ADD COLUMN status VARCHAR(20) DEFAULT 'Active';",
        "ALTER TABLE user ADD COLUMN permissions TEXT;",
        "ALTER TABLE ledger ADD COLUMN staff_id INT NULL;",
        "ALTER TABLE booking ADD COLUMN IF NOT EXISTS created_by INT NULL;",
        "ALTER TABLE ledger ADD COLUMN remarks TEXT NULL;",
        "ALTER TABLE room ADD COLUMN current_elec_reading FLOAT DEFAULT 0.0;",
        "ALTER TABLE room ADD COLUMN current_water_reading FLOAT DEFAULT 0.0;",
        "ALTER TABLE booking ADD COLUMN photo1 LONGBLOB;",
        "ALTER TABLE booking ADD COLUMN photo2 LONGBLOB;",
        "ALTER TABLE booking ADD COLUMN photo3 LONGBLOB;",
        "ALTER TABLE booking ADD COLUMN photo4 LONGBLOB;",
        "ALTER TABLE invoice ADD COLUMN tax_rate FLOAT DEFAULT 0.0;",
        "ALTER TABLE invoice ADD COLUMN tax_amount FLOAT DEFAULT 0.0;",
        "ALTER TABLE booking ADD COLUMN driver_id_card LONGBLOB;",
        "ALTER TABLE booking ADD COLUMN supplier_paid FLOAT DEFAULT 0.0;",
        "ALTER TABLE customer ADD COLUMN IF NOT EXISTS opening_balance FLOAT DEFAULT 0.0;",
        "ALTER TABLE customer ADD COLUMN IF NOT EXISTS ob_remarks VARCHAR(255);",
        "ALTER TABLE supplier ADD COLUMN IF NOT EXISTS opening_balance FLOAT DEFAULT 0.0;",
        "ALTER TABLE supplier ADD COLUMN IF NOT EXISTS ob_remarks VARCHAR(255);",
        "ALTER TABLE ledger ADD COLUMN IF NOT EXISTS capital_account_id INT NULL;",
        "UPDATE ledger SET payment_mode = 'POS Payment' WHERE payment_mode = 'Credit Card';",
    ]
    results = []
    for q in queries:
        try:
            db.session.execute(text(q))
            db.session.commit()
            results.append(f"<span style='color: green;'>✅ SUCCESS:</span> {q}")
        except Exception as e:
            db.session.rollback()
            results.append(f"<span style='color: red;'>❌ FAILED:</span> {q} <br> <b style='color: #666;'>Reason:</b> {str(e)}")

    booking_results = ensure_booking_columns()
    for r in booking_results:
        if r.startswith('Added column') or r.endswith('already exists.'): results.append(f"<span style='color: green;'>✅ SUCCESS:</span> {r}")
        else: results.append(f"<span style='color: red;'>❌ FAILED:</span> {r}")
            
    html_output = "<div style='font-family: Arial; padding: 20px;'><h1>Database Fix Diagnostics</h1><ul>"
    for r in results: html_output += f"<li style='margin-bottom: 15px; background: #f4f4f4; padding: 10px; border-radius: 5px;'>{r}</li>"
    html_output += "</ul><div style='margin-top: 20px; display: flex; gap: 10px;'><a href='/migrate_photos' style='display: inline-block; padding: 10px 20px; background: #10b981; color: white; text-decoration: none; border-radius: 5px; font-weight: bold;'>🚀 Migrate Photos to Filesystem</a><a href='/booking' style='display: inline-block; padding: 10px 20px; background: #1e3a8a; color: white; text-decoration: none; border-radius: 5px; font-weight: bold;'>Go to Bookings</a></div></div>"
    return html_output

@app.route('/migrate_photos')
def migrate_photos():
    pass # Add your security logic if needed
    comp_id = session.get('company_id')
    bookings = Booking.query.filter_by(company_id=comp_id).all()
    migrated_count, error_count = 0, 0
    os.makedirs('static/uploads', exist_ok=True)
    for b in bookings:
        updated = False
        for p in ['photo1', 'photo2', 'photo3', 'photo4', 'driver_id_card']:
            data = getattr(b, p)
            if data and isinstance(data, bytes):
                try:
                    text = data.decode('utf-8')
                    if text.endswith('.jpg'): continue
                except: pass
                filename = save_photo_to_disk(b.id, p, data, b.company_id)
                if filename:
                    setattr(b, p, filename.encode('utf-8'))
                    updated = True
                    migrated_count += 1
                else: error_count += 1
        if updated: db.session.commit()
    return f"Migration Complete! Migrated {migrated_count} photos. Errors: {error_count}"

# ==========================================
# SAAS ROUTES REGISTRATION
# ==========================================
register_saas_routes(app, db)

with app.app_context():
    ensure_ledger_columns()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
