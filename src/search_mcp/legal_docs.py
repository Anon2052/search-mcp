# ruff: noqa: E501  (verbatim legal text; long markdown lines are intentional)
"""Legal documents served as public HTTP endpoints (Stripe onboarding URLs).

Served VERBATIM from the filled-in source documents (IOConnex LLC,
effective September 23, 2026; hardened September 27, 2026). Regenerate from
~/workspace/goals/muse-connectors-passive-income-build/files/legal/
if the sources change.
"""

TERMS_OF_SERVICE: str = """# Terms of Service

**IOConnex LLC**, an Arizona limited liability company ("IOConnex," "we," "us," "our")
Effective date: September 23, 2026 · Last updated: September 27, 2026


## 1. Acceptance of Terms; Eligibility

By accessing or using our API services — including our MCP (Model Context Protocol) servers and any related websites, documentation, or endpoints (collectively, the "Services") — you agree to be bound by these Terms of Service ("Terms"). If you access the Services on behalf of a company, organization, or other entity, you represent and warrant that you have full authority to bind that entity, and "you" means that entity.

You must be at least 18 years old and capable of forming a binding contract. If you do not agree to these Terms, do not access or use the Services. Your continued use of the Services constitutes ongoing acceptance of the then-current Terms.

## 2. Description of Services; No Service-Level Agreement

We provide programmatic API access to software tools, including web search, document conversion, and price tracking, delivered over HTTP and MCP transports. The Services are provided on an "as-available" basis. We do not guarantee uptime, response times, throughput, or result accuracy, and we may modify, suspend, or discontinue any part of the Services at any time, with or without notice.

## 3. Access Credentials; Responsibility for Your Agents

Certain Services require a subscriber key or other credential. You are responsible for maintaining the confidentiality of your credentials and for **all** activity conducted under them — including calls made by autonomous AI agents, scripts, or integrations you operate. An agent acting with your credentials acts as you: you authorize, and are financially responsible for, its usage and charges. Notify us immediately at the contact below of any unauthorized use.

## 4. Payments

- **Per-call (x402):** Certain endpoints require a micropayment in USDC on the Base network under the x402 protocol. The price per call is published in the `402 Payment Required` response. By submitting a signed payment authorization (EIP-3009), you authorize the on-chain debit. You are responsible for network (gas) fees. On-chain settlements are irreversible — see our Refund Policy.
- **Subscriptions (Stripe):** Monthly plans are billed through Stripe, Inc. Subscriptions renew automatically each month until canceled. You may cancel at any time; access continues through the end of the paid period. Failed payments may result in suspension until the balance is resolved.
- **Taxes and price changes:** Prices are in USD unless stated otherwise. You are responsible for all applicable taxes, duties, and governmental charges. We may change prices at any time: per-call changes apply immediately to new calls; subscription changes apply from your next renewal with prior notice.

## 5. Acceptable Use

You agree not to, and not to permit any third party (including your agents) to:

- Use the Services in violation of any applicable law or regulation, including sanctions administered by OFAC and applicable export-control laws;
- Attempt to bypass payment requirements, rate limits, authentication, or access controls;
- Resell, sublicense, or redistribute the Services without our prior written permission;
- Use the Services to infringe intellectual property rights or to collect personal data about third parties without consent;
- Upload or transmit malware, or probe, scan, attack, or overload our infrastructure;
- Systematically scrape, harvest, or replicate the Services or their outputs to build a competing product or service.

We may suspend or terminate access immediately, without notice, for violations of this section. Per-call payments already consumed are not refunded upon termination for cause.

## 6. Intellectual Property

The Services — including all software, models, data compilations, trademarks, trade dress, and documentation — are and remain the exclusive property of IOConnex LLC and are protected by intellectual property laws. We grant you a limited, revocable, non-exclusive, non-transferable, non-sublicensable license to access and use the Services strictly in accordance with these Terms.

You retain all rights in content you submit to the Services ("Inputs"); you grant us a worldwide license to process Inputs solely as necessary to provide the Services. Outputs generated for you are licensed to you for your lawful business purposes; no ownership in our underlying systems, models, or data transfers to you.

## 7. Copyright Complaints (DMCA)

If you believe material accessible via the Services infringes your copyright, send a notice to hello@ioconnex.dev containing: (a) identification of the copyrighted work; (b) identification of the allegedly infringing material (URL or API reference); (c) your contact information; (d) a statement of good-faith belief that the use is unauthorized; (e) a statement, under penalty of perjury, that you are authorized to act for the copyright owner; and (f) your physical or electronic signature. We will process valid notices under the DMCA, including counter-notice procedures.

## 8. Third-Party Services

The Services interoperate with independent third parties — including Stripe, Inc. for card payments, Coinbase Developer Platform as x402 payment facilitator, and upstream data providers. Those parties' own terms and privacy policies govern their processing, and we are not responsible for their services, outages, acts, or omissions.

## 9. Disclaimers

THE SERVICES ARE PROVIDED "AS IS" AND "AS AVAILABLE," WITHOUT WARRANTIES OF ANY KIND, WHETHER EXPRESS, IMPLIED, OR STATUTORY, INCLUDING WITHOUT LIMITATION WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE, AND NON-INFRINGEMENT. WE DO NOT WARRANT THAT THE SERVICES WILL BE UNINTERRUPTED, TIMELY, SECURE, ERROR-FREE, OR FREE OF HARMFUL COMPONENTS. OUTPUTS (SEARCH RESULTS, CONVERTED DOCUMENTS, PRICE DATA) ARE PROVIDED FOR INFORMATIONAL PURPOSES ONLY — INDEPENDENTLY VERIFY ANY OUTPUT BEFORE RELYING ON IT. Any beta, experimental, or pre-release features are provided without warranty of any kind.

## 10. Limitation of Liability

TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW:

(a) IN NO EVENT SHALL IOCONNEX LLC, ITS MEMBERS, OFFICERS, EMPLOYEES, CONTRACTORS, OR AGENTS BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, EXEMPLARY, OR PUNITIVE DAMAGES, OR FOR ANY LOSS OF PROFITS, REVENUE, DATA, GOODWILL, OR USE, ARISING OUT OF OR RELATED TO THE SERVICES OR THESE TERMS, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGES; AND

(b) OUR TOTAL AGGREGATE LIABILITY FOR ALL CLAIMS ARISING OUT OF OR RELATED TO THE SERVICES SHALL NOT EXCEED THE AMOUNTS YOU ACTUALLY PAID US IN THE THREE (3) MONTHS PRECEDING THE EVENT GIVING RISE TO THE CLAIM.

The limitations in this section are a fundamental basis of the bargain between you and us. Some jurisdictions do not allow the exclusion of certain warranties or the limitation of certain damages; in such jurisdictions, our liability is limited to the maximum extent permitted by law.

## 11. Indemnification

You agree to defend, indemnify, and hold harmless IOConnex LLC and its members, officers, employees, contractors, and agents from and against any and all claims, damages, losses, liabilities, costs, and expenses (including reasonable attorneys' fees) arising out of or related to: (a) your use or misuse of the Services; (b) your violation of these Terms or any applicable law or regulation; (c) your Inputs or the actions of agents operating under your credentials; or (d) your infringement or violation of any third-party right. We reserve the right, at our expense, to assume exclusive control of the defense of any matter subject to indemnification, and you agree to cooperate with us.

## 12. Dispute Resolution; Binding Arbitration; Class-Action Waiver

**PLEASE READ THIS SECTION CAREFULLY — IT AFFECTS YOUR LEGAL RIGHTS, INCLUDING YOUR RIGHT TO SUE IN COURT.**

(a) **Informal resolution first.** Before initiating arbitration, you agree to contact us at hello@ioconnex.dev to attempt to resolve the dispute informally for at least thirty (30) days.

(b) **Binding individual arbitration.** Any dispute, claim, or controversy arising out of or relating to the Services or these Terms that is not resolved informally shall be resolved exclusively by binding arbitration on an individual basis, administered by the American Arbitration Association ("AAA") under its Commercial Arbitration Rules, before a single arbitrator, in Maricopa County, Arizona. Judgment on the arbitral award may be entered in any court of competent jurisdiction.

(c) **Class-action and collective-action waiver.** TO THE FULLEST EXTENT PERMITTED BY LAW, YOU AND IOCONNEX LLC AGREE THAT EACH MAY BRING CLAIMS ONLY IN AN INDIVIDUAL CAPACITY AND NOT AS A PLAINTIFF OR CLASS MEMBER IN ANY PURPORTED CLASS, COLLECTIVE, CONSOLIDATED, OR REPRESENTATIVE ACTION OR PROCEEDING. The arbitrator may not consolidate claims or preside over any representative proceeding.

(d) **Jury-trial waiver.** You waive any right to a trial by jury.

(e) **Small-claims carve-out.** Notwithstanding the above, either party may bring an individual claim in small-claims court in Maricopa County, Arizona, in lieu of arbitration.

(f) **Opt-out.** You may opt out of this arbitration section (but not the class-action waiver in (c), which survives) by emailing hello@ioconnex.dev with the subject line "Arbitration Opt-Out" within thirty (30) days after first accepting these Terms, including your name and a clear statement of intent to opt out.

(g) **Severability.** If the class-action waiver in (c) is found unenforceable as to a particular claim, then this entire Section 12 shall not apply to that claim, which shall instead proceed exclusively in the state or federal courts located in Maricopa County, Arizona.

## 13. Termination and Suspension

We may suspend or terminate your access to the Services immediately, with or without notice, for: violation of these Terms; non-payment; suspected fraud, abuse, or security risk; or legal or regulatory requirements. You may stop using the Services at any time. Sections 6 and 8 through 15 survive any termination.

## 14. Changes to These Terms

We may update these Terms from time to time. We will post the revised Terms with a new effective date and, for material changes, provide reasonable advance notice where practicable. Your continued use of the Services after the effective date constitutes acceptance of the revised Terms.

## 15. General Provisions

- **Governing law:** These Terms are governed by the laws of the State of Arizona, without regard to conflict-of-law principles.
- **Entire agreement:** These Terms, together with the Privacy Policy and Refund Policy, constitute the entire agreement regarding the Services.
- **Severability:** If any provision is held unenforceable, it shall be modified to the minimum extent necessary, and the remainder continues in full force.
- **No waiver:** Failure to enforce any provision is not a waiver of future enforcement.
- **Assignment:** You may not assign these Terms without our prior written consent. We may assign freely, including in connection with a merger, acquisition, or sale of assets.
- **Force majeure:** We are not liable for failures or delays caused by events beyond our reasonable control.
- **Notices:** We may notify you via the contact information associated with your use of the Services; you may notify us at hello@ioconnex.dev.
- **No agency:** Nothing in these Terms creates a partnership, joint venture, employment, or agency relationship.

## 16. Contact

IOConnex LLC
hello@ioconnex.dev
"""

