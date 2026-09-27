// Country → currency mapping (top ~30 countries; expand as needed)
window.COUNTRY_DATA = [
    { code: 'OM', name: 'Oman',           currency: 'OMR', symbol: 'ر.ع.', timezone: 'Asia/Muscat' },
    { code: 'AE', name: 'UAE',            currency: 'AED', symbol: 'د.إ',  timezone: 'Asia/Dubai' },
    { code: 'SA', name: 'Saudi Arabia',   currency: 'SAR', symbol: 'ر.س', timezone: 'Asia/Riyadh' },
    { code: 'KW', name: 'Kuwait',         currency: 'KWD', symbol: 'د.ك',  timezone: 'Asia/Kuwait' },
    { code: 'QA', name: 'Qatar',          currency: 'QAR', symbol: 'ر.ق', timezone: 'Asia/Qatar' },
    { code: 'BH', name: 'Bahrain',        currency: 'BHD', symbol: 'د.ب',  timezone: 'Asia/Bahrain' },
    { code: 'IN', name: 'India',          currency: 'INR', symbol: '₹',   timezone: 'Asia/Kolkata' },
    { code: 'PK', name: 'Pakistan',       currency: 'PKR', symbol: '₨',   timezone: 'Asia/Karachi' },
    { code: 'LK', name: 'Sri Lanka',      currency: 'LKR', symbol: 'Rs',  timezone: 'Asia/Colombo' },
    { code: 'BD', name: 'Bangladesh',     currency: 'BDT', symbol: '৳',   timezone: 'Asia/Dhaka' },
    { code: 'PH', name: 'Philippines',    currency: 'PHP', symbol: '₱',   timezone: 'Asia/Manila' },
    { code: 'ID', name: 'Indonesia',      currency: 'IDR', symbol: 'Rp',  timezone: 'Asia/Jakarta' },
    { code: 'MY', name: 'Malaysia',       currency: 'MYR', symbol: 'RM',  timezone: 'Asia/Kuala_Lumpur' },
    { code: 'SG', name: 'Singapore',      currency: 'SGD', symbol: 'S$',  timezone: 'Asia/Singapore' },
    { code: 'TH', name: 'Thailand',       currency: 'THB', symbol: '฿',   timezone: 'Asia/Bangkok' },
    { code: 'EG', name: 'Egypt',          currency: 'EGP', symbol: 'ج.م', timezone: 'Africa/Cairo' },
    { code: 'JO', name: 'Jordan',         currency: 'JOD', symbol: 'د.ا', timezone: 'Asia/Amman' },
    { code: 'GB', name: 'United Kingdom', currency: 'GBP', symbol: '£',   timezone: 'Europe/London' },
    { code: 'US', name: 'United States',  currency: 'USD', symbol: '$',   timezone: 'America/New_York' },
    { code: 'CA', name: 'Canada',         currency: 'CAD', symbol: 'C$',  timezone: 'America/Toronto' },
    { code: 'AU', name: 'Australia',      currency: 'AUD', symbol: 'A$',  timezone: 'Australia/Sydney' },
    { code: 'DE', name: 'Germany',        currency: 'EUR', symbol: '€',   timezone: 'Europe/Berlin' },
    { code: 'FR', name: 'France',         currency: 'EUR', symbol: '€',   timezone: 'Europe/Paris' },
    { code: 'IT', name: 'Italy',          currency: 'EUR', symbol: '€',   timezone: 'Europe/Rome' },
    { code: 'ES', name: 'Spain',          currency: 'EUR', symbol: '€',   timezone: 'Europe/Madrid' },
    // ... add more as needed
];

window.COUNTRY_DATA.sort((a, b) => a.name.localeCompare(b.name));