"""
Drug Intelligence server — Railway-ready
"""
import os, re, json, pathlib, anthropic, urllib.request, urllib.parse, hashlib, secrets, smtplib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

PORT = int(os.environ.get("PORT", 8501))
BASE = pathlib.Path(__file__).parent

# ── AUTH CONFIG ────────────────────────────────────────────────────────────────
GUEST_USERNAME = os.environ.get("GUEST_USERNAME", "guest")
GUEST_PASSWORD = os.environ.get("GUEST_PASSWORD", "drugintelligence2025")

# In-memory session store: token -> True
SESSIONS = {}

def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()

def verify_credentials(username, password):
    return username == GUEST_USERNAME and password == GUEST_PASSWORD

def create_session():
    token = secrets.token_hex(32)
    SESSIONS[token] = True
    return token

def is_valid_session(token):
    return token and SESSIONS.get(token, False)

def get_session_token(headers):
    cookie = headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("di_session="):
            return part[len("di_session="):]
    return None

# ── EMAIL CONFIG ───────────────────────────────────────────────────────────────
CONTACT_EMAIL = "nishanth.kandepedu@zohomail.in"
SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.zoho.in")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASS     = os.environ.get("SMTP_PASS", "")

def send_contact_email(name, email, message):
    if not SMTP_USER or not SMTP_PASS:
        # Log to console if email not configured
        print(f"\n📬 CONTACT REQUEST\nFrom: {name} <{email}>\nMessage: {message}\n")
        return True
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = CONTACT_EMAIL
        msg["Subject"] = f"Drug Intelligence — Access Request from {name}"
        body = f"""New contact request from Drug Intelligence login page:

Name: {name}
Email: {email}

Message:
{message}

---
Sent from drugintelligence.bio
"""
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, CONTACT_EMAIL, msg.as_string())
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

# ── API KEY ────────────────────────────────────────────────────────────────────
def get_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    secrets_file = BASE / ".streamlit" / "secrets.toml"
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            m = re.match(r"ANTHROPIC_API_KEY\s*=\s*[\"'](.+)[\"']", line.strip())
            if m:
                return m.group(1).strip()
    return ""

api_key = get_api_key()

# ── HTML PAGES ─────────────────────────────────────────────────────────────────
login_html = (BASE / "login.html").read_text(encoding="utf-8")
login_bytes = login_html.encode("utf-8")

app_html = (BASE / "trialens.html").read_text(encoding="utf-8")
configured = "true" if api_key else "false"
inject = "<script>\nwindow.__TL_KEY__ = '';\nwindow.__TL_CONFIGURED__ = " + configured + ";\n</script>"
app_html = app_html.replace("</head>", inject + "\n</head>", 1)
app_bytes = app_html.encode("utf-8")

# ── HTTP UTILS ─────────────────────────────────────────────────────────────────
def http_get(url, timeout=8):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "DrugIntelligence/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def fetch_trials(compound):
    params = urllib.parse.urlencode({
        "query.intr": compound, "pageSize": "6",
        "format": "json", "sort": "LastUpdatePostDate:desc"
    })
    data = http_get("https://clinicaltrials.gov/api/v2/studies?" + params)
    trials = []
    for study in data.get("studies", []):
        p = study.get("protocolSection", {})
        ident    = p.get("identificationModule", {})
        status   = p.get("statusModule", {})
        design   = p.get("designModule", {})
        desc     = p.get("descriptionModule", {})
        sponsor  = p.get("sponsorCollaboratorsModule", {})
        conds    = p.get("conditionsModule", {})
        outcomes = p.get("outcomesModule", {})
        phases   = design.get("phases", [])
        phase_str = phases[0].replace("PHASE","Phase ").replace("_"," ") if phases else ""
        raw_status = status.get("overallStatus","")
        primary_outcomes = outcomes.get("primaryOutcomes",[])
        primary_outcome = primary_outcomes[0].get("measure","") if primary_outcomes else ""
        trials.append({
            "nctId":        ident.get("nctId",""),
            "title":        ident.get("briefTitle",""),
            "status":       raw_status.replace("_"," ").title(),
            "phase":        phase_str,
            "conditions":   conds.get("conditions",[])[:3],
            "sponsor":      sponsor.get("leadSponsor",{}).get("name",""),
            "enrollment":   design.get("enrollmentInfo",{}).get("count",""),
            "startDate":    status.get("startDateStruct",{}).get("date",""),
            "completionDate": status.get("primaryCompletionDateStruct",{}).get("date",""),
            "summary":      desc.get("briefSummary",""),
            "primaryOutcome": primary_outcome,
            "source":       "ClinicalTrials.gov",
        })
    return trials