PRIVACY_POLICY: str = """# Privacy Policy

**IOConnex LLC**, an Arizona limited liability company ("IOConnex," "we," "us," "our")
Effective date: September 23, 2026 · Last updated: September 27, 2026


## 1. Scope

This policy describes how we collect, use, disclose, and safeguard information when you use our API services, including our MCP servers (the "Services"). Our Services are APIs used primarily by software and AI agents; we collect the minimum data necessary to operate them.

## 2. Information We Collect

**a) Information you provide:** support correspondence and any information you send us when you contact us.

**b) Information collected automatically (request metadata):** IP address, request path, user-agent, timestamp, and usage counters — collected for rate limiting, metering, abuse prevention, security, and operational logs.

**c) Payment information:**
- *x402 micropayments:* payer wallet address, payment amount, and authorization signatures. These are recorded on the public Base blockchain and are visible to anyone. To verify and settle payments, the payment payload is transmitted to our x402 facilitator (Coinbase Developer Platform). We log transaction metadata (transaction hash, payer address, amount) server-side and temporarily hold payment signatures in server memory (up to 5 minutes) to prevent replay attacks.
- *Stripe subscriptions:* payments are processed by Stripe, Inc. We never receive or store full card numbers. We store your Stripe customer identifier, the email address from your Checkout session, and the SHA-256 hash of your subscriber key.

**d) Upstream processing:** to fulfill requests, query contents are transmitted to upstream data providers (e.g., our search index provider). We do not use your API inputs to train machine-learning models.

We do not require user accounts, do not use advertising trackers or third-party analytics cookies, and do not collect names, addresses, or other profile data unless you provide them.

## 3. How We Use Information

- To operate, meter, bill for, and support the Services;
- To prevent abuse, fraud, attacks, and overload, and to enforce our Terms;
- To respond to support requests and legal process;
- To comply with legal, tax, and accounting obligations.

Legal bases for processing (where GDPR applies): performance of a contract, our legitimate interests in operating and securing the Services, and compliance with legal obligations.

## 4. How We Disclose Information

We disclose information only as follows:

- **Service providers (subprocessors):** Stripe, Inc. (payments); Coinbase Developer Platform (x402 facilitation); Render (hosting); Cloudflare (DNS/CDN); and upstream data providers (to fulfill search/fetch/convert requests). Each processes data only as needed to provide its service.
- **Legal compliance:** when required by law, subpoena, or court order, or to protect our rights, users, or the public.
- **Business transfers:** in connection with a merger, acquisition, or sale of assets, subject to this policy's protections.
- **With your consent** or at your direction.

**We do not sell your personal information, and we do not share it for cross-context behavioral advertising** (as those terms are defined under the CCPA/CPRA). We do not share request data with advertisers or data brokers.

## 5. International Data Transfers

The Services are operated in the United States. If you access the Services from outside the US, you consent to the transfer, storage, and processing of your information in the US, where data-protection laws may differ from those in your jurisdiction.

## 6. Data Retention

- Operational and security logs: retained up to 12 months, then deleted or anonymized (our hosting provider's own retention also applies).
- Payment and tax records: retained as required by law, generally 7 years.
- Support correspondence: retained as long as needed to resolve the matter.

Blockchain records of x402 payments are public and permanent; they cannot be deleted by us.

## 7. Security

We use industry-standard safeguards: TLS encryption in transit, hashed subscriber credentials, minimal data collection, and access controls. No system is perfectly secure, and we cannot guarantee absolute security. If we become aware of a breach affecting your personal information, we will notify you as required by applicable law.

## 8. Your Privacy Rights

Depending on your jurisdiction (including California under the CCPA/CPRA, and the EEA/UK under the GDPR), you may have the right to: know what personal information we collect and why; access or receive a portable copy of it; correct inaccuracies; request deletion; and opt out of any sale or sharing of personal information (we do not sell or share, as defined by law).

To exercise these rights, email hello@ioconnex.dev. We will verify your request (e.g., via your subscriber key or Stripe customer identifier) and respond within the time required by law (generally 30–45 days). You may use an authorized agent with your written permission. We will not discriminate against you for exercising your rights. Note: deletion requests cannot remove public blockchain records.

## 9. Children

The Services are not directed to children under 13, and we do not knowingly collect their information. Our Terms require users to be at least 18.

## 10. Changes to This Policy

We may update this policy; material changes will be posted with a revised effective date and, where practicable, reasonable advance notice. Continued use after the effective date constitutes acceptance.

## 11. Contact

IOConnex LLC — Privacy inquiries: hello@ioconnex.dev
"""

