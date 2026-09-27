"""
SaaS middleware & Super Admin routes.
Import this module in app.py after app is created.
"""
from flask import session, redirect, url_for, jsonify, request, render_template_string
from functools import wraps
from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash, check_password_hash

# ─────────────────────────────────────────────
# DECORATORS
# ─────────────────────────────────────────────

def super_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'Super Admin':
            return jsonify({"success": False, "message": "Super Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated


def subscription_active(f):
    """Block access if tenant subscription is expired or suspended."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Super Admin bypasses subscription checks
        if session.get('role') == 'Super Admin':
            return f(*args, **kwargs)
        from app import Company
        comp = Company.query.get(session.get('company_id'))
        if comp and not comp.is_active:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({"success": False, "message": "Subscription expired or suspended. Please contact support."}), 403
            return redirect(url_for('subscription_expired'))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────
# REGISTER SAAS ROUTES ON APP
# ─────────────────────────────────────────────

def register_saas_routes(app, db):
    from app import Company, User, Booking, Branch
    try:
        from app import CURRENCY_SYMBOLS, _resolve_currency
    except Exception:
        CURRENCY_SYMBOLS = {'USD': '$'}
        def _resolve_currency(data):
            code = ((data.get('currency_code') or data.get('currency') or 'USD') or 'USD').upper().strip()[:3]
            symbol = (data.get('currency_symbol') or CURRENCY_SYMBOLS.get(code) or '$')
            return code, symbol
    PLAN_LIMITS = {
        'trial':     {'bookings': 999999, 'users': 3,      'branches': 1,      'label': 'Free Trial'},
        'gold':     {'bookings': 999999, 'users': 3,      'branches': 5,      'label': 'Gold'},
        'platinum': {'bookings': 999999, 'users': 999999, 'branches': 999999, 'label': 'Platinum'},
    }

    # ── Subscription expired page ────────────────────────
    @app.route('/subscription-expired')
    def subscription_expired():
        return render_template_string(EXPIRED_PAGE), 403

    # ── Public self-service registration ─────────────────
    @app.route('/signup')
    def signup_page():
        return render_template_string(SIGNUP_PAGE)

    @app.route('/api/signup', methods=['POST'])
    def signup():
        data = request.json or {}
        company_name = (data.get('company_name') or '').strip()
        email = (data.get('email') or '').strip()
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''

        if not all([company_name, email, username, password]):
            return jsonify({"success": False, "message": "All fields are required"})
        if User.query.filter_by(username=username).first():
            return jsonify({"success": False, "message": "Username already taken"})

        try:
            # 14-day trial
            trial_expiry = datetime.now(timezone.utc) + timedelta(days=14)
            # Locale settings chosen on the signup form — persist them so currency,
            # timezone and decimals work everywhere in the app for this tenant.
            ccode, csym = _resolve_currency(data)
            country_code = ((data.get('country_code') or 'US') or 'US').upper()[:2]
            tz = data.get('timezone') or 'UTC'
            try:
                cdec = int(data.get('currency_decimals'))
            except (TypeError, ValueError):
                cdec = 2
            if cdec not in (0, 2, 3):
                cdec = 2
            phone = (data.get('phone') or '').strip()[:40]
            comp = Company(
                company_name=company_name,
                master_email=email,
                plan="trial",
                subscription_status="Active",
                expiry_date=trial_expiry,
                currency_code=ccode,
                currency_symbol=csym,
                currency_decimals=cdec,
                country_code=country_code,
                timezone=tz,
                phone=phone,
            )
            db.session.add(comp); db.session.flush()

            branch = Branch(company_id=comp.id, branch_name="Main Branch")
            db.session.add(branch); db.session.flush()

            import json as _json
            user = User(
                company_id=comp.id,
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                role="Company Admin",
                status="Active",
                permissions=_json.dumps(["Admin"]),
            )
            db.session.add(user)
            db.session.commit()

            session.permanent = True
            session['user_id'] = user.id
            session['username'] = user.username
            session['company_id'] = comp.id
            session['role'] = user.role
            return jsonify({"success": True, "redirect": "/"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"success": False, "message": str(e)})

    # ── Super Admin Dashboard ─────────────────────────────
    @app.route('/superadmin')
    @super_admin_required
    def superadmin_dashboard():
        companies = Company.query.order_by(Company.date_created.desc()).all()
        stats = {
            "total": len(companies),
            "active": sum(1 for c in companies if c.is_active),
            "trial": sum(1 for c in companies if c.plan == "trial"),
            "suspended": sum(1 for c in companies if c.subscription_status == "Suspended"),
        }
        return render_template_string(SUPERADMIN_PAGE, companies=companies, stats=stats, plans=PLAN_LIMITS)

    # ── Super Admin API – list tenants ────────────────────
    @app.route('/api/superadmin/tenants')
    @super_admin_required
    def sa_list_tenants():
        companies = Company.query.order_by(Company.date_created.desc()).all()
        result = []
        for c in companies:
            booking_count = Booking.query.filter_by(company_id=c.id).count()
            user_count = User.query.filter_by(company_id=c.id).count()
            result.append({
                "id": c.id,
                "company_name": c.company_name,
                "master_email": c.master_email,
                "plan": c.plan,
                "plan_label": c.plan_label,
                "status": c.subscription_status,
                "is_active": c.is_active,
                "expiry_date": c.expiry_date.strftime('%Y-%m-%d') if c.expiry_date else "—",
                "days_remaining": c.days_remaining,
                "billing_cycle": c.billing_cycle,
                "bookings": booking_count,
                "users": user_count,
                "date_created": c.date_created.strftime('%Y-%m-%d') if c.date_created else "—",
                "notes": c.notes or "",
                "currency_code": getattr(c, 'currency_code', '') or '',
                "currency_symbol": getattr(c, 'currency_symbol', '') or '',
                "currency_decimals": getattr(c, 'currency_decimals', 2) if getattr(c, 'currency_decimals', 2) is not None else 2,
                "country_code": getattr(c, 'country_code', '') or '',
                "timezone": getattr(c, 'timezone', '') or '',
                "phone": getattr(c, 'phone', '') or '',
            })
        return jsonify(result)

    # ── Super Admin API – update tenant subscription ──────
    @app.route('/api/superadmin/tenant/<int:company_id>', methods=['POST'])
    @super_admin_required
    def sa_update_tenant(company_id):
        comp = Company.query.get_or_404(company_id)
        data = request.json or {}

        if 'plan' in data:
            comp.plan = data['plan']
        if 'status' in data:
            comp.subscription_status = data['status']
        if 'billing_cycle' in data:
            comp.billing_cycle = data['billing_cycle']
        if 'expiry_date' in data and data['expiry_date']:
            try:
                comp.expiry_date = datetime.strptime(data['expiry_date'], '%Y-%m-%d')
            except ValueError:
                pass
        if 'extend_days' in data:
            base = comp.expiry_date or datetime.now(timezone.utc)
            comp.expiry_date = base + timedelta(days=int(data['extend_days']))
        if 'notes' in data:
            comp.notes = data['notes']

        db.session.commit()
        return jsonify({"success": True, "days_remaining": comp.days_remaining, "is_active": comp.is_active})

    # ── Super Admin API – impersonate tenant ─────────────
    @app.route('/api/superadmin/impersonate/<int:company_id>', methods=['POST'])
    @super_admin_required
    def sa_impersonate(company_id):
        comp = Company.query.get_or_404(company_id)
        admin_user = User.query.filter_by(company_id=company_id, role="Company Admin").first()
        if not admin_user:
            return jsonify({"success": False, "message": "No admin user found for this tenant"})
        # Store super admin identity so they can return
        session['sa_return_user_id'] = session['user_id']
        session['sa_return_company_id'] = session['company_id']
        session['sa_return_role'] = session.get('role')

        session['user_id'] = admin_user.id
        session['username'] = admin_user.username
        session['company_id'] = comp.id
        session['role'] = admin_user.role
        return jsonify({"success": True, "redirect": "/"})

    # ── Return from impersonation ─────────────────────────
    @app.route('/api/superadmin/unimpersonate', methods=['POST'])
    def sa_unimpersonate():
        if 'sa_return_user_id' not in session:
            return jsonify({"success": False, "message": "Not impersonating"})
        sa_user = User.query.get(session['sa_return_user_id'])
        session['user_id'] = session.pop('sa_return_user_id')
        session['company_id'] = session.pop('sa_return_company_id')
        session['role'] = session.pop('sa_return_role')
        session['username'] = sa_user.username if sa_user else 'superadmin'
        return jsonify({"success": True, "redirect": "/superadmin"})

    # ── Super Admin: Create new tenant manually ─────────────
    @app.route('/api/superadmin/create_tenant', methods=['POST'])
    @super_admin_required
    def sa_create_tenant():
        data = request.json or {}
        company_name = (data.get('company_name') or '').strip()
        email = (data.get('email') or '').strip()
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        plan = data.get('plan', 'trial')
        days = int(data.get('trial_days', 14))

        if not all([company_name, email, username, password]):
            return jsonify({'success': False, 'message': 'All fields required'})
        if User.query.filter_by(username=username).first():
            return jsonify({'success': False, 'message': 'Username already taken'})
        try:
            from datetime import datetime, timedelta
            expiry = datetime.now(timezone.utc) + timedelta(days=days)
            ccode, csym = _resolve_currency(data)
            country_code = ((data.get('country_code') or 'US') or 'US').upper()[:2]
            tz = data.get('timezone') or 'UTC'
            try:
                cdec = int(data.get('currency_decimals'))
            except (TypeError, ValueError):
                cdec = 2
            if cdec not in (0, 2, 3):
                cdec = 2
            phone = (data.get('phone') or '').strip()[:40]
            comp = Company(company_name=company_name, master_email=email,
                          plan=plan, subscription_status='Active', expiry_date=expiry,
                          currency_code=ccode, currency_symbol=csym, currency_decimals=cdec,
                          country_code=country_code, timezone=tz, phone=phone)
            db.session.add(comp); db.session.flush()
            db.session.add(Branch(company_id=comp.id, branch_name='Main Branch'))
            user = User(company_id=comp.id, username=username, email=email,
                       password_hash=generate_password_hash(password),
                       role='Company Admin', status='Active')
            db.session.add(user)
            db.session.commit()
            return jsonify({'success': True, 'company_id': comp.id})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': str(e)})

    # ── Super Admin: Reset user password ─────────────────
    @app.route('/api/superadmin/reset_password', methods=['POST'])
    @super_admin_required
    def sa_reset_password():
        data = request.json or {}
        username = (data.get('username') or '').strip()
        new_password = data.get('new_password') or ''
        if not username or not new_password:
            return jsonify({'success': False, 'message': 'Username and new password required'})
        user = User.query.filter_by(username=username).first()
        if not user:
            return jsonify({'success': False, 'message': f'User {username} not found'})
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        return jsonify({'success': True, 'message': f'Password reset for {username}'})

    # ── Super Admin: List users for a tenant ─────────────
    @app.route('/api/superadmin/tenant_users/<int:company_id>')
    @super_admin_required
    def sa_tenant_users(company_id):
        users = User.query.filter_by(company_id=company_id).all()
        return jsonify([{'id': u.id, 'username': u.username, 'full_name': u.full_name,
                        'email': getattr(u, 'email', '') or '',
                        'role': u.role, 'status': u.status} for u in users])

    # ── Super Admin: export ALL users (every tenant) to Excel (CSV) ──
    @app.route('/api/superadmin/export_users')
    @super_admin_required
    def sa_export_users():
        import csv, io
        from flask import Response
        companies = {c.id: c for c in Company.query.all()}
        users = User.query.order_by(User.company_id.asc(), User.id.asc()).all()
        buf = io.StringIO()
        buf.write('\ufeff')  # UTF-8 BOM so Excel renders unicode / currency symbols correctly
        w = csv.writer(buf)
        w.writerow(['User ID', 'Company', 'Company Phone', 'Company Email',
                    'Country', 'Currency', 'Timezone', 'Plan', 'Subscription Status',
                    'Expiry Date', 'Username', 'Full Name', 'User Email',
                    'Role', 'User Status'])
        for u in users:
            c = companies.get(u.company_id)
            w.writerow([
                u.id,
                (c.company_name if c else ''),
                (getattr(c, 'phone', '') or '') if c else '',
                (c.master_email if c else ''),
                (getattr(c, 'country_code', '') or '') if c else '',
                (getattr(c, 'currency_code', '') or '') if c else '',
                (getattr(c, 'timezone', '') or '') if c else '',
                (c.plan if c else ''),
                (c.subscription_status if c else ''),
                (c.expiry_date.strftime('%Y-%m-%d') if c and c.expiry_date else ''),
                u.username or '',
                u.full_name or '',
                getattr(u, 'email', '') or '',
                u.role or '',
                u.status or '',
            ])
        fname = 'all_users_' + datetime.now().strftime('%Y%m%d_%H%M') + '.csv'
        return Response(buf.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename={fname}'})

    # ── Super Admin: Edit a tenant's company profile ─────
    @app.route('/api/superadmin/company/<int:company_id>', methods=['POST'])
    @super_admin_required
    def sa_update_company(company_id):
        comp = Company.query.get_or_404(company_id)
        data = request.json or {}
        if 'company_name' in data and (data.get('company_name') or '').strip():
            comp.company_name = data['company_name'].strip()
        if 'master_email' in data:
            comp.master_email = (data.get('master_email') or '').strip()
        if 'phone' in data:
            comp.phone = (data.get('phone') or '').strip()[:40]
        if 'country_code' in data and data.get('country_code'):
            comp.country_code = (data['country_code'] or 'US').upper()[:2]
        if 'timezone' in data and data.get('timezone'):
            comp.timezone = data['timezone']
        # Currency: only touch if a currency choice was actually sent
        if any(k in data and data.get(k) for k in ('currency_code', 'currency', 'currency_symbol')):
            code, symbol = _resolve_currency(data)
            comp.currency_code = code
            comp.currency_symbol = symbol
        if 'currency_decimals' in data and data.get('currency_decimals') not in (None, ''):
            try:
                _cd = int(data.get('currency_decimals'))
                comp.currency_decimals = _cd if _cd in (0, 2, 3) else 2
            except (TypeError, ValueError):
                pass
        db.session.commit()
        return jsonify({'success': True,
                        'currency_code': comp.currency_code,
                        'currency_symbol': comp.currency_symbol,
                        'currency_decimals': getattr(comp, 'currency_decimals', 2)})

    # ── Super Admin: photo storage location per company ──
    # The tenant no longer chooses where photos live; only the Super Admin does,
    # from the 📁 action on the tenant row.
    @app.route('/api/superadmin/storage/<int:company_id>', methods=['GET'])
    @super_admin_required
    def sa_get_storage(company_id):
        comp = Company.query.get_or_404(company_id)
        from app import get_storage_info
        info = get_storage_info(company_id)
        info['success'] = True
        info['company_name'] = comp.company_name
        return jsonify(info)

    @app.route('/api/superadmin/storage/<int:company_id>', methods=['POST'])
    @super_admin_required
    def sa_set_storage(company_id):
        Company.query.get_or_404(company_id)
        from app import apply_storage_path_change
        data = request.json or {}
        result = apply_storage_path_change(
            company_id,
            data.get('path') or '',
            bool(data.get('migrate', True))
        )
        return jsonify(result)

    # ── Super Admin: Reset a specific user's password ────
    @app.route('/api/superadmin/user/<int:user_id>/password', methods=['POST'])
    @super_admin_required
    def sa_reset_user_password(user_id):
        data = request.json or {}
        new_password = data.get('new_password') or ''
        if len(new_password) < 6:
            return jsonify({'success': False, 'message': 'Password must be at least 6 characters'})
        user = User.query.get_or_404(user_id)
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        return jsonify({'success': True, 'message': f'Password reset for {user.username}'})

    # ── Tenant: get own subscription info ─────────────────
    @app.route('/api/my_subscription')
    def my_subscription():
        comp = Company.query.get(session.get('company_id'))
        if not comp:
            return jsonify({})
        limits = PLAN_LIMITS.get(comp.plan, {})
        booking_count = Booking.query.filter_by(company_id=comp.id).count()
        user_count = User.query.filter_by(company_id=comp.id).count()
        return jsonify({
            "plan": comp.plan,
            "plan_label": comp.plan_label,
            "status": comp.subscription_status,
            "is_active": comp.is_active,
            "expiry_date": comp.expiry_date.strftime('%Y-%m-%d') if comp.expiry_date else None,
            "days_remaining": comp.days_remaining,
            "limits": limits,
            "usage": {"bookings": booking_count, "users": user_count},
        })

    app.logger.info("SaaS routes registered")


# ─────────────────────────────────────────────
# HTML TEMPLATES
# ─────────────────────────────────────────────

EXPIRED_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Subscription Expired</title>
<style>
  body{font-family:Arial,sans-serif;background:#f0f4f8;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  .card{background:#fff;border-radius:12px;padding:48px 40px;text-align:center;max-width:480px;box-shadow:0 4px 24px rgba(0,0,0,.12)}
  h1{color:#e53e3e;margin-bottom:12px}
  p{color:#555;line-height:1.6}
  a.btn{display:inline-block;margin-top:24px;padding:12px 28px;background:#2563eb;color:#fff;border-radius:6px;text-decoration:none;font-weight:600}
</style>
</head>
<body>
<div class="card">
  <h1>⚠️ Subscription Expired</h1>
  <p>Your subscription has expired or has been suspended.<br>Please contact support to renew your plan and restore access.</p>
  <a class="btn" href="/logout">Back to Login</a>
</div>
</body>
</html>
"""

SUPERADMIN_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Super Admin – ServiceBook SaaS</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',Arial,sans-serif;background:#f1f5f9;color:#1e293b}
  header{background:#1e3a8a;color:#fff;padding:16px 32px;display:flex;align-items:center;justify-content:space-between}
  header h1{font-size:1.2rem;font-weight:700}
  header a{color:#93c5fd;text-decoration:none;font-size:.85rem;margin-left:16px}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;padding:24px 32px 0}
  .stat{background:#fff;border-radius:10px;padding:20px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.08)}
  .stat .num{font-size:2rem;font-weight:800;color:#1e3a8a}
  .stat .lbl{font-size:.8rem;color:#64748b;margin-top:4px}
  .section{padding:20px 32px}
  .section h2{font-size:1rem;font-weight:700;margin-bottom:12px;color:#475569}
  .toolbar{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}
  .btn{padding:8px 16px;border-radius:6px;border:none;cursor:pointer;font-size:.85rem;font-weight:600}
  .btn-blue{background:#2563eb;color:#fff}
  .btn-blue:hover{background:#1d4ed8}
  .btn-green{background:#16a34a;color:#fff}
  .btn-green:hover{background:#15803d}
  .btn-red{background:#dc2626;color:#fff}
  .btn-red:hover{background:#b91c1c}
  .btn-purple{background:#7c3aed;color:#fff}
  .btn-purple:hover{background:#6d28d9}
  table{width:100%;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08);border-collapse:collapse;font-size:.83rem}
  th{background:#f8fafc;padding:10px 12px;text-align:left;font-weight:600;color:#475569;border-bottom:1px solid #e2e8f0}
  td{padding:8px 12px;border-bottom:1px solid #f1f5f9;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  .badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:.73rem;font-weight:600}
  .badge-active{background:#dcfce7;color:#166534}
  .badge-suspended{background:#fee2e2;color:#991b1b}
  .badge-trial{background:#fef3c7;color:#92400e}
  .badge-starter{background:#dbeafe;color:#1e40af}
  .badge-pro{background:#ede9fe;color:#5b21b6}
  .badge-enterprise{background:#f0fdf4;color:#14532d}
  select,input[type=text],input[type=date],input[type=number],input[type=password],input[type=email]{padding:5px 8px;border:1px solid #d1d5db;border-radius:6px;font-size:.83rem;width:100%}
  .modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.5);z-index:100;align-items:center;justify-content:center}
  .modal.open{display:flex}
  .modal-box{background:#fff;border-radius:12px;padding:28px;width:480px;max-width:95vw}
  .modal-box h3{font-size:1.1rem;font-weight:700;margin-bottom:16px;color:#1e3a8a}
  .form-row{margin-bottom:12px}
  .form-row label{display:block;font-size:.82rem;font-weight:600;color:#374151;margin-bottom:4px}
  .modal-actions{display:flex;gap:10px;margin-top:18px;justify-content:flex-end}
  .toast{position:fixed;bottom:24px;right:24px;background:#1e3a8a;color:#fff;padding:12px 20px;border-radius:8px;display:none;font-size:.9rem;z-index:999}
  .save-btn{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:5px 12px;cursor:pointer;font-size:.8rem;font-weight:600}
  .imp-btn{background:#7c3aed;color:#fff;border:none;border-radius:6px;padding:5px 8px;cursor:pointer;font-size:.78rem}
  .users-btn{background:#0891b2;color:#fff;border:none;border-radius:6px;padding:5px 8px;cursor:pointer;font-size:.78rem}
</style>
</head>
<body>
<header>
  <h1>🛡️ ServiceBook SaaS — Super Admin</h1>
  <div>
    <a href="/">← App</a>
    <a href="/logout">Logout</a>
  </div>
</header>

<div class="stats">
  <div class="stat"><div class="num" id="s-total">—</div><div class="lbl">Total Tenants</div></div>
  <div class="stat"><div class="num" id="s-active">—</div><div class="lbl">Active</div></div>
  <div class="stat"><div class="num" id="s-trial">—</div><div class="lbl">On Trial</div></div>
  <div class="stat"><div class="num" id="s-suspended">—</div><div class="lbl">Suspended</div></div>
</div>

<div class="section">
  <div class="toolbar">
    <button class="btn btn-green" onclick="openCreateModal()">➕ Create New Company</button>
    <button class="btn btn-blue" onclick="openResetModal()">🔑 Reset User Password</button>
  </div>
  <h2>All Tenants</h2>
  <table>
    <thead>
      <tr><th>#</th><th>Company</th><th>Email</th><th>Plan</th><th>Status</th><th>Expiry</th><th>Days Left</th><th>Bookings</th><th>Users</th><th>Extend Days</th><th>Actions</th></tr>
    </thead>
    <tbody id="tbody"><tr><td colspan="11" style="text-align:center;padding:20px;color:#94a3b8">Loading…</td></tr></tbody>
  </table>
</div>

<!-- Create Company Modal -->
<div class="modal" id="createModal">
  <div class="modal-box">
    <h3>➕ Create New Company</h3>
    <div class="form-row"><label>Company Name</label><input id="c_company" type="text" placeholder="Travel Agency Name"></div>
    <div class="form-row"><label>Email</label><input id="c_email" type="email" placeholder="admin@company.com"></div>
    <div class="form-row"><label>Admin Username</label><input id="c_username" type="text" placeholder="username"></div>
    <div class="form-row"><label>Password</label><input id="c_password" type="password" placeholder="Min 6 characters"></div>
    <div class="form-row"><label>Plan</label>
      <select id="c_plan"><option value="trial">Free Trial</option><option value="starter">Starter</option><option value="pro">Pro</option><option value="enterprise">Enterprise</option></select>
    </div>
    <div class="form-row"><label>Trial Days</label><input id="c_days" type="number" value="14" min="1"></div>
    <div class="modal-actions">
      <button class="btn btn-blue" onclick="document.getElementById('createModal').classList.remove('open')">Cancel</button>
      <button class="btn btn-green" onclick="createTenant()">Create Company</button>
    </div>
  </div>
</div>

<!-- Reset Password Modal -->
<div class="modal" id="resetModal">
  <div class="modal-box">
    <h3>🔑 Reset User Password</h3>
    <div class="form-row"><label>Username</label><input id="r_username" type="text" placeholder="Enter exact username"></div>
    <div class="form-row"><label>New Password</label><input id="r_password" type="password" placeholder="New password"></div>
    <div class="modal-actions">
      <button class="btn btn-blue" onclick="document.getElementById('resetModal').classList.remove('open')">Cancel</button>
      <button class="btn btn-red" onclick="resetPassword()">Reset Password</button>
    </div>
  </div>
</div>

<!-- Users Modal -->
<div class="modal" id="usersModal">
  <div class="modal-box" style="width:600px">
    <h3 id="usersTitle">👥 Company Users</h3>
    <table style="margin-top:12px">
      <thead><tr><th>Username</th><th>Full Name</th><th>Role</th><th>Status</th></tr></thead>
      <tbody id="usersList"></tbody>
    </table>
    <div class="modal-actions">
      <button class="btn btn-blue" onclick="document.getElementById('usersModal').classList.remove('open')">Close</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const PLANS = ['free','gold','platinum'];
const STATUSES = ['Active','Suspended','Cancelled'];

async function load(){
  const r = await fetch('/api/superadmin/tenants');
  const tenants = await r.json();
  
  let total=tenants.length, active=0, trial=0, suspended=0;
  tenants.forEach(t => {
    if(t.is_active) active++;
    if(t.plan==='trial') trial++;
    if(t.status==='Suspended') suspended++;
  });
  document.getElementById('s-total').textContent = total;
  document.getElementById('s-active').textContent = active;
  document.getElementById('s-trial').textContent = trial;
  document.getElementById('s-suspended').textContent = suspended;

  const tbody = document.getElementById('tbody');
  tbody.innerHTML = '';
  tenants.forEach(t => {
    const daysColor = t.days_remaining < 7 ? 'color:#dc2626;font-weight:700' : t.days_remaining < 30 ? 'color:#d97706' : '';
    const planSel = `<select id="plan_${t.id}" onchange="markDirty(${t.id})">${PLANS.map(p=>`<option value="${p}"${p===t.plan?' selected':''}>${p}</option>`).join('')}</select>`;
    const statusSel = `<select id="status_${t.id}" onchange="markDirty(${t.id})">${STATUSES.map(s=>`<option value="${s}"${s===t.status?' selected':''}>${s}</option>`).join('')}</select>`;
    const expiryIn = `<input type="date" id="expiry_${t.id}" value="${t.expiry_date!=='—'?t.expiry_date:''}" onchange="markDirty(${t.id})" style="width:120px">`;
    const extendIn = `<input type="number" id="extend_${t.id}" placeholder="days" min="1" style="width:70px">`;
    tbody.insertAdjacentHTML('beforeend', `<tr id="row_${t.id}">
      <td>${t.id}</td>
      <td><strong>${t.company_name}</strong></td>
      <td style="font-size:.78rem">${t.master_email||''}</td>
      <td>${planSel}</td>
      <td>${statusSel}</td>
      <td>${expiryIn}</td>
      <td style="${daysColor}">${t.days_remaining===9999?'∞':t.days_remaining+'d'}</td>
      <td>${t.bookings}</td>
      <td>${t.users}</td>
      <td>${extendIn}</td>
      <td style="display:flex;gap:4px;flex-wrap:wrap">
        <button class="save-btn" onclick="save(${t.id})">Save</button>
        <button class="imp-btn" onclick="impersonate(${t.id})" title="Login as tenant">👤</button>
        <button class="users-btn" onclick="showUsers(${t.id},'${t.company_name}')">👥</button>
      </td>
    </tr>`);
  });
}

function markDirty(id){ document.getElementById(`row_${id}`).style.background='#fffbeb'; }

async function save(id){
  const plan = document.getElementById(`plan_${id}`).value;
  const status = document.getElementById(`status_${id}`).value;
  const expiry = document.getElementById(`expiry_${id}`).value;
  const extend = parseInt(document.getElementById(`extend_${id}`).value)||0;
  const payload = {plan, status};
  if(expiry) payload.expiry_date = expiry;
  if(extend > 0) payload.extend_days = extend;
  const r = await fetch(`/api/superadmin/tenant/${id}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const d = await r.json();
  if(d.success){ document.getElementById(`row_${id}`).style.background=''; document.getElementById(`extend_${id}`).value=''; showToast('Saved!'); load(); }
  else alert(d.message);
}

async function impersonate(id){
  if(!confirm('Log in as this tenant?')) return;
  const r = await fetch(`/api/superadmin/impersonate/${id}`,{method:'POST'});
  const d = await r.json();
  if(d.success) window.location.href = d.redirect||'/';
  else alert(d.message);
}

async function showUsers(companyId, companyName){
  document.getElementById('usersTitle').textContent = '👥 Users — ' + companyName;
  document.getElementById('usersList').innerHTML = '<tr><td colspan="4">Loading...</td></tr>';
  document.getElementById('usersModal').classList.add('open');
  const r = await fetch(`/api/superadmin/tenant_users/${companyId}`);
  const users = await r.json();
  const tbody = document.getElementById('usersList');
  if(users.length === 0){ tbody.innerHTML = '<tr><td colspan="4" style="text-align:center">No users found</td></tr>'; return; }
  tbody.innerHTML = users.map(u => `<tr><td><strong>${u.username}</strong></td><td>${u.full_name||'—'}</td><td>${u.role||'—'}</td><td><span class="badge badge-${u.status==='Active'?'active':'suspended'}">${u.status}</span></td></tr>`).join('');
}

function openCreateModal(){ document.getElementById('createModal').classList.add('open'); }
function openResetModal(){ document.getElementById('resetModal').classList.add('open'); }

async function createTenant(){
  const payload = {
    company_name: document.getElementById('c_company').value,
    email: document.getElementById('c_email').value,
    username: document.getElementById('c_username').value,
    password: document.getElementById('c_password').value,
    plan: document.getElementById('c_plan').value,
    trial_days: document.getElementById('c_days').value,
  };
  const r = await fetch('/api/superadmin/create_tenant',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const d = await r.json();
  if(d.success){ document.getElementById('createModal').classList.remove('open'); showToast('Company created!'); load(); }
  else alert(d.message);
}

async function resetPassword(){
  const payload = { username: document.getElementById('r_username').value, new_password: document.getElementById('r_password').value };
  const r = await fetch('/api/superadmin/reset_password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const d = await r.json();
  if(d.success){ document.getElementById('resetModal').classList.remove('open'); showToast('Password reset!'); }
  else alert(d.message);
}

function showToast(msg){ const t=document.getElementById('toast'); t.textContent=msg; t.style.display='block'; setTimeout(()=>t.style.display='none',2500); }

load();
</script>
</body>
</html>
"""

SIGNUP_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Sign Up – ServiceBook ERP</title>
<style>
  *{box-sizing:border-box}
  body{font-family:'Segoe UI',Arial,sans-serif;background:linear-gradient(135deg,#1e3a8a 0%,#2563eb 100%);min-height:100vh;display:flex;align-items:center;justify-content:center;margin:0;padding:20px}
  .card{background:#fff;border-radius:16px;padding:40px 36px;width:100%;max-width:480px;box-shadow:0 8px 40px rgba(0,0,0,.2)}
  h2{margin:0 0 8px;color:#1e3a8a;font-size:1.6rem}
  .subtitle{color:#64748b;margin-bottom:24px;font-size:.9rem}
  label{display:block;font-size:.85rem;font-weight:600;color:#374151;margin-bottom:4px;margin-top:14px}
  label:first-of-type{margin-top:0}
  input,select{width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;font-size:.95rem;outline:none;transition:border .2s;background:#fff}
  input:focus,select:focus{border-color:#2563eb}
  button{width:100%;padding:12px;background:#2563eb;color:#fff;border:none;border-radius:8px;font-size:1rem;font-weight:700;cursor:pointer;margin-top:20px}
  button:hover{background:#1d4ed8}
  button:disabled{opacity:.6;cursor:not-allowed}
  .trial-badge{background:#dcfce7;color:#166534;border-radius:20px;padding:6px 14px;font-size:.8rem;font-weight:600;display:inline-block;margin-bottom:20px}
  .login-link{text-align:center;margin-top:16px;font-size:.85rem;color:#64748b}
  .login-link a{color:#2563eb;font-weight:600;text-decoration:none}
  .error{color:#dc2626;font-size:.85rem;margin-bottom:12px;display:none;padding:8px 12px;background:#fef2f2;border-radius:6px}
  .locale-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .helper{font-size:.75rem;color:#64748b;margin-top:4px}
</style>
</head>
<body>
<div class="card">
  <h2>Get Started Free</h2>
  <p class="subtitle">Create your ServiceBook ERP account</p>
  <div class="trial-badge">✓ 14-day free trial — no credit card required</div>
  <div class="error" id="errMsg"></div>
  
  <label>Company Name</label>
  <input id="company_name" placeholder="Your Travel Agency / Business">
  
  <label>Email Address</label>
  <input id="email" type="email" placeholder="you@company.com">
  
  <label>Username</label>
  <input id="username" placeholder="Choose a username">
  
  <label>Password</label>
  <input id="password" type="password" placeholder="Min 6 characters">
  
  <label>Country</label>
  <select id="country" onchange="onCountryChange()">
    <option value="">-- Select your country --</option>
  </select>
  
  <div class="locale-row">
    <div>
      <label>Currency</label>
      <select id="currency"></select>
    </div>
    <div>
      <label>Timezone</label>
      <input id="timezone" readonly style="background:#f8fafc;font-size:.85rem">
    </div>
  </div>

  <label>Phone Number</label>
  <div style="display:grid;grid-template-columns:92px 1fr;gap:8px">
    <input id="dial_code" placeholder="+1" style="text-align:center">
    <input id="phone" type="tel" placeholder="Phone number">
  </div>
  <div class="helper">The country code fills in automatically when you choose your country.</div>

  <label style="margin-top:14px;display:block">Decimal Places</label>
  <select id="currency_decimals">
    <option value="2" selected>2 — e.g. 1,234.00</option>
    <option value="3">3 — e.g. 1,234.000</option>
    <option value="0">0 — e.g. 1,234</option>
  </select>
  <div class="helper">Controls how all amounts are displayed across the software.</div>

  <button onclick="doSignup()" id="signupBtn" style="margin-top:16px">Create Account →</button>
  <div class="login-link">Already have an account? <a href="/login">Sign in</a></div>
</div>

<script src="/static/locate_data.js"></script>
<script>
// Build country dropdown
function buildCountryDropdown(){
  const sel = document.getElementById('country');
  window.COUNTRY_DATA.forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.code;
    opt.textContent = c.name;
    opt.dataset.currency = c.currency;
    opt.dataset.symbol = c.symbol;
    opt.dataset.timezone = c.timezone;
    sel.appendChild(opt);
  });
}

// Build currency dropdown (unique currencies)
function buildCurrencyDropdown(){
  const sel = document.getElementById('currency');
  const seen = new Set();
  window.COUNTRY_DATA.forEach(c => {
    if(seen.has(c.currency)) return;
    seen.add(c.currency);
    const opt = document.createElement('option');
    opt.value = c.currency;
    opt.textContent = c.currency + ' (' + c.symbol + ')';
    opt.dataset.symbol = c.symbol;
    sel.appendChild(opt);
  });
}

// Auto-detect timezone from browser
function autoDetectCountry(){
  const detectedTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const match = window.COUNTRY_DATA.find(c => c.timezone === detectedTz);
  if(match){
    document.getElementById('country').value = match.code;
    onCountryChange();
  } else {
    // Fallback: just set the timezone field
    document.getElementById('timezone').value = detectedTz;
  }
}

// International dialing codes for the supported countries.
const DIAL_CODES = {
  OM:'+968', AE:'+971', SA:'+966', KW:'+965', QA:'+974', BH:'+973',
  IN:'+91', PK:'+92', LK:'+94', BD:'+880', PH:'+63', ID:'+62', MY:'+60',
  SG:'+65', TH:'+66', EG:'+20', JO:'+962', GB:'+44', US:'+1', CA:'+1',
  AU:'+61', DE:'+49', FR:'+33', IT:'+39', ES:'+34'
};

// When country changes, auto-fill currency + timezone + dialing code
function onCountryChange(){
  const sel = document.getElementById('country');
  const opt = sel.options[sel.selectedIndex];
  if(!opt.value) return;
  document.getElementById('currency').value = opt.dataset.currency;
  document.getElementById('timezone').value = opt.dataset.timezone;
  const dial = DIAL_CODES[opt.value];
  if(dial) document.getElementById('dial_code').value = dial;
}

// Initialize on page load
buildCountryDropdown();
buildCurrencyDropdown();
autoDetectCountry();

async function doSignup(){
  const btn = document.getElementById('signupBtn');
  const err = document.getElementById('errMsg');
  err.style.display = 'none';
  
  // Validation
  const company_name = document.getElementById('company_name').value.trim();
  const email = document.getElementById('email').value.trim();
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  const country = document.getElementById('country').value;
  const currency = document.getElementById('currency').value;
  const timezone = document.getElementById('timezone').value;
  const currency_decimals = document.getElementById('currency_decimals').value;
  const dialCode = document.getElementById('dial_code').value.trim();
  const phoneNumber = document.getElementById('phone').value.trim();
  const phone = (dialCode ? dialCode + ' ' : '') + phoneNumber;
  
  if(!company_name || !email || !username || !password){
    err.textContent = 'All fields are required';
    err.style.display = 'block';
    return;
  }
  if(!country){
    err.textContent = 'Please select your country';
    err.style.display = 'block';
    return;
  }
  if(!phoneNumber){
    err.textContent = 'Please enter your phone number';
    err.style.display = 'block';
    return;
  }
  if(password.length < 6){
    err.textContent = 'Password must be at least 6 characters';
    err.style.display = 'block';
    return;
  }
  
  // Get symbol for currency
  const currencySel = document.getElementById('currency');
  const currencySymbol = currencySel.options[currencySel.selectedIndex].dataset.symbol;
  
  btn.disabled = true; 
  btn.textContent = 'Creating account…';
  
  try {
    const r = await fetch('/api/signup', {
      method:'POST', 
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        company_name, email, username, password,
        country_code: country,
        currency_code: currency,
        currency_symbol: currencySymbol,
        currency_decimals: currency_decimals,
        timezone: timezone,
        phone: phone
      })
    });
    const d = await r.json();
    if(d.success){ 
      window.location.href = d.redirect || '/'; 
    } else { 
      err.textContent = d.message; 
      err.style.display='block'; 
      btn.disabled=false; 
      btn.textContent='Create Account →'; 
    }
  } catch(e) {
    err.textContent = 'Network error. Please try again.';
    err.style.display = 'block';
    btn.disabled = false;
    btn.textContent = 'Create Account →';
  }
}
</script>
</body>
</html>
"""

SUPERADMIN_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Super Admin – ServiceBook SaaS</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',Arial,sans-serif;background:#f1f5f9;color:#1e293b}
  header{background:#1e3a8a;color:#fff;padding:16px 32px;display:flex;align-items:center;justify-content:space-between}
  header h1{font-size:1.2rem;font-weight:700}
  header a{color:#93c5fd;text-decoration:none;font-size:.85rem}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;padding:24px 32px 0}
  .stat{background:#fff;border-radius:10px;padding:20px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.08)}
  .stat .num{font-size:2rem;font-weight:800;color:#1e3a8a}
  .stat .lbl{font-size:.8rem;color:#64748b;margin-top:4px}
  .section{padding:24px 32px}
  .section h2{font-size:1rem;font-weight:700;margin-bottom:14px;color:#475569}
  table{width:100%;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08);border-collapse:collapse;font-size:.85rem}
  th{background:#f8fafc;padding:10px 14px;text-align:left;font-weight:600;color:#475569;border-bottom:1px solid #e2e8f0}
  td{padding:10px 14px;border-bottom:1px solid #f1f5f9;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  .badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:.75rem;font-weight:600}
  .badge-active{background:#dcfce7;color:#166534}
  .badge-suspended{background:#fee2e2;color:#991b1b}
  .badge-trial{background:#fef3c7;color:#92400e}
  .badge-starter{background:#dbeafe;color:#1e40af}
  .badge-pro{background:#ede9fe;color:#5b21b6}
  .badge-enterprise{background:#f0fdf4;color:#14532d}
  select,input[type=text],input[type=date],input[type=number]{padding:5px 8px;border:1px solid #d1d5db;border-radius:6px;font-size:.83rem;width:100%}
  button.save{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:5px 14px;cursor:pointer;font-size:.83rem;font-weight:600}
  button.save:hover{background:#1d4ed8}
  button.imp{background:#7c3aed;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:.78rem}
  button.imp:hover{background:#6d28d9}
  button.edit{background:#0891b2;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:.78rem}
  button.edit:hover{background:#0e7490}
  button.usersbtn{background:#475569;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:.78rem}
  button.usersbtn:hover{background:#334155}
  button.storagebtn{background:#6366f1;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:.78rem}
  button.storagebtn:hover{background:#4f46e5}
  .toolbar{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}
  .btn{padding:9px 18px;border-radius:8px;border:none;cursor:pointer;font-size:.88rem;font-weight:700}
  .btn-green{background:#16a34a;color:#fff}
  .btn-green:hover{background:#15803d}
  .btn-blue{background:#2563eb;color:#fff}
  .btn-blue:hover{background:#1d4ed8}
  .btn-red{background:#dc2626;color:#fff}
  .btn-red:hover{background:#b91c1c}
  .btn-gray{background:#e2e8f0;color:#334155}
  .btn-gray:hover{background:#cbd5e1}
  .modal{display:none;position:fixed;inset:0;background:rgba(15,23,42,.55);z-index:200;align-items:center;justify-content:center;padding:20px}
  .modal.open{display:flex}
  .modal-box{background:#fff;border-radius:14px;padding:26px 28px;width:480px;max-width:96vw;max-height:92vh;overflow:auto;box-shadow:0 12px 40px rgba(0,0,0,.25)}
  .modal-box h3{font-size:1.15rem;font-weight:800;margin-bottom:16px;color:#1e3a8a}
  .form-row{margin-bottom:12px}
  .form-row label{display:block;font-size:.8rem;font-weight:700;color:#374151;margin-bottom:4px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .modal-actions{display:flex;gap:10px;margin-top:20px;justify-content:flex-end}
  .modal-err{color:#dc2626;font-size:.82rem;margin-bottom:10px;display:none;padding:8px 12px;background:#fef2f2;border-radius:6px}
  .u-table{width:100%;border-collapse:collapse;font-size:.83rem;margin-top:8px}
  .u-table th{background:#f8fafc;padding:8px;text-align:left;font-weight:700;color:#475569;border-bottom:1px solid #e2e8f0}
  .u-table td{padding:8px;border-bottom:1px solid #f1f5f9;vertical-align:middle}
  .toast{position:fixed;bottom:24px;right:24px;background:#1e3a8a;color:#fff;padding:12px 20px;border-radius:8px;display:none;font-size:.9rem;z-index:999}
</style>
</head>
<body>
<header>
  <h1>🛡️ ServiceBook SaaS — Super Admin</h1>
  <div style="display:flex;gap:20px;align-items:center">
    <a href="/">← App</a>
    <a href="/logout">Logout</a>
  </div>
</header>

<div class="stats">
  <div class="stat"><div class="num">{{stats.total}}</div><div class="lbl">Total Tenants</div></div>
  <div class="stat"><div class="num">{{stats.active}}</div><div class="lbl">Active</div></div>
  <div class="stat"><div class="num">{{stats.trial}}</div><div class="lbl">On Trial</div></div>
  <div class="stat"><div class="num">{{stats.suspended}}</div><div class="lbl">Suspended</div></div>
</div>

<div class="section">
  <div class="toolbar">
    <button class="btn btn-green" onclick="openCreate()">➕ Create New Company</button>
    <a class="btn btn-blue" href="/api/superadmin/export_users" style="text-decoration:none;display:inline-block">📥 Export All Users (Excel)</a>
  </div>
  <h2>All Tenants</h2>
  <table id="tenantTable">
    <thead>
      <tr>
        <th>#</th><th>Company</th><th>Email</th><th>Phone</th><th>Plan</th><th>Status</th>
        <th>Expiry</th><th>Days Left</th><th>Bookings</th><th>Users</th>
        <th>Extend</th><th>Actions</th>
      </tr>
    </thead>
    <tbody id="tbody">
      <tr><td colspan="12" style="text-align:center;padding:20px;color:#94a3b8">Loading…</td></tr>
    </tbody>
  </table>
</div>

<div class="toast" id="toast"></div>

<!-- Create Company Modal -->
<div class="modal" id="createModal">
  <div class="modal-box">
    <h3>➕ Create New Company</h3>
    <div class="modal-err" id="cErr"></div>
    <div class="form-row"><label>Company Name</label><input type="text" id="c_company" placeholder="Business / Agency name"></div>
    <div class="form-row"><label>Email Address</label><input type="text" id="c_email" placeholder="admin@company.com"></div>
    <div class="form-row"><label>Phone Number</label><input type="text" id="c_phone" placeholder="+1 555 123 4567"></div>
    <div class="grid2">
      <div class="form-row"><label>Admin Username</label><input type="text" id="c_username" placeholder="username"></div>
      <div class="form-row"><label>Password</label><input type="password" id="c_password" placeholder="Min 6 characters"></div>
    </div>
    <div class="form-row"><label>Country</label>
      <select id="c_country" onchange="onCountryChange('c')"><option value="">-- Select country --</option></select>
    </div>
    <div class="grid2">
      <div class="form-row"><label>Currency</label><select id="c_currency"></select></div>
      <div class="form-row"><label>Timezone</label><input type="text" id="c_timezone" readonly style="background:#f8fafc"></div>
    </div>
    <div class="grid2">
      <div class="form-row"><label>Decimal Places</label>
        <select id="c_decimals">
          <option value="2" selected>2 — e.g. 0.00</option>
          <option value="3">3 — e.g. 0.000</option>
          <option value="0">0 — e.g. 0</option>
        </select>
      </div>
      <div class="form-row"><label>Plan</label><select id="c_plan"></select></div>
    </div>
    <div class="form-row"><label>Trial Days</label><input type="number" id="c_days" value="14" min="1"></div>
    <div class="modal-actions">
      <button class="btn btn-gray" onclick="closeModal('createModal')">Cancel</button>
      <button class="btn btn-green" id="c_submit" onclick="createCompany()">Create Company</button>
    </div>
  </div>
</div>

<!-- Edit Company Modal -->
<div class="modal" id="editModal">
  <div class="modal-box">
    <h3>✏️ Edit Company</h3>
    <div class="modal-err" id="eErr"></div>
    <input type="hidden" id="e_id">
    <div class="form-row"><label>Company Name</label><input type="text" id="e_company"></div>
    <div class="form-row"><label>Email Address</label><input type="text" id="e_email"></div>
    <div class="form-row"><label>Phone Number</label><input type="text" id="e_phone"></div>
    <div class="form-row"><label>Country</label>
      <select id="e_country" onchange="onCountryChange('e')"><option value="">-- Select country --</option></select>
    </div>
    <div class="grid2">
      <div class="form-row"><label>Currency</label><select id="e_currency"></select></div>
      <div class="form-row"><label>Timezone</label><input type="text" id="e_timezone" readonly style="background:#f8fafc"></div>
    </div>
    <div class="form-row"><label>Decimal Places</label>
      <select id="e_decimals">
        <option value="2">2 — e.g. 0.00</option>
        <option value="3">3 — e.g. 0.000</option>
        <option value="0">0 — e.g. 0</option>
      </select>
    </div>
    <div class="modal-actions">
      <button class="btn btn-gray" onclick="closeModal('editModal')">Cancel</button>
      <button class="btn btn-blue" id="e_submit" onclick="saveCompany()">Save Changes</button>
    </div>
  </div>
</div>

<!-- Users Modal -->
<div class="modal" id="usersModal">
  <div class="modal-box" style="width:640px">
    <h3 id="usersTitle">👥 Company Users</h3>
    <table class="u-table">
      <thead><tr><th>Username</th><th>Name / Role</th><th>Status</th><th>Reset Password</th></tr></thead>
      <tbody id="usersList"></tbody>
    </table>
    <div class="modal-actions">
      <button class="btn btn-gray" onclick="closeModal('usersModal')">Close</button>
    </div>
  </div>
</div>

<!-- Photo Storage Modal -->
<div class="modal" id="storageModal">
  <div class="modal-box">
    <h3 id="storageTitle">📁 Photo Storage Location</h3>
    <div class="modal-err" id="sErr"></div>
    <input type="hidden" id="s_id">
    <div class="form-row">
      <label>Current Path</label>
      <input type="text" id="s_current" readonly style="background:#f8fafc;font-family:monospace;font-size:.78rem">
      <div id="s_stats" style="font-size:.75rem;color:#64748b;margin-top:4px">Loading…</div>
    </div>
    <div class="form-row">
      <label>Save Photos To *</label>
      <input type="text" id="s_new" style="font-family:monospace;font-size:.78rem">
      <div id="s_help" style="font-size:.72rem;color:#64748b;margin-top:4px"></div>
    </div>
    <div style="padding:10px 12px;background:#fef3c7;border:1px solid #fcd34d;border-radius:8px;margin:12px 0 4px">
      <label style="display:flex;align-items:center;gap:10px;cursor:pointer;margin:0;font-size:.78rem;color:#78350f;font-weight:400">
        <input type="checkbox" id="s_migrate" checked style="width:auto;margin:0">
        <span><b>Move this company's existing photos</b> to the new location (recommended)</span>
      </label>
    </div>
    <div class="modal-actions">
      <button class="btn btn-gray" onclick="closeModal('storageModal')">Cancel</button>
      <button class="btn btn-blue" id="s_submit" onclick="saveStorage()">Save Location</button>
    </div>
  </div>
</div>

<script src="/static/locate_data.js"></script>
<script>
const PLANS = ['trial','starter','pro','enterprise'];
const STATUSES = ['Active','Suspended','Cancelled'];
let TENANTS = [];

// ---- Country / currency / timezone dropdowns (shared with signup) ----
function buildCountrySelect(id){
  const sel = document.getElementById(id);
  if(!sel || !window.COUNTRY_DATA) return;
  window.COUNTRY_DATA.forEach(c => {
    const o = document.createElement('option');
    o.value = c.code; o.textContent = c.name;
    o.dataset.currency = c.currency; o.dataset.symbol = c.symbol; o.dataset.timezone = c.timezone;
    sel.appendChild(o);
  });
}
function buildCurrencySelect(id){
  const sel = document.getElementById(id);
  if(!sel || !window.COUNTRY_DATA) return;
  const seen = new Set();
  window.COUNTRY_DATA.forEach(c => {
    if(seen.has(c.currency)) return;
    seen.add(c.currency);
    const o = document.createElement('option');
    o.value = c.currency; o.textContent = c.currency + ' (' + c.symbol + ')';
    o.dataset.symbol = c.symbol;
    sel.appendChild(o);
  });
}
function buildPlanSelect(id){
  const sel = document.getElementById(id);
  if(!sel) return;
  sel.innerHTML = PLANS.map(p=>`<option value="${p}">${p}</option>`).join('');
}
// Prefix 'c' = create modal, 'e' = edit modal
function onCountryChange(prefix){
  const sel = document.getElementById(prefix+'_country');
  const opt = sel.options[sel.selectedIndex];
  if(!opt || !opt.value) return;
  document.getElementById(prefix+'_currency').value = opt.dataset.currency;
  document.getElementById(prefix+'_timezone').value = opt.dataset.timezone;
}
function currencySymbolOf(id){
  const sel = document.getElementById(id);
  const opt = sel.options[sel.selectedIndex];
  return opt ? (opt.dataset.symbol || '') : '';
}
function initDropdowns(){
  buildCountrySelect('c_country'); buildCurrencySelect('c_currency'); buildPlanSelect('c_plan');
  buildCountrySelect('e_country'); buildCurrencySelect('e_currency');
}
function closeModal(id){ document.getElementById(id).classList.remove('open'); }

async function load(){
  const r = await fetch('/api/superadmin/tenants');
  const tenants = await r.json();
  TENANTS = tenants;
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = '';
  tenants.forEach(t => {
    const planBadge = `<span class="badge badge-${t.plan}">${t.plan_label}</span>`;
    const statusBadge = `<span class="badge badge-${t.status.toLowerCase()}">${t.status}</span>`;
    const daysColor = t.days_remaining < 7 ? 'color:#dc2626;font-weight:700' : t.days_remaining < 30 ? 'color:#d97706' : '';
    const planSel = `<select id="plan_${t.id}" onchange="markDirty(${t.id})">${PLANS.map(p=>`<option value="${p}"${p===t.plan?' selected':''}>${p}</option>`).join('')}</select>`;
    const statusSel = `<select id="status_${t.id}" onchange="markDirty(${t.id})">${STATUSES.map(s=>`<option value="${s}"${s===t.status?' selected':''}>${s}</option>`).join('')}</select>`;
    const expiryIn = `<input type="date" id="expiry_${t.id}" value="${t.expiry_date!=='—'?t.expiry_date:''}" onchange="markDirty(${t.id})">`;
    const extendIn = `<input type="number" id="extend_${t.id}" placeholder="days" min="1" style="width:70px">`;
    const saveBtn = `<button class="save" onclick="save(${t.id})">Save</button>`;
    const impBtn = `<button class="imp" onclick="impersonate(${t.id})" title="Log in as this tenant">👤</button>`;
    const editBtn = `<button class="edit" onclick="openEdit(${t.id})" title="Edit company details">✏️</button>`;
    const usersBtn = `<button class="usersbtn" onclick="showUsers(${t.id})" title="View users">👥</button>`;
    const storeBtn = `<button class="storagebtn" onclick="openStorage(${t.id})" title="Photo storage location">📁</button>`;
    tbody.insertAdjacentHTML('beforeend', `<tr id="row_${t.id}">
      <td>${t.id}</td>
      <td><strong>${t.company_name}</strong></td>
      <td style="font-size:.8rem">${t.master_email}</td>
      <td style="font-size:.8rem">${t.phone || '—'}</td>
      <td>${planSel}</td>
      <td>${statusSel}</td>
      <td>${expiryIn}</td>
      <td style="${daysColor}">${t.days_remaining===9999?'∞':t.days_remaining+'d'}</td>
      <td>${t.bookings}</td>
      <td>${t.users}</td>
      <td>${extendIn}</td>
      <td style="display:flex;gap:6px;flex-wrap:wrap">${saveBtn}${editBtn}${usersBtn}${storeBtn}${impBtn}</td>
    </tr>`);
  });
}

function markDirty(id){ document.getElementById(`row_${id}`).style.background='#fffbeb'; }

async function save(id){
  const plan = document.getElementById(`plan_${id}`).value;
  const status = document.getElementById(`status_${id}`).value;
  const expiry = document.getElementById(`expiry_${id}`).value;
  const extend = parseInt(document.getElementById(`extend_${id}`).value) || 0;
  const payload = {plan, status};
  if(expiry) payload.expiry_date = expiry;
  if(extend > 0) payload.extend_days = extend;

  const r = await fetch(`/api/superadmin/tenant/${id}`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  const d = await r.json();
  if(d.success){
    document.getElementById(`row_${id}`).style.background='';
    document.getElementById(`extend_${id}`).value = '';
    showToast('Saved!');
    load();
  } else { alert(d.message); }
}

async function impersonate(id){
  if(!confirm('Log in as this tenant? You can return via /api/superadmin/unimpersonate')) return;
  const r = await fetch(`/api/superadmin/impersonate/${id}`, {method:'POST'});
  const d = await r.json();
  if(d.success) window.location.href = d.redirect || '/';
  else alert(d.message);
}

// ---- Create Company ----
function openCreate(){
  document.getElementById('cErr').style.display='none';
  ['c_company','c_email','c_phone','c_username','c_password'].forEach(i=>document.getElementById(i).value='');
  document.getElementById('c_days').value='14';
  document.getElementById('c_country').value='';
  document.getElementById('c_timezone').value = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  document.getElementById('createModal').classList.add('open');
}
async function createCompany(){
  const err = document.getElementById('cErr');
  const btn = document.getElementById('c_submit');
  err.style.display='none';
  const payload = {
    company_name: document.getElementById('c_company').value.trim(),
    email: document.getElementById('c_email').value.trim(),
    phone: document.getElementById('c_phone').value.trim(),
    username: document.getElementById('c_username').value.trim(),
    password: document.getElementById('c_password').value,
    plan: document.getElementById('c_plan').value,
    trial_days: document.getElementById('c_days').value,
    country_code: document.getElementById('c_country').value,
    currency_code: document.getElementById('c_currency').value,
    currency_symbol: currencySymbolOf('c_currency'),
    currency_decimals: document.getElementById('c_decimals').value,
    timezone: document.getElementById('c_timezone').value,
  };
  if(!payload.company_name || !payload.email || !payload.username || !payload.password){
    err.textContent='Company, email, username and password are required'; err.style.display='block'; return;
  }
  if(payload.password.length < 6){ err.textContent='Password must be at least 6 characters'; err.style.display='block'; return; }
  btn.disabled=true; btn.textContent='Creating…';
  try{
    const r = await fetch('/api/superadmin/create_tenant',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d = await r.json();
    if(d.success){ closeModal('createModal'); showToast('Company created!'); load(); }
    else { err.textContent = d.message || 'Failed to create company'; err.style.display='block'; }
  } catch(e){ err.textContent='Network error. Please try again.'; err.style.display='block'; }
  btn.disabled=false; btn.textContent='Create Company';
}

// ---- Edit Company ----
function openEdit(id){
  const t = TENANTS.find(x=>x.id===id);
  if(!t) return;
  document.getElementById('eErr').style.display='none';
  document.getElementById('e_id').value = t.id;
  document.getElementById('e_company').value = t.company_name || '';
  document.getElementById('e_email').value = t.master_email || '';
  document.getElementById('e_phone').value = t.phone || '';
  document.getElementById('e_country').value = (t.country_code || '').toUpperCase();
  document.getElementById('e_currency').value = (t.currency_code || '').toUpperCase();
  document.getElementById('e_timezone').value = t.timezone || '';
  document.getElementById('e_decimals').value = String(t.currency_decimals != null ? t.currency_decimals : 2);
  document.getElementById('editModal').classList.add('open');
}
async function saveCompany(){
  const err = document.getElementById('eErr');
  const btn = document.getElementById('e_submit');
  err.style.display='none';
  const id = document.getElementById('e_id').value;
  const payload = {
    company_name: document.getElementById('e_company').value.trim(),
    master_email: document.getElementById('e_email').value.trim(),
    phone: document.getElementById('e_phone').value.trim(),
    country_code: document.getElementById('e_country').value,
    currency_code: document.getElementById('e_currency').value,
    currency_symbol: currencySymbolOf('e_currency'),
    currency_decimals: document.getElementById('e_decimals').value,
    timezone: document.getElementById('e_timezone').value,
  };
  if(!payload.company_name){ err.textContent='Company name is required'; err.style.display='block'; return; }
  btn.disabled=true; btn.textContent='Saving…';
  try{
    const r = await fetch(`/api/superadmin/company/${id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d = await r.json();
    if(d.success){ closeModal('editModal'); showToast('Company updated!'); load(); }
    else { err.textContent = d.message || 'Failed to update'; err.style.display='block'; }
  } catch(e){ err.textContent='Network error. Please try again.'; err.style.display='block'; }
  btn.disabled=false; btn.textContent='Save Changes';
}

// ---- Photo storage location (Super Admin only) ----
let STORAGE_OS = null;
function storageHelpHtml(example){
  var ex = example ? ' like <code>' + example + '</code>' : '';
  if(STORAGE_OS === 'windows'){
    return 'This server runs on <b>Windows</b>. Use a full Windows path' + ex +
           ', or just a folder name like <code>company_photos</code> (created next to the app). ' +
           'The folder is created automatically.';
  }
  if(STORAGE_OS === 'linux'){
    return 'This server runs on <b>Linux</b>. Use a full Linux path' + ex +
           ', or just a folder name like <code>company_photos</code> (created next to the app). ' +
           'Windows paths will be rejected. The folder is created automatically.';
  }
  return 'Enter a path on the server, or just a folder name (created next to the app).';
}
async function openStorage(id){
  const t = TENANTS.find(x=>x.id===id);
  document.getElementById('sErr').style.display='none';
  document.getElementById('s_id').value = id;
  document.getElementById('storageTitle').textContent = '📁 Photo Storage — ' + (t ? t.company_name : '');
  document.getElementById('s_current').value = '';
  document.getElementById('s_new').value = '';
  document.getElementById('s_stats').textContent = 'Loading…';
  document.getElementById('s_help').innerHTML = '';
  document.getElementById('s_migrate').checked = true;
  document.getElementById('storageModal').classList.add('open');
  try{
    const r = await fetch(`/api/superadmin/storage/${id}`);
    const d = await r.json();
    if(!d.success) throw new Error(d.message || 'Failed');
    STORAGE_OS = d.os || null;
    document.getElementById('s_current').value = d.path || '';
    document.getElementById('s_new').value = d.path || '';
    document.getElementById('s_new').placeholder = 'e.g. ' + (d.example_path || 'company_photos');
    document.getElementById('s_stats').textContent = `📦 ${d.file_count} file(s) • ${d.size_mb} MB used`;
    document.getElementById('s_help').innerHTML = storageHelpHtml(d.example_path);
  } catch(e){
    document.getElementById('s_stats').textContent = '⚠️ Could not load storage info';
  }
}
async function saveStorage(){
  const err = document.getElementById('sErr');
  const btn = document.getElementById('s_submit');
  err.style.display='none';
  const id = document.getElementById('s_id').value;
  const path = document.getElementById('s_new').value.trim();
  const migrate = document.getElementById('s_migrate').checked;
  if(!path){ err.textContent='Path is required'; err.style.display='block'; return; }
  if(!confirm('Save photos to:\\n\\n' + path + '\\n\\n' + (migrate ? 'Existing photos will be moved.' : 'Existing photos will stay where they are.'))) return;
  btn.disabled=true; btn.textContent='Saving…';
  try{
    const r = await fetch(`/api/superadmin/storage/${id}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: path, migrate: migrate})
    });
    const d = await r.json();
    if(d.success){ closeModal('storageModal'); showToast(d.message || 'Storage location updated'); }
    else { err.textContent = d.message || 'Failed to update'; err.style.display='block'; }
  } catch(e){ err.textContent='Network error. Please try again.'; err.style.display='block'; }
  btn.disabled=false; btn.textContent='Save Location';
}

// ---- Users + per-user password reset ----
async function showUsers(id){
  const t = TENANTS.find(x=>x.id===id);
  document.getElementById('usersTitle').textContent = '👥 Users — ' + (t ? t.company_name : '');
  document.getElementById('usersList').innerHTML = '<tr><td colspan="4" style="text-align:center;color:#94a3b8">Loading…</td></tr>';
  document.getElementById('usersModal').classList.add('open');
  const r = await fetch(`/api/superadmin/tenant_users/${id}`);
  const users = await r.json();
  const body = document.getElementById('usersList');
  if(!users.length){ body.innerHTML='<tr><td colspan="4" style="text-align:center">No users found</td></tr>'; return; }
  body.innerHTML = users.map(u => `<tr>
    <td><strong>${u.username}</strong></td>
    <td>${u.full_name||'—'}<br><span style="color:#64748b;font-size:.75rem">${u.role||''}</span></td>
    <td><span class="badge badge-${u.status==='Active'?'active':'suspended'}">${u.status||''}</span></td>
    <td>
      <div style="display:flex;gap:6px">
        <input type="password" id="pw_${u.id}" placeholder="New password" style="width:130px">
        <button class="save" onclick="resetUserPw(${u.id},'${(u.username||'').replace(/'/g,"")}')">Set</button>
      </div>
    </td>
  </tr>`).join('');
}
async function resetUserPw(userId, username){
  const inp = document.getElementById(`pw_${userId}`);
  const pw = inp.value;
  if(pw.length < 6){ alert('Password must be at least 6 characters'); return; }
  if(!confirm(`Reset password for "${username}"?`)) return;
  const r = await fetch(`/api/superadmin/user/${userId}/password`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({new_password:pw})});
  const d = await r.json();
  if(d.success){ inp.value=''; showToast(d.message || 'Password reset'); }
  else alert(d.message || 'Failed');
}

function showToast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display='block';
  setTimeout(()=>t.style.display='none', 2500);
}

initDropdowns();
load();
</script>
</body>
</html>
"""
