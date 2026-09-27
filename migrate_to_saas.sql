-- ============================================================
-- Migration: Add SaaS subscription columns to 'company' table
-- Run ONCE on your existing database.
-- ============================================================

ALTER TABLE company
  ADD COLUMN IF NOT EXISTS plan VARCHAR(50) NOT NULL DEFAULT 'trial',
  ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(50) NOT NULL DEFAULT 'Active',
  ADD COLUMN IF NOT EXISTS trial_start DATETIME DEFAULT CURRENT_TIMESTAMP,
  ADD COLUMN IF NOT EXISTS expiry_date DATETIME NULL,
  ADD COLUMN IF NOT EXISTS billing_cycle VARCHAR(20) DEFAULT 'monthly',
  ADD COLUMN IF NOT EXISTS notes TEXT NULL,
  ADD COLUMN IF NOT EXISTS date_created DATETIME DEFAULT CURRENT_TIMESTAMP;

-- Set existing companies to 'pro' active (they were already paying/using)
UPDATE company SET plan = 'pro', subscription_status = 'Active', expiry_date = NULL
WHERE plan = 'trial' AND date_created IS NOT NULL;

-- Add 'role' column to user if missing (for Super Admin support)
ALTER TABLE user
  ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'Active',
  ADD COLUMN IF NOT EXISTS permissions TEXT NULL;

-- ============================================================
-- After running SQL, execute: python create_superadmin.py
-- ============================================================