REFUND_POLICY: str = """# Refund Policy

**IOConnex LLC**, an Arizona limited liability company ("IOConnex," "we," "us," "our")
Effective date: September 23, 2026 · Last updated: September 27, 2026


## 1. x402 Micropayments — Final and Non-Refundable

Per-call payments made via the x402 protocol (USDC on Base) are settled on a public blockchain and are **final and non-refundable**. Blockchain transactions are irreversible and cannot be undone by us. Network (gas) fees are your responsibility and are never refundable.

If a paid call fails due to our error (for example, a 5xx response after your payment settled), contact us at hello@ioconnex.dev and we will, at our sole discretion, credit or re-run the call. If you are charged twice for the same call due to a processing error, contact us and we will correct it.

## 2. Stripe Subscriptions — 30-Day Money-Back on Initial Purchase

Monthly subscription plans include a **30-day money-back guarantee on your initial purchase only**. If you are not satisfied, contact us within 30 days of the initial charge for a full refund of that charge.

- Renewal charges after the first 30 days are non-refundable.
- You may cancel at any time; access continues through the end of the paid period. We do not prorate partial months.
- If a renewal payment fails, access may be suspended until payment is resolved.

## 3. How to Request a Refund

Email hello@ioconnex.dev with your Stripe receipt or subscription ID (or, for x402, the transaction hash) and a description of the issue. We will respond within 5 business days.

## 4. Chargebacks and Payment Disputes

Contact us first — most issues are resolved within days. If you initiate a chargeback or payment dispute without contacting us, we will submit our transaction records to contest it, and we may suspend or terminate your access pending resolution. Abusive or bad-faith chargebacks will result in permanent termination.

## 5. Currency and Taxes

All prices are in US dollars (USD) unless stated otherwise. You are responsible for any taxes, duties, or governmental charges arising from your purchases.

## 6. Legal Minimums

Nothing in this policy limits refunds or rights required by applicable consumer-protection law in your jurisdiction. Except as required by law, all refund decisions are at our sole discretion.

## 7. Contact

IOConnex LLC — Billing inquiries: hello@ioconnex.dev
"""

