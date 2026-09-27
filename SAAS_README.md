# ServiceBook ERP — SaaS Conversion Guide

## What Changed

### New Files
| File | Purpose |
|------|---------|
| `saas_middleware.py` | All SaaS logic: routes, decorators, HTML templates |
| `create_superadmin.py` | One-time script to create the Super Admin user |
| `migrate_to_saas.sql` | SQL migration for existing databases |

### Modified Files
| File | Changes |
|------|---------|
| `app.py` | Added SaaS imports, updated auth middleware, login stores `role`, subscription check, `register_saas_routes()` call |
| `models.py` | `Company` now has `plan`, `subscription_status`, `expiry_date`, `billing_cycle`, `notes`, `date_created`, `is_active` property |

---

## Plan Tiers

| Plan | Bookings | Users | Branches |
|------|---------|-------|---------|
| Trial (14 days) | 50 | 2 | 1 |
| Starter | 500 | 5 | 1 |
| Pro | 5,000 | 20 | 5 |
| Enterprise | Unlimited | Unlimited | Unlimited |

---

## New Routes

| Route | Who | Purpose |
|-------|-----|---------|
| `/signup` | Public | Self-service tenant registration |
| `/api/signup` | Public POST | Create tenant + 14-day trial |
| `/subscription-expired` | Any | Shown when plan is expired/suspended |
| `/superadmin` | Super Admin | Dashboard to manage all tenants |
| `/api/superadmin/tenants` | Super Admin | List all tenants (JSON) |
| `/api/superadmin/tenant/<id>` | Super Admin POST | Update plan/status/expiry |
| `/api/superadmin/impersonate/<id>` | Super Admin POST | Log in as a tenant |
| `/api/superadmin/unimpersonate` | Super Admin POST | Return to Super Admin |
| `/api/my_subscription` | Tenant | Get own plan/usage info |

---

## Setup Steps

### Fresh Deployment
```bash
# 1. Deploy the app
# 2. Create super admin
python create_superadmin.py superadmin YourSecurePassword!
# 3. Customers register at /signup
```

### Migrating Existing Single-Company Database
```bash
# 1. Run the SQL migration
mysql -u user -p your_db < migrate_to_saas.sql
# 2. Create super admin
python create_superadmin.py superadmin YourSecurePassword!
# 3. Log in at /superadmin to manage tenants
```

---

## Super Admin Login
- Go to `/login`, use the superadmin credentials
- After login, navigate to `/superadmin` for the tenant management panel
- Super Admin bypasses all subscription restrictions

## Tenant Self-Service
- New customers go to `/signup`
- 14-day free trial starts automatically
- When trial expires, they see `/subscription-expired` until Super Admin upgrades their plan

## Subscription Enforcement
- Every request checks `Company.is_active` (status == Active AND expiry not passed)
- API calls get a 403 JSON response
- Page requests redirect to `/subscription-expired`
- Super Admin role bypasses all checks

---

## Environment Variables (recommended for production)
```bash
SQLALCHEMY_DATABASE_URI=mysql+pymysql://user:pass@host/dbname
SECRET_KEY=your-very-long-random-secret
```
