/**
 * Universal Loading Overlay for Service Book ERP
 * Automatically shows a premium loading spinner on:
 *   1. All <form> submissions (method="POST" or GET)
 *   2. All fetch() API calls (GET & POST)
 * 
 * Usage: Just include this script in any HTML page:
 *   <script src="/static/loading.js"></script>
 */
(function () {
    'use strict';

    // ======== 1. INJECT CSS + HTML OVERLAY ========
    const style = document.createElement('style');
    style.textContent = `
        /* ===== UNIVERSAL LOADING OVERLAY ===== */
        #erpLoadingOverlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(15, 23, 42, 0.45);
            backdrop-filter: blur(4px);
            -webkit-backdrop-filter: blur(4px);
            z-index: 99999;
            align-items: center;
            justify-content: center;
            transition: opacity 0.2s ease;
            opacity: 0;
        }
        #erpLoadingOverlay.erp-loading-visible {
            display: flex;
            opacity: 1;
        }
        .erp-loading-card {
            background: white;
            border-radius: 16px;
            padding: 36px 48px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 20px;
            animation: erpLoadFadeIn 0.25s ease;
        }
        @keyframes erpLoadFadeIn {
            from { opacity: 0; transform: translateY(12px) scale(0.96); }
            to   { opacity: 1; transform: translateY(0) scale(1); }
        }

        /* Triple-dot spinner */
        .erp-spinner {
            display: flex;
            gap: 8px;
        }
        .erp-spinner-dot {
            width: 14px;
            height: 14px;
            border-radius: 50%;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            animation: erpDotBounce 1.4s ease-in-out infinite both;
        }
        .erp-spinner-dot:nth-child(1) { animation-delay: -0.32s; }
        .erp-spinner-dot:nth-child(2) { animation-delay: -0.16s; }
        .erp-spinner-dot:nth-child(3) { animation-delay: 0s; }

        @keyframes erpDotBounce {
            0%, 80%, 100% { transform: scale(0.4); opacity: 0.4; }
            40%            { transform: scale(1);   opacity: 1;   }
        }

        .erp-loading-text {
            font-family: 'Segoe UI', Tahoma, sans-serif;
            font-size: 14px;
            font-weight: 600;
            color: #475569;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }

        /* Hide overlay when printing */
        @media print {
            #erpLoadingOverlay { display: none !important; }
        }
    `;
    document.head.appendChild(style);

    // Create overlay element
    const overlay = document.createElement('div');
    overlay.id = 'erpLoadingOverlay';
    overlay.innerHTML = `
        <div class="erp-loading-card">
            <div class="erp-spinner">
                <div class="erp-spinner-dot"></div>
                <div class="erp-spinner-dot"></div>
                <div class="erp-spinner-dot"></div>
            </div>
            <div class="erp-loading-text">Processing...</div>
        </div>
    `;

    // Append overlay as soon as body is available
    function attachOverlay() {
        if (document.body) {
            document.body.appendChild(overlay);
        } else {
            document.addEventListener('DOMContentLoaded', () => document.body.appendChild(overlay));
        }
    }
    attachOverlay();

    // ======== 2. SHOW / HIDE HELPERS ========
    let activeRequests = 0;

    function showLoading() {
        activeRequests++;
        overlay.classList.add('erp-loading-visible');
    }

    function hideLoading() {
        activeRequests = Math.max(0, activeRequests - 1);
        if (activeRequests === 0) {
            overlay.classList.remove('erp-loading-visible');
        }
    }

    // ======== 3. INTERCEPT ALL FORM SUBMISSIONS ========
    document.addEventListener('submit', function (e) {
        const form = e.target;
        if (!form || form.tagName !== 'FORM') return;

        // Skip forms that are inline delete buttons with confirm() — 
        // the loading shows even if user clicks Cancel on confirm(), 
        // but disappears on page navigation anyway.
        // We show loading for ALL form submits to cover POST + page reloads.
        showLoading();
    }, true);

    // ======== 4. MONKEY-PATCH fetch() FOR AJAX CALLS ========
    const originalFetch = window.fetch;

    window.fetch = function (...args) {
        showLoading();
        return originalFetch.apply(this, args)
            .then(response => {
                hideLoading();
                return response;
            })
            .catch(error => {
                hideLoading();
                throw error;
            });
    };

    // ======== 5. SAFETY: HIDE ON PAGE SHOW (back/forward cache) ========
    window.addEventListener('pageshow', function () {
        activeRequests = 0;
        overlay.classList.remove('erp-loading-visible');
    });

    // ======== 6. EXPOSE GLOBALLY ========
    window.erpShowLoading = showLoading;
    window.erpHideLoading = hideLoading;

})();