# ---------------------------------------------------------------------------
# Support page served at /support (directory listings' supportURL target).
# Plain HTML, no pricing or promotion content.
SUPPORT_PAGE: str = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Support — IOConnex Web Search</title>
</head>
<body>
<h1>Support — IOConnex Web Search</h1>
<p>IOConnex Web Search is a service of <strong>IOConnex LLC</strong>.</p>
<h2>Contact us</h2>
<p>Email: <a href="mailto:hello@ioconnex.dev">hello@ioconnex.dev</a></p>
<p>We read every message and aim to respond within two business days.</p>
<h2>What to include</h2>
<ul>
<li>Which tool you used (web search or page fetch)</li>
<li>The date and approximate time of the request (with timezone)</li>
<li>The query or URL involved, and what you expected to happen</li>
<li>Any error message you received</li>
</ul>
<h2>Service information</h2>
<ul>
<li><a href="/legal/privacy">Privacy policy</a></li>
<li><a href="/legal/terms">Terms of service</a></li>
<li><a href="/legal/refunds">Refund policy</a></li>
</ul>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Landing / live-demo page served at / (the ioconnex.dev "superpowers" links
# point at the bare service roots). Mirrors the doc-convert demo page's look.
# Always free: "/" is in EXEMPT_PREFIXES in x402.py (exact-path match only).
LANDING_PAGE: str = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web Search — Live Demo · IOConnex</title>
<style>
  :root { --bg:#0b0e14; --panel:#12161f; --line:#1e2532; --txt:#e8ecf3; --mut:#9aa4b5;
          --acc:#5eead4; --acc2:#38bdf8; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
         min-height:100vh; display:flex; flex-direction:column; align-items:center; }
  .wrap { width:100%; max-width:860px; padding:40px 20px 60px; }
  header { text-align:center; margin-bottom:32px; }
  .brand { font-size:13px; letter-spacing:.22em; color:var(--acc); text-transform:uppercase; }
  h1 { font-size:30px; margin:10px 0 8px; }
  p.sub { color:var(--mut); font-size:16px; max-width:620px; margin:0 auto 6px; line-height:1.55; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:14px;
          padding:24px; margin-top:24px; }
  .tabs { display:flex; gap:8px; margin-bottom:18px; }
  .tab { flex:1; text-align:center; padding:10px; border:1px solid var(--line); border-radius:10px;
         cursor:pointer; color:var(--mut); font-size:14px; user-select:none; }
  .tab.on { color:var(--txt); border-color:var(--acc); background:#0e2a26; }
  .row { display:flex; gap:10px; margin-top:14px; }
  input[type=text] { flex:1; background:#0b0e14; border:1px solid var(--line); border-radius:10px;
                     color:var(--txt); padding:12px 14px; font-size:14px; }
  button.go { background:linear-gradient(135deg,var(--acc),var(--acc2)); border:0; border-radius:10px;
              color:#06251f; font-weight:700; font-size:15px; padding:12px 26px; cursor:pointer; }
  button.go:disabled { opacity:.5; cursor:wait; }
  .hint { font-size:12.5px; color:var(--mut); margin-top:10px; line-height:1.5; }
  .hint code { color:var(--acc2); }
  .security { margin-top:28px; background:var(--panel); border:1px solid var(--line);
              border-radius:14px; padding:20px 24px; }
  .security h3 { margin:0 0 10px; font-size:15px; color:var(--acc); }
  .security ul { margin:0; padding-left:20px; color:var(--mut); font-size:13.5px; line-height:1.8; }
  .security li strong { color:var(--txt); font-weight:600; }
  #status { margin-top:14px; font-size:14px; color:var(--acc2); min-height:20px; }
  #status.err { color:#f87171; }
  #out { display:none; margin-top:18px; }
  #out .res { border-top:1px solid var(--line); padding:12px 2px; }
  #out .res a { color:var(--acc2); font-size:15px; text-decoration:none; }
  #out .res a:hover { text-decoration:underline; }
  #out .res .u { color:var(--mut); font-size:12px; word-break:break-all; }
  #out .res .s { color:var(--txt); font-size:13.5px; line-height:1.55; margin-top:4px; }
  #out pre { background:#0b0e14; border:1px solid var(--line); border-radius:10px; padding:18px;
             white-space:pre-wrap; word-break:break-word; font-size:13.5px; line-height:1.6;
             max-height:520px; overflow:auto; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  footer { margin-top:36px; text-align:center; color:var(--mut); font-size:13px; line-height:1.7; }
  footer a { color:var(--acc); text-decoration:none; }
  footer .cta { font-size:15px; color:var(--txt); }
  footer .cta a { font-weight:700; border-bottom:1px solid var(--acc); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">IOConnex · Web Search</div>
    <h1>Search the web, live.</h1>
    <p class="sub">Type a query and watch real results come back from the same API
      AI agents call — no signup, no account. First calls are free each month;
      after that it is $0.01 per search, paid per call or by subscription.</p>
  </header>

  <div class="card">
    <div class="tabs">
      <div class="tab on" id="tabSearch">Search the web</div>
      <div class="tab" id="tabFetch">Fetch a page as text</div>
    </div>

    <div id="paneSearch">
      <div class="row">
        <input type="text" id="qIn" placeholder="e.g. best small-business accounting software 2026">
        <button class="go" id="qBtn">Search</button>
      </div>
    </div>

    <div id="paneFetch" style="display:none">
      <div class="row">
        <input type="text" id="urlIn" placeholder="https://example.com/article">
        <button class="go" id="urlBtn">Fetch</button>
      </div>
    </div>

    <div class="hint">This demo runs on the public free tier (a per-IP monthly allowance).
      Building an agent? Connect MCP at <code>https://search.ioconnex.dev/mcp</code>
      or POST to <code>/search</code> — full reference at <a href="/docs" style="color:var(--acc)">/docs</a>.</div>
    <div id="status"></div>
    <div id="out"></div>
  </div>

  <div class="security">
    <h3>How your query is handled</h3>
    <ul>
      <li><strong>Encrypted in transit</strong> — TLS end to end.</li>
      <li><strong>No account, no cookies</strong> — the demo just calls the public API.</li>
      <li><strong>Nothing stored</strong> — IOConnex does not retain demo queries or fetched pages.</li>
      <li><strong>Pay per call</strong> — past the free tier, agents pay per search; no card on file.</li>
    </ul>
  </div>

  <footer>
    One of three IOConnex services for AI agents — search, document conversion, price tracking.<br>
    <a href="https://ioconnex.dev">ioconnex.dev</a> · <a href="/support">Support</a> ·
    <a href="/legal/terms">Terms</a> · <a href="/legal/privacy">Privacy</a><br><br>
    <span class="cta">Not seeing the AI help you are looking for?
    <a href="mailto:hello@ioconnex.dev?subject=IOConnex%20AI%20help">Talk to us</a>.</span>
  </footer>
</div>

<script>
const $ = id => document.getElementById(id);
const status = (t, err) => { const s = $('status'); s.textContent = t; s.className = err ? 'err' : ''; };
function esc(s){ return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function showSearch(results){
  const out = $('out');
  if (!results || !results.length) { out.innerHTML = '<p class="res">No results came back.</p>'; }
  else {
    out.innerHTML = results.map(r =>
      '<div class="res"><a href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.title || r.url) + '</a>' +
      '<div class="u">' + esc(r.url) + '</div><div class="s">' + esc(r.snippet || '') + '</div></div>').join('');
  }
  out.style.display = 'block';
}
function showText(text){
  const out = $('out');
  out.innerHTML = '<pre></pre>';
  out.querySelector('pre').textContent = text;
  out.style.display = 'block';
}
async function doSearch(){
  const q = $('qIn').value.trim();
  if (!q) { status('Enter a query first.', true); return; }
  status('Searching …'); $('out').style.display = 'none'; $('qBtn').disabled = true;
  try {
    const res = await fetch('/search', { method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({query:q, count:5}) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    status('Done — ' + (data.results ? data.results.length : 0) + ' results.');
    showSearch(data.results);
  } catch(e){ status('Search failed: ' + e.message + '. The free tier may be used up for this network — agents can still call the paid API.', true); }
  $('qBtn').disabled = false;
}
async function doFetch(){
  const u = $('urlIn').value.trim();
  if (!u) { status('Enter a URL first.', true); return; }
  status('Fetching …'); $('out').style.display = 'none'; $('urlBtn').disabled = true;
  try {
    const res = await fetch('/fetch', { method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({url:u}) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    status('Done.');
    showText(data.text || JSON.stringify(data, null, 2));
  } catch(e){ status('Fetch failed: ' + e.message, true); }
  $('urlBtn').disabled = false;
}
$('qBtn').onclick = doSearch;
$('qIn').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
$('urlBtn').onclick = doFetch;
$('urlIn').addEventListener('keydown', e => { if (e.key === 'Enter') doFetch(); });
$('tabSearch').onclick = () => { $('tabSearch').classList.add('on'); $('tabFetch').classList.remove('on');
  $('paneSearch').style.display=''; $('paneFetch').style.display='none'; };
$('tabFetch').onclick = () => { $('tabFetch').classList.add('on'); $('tabSearch').classList.remove('on');
  $('paneFetch').style.display=''; $('paneSearch').style.display='none'; };
</script>
</body>
</html>
"""