def fetch_pubchem_properties(compound):
    search_url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/" + urllib.parse.quote(compound) + "/cids/JSON"
    cid_data = http_get(search_url)
    cids = cid_data.get("IdentifierList",{}).get("CID",[])
    if not cids: return None, None
    cid = cids[0]
    props_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}/property/MolecularWeight,XLogP,TPSA,HBondDonorCount,HBondAcceptorCount,RotatableBondCount,HeavyAtomCount/JSON"
    props_data = http_get(props_url)
    props = props_data.get("PropertyTable",{}).get("Properties",[{}])[0]
    return {
        "molecular_weight": props.get("MolecularWeight"),
        "logp":             props.get("XLogP"),
        "tpsa":             props.get("TPSA"),
        "hbd":              props.get("HBondDonorCount"),
        "hba":              props.get("HBondAcceptorCount"),
        "rotatable_bonds":  props.get("RotatableBondCount"),
        "heavy_atoms":      props.get("HeavyAtomCount"),
    }, cid

def fetch_real_sar_data(compound):
    result = {"bioactivity": [], "sources": [], "physicochemical": None}
    try:
        search_url = "https://www.ebi.ac.uk/chembl/api/data/molecule?pref_name__iexact=" + urllib.parse.quote(compound) + "&format=json&limit=1"
        mol_data = http_get(search_url)
        mols = mol_data.get("molecules",[])
        if mols:
            chembl_id = mols[0].get("molecule_chembl_id","")
            if chembl_id:
                act_url = f"https://www.ebi.ac.uk/chembl/api/data/activity?molecule_chembl_id={chembl_id}&standard_type__in=IC50,Ki,EC50,Kd,GI50&format=json&limit=20"
                act_data = http_get(act_url)
                for a in act_data.get("activities",[]):
                    val = a.get("standard_value")
                    unit = a.get("standard_units","")
                    atype = a.get("standard_type","")
                    assay_desc = a.get("assay_description","")
                    doc_ref = a.get("document_chembl_id","")
                    if val:
                        result["bioactivity"].append({
                            "type": atype, "value": val, "unit": unit,
                            "assay": assay_desc[:80] if assay_desc else "",
                            "reference": doc_ref, "source": "ChEMBL",
                        })
                if result["bioactivity"]:
                    result["sources"].append("ChEMBL (" + chembl_id + ")")
    except Exception as e:
        result["chembl_error"] = str(e)
    try:
        props, cid = fetch_pubchem_properties(compound)
        if props:
            result["physicochemical"] = props
            result["pubchem_cid"] = cid
            result["sources"].append("PubChem (CID " + str(cid) + ")")
    except Exception as e:
        result["pubchem_error"] = str(e)
    return result

