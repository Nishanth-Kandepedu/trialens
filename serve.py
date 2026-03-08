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
def http_get(url, timeout=20, retries=2):
    """GET with retry on timeout/transient errors."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "DrugIntelligence/1.0"})
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            if attempt < retries:
                import time; time.sleep(1)
    raise last_err

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

def fetch_pubchem_bioassay(cid):
    """
    Fetch bioassay data from PubChem for a given CID.
    Captures both potency assays (IC50, Ki, EC50) AND ADMET assays.
    This is the primary bioactivity source when ChEMBL API is unreachable.
    """
    assays = []
    try:
        aid_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}/assaysummary/JSON"
        data = http_get(aid_url)
        rows = data.get("Table", {}).get("Row", [])
        print(f"[PubChem] assaysummary: {len(rows)} rows for CID {cid}", flush=True)

        potency_keywords = ["ic50", "ki", "ec50", "kd ", "kd,", "inhibit", "binding",
                            "affinity", "potency", "activity", "selectiv"]
        admet_keywords   = ["herg", "cyp", "clearance", "half-life", "bioavail", "permeab",
                            "protein bind", "toxicity", "ames", "metaboli", "absorption",
                            "caco", "bbb", "pgp", "solubil", "plasma", "logp", "mtt",
                            "cytotox", "lethal", "ld50"]

        for row in rows[:200]:
            cells = row.get("Cell", [])
            if len(cells) < 6:
                continue
            aid          = str(cells[0]) if cells else ""
            assay_name   = str(cells[3]) if len(cells) > 3 else ""
            outcome      = str(cells[5]) if len(cells) > 5 else ""
            act_val      = str(cells[6]) if len(cells) > 6 else ""
            act_unit     = str(cells[7]) if len(cells) > 7 else ""
            name_lower   = assay_name.lower()

            # Skip rows with no value
            if not act_val or act_val in ("", "0", "null", "None"):
                continue

            if any(kw in name_lower for kw in potency_keywords):
                assays.append({
                    "type": assay_name[:80],
                    "value": act_val,
                    "unit": act_unit,
                    "outcome": outcome,
                    "reference": f"PubChem AID {aid}",
                    "source": f"PubChem CID {cid}",
                    "assay_category": "potency"
                })
            elif any(kw in name_lower for kw in admet_keywords):
                assays.append({
                    "type": assay_name[:80],
                    "value": act_val,
                    "unit": act_unit,
                    "outcome": outcome,
                    "reference": f"PubChem AID {aid}",
                    "source": f"PubChem CID {cid}",
                    "assay_category": "ADME"
                })
    except Exception as e:
        print(f"[PubChem] bioassay error for CID {cid}: {e}", flush=True)
    return assays[:50]

def resolve_chembl_id(compound):
    """
    Resolve a compound name to a ChEMBL ID.
    Returns (chembl_id, debug_log).

    Primary path: PubChem name -> CID -> cross-reference -> ChEMBL ID
    This avoids direct calls to ebi.ac.uk which may be blocked by some proxies.
    Fallback: direct ChEMBL API calls.
    """
    q = compound.strip()
    log = []

    def pick_best(mols, query):
        if not mols:
            return ""
        ql = query.lower().strip()
        for m in mols:
            if (m.get("pref_name") or "").lower().strip() == ql:
                return m.get("molecule_chembl_id", "")
        hits = sorted(
            [m for m in mols if (m.get("pref_name") or "").lower().startswith(ql)],
            key=lambda m: len(m.get("pref_name") or "")
        )
        if hits:
            return hits[0].get("molecule_chembl_id", "")
        for m in mols:
            if ql in (m.get("pref_name") or "").lower():
                return m.get("molecule_chembl_id", "")
        return mols[0].get("molecule_chembl_id", "")

    # ── Path 1: PubChem name → CID → cross-ref to ChEMBL (most reliable) ────
    try:
        cid_url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/" + urllib.parse.quote(q) + "/cids/JSON"
        cid_data = http_get(cid_url)
        cids = cid_data.get("IdentifierList", {}).get("CID", [])
        if cids:
            pubchem_cid = cids[0]
            log.append(f"PubChem CID: {pubchem_cid}")
            # Get cross-references from PubChem — includes ChEMBL SID/source
            xref_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{pubchem_cid}/xrefs/RegistryID/JSON"
            xref_data = http_get(xref_url)
            reg_ids = xref_data.get("InformationList", {}).get("Information", [{}])[0].get("RegistryID", [])
            for rid in reg_ids:
                if str(rid).upper().startswith("CHEMBL"):
                    log.append(f"Found via PubChem xref: {rid}")
                    print(f"[ChEMBL] '{compound}' -> '{rid}' via PubChem xref", flush=True)
                    return rid, log
            log.append(f"PubChem xref: no ChEMBL ID in {len(reg_ids)} registry IDs")
        else:
            log.append("PubChem: no CID found")
    except Exception as e:
        log.append(f"PubChem xref ERROR: {e}")
        print(f"[ChEMBL] PubChem xref error for '{compound}': {e}", flush=True)

    # ── Path 2: UniChem (different subdomain from ChEMBL API, often not blocked) ─
    try:
        # UniChem source 1 = ChEMBL
        unichem_url = "https://www.ebi.ac.uk/unichem/rest/orphanIdSearch/search?searchTerm=" + urllib.parse.quote(q)
        # Actually use the compound name → InChI → ChEMBL approach via UniChem
        # Simpler: use UniChem's name search endpoint
        unichem_url = "https://www.ebi.ac.uk/unichem/api/v1/compounds?name=" + urllib.parse.quote(q) + "&type=name"
        uc_data = http_get(unichem_url)
        compounds_list = uc_data.get("compounds", []) if isinstance(uc_data, dict) else []
        for c in compounds_list[:3]:
            for src in (c.get("sources") or []):
                if (src.get("shortName") or "").upper() == "CHEMBL":
                    cid = src.get("compoundId", "")
                    if cid:
                        log.append(f"UniChem match: {cid}")
                        print(f"[ChEMBL] '{compound}' -> '{cid}' via UniChem", flush=True)
                        return cid, log
        log.append(f"UniChem: no match")
    except Exception as e:
        log.append(f"UniChem ERROR: {e}")
        print(f"[ChEMBL] UniChem error for '{compound}': {e}", flush=True)

    # ── Path 3: Direct ChEMBL API (may be blocked) ───────────────────────────
    BASE = "https://www.ebi.ac.uk/chembl/api/data"
    for endpoint, label in [
        (f"{BASE}/molecule?pref_name__iexact={urllib.parse.quote(q)}&format=json&limit=3", "chembl_pref"),
        (f"{BASE}/molecule/search?q={urllib.parse.quote(q)}&format=json&limit=10", "chembl_search"),
    ]:
        try:
            resp = http_get(endpoint)
            mols = resp.get("molecules", [])
            names = [m.get("pref_name", "?") for m in mols[:4]]
            log.append(f"{label}: {len(mols)} results {names}")
            print(f"[ChEMBL] {label}: {len(mols)} results {names}", flush=True)
            cid = pick_best(mols, q)
            if cid:
                log.append(f"MATCHED via {label}: {cid}")
                print(f"[ChEMBL] '{compound}' -> '{cid}' via {label}", flush=True)
                return cid, log
        except Exception as e:
            log.append(f"{label} ERROR: {e}")
            print(f"[ChEMBL] {label} error: {e}", flush=True)

    print(f"[ChEMBL] '{compound}' NOT FOUND. Log: {log}", flush=True)
    return "", log


def fetch_real_sar_data(compound):
    result = {"bioactivity": [], "adme_data": [], "sources": [], "physicochemical": None}

    # ── ChEMBL: Potency + ADME ──────────────────────────────────────────────
    try:
        chembl_id, chembl_log = resolve_chembl_id(compound)
        result["chembl_lookup_log"] = chembl_log
        if chembl_id:
            result["chembl_id"] = chembl_id

            # Potency data
            try:
                act_url = f"https://www.ebi.ac.uk/chembl/api/data/activity?molecule_chembl_id={chembl_id}&standard_type__in=IC50,Ki,EC50,Kd,GI50,MIC,CC50&format=json&limit=100&order_by=pchembl_value"
                act_data = http_get(act_url)
                for a in act_data.get("activities", []):
                    val = a.get("standard_value")
                    if val:
                        result["bioactivity"].append({
                            "type": a.get("standard_type", ""),
                            "value": val,
                            "unit": a.get("standard_units", ""),
                            "assay": (a.get("assay_description") or "")[:80],
                            "reference": a.get("document_chembl_id", ""),
                            "source": "ChEMBL",
                            "assay_category": "potency"
                        })
            except Exception as e:
                result["chembl_potency_error"] = str(e)
                print(f"[ChEMBL] potency fetch BLOCKED: {e}", flush=True)

            # ADME assays (assay_type=A covers ADME in ChEMBL)
            try:
                adme_url = f"https://www.ebi.ac.uk/chembl/api/data/activity?molecule_chembl_id={chembl_id}&assay_type=A&format=json&limit=50"
                adme_data = http_get(adme_url)
                for a in adme_data.get("activities", []):
                    val = a.get("standard_value")
                    atype = a.get("standard_type", "")
                    if val and atype:
                        entry = {
                            "type": atype,
                            "value": val,
                            "unit": a.get("standard_units", ""),
                            "assay": (a.get("assay_description") or "")[:80],
                            "reference": a.get("document_chembl_id", ""),
                            "source": "ChEMBL",
                            "assay_category": "ADME"
                        }
                        result["adme_data"].append(entry)
                        result["bioactivity"].append(entry)
            except Exception as e:
                result["chembl_adme_error"] = str(e)

            # Toxicity assays (assay_type=T)
            try:
                tox_url = f"https://www.ebi.ac.uk/chembl/api/data/activity?molecule_chembl_id={chembl_id}&assay_type=T&format=json&limit=20"
                tox_data = http_get(tox_url)
                for a in tox_data.get("activities", []):
                    val = a.get("standard_value")
                    atype = a.get("standard_type", "")
                    if val and atype:
                        entry = {
                            "type": atype,
                            "value": val,
                            "unit": a.get("standard_units", ""),
                            "assay": (a.get("assay_description") or "")[:80],
                            "reference": a.get("document_chembl_id", ""),
                            "source": "ChEMBL",
                            "assay_category": "toxicity"
                        }
                        result["adme_data"].append(entry)
                        result["bioactivity"].append(entry)
            except Exception:
                pass

            result["sources"].append("ChEMBL (" + chembl_id + ")")
    except Exception as e:
        result["chembl_error"] = str(e)

    # ── PubChem: Physicochemical + Bioassay ──────────────────────────────────
    try:
        props, cid = fetch_pubchem_properties(compound)
        if props:
            result["physicochemical"] = props
            result["pubchem_cid"] = cid
            result["sources"].append("PubChem (CID " + str(cid) + ")")

            # Fetch PubChem bioassay data (potency + ADMET)
            try:
                pubchem_assays = fetch_pubchem_bioassay(cid)
                if pubchem_assays:
                    result["adme_data"].extend(pubchem_assays)
                    result["bioactivity"].extend(pubchem_assays)
                    result["sources"].append("PubChem BioAssay")
                print(f"[PubChem] bioassay fetch: {len(pubchem_assays)} assays", flush=True)
            except Exception as e:
                print(f"[PubChem] bioassay error: {e}", flush=True)
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

        # ── NCT SEARCH ──
        if path == "/api/nct-search":
            try:
                q = body.get("query", "").strip()
                if not q:
                    self.send_json({"trials": []})
                    return
                # Search by NCT ID or keyword across title/condition/intervention
                params = urllib.parse.urlencode({
                    "pageSize": "20", "format": "json",
                    "sort": "LastUpdatePostDate:desc",
                    "query.term": q,
                    "filter.advanced": "AREA[StudyType]INTERVENTIONAL AND AREA[InterventionType]DRUG"
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
                    nct_id = ident.get("nctId","")
                    trials.append({
                        "id": nct_id,
                        "title": ident.get("briefTitle",""),
                        "conditions": ", ".join(conds.get("conditions",[])[:2]),
                        "sponsor": sponsor.get("leadSponsor",{}).get("name",""),
                        "status": raw_status.replace("_"," ").title(),
                        "phase": phase_str,
                        "lastUpdate": last_update,
                        "startDate": start[:7] if start else "",
                        "url": "https://clinicaltrials.gov/study/" + nct_id,
                    })
                self.send_json({"trials": trials, "source": "ClinicalTrials.gov", "total": len(trials)})
            except Exception as e:
                self.send_json({"error": str(e), "trials": []})
            return

        # ── PROTECTED API ROUTES ──
        if not is_valid_session(token):
            self.send_json({"error": "Unauthorised"}, 401)
            return

        if path == "/api/feed":
            try:
                import datetime
                yesterday = (datetime.date.today() - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
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