# ── REQUEST HANDLER ────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        token = get_session_token(self.headers)

        if path == "/" or path == "":
            if is_valid_session(token):
                self.redirect("/app")
            else:
                self.redirect("/login")
            return

        if path == "/login":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(login_bytes)))
            self.end_headers()
            self.wfile.write(login_bytes)
            return

        if path == "/app":
            if not is_valid_session(token):
                self.redirect("/login")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(app_bytes)))
            self.end_headers()
            self.wfile.write(app_bytes)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        token = get_session_token(self.headers)

        # ── LOGIN ──
        if path == "/api/login":
            username = body.get("username", "").strip()
            password = body.get("password", "").strip()
            if verify_credentials(username, password):
                session_token = create_session()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"di_session={session_token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400")
                self.send_header("Access-Control-Allow-Origin", "*")
                resp = json.dumps({"success": True}).encode()
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            else:
                self.send_json({"success": False, "error": "Invalid credentials"}, 401)
            return

        # ── LOGOUT ──
        if path == "/api/logout":
            token = get_session_token(self.headers)
            if token and token in SESSIONS:
                del SESSIONS[token]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "di_session=; Path=/; HttpOnly; Max-Age=0")
            resp = json.dumps({"success": True}).encode()
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        # ── CONTACT ──
        if path == "/api/contact":
            name    = body.get("name", "").strip()
            email   = body.get("email", "").strip()
            message = body.get("message", "").strip()
            if not name or not email or not message:
                self.send_json({"success": False, "error": "Missing fields"})
                return
            ok = send_contact_email(name, email, message)
            self.send_json({"success": ok})
            return

        # ── PROTECTED API ROUTES ──
        if not is_valid_session(token):
            self.send_json({"error": "Unauthorised"}, 401)
            return

        if path == "/api/feed":
            try:
                import datetime
                yesterday = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
                today = datetime.date.today().strftime("%Y-%m-%d")
                params = urllib.parse.urlencode({
                    "pageSize": "500", "format": "json",
                    "sort": "LastUpdatePostDate:desc",
                    "filter.advanced": (
                        "AREA[LastUpdatePostDate]RANGE[" + yesterday + "," + today + "] AND "
                        "AREA[StudyType]INTERVENTIONAL AND "
                        "AREA[InterventionType]DRUG"
                    )
                })
                data = http_get("https://clinicaltrials.gov/api/v2/studies?" + params)
                trials = []
                for study in data.get("studies", []):
                    p = study.get("protocolSection", {})
                    ident   = p.get("identificationModule", {})
                    status  = p.get("statusModule", {})
                    design  = p.get("designModule", {})
                    sponsor = p.get("sponsorCollaboratorsModule", {})
                    conds   = p.get("conditionsModule", {})
                    phases  = design.get("phases", [])
                    phase_str = phases[0].replace("PHASE","Phase ").replace("_"," ") if phases else ""
                    raw_status = status.get("overallStatus","")
                    last_update = status.get("lastUpdatePostDateStruct",{}).get("date","")
                    start = status.get("startDateStruct",{}).get("date","")
                    trials.append({
                        "id": ident.get("nctId",""),
                        "title": ident.get("briefTitle",""),
                        "conditions": ", ".join(conds.get("conditions",[])[:2]),
                        "sponsor": sponsor.get("leadSponsor",{}).get("name",""),
                        "status": raw_status.replace("_"," ").title(),
                        "phase": phase_str,
                        "lastUpdate": last_update,
                        "startDate": start[:7] if start else "",
                    })
                resp = json.dumps({"trials": trials, "total": len(trials)}).encode()
            except Exception as e:
                resp = json.dumps({"error": str(e)}).encode()

        elif path == "/api/trials":
            try:
                resp = json.dumps({"trials": fetch_trials(body.get("compound",""))}).encode()
            except Exception as e:
                resp = json.dumps({"error": str(e)}).encode()

        elif path == "/api/chembl":
            try:
                resp = json.dumps(fetch_real_sar_data(body.get("compound",""))).encode()
            except Exception as e:
                resp = json.dumps({"error": str(e)}).encode()

        elif path == "/api/claude":
            try:
                client = anthropic.Anthropic(api_key=api_key)
                msg = client.messages.create(
                    model="claude-opus-4-5", max_tokens=3000,
                    system=body.get("system",""),
                    messages=[{"role":"user","content":body.get("prompt","")}],
                )
                raw = msg.content[0].text.strip()
                raw = re.sub(r"```json\s*|\s*```","",raw).strip()
                resp = json.dumps({"result": json.loads(raw)}).encode()
            except Exception as e:
                resp = json.dumps({"error": str(e)}).encode()

        else:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(resp)))
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(resp)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "?"
        path = args[0].split(" ")[1] if " " in str(args[0]) else str(args[0])
        print("  " + str(status) + "  " + path)

print("Drug Intelligence running on port " + str(PORT) + " | API key: " + ("configured" if api_key else "NOT SET"))
HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
