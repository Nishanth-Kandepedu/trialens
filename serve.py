"""
Drug Intelligence server — Railway-ready
"""
import os, re, json, time, pathlib, anthropic, urllib.request, urllib.parse, hashlib, secrets, smtplib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

PORT = int(os.environ.get("PORT", 8501))
BASE = pathlib.Path(__file__).parent

# ── AUTH CONFIG ────────────────────────────────────────────────────────────────
GUEST_USERNAME = os.environ.get("GUEST_USERNAME", "guest")
GUEST_PASSWORD = os.environ.get("GUEST_PASSWORD", "Guest1@")

# In-memory session store: token -> last_activity_timestamp
SESSIONS = {}
INACTIVITY_TIMEOUT = 30 * 60  # 30 minutes in seconds

def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()

def verify_credentials(username, password):
    return username == GUEST_USERNAME and password == GUEST_PASSWORD

def create_session():
    token = secrets.token_hex(32)
    SESSIONS[token] = time.time()
    return token

def touch_session(token):
    if token and token in SESSIONS:
        SESSIONS[token] = time.time()

def is_valid_session(token):
    if not token or token not in SESSIONS:
        return False
    last_active = SESSIONS[token]
    if time.time() - last_active > INACTIVITY_TIMEOUT:
        del SESSIONS[token]
        return False
    return True

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

def normalise_phase(raw):
    """Convert ClinicalTrials.gov phase codes to clean display strings."""
    if not raw:
        return ""
    r = raw.upper().replace("EARLY_PHASE_1","EARLY_PHASE1").replace("_","")
    if "EARLYPHASE1" in r or ("EARLY" in r and "1" in r): return "Early Phase 1"
    if "PHASE1" in r or "PHASE 1" in r: return "Phase 1"
    if "PHASE2" in r or "PHASE 2" in r: return "Phase 2"
    if "PHASE3" in r or "PHASE 3" in r: return "Phase 3"
    if "PHASE4" in r or "PHASE 4" in r: return "Phase 4"
    return raw.replace("_"," ").title()

def fetch_trials(compound):
    base_params = {
        "query.intr": compound,
        "format": "json",
        "sort": "LastUpdatePostDate:desc"
    }
    # countTotal=true tells the API to include totalCount in the response
    count_params = urllib.parse.urlencode({**base_params, "pageSize": "1", "countTotal": "true"})
    try:
        count_data  = http_get("https://clinicaltrials.gov/api/v2/studies?" + count_params)
        total_count = count_data.get("totalCount", 0)
        print(f"[Trials] totalCount={total_count} for '{compound}'", flush=True)
    except Exception as e:
        print(f"[Trials] count fetch error: {e}", flush=True)
        total_count = 0

    # Then: fetch up to 100 newest trials
    params = urllib.parse.urlencode({**base_params, "pageSize": "100"})
    data   = http_get("https://clinicaltrials.gov/api/v2/studies?" + params)
    trials = []
    for study in data.get("studies", []):
      try:
        if not isinstance(study, dict): continue
        p = study.get("protocolSection", {})
        if not isinstance(p, dict): continue
        ident    = p.get("identificationModule", {})
        status   = p.get("statusModule", {})
        design   = p.get("designModule", {})
        desc     = p.get("descriptionModule", {})
        desc     = desc if isinstance(desc, dict) else {}
        def clean_nct_text(t, maxlen=1200):
            import re
            t = (t or "").strip()
            t = re.sub(r'\\([>*#\[\]()])', r'\1', t)  # unescape markdown
            t = re.sub(r'\r\n|\r', '\n', t)
            t = re.sub(r'\n{3,}', '\n\n', t)
            return t[:maxlen] + ('…' if len(t) > maxlen else '')
        brief_summary = clean_nct_text(desc.get("briefSummary", ""))
        detailed_desc = clean_nct_text(desc.get("detailedDescription", ""), 800)
        summary = brief_summary or detailed_desc
        sponsor  = p.get("sponsorCollaboratorsModule", {})
        conds    = p.get("conditionsModule", {})
        outcomes = p.get("outcomesModule", {})
        phases   = design.get("phases", [])
        phase_str = normalise_phase(phases[0] if phases else "")
        raw_status = status.get("overallStatus","")
        primary_outcomes   = outcomes.get("primaryOutcomes", []) if isinstance(outcomes, dict) else []
        secondary_outcomes = outcomes.get("secondaryOutcomes", []) if isinstance(outcomes, dict) else []
        primary_outcome    = primary_outcomes[0].get("measure","") if primary_outcomes and isinstance(primary_outcomes[0], dict) else ""
        # All measures with timeframe
        all_outcomes = []
        for o in primary_outcomes[:3]:
            if not isinstance(o, dict): continue
            m = o.get("measure","").strip()
            tf = o.get("timeFrame","").strip()
            if m: all_outcomes.append({"type":"primary","measure":m,"timeframe":tf})
        for o in secondary_outcomes[:4]:
            if not isinstance(o, dict): continue
            m = o.get("measure","").strip()
            tf = o.get("timeFrame","").strip()
            if m: all_outcomes.append({"type":"secondary","measure":m,"timeframe":tf})

        # Interventions — dose, route, arm descriptions
        arms_module = p.get("armsInterventionsModule", {})
        arms_module = arms_module if isinstance(arms_module, dict) else {}
        interventions = []
        for iv in arms_module.get("interventions", [])[:4]:
            if not isinstance(iv, dict): continue
            desc = (iv.get("description") or "").strip()
            interventions.append({
                "name": (iv.get("name") or "").strip(),
                "type": (iv.get("type") or "").strip(),
                "desc": desc[:200],
            })

        # Eligibility — age, sex, key criteria
        elig = p.get("eligibilityModule", {})
        elig = elig if isinstance(elig, dict) else {}
        def clean_age(v):
            import re
            v = str(v or "").strip()
            # Keep only age-like strings: "18 Years", "N/A", etc.
            if re.match(r'^[\w\s]+$', v) and len(v) < 30:
                return v
            return ""
        eligibility = {
            "minAge":   clean_age(elig.get("minimumAge","")),
            "maxAge":   clean_age(elig.get("maximumAge","")),
            "sex":      str(elig.get("sex","")).strip()[:20],
            "criteria": clean_nct_text(elig.get("eligibilityCriteria") or "", 600),
        }

        # Posted results — if available
        results_section = study.get("resultsSection", {})
        results_section = results_section if isinstance(results_section, dict) else {}
        posted_results = None
        if results_section:
            om_module = results_section.get("outcomeMeasuresModule", {})
            om_module = om_module if isinstance(om_module, dict) else {}
            outcome_measures = om_module.get("outcomeMeasures", [])
            adverse = results_section.get("adverseEventsModule", {})
            adverse = adverse if isinstance(adverse, dict) else {}
            if outcome_measures:
                posted_results = {
                    "hasResults": True,
                    "measures": [{"title": om.get("title",""), "type": om.get("type","")}
                                 for om in outcome_measures[:4] if isinstance(om, dict)],
                    "totalAE": adverse.get("totalNumberOfParticipants",""),
                }

        lead_sponsor = sponsor.get("leadSponsor", {}) if isinstance(sponsor, dict) else {}
        enroll_info  = design.get("enrollmentInfo", {}) if isinstance(design, dict) else {}
        start_struct = status.get("startDateStruct", {}) if isinstance(status, dict) else {}
        comp_struct  = status.get("primaryCompletionDateStruct", {}) if isinstance(status, dict) else {}
        trials.append({
            "nctId":          ident.get("nctId","") if isinstance(ident, dict) else "",
            "title":          ident.get("briefTitle","") if isinstance(ident, dict) else "",
            "status":         raw_status.replace("_"," ").title(),
            "phase":          phase_str,
            "conditions":     (conds.get("conditions",[]) if isinstance(conds, dict) else [])[:3],
            "sponsor":        lead_sponsor.get("name","") if isinstance(lead_sponsor, dict) else "",
            "enrollment":     enroll_info.get("count","") if isinstance(enroll_info, dict) else "",
            "startDate":      start_struct.get("date","") if isinstance(start_struct, dict) else "",
            "completionDate": comp_struct.get("date","") if isinstance(comp_struct, dict) else "",
            "summary":        summary,
            "primaryOutcome": primary_outcome,
            "outcomes":       all_outcomes,
            "interventions":  interventions,
            "eligibility":    eligibility,
            "postedResults":  posted_results,
            "source":         "ClinicalTrials.gov",
        })
      except Exception as e:
        import traceback
        print(f"[Trials] skipping study due to error: {e}", flush=True)
        traceback.print_exc()
        continue
    return trials, total_count

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

    Waterfall:
    1. PubChem name -> CID -> InChIKey -> ChEMBL (most robust cross-ref)
    2. PubChem RegistryID xref (quick, works for well-known drugs)
    3. UniChem InChIKey lookup (ebi.ac.uk/unichem - different from ChEMBL API)
    4. Direct ChEMBL API pref_name / search (may be blocked on some hosts)
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

    pubchem_cid = None
    inchikey    = None

    # ── Step 1: PubChem name → CID + InChIKey ────────────────────────────────
    try:
        cid_url  = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/" + urllib.parse.quote(q) + "/cids/JSON"
        cid_data = http_get(cid_url)
        cids     = cid_data.get("IdentifierList", {}).get("CID", [])
        if cids:
            pubchem_cid = cids[0]
            log.append(f"PubChem CID: {pubchem_cid}")
            # Fetch InChIKey — universal cross-reference key
            ik_url   = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{pubchem_cid}/property/InChIKey/JSON"
            ik_data  = http_get(ik_url)
            props    = ik_data.get("PropertyTable", {}).get("Properties", [{}])
            inchikey = props[0].get("InChIKey", "") if props else ""
            if inchikey:
                log.append(f"InChIKey: {inchikey}")
        else:
            log.append("PubChem: no CID found")
    except Exception as e:
        log.append(f"PubChem CID/InChIKey ERROR: {e}")
        print(f"[ChEMBL] PubChem step error: {e}", flush=True)

    # ── Step 2: PubChem RegistryID xref (fast, works for common drugs) ────────
    if pubchem_cid:
        try:
            xref_url  = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{pubchem_cid}/xrefs/RegistryID/JSON"
            xref_data = http_get(xref_url)
            reg_ids   = xref_data.get("InformationList", {}).get("Information", [{}])[0].get("RegistryID", [])
            log.append(f"PubChem xref: {len(reg_ids)} registry IDs")
            for rid in reg_ids:
                if str(rid).upper().startswith("CHEMBL"):
                    log.append(f"xref match: {rid}")
                    print(f"[ChEMBL] '{compound}' -> '{rid}' via PubChem xref", flush=True)
                    return str(rid).upper(), log
        except Exception as e:
            log.append(f"PubChem xref ERROR: {e}")

    # ── Step 3: ChEMBL lookup by InChIKey (most reliable if reachable) ────────
    if inchikey:
        try:
            ik_url = f"https://www.ebi.ac.uk/chembl/api/data/molecule?molecule_structures__standard_inchi_key={inchikey}&format=json&limit=1"
            resp   = http_get(ik_url)
            mols   = resp.get("molecules", [])
            log.append(f"ChEMBL InChIKey({inchikey}): {len(mols)} results")
            if mols:
                cid = mols[0].get("molecule_chembl_id", "")
                if cid:
                    log.append(f"InChIKey match: {cid}")
                    print(f"[ChEMBL] '{compound}' -> '{cid}' via InChIKey", flush=True)
                    return cid, log
        except Exception as e:
            log.append(f"ChEMBL InChIKey ERROR: {e}")
            print(f"[ChEMBL] InChIKey lookup error: {e}", flush=True)

    # ── Step 4: UniChem InChIKey lookup ──────────────────────────────────────
    # UniChem REST API: GET /unichem/rest/inchikey/{inchikey}
    if inchikey:
        try:
            uc_url  = f"https://www.ebi.ac.uk/unichem/rest/inchikey/{inchikey}"
            uc_data = http_get(uc_url)
            # Response is a list of {src_id, src_compound_id, ...}; src_id=1 is ChEMBL
            entries = uc_data if isinstance(uc_data, list) else []
            for entry in entries:
                if str(entry.get("src_id", "")) == "1":  # 1 = ChEMBL in UniChem
                    cid = entry.get("src_compound_id", "")
                    if cid:
                        # Ensure it has CHEMBL prefix
                        if not cid.upper().startswith("CHEMBL"):
                            cid = "CHEMBL" + cid
                        log.append(f"UniChem match: {cid}")
                        print(f"[ChEMBL] '{compound}' -> '{cid}' via UniChem InChIKey", flush=True)
                        return cid, log
            log.append(f"UniChem: no ChEMBL entry in {len(entries)} sources")
        except Exception as e:
            log.append(f"UniChem ERROR: {e}")
            print(f"[ChEMBL] UniChem error: {e}", flush=True)

    # ── Step 5: Direct ChEMBL API — pref_name, then full-text ────────────────
    BASE = "https://www.ebi.ac.uk/chembl/api/data"
    for url, label in [
        (f"{BASE}/molecule?pref_name__iexact={urllib.parse.quote(q)}&format=json&limit=3",  "chembl_pref"),
        (f"{BASE}/molecule/search?q={urllib.parse.quote(q)}&format=json&limit=10",           "chembl_search"),
    ]:
        try:
            resp  = http_get(url)
            mols  = resp.get("molecules", [])
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
    def norm_species(raw):
        """Canonical species normaliser — single source of truth."""
        s = (raw or '').lower().strip()
        if not s: return ''
        if 'homo sapiens' in s or s == 'human' or s.startswith('human '): return 'Human'
        if 'rattus' in s or s == 'rat' or s.startswith('rat '): return 'Rat'
        if 'mus musculus' in s or s == 'mouse' or 'murine' in s: return 'Mouse'
        if 'macaca' in s or 'monkey' in s or 'primate' in s: return 'Non-human primate'
        if 'canis' in s or 'canine' in s or ' dog' in s: return 'Dog'
        if 'saccharomyces' in s or 'yeast' in s: return 'Yeast'
        if 'e. coli' in s or 'escherichia' in s: return 'E. coli'
        if s: return raw.strip()  # preserve unknown species verbatim
        return ''
    def species_from_desc(assay_desc, target_name=''):
        d = ((assay_desc or '') + ' ' + (target_name or '')).lower()
        if 'homo sapiens' in d or 'human' in d: return 'Human'
        if 'rattus' in d or ' rat ' in d or d.startswith('rat '): return 'Rat'
        if 'mus musculus' in d or 'mouse' in d or 'murine' in d: return 'Mouse'
        if 'macaca' in d or 'monkey' in d or 'primate' in d: return 'Non-human primate'
        if 'canis' in d or 'canine' in d or ' dog ' in d: return 'Dog'
        if 'saccharomyces' in d or 'yeast' in d: return 'Yeast'
        if 'e. coli' in d or 'escherichia' in d: return 'E. coli'
        return ''

    result = {"bioactivity": [], "adme_data": [], "sources": [], "physicochemical": None}

    # ── ChEMBL: Potency + ADME ──────────────────────────────────────────────
    try:
        chembl_id, chembl_log = resolve_chembl_id(compound)
        result["chembl_lookup_log"] = chembl_log
        if chembl_id:
            result["chembl_id"] = chembl_id

            # Potency data — paginate through all results
            try:
                offset = 0
                page_size = 200
                max_records = 500
                potency_count = 0
                while potency_count < max_records:
                    act_url = (f"https://www.ebi.ac.uk/chembl/api/data/activity"
                               f"?molecule_chembl_id={chembl_id}"
                               f"&standard_type__in=IC50,Ki,EC50,Kd,GI50,MIC,CC50"
                               f"&format=json&limit={page_size}&offset={offset}"
                               f"&order_by=pchembl_value")
                    act_data = http_get(act_url)
                    activities = act_data.get("activities", [])
                    if not activities:
                        break
                    for a in activities:
                        val = a.get("standard_value")
                        if val:
                            raw_sp = (a.get("target_organism") or a.get("assay_organism") or "").strip()
                            species = norm_species(raw_sp) or species_from_desc(
                                a.get("assay_description"), a.get("target_pref_name"))
                            pchembl = a.get("pchembl_value")
                            result["bioactivity"].append({
                                "type":           a.get("standard_type", ""),
                                "value":          val,
                                "unit":           a.get("standard_units", ""),
                                "pchembl_value":  float(pchembl) if pchembl else None,
                                "assay":          (a.get("assay_description") or "")[:120],
                                "target":         (a.get("target_pref_name") or "")[:80],
                                "species":        species,
                                "reference":      a.get("document_chembl_id", ""),
                                "source":         "ChEMBL",
                                "assay_category": "potency"
                            })
                            potency_count += 1
                    # Check if more pages exist
                    page_meta = act_data.get("page_meta", {})
                    if not page_meta.get("next"):
                        break
                    offset += page_size
                print(f"[ChEMBL] potency records fetched: {potency_count}", flush=True)
            except Exception as e:
                result["chembl_potency_error"] = str(e)
                print(f"[ChEMBL] potency fetch error: {e}", flush=True)

            # ADME assays (assay_type=A covers ADME in ChEMBL)
            # Sub-categorise each entry into Absorption / Distribution / Metabolism / Excretion / Toxicity
            def admet_subcategory(atype, assay_desc):
                t = (atype or '').upper().strip()
                d = (assay_desc or '').lower()

                # Absorption — permeability, oral bioavailability, Caco-2, PAMPA
                abs_types = {'PAPP','PERMEABILITY','ABSORPT','CACO2','PAMPA','F','FA','%F','BIOAVAIL','LOG P','LOGP'}
                abs_desc  = ['absorption','permeability','caco-2','caco2','pampa','oral bioavail','intestinal','efflux','p-gp','pgp','mdr1']
                if t in abs_types or any(t.startswith(x) for x in ['PERM','ABSORPT','CACO','PAMPA','BIOAVAIL']) or any(x in d for x in abs_desc):
                    return 'Absorption'

                # Distribution — protein binding, volume of distribution, tissue partitioning
                dist_types = {'VD','VD/F','VDSS','VD_SS','PPB','FU','FUP','FUPLASMA','FU_PLASMA','PLASMA_PROTEIN_BINDING','LOG D','LOGD','KP'}
                dist_desc  = ['protein bind','plasma protein','volume of dist','vdss','unbound fraction','fu,','blood-brain','bbb','tissue distribution','partitioning']
                if t in dist_types or any(x in d for x in dist_desc):
                    return 'Distribution'

                # Excretion — AUC, Cmax, Tmax, renal, urine, PK parameters
                excr_types = {'AUC','AUCINF','AUC0-INF','AUC0-T','AUC_0-INF','CMAX','TMAX','CLR','CL_RENAL','RENAL_CL','MRT','URINE'}
                excr_desc  = ['excretion','renal clearance','urinary','urine','biliary','fecal','faecal','auc','cmax','tmax','mean residence','elimination half']
                if t in excr_types or any(t.startswith(x) for x in ['AUC','CMAX','TMAX','CLR']) or any(x in d for x in excr_desc):
                    return 'Excretion'

                # Metabolism — half-life, intrinsic clearance, CYP, microsomal
                metab_types = {'T1/2','THALF','HALF-LIFE','HALF_LIFE','CL','CLINT','CLINTRINSIC','CLH','CLHEP','CL/F','CLtot',
                               'CYP1A2','CYP2C9','CYP2C19','CYP2D6','CYP3A4','CYP3A5','CYP2B6','CYP2C8',
                               'INTRINSIC_CL','MICROSOMAL_CL','METABOLIC_STABILITY'}
                metab_desc  = ['metabol','cyp','ugt','microsom','hepatocyte','intrinsic clearance','half-life','t1/2',
                               'oxidation','glucuronid','hydroxyl','demethyl','first-pass','liver','hepatic']
                if t in metab_types or any(t.startswith(x) for x in ['CYP','CL','T1/2','THALF','CLH','CLINT']) or any(x in d for x in metab_desc):
                    return 'Metabolism'

                # Default — use description as final tiebreak
                if any(x in d for x in ['absorpt','permea','caco','pampa']):
                    return 'Absorption'
                if any(x in d for x in ['distribut','protein','vd ','vdss']):
                    return 'Distribution'
                if any(x in d for x in ['excret','renal','urin','biliar','auc','cmax']):
                    return 'Excretion'
                return 'Metabolism'

            try:
                adme_url = f"https://www.ebi.ac.uk/chembl/api/data/activity?molecule_chembl_id={chembl_id}&assay_type=A&format=json&limit=200"
                adme_data = http_get(adme_url)
                for a in adme_data.get("activities", []):
                    val = a.get("standard_value")
                    atype = a.get("standard_type", "")
                    if val and atype:
                        raw_sp = (a.get("target_organism") or a.get("assay_organism") or "").strip()
                        species = norm_species(raw_sp) or species_from_desc(
                            a.get("assay_description"), a.get("target_pref_name"))
                        desc = (a.get("assay_description") or "")[:80]
                        entry = {
                            "type": atype,
                            "value": val,
                            "unit": a.get("standard_units", ""),
                            "assay": desc,
                            "species": species,
                            "reference": a.get("document_chembl_id", ""),
                            "source": "ChEMBL",
                            "assay_category": admet_subcategory(atype, desc)
                        }
                        result["adme_data"].append(entry)
                        result["bioactivity"].append(entry)
            except Exception as e:
                result["chembl_adme_error"] = str(e)

            # Toxicity assays (assay_type=T)
            try:
                tox_url = f"https://www.ebi.ac.uk/chembl/api/data/activity?molecule_chembl_id={chembl_id}&assay_type=T&format=json&limit=100"
                tox_data = http_get(tox_url)
                for a in tox_data.get("activities", []):
                    val = a.get("standard_value")
                    atype = a.get("standard_type", "")
                    if val and atype:
                        raw_sp = (a.get("target_organism") or a.get("assay_organism") or "").strip()
                        species = norm_species(raw_sp) or species_from_desc(
                            a.get("assay_description"), a.get("target_pref_name"))
                        entry = {
                            "type": atype,
                            "value": val,
                            "unit": a.get("standard_units", ""),
                            "assay": (a.get("assay_description") or "")[:80],
                            "species": species,
                            "reference": a.get("document_chembl_id", ""),
                            "source": "ChEMBL",
                            "assay_category": "Toxicity"
                        }
                        result["adme_data"].append(entry)
                        result["bioactivity"].append(entry)
            except Exception:
                pass


            # In vivo efficacy — assay_type=F filtered to genuine animal model assays
            # Exclude: pure in vitro functional (cell-free, cell-based IC50/EC50/Ki)
            # Include: whole-animal endpoints — PK, tumour, behavioural, physiological
            IN_VIVO_TYPES = {
                'ED50','AUC','AUCINF','AUC0-INF','AUC0-T','CMAX','TMAX','T1/2','THALF',
                'CL','VD','VDSS','MED','MTD','LD50','TGI','% TGI','% INHIBITION',
                'TUMOR VOLUME','BODY WEIGHT','SURVIVAL','% SURVIVAL','ID50',
                'EFFICACY','IN VIVO IC50','IN VIVO EC50','MPE','ANALGESIA',
                'ANTI-TUMOR','ANTITUMOR','% TUMOR INHIBITION',
            }
            IN_VIVO_DESC_MARKERS = [
                'in vivo','animal model','xenograft','tumor','tumour','mouse model',
                'rat model','oral','i.v.','intravenous','i.p.','subcutaneous',
                'pharmacokinetic','pk study','bioavailability','plasma','blood',
                'dose-response in','efficacy in','treated mice','treated rats',
            ]
            try:
                invivo_url = (f"https://www.ebi.ac.uk/chembl/api/data/activity"
                              f"?molecule_chembl_id={chembl_id}&assay_type=F"
                              f"&format=json&limit=500")
                invivo_data = http_get(invivo_url)
                result["invivo_data"] = []
                for a in invivo_data.get("activities", []):
                    val = a.get("standard_value")
                    atype = (a.get("standard_type") or "").strip().upper()
                    if not val or not atype:
                        continue
                    desc = (a.get("assay_description") or "").lower()
                    # Accept if: endpoint is a known in vivo type, OR description has in vivo marker
                    is_invivo_type = atype in IN_VIVO_TYPES or any(atype.startswith(t) for t in IN_VIVO_TYPES)
                    is_invivo_desc = any(m in desc for m in IN_VIVO_DESC_MARKERS)
                    # Reject pure in vitro binding endpoints unless desc confirms in vivo
                    is_vitro_binding = atype in {'IC50','EC50','KI','KD','GI50','MIC','CC50','AC50'} and not is_invivo_desc
                    if not (is_invivo_type or is_invivo_desc) or is_vitro_binding:
                        continue
                    raw_sp = (a.get("assay_organism") or a.get("target_organism") or "").strip()
                    species = norm_species(raw_sp) or species_from_desc(desc)
                    pchembl = a.get("pchembl_value")
                    result["invivo_data"].append({
                        "type":          a.get("standard_type", atype),
                        "value":         val,
                        "unit":          a.get("standard_units", ""),
                        "pchembl_value": float(pchembl) if pchembl else None,
                        "assay":         (a.get("assay_description") or "")[:120],
                        "target":        (a.get("target_pref_name") or "")[:80],
                        "species":       species,
                        "reference":     a.get("document_chembl_id", ""),
                        "source":        "ChEMBL",
                        "assay_category":"in_vivo"
                    })
                print(f"[ChEMBL] in vivo records: {len(result['invivo_data'])}", flush=True)
            except Exception as e:
                result["invivo_error"] = str(e)

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

        if path == "/api/structure":
            qs = urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            compound_name = params.get("compound", [None])[0]
            cid = params.get("cid", [None])[0]
            img_bytes = None
            content_type = "image/png"
            # Resolve ChEMBL ID from compound name using existing robust lookup
            if compound_name:
                try:
                    chembl_id, _ = resolve_chembl_id(compound_name)
                except Exception:
                    chembl_id = ""
            else:
                chembl_id = ""
            # Try ChEMBL image first
            if chembl_id:
                try:
                    url = f"https://www.ebi.ac.uk/chembl/api/data/image/{chembl_id}?engine=indigo"
                    req = urllib.request.Request(url, headers={"User-Agent": "DrugIntelligence/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as r:
                        img_bytes = r.read()
                        ct = r.headers.get("Content-Type", "image/png")
                        content_type = ct.split(";")[0].strip()
                    print(f"[Structure] ChEMBL {chembl_id} OK ({len(img_bytes)} bytes)", flush=True)
                except Exception as e:
                    print(f"[Structure] ChEMBL failed: {e}", flush=True)
            # Fallback to PubChem
            if not img_bytes and cid:
                try:
                    url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}/PNG?image_size=300x200"
                    req = urllib.request.Request(url, headers={"User-Agent": "DrugIntelligence/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as r:
                        img_bytes = r.read()
                        content_type = "image/png"
                    print(f"[Structure] PubChem CID {cid} OK", flush=True)
                except Exception as e:
                    print(f"[Structure] PubChem failed: {e}", flush=True)
            if img_bytes:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(img_bytes)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(img_bytes)
            else:
                self.send_response(404)
                self.end_headers()
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
                    phase_str = normalise_phase(phases[0] if phases else "")
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
        touch_session(token)  # reset inactivity timer on every API call

        if path == "/api/feed":
            try:
                import datetime
                today = datetime.date.today()
                # Expand window until we find results — handles weekends/holidays
                # Try 2 days, then 7, then 14, then 30
                trials_raw = []
                window_days = 2
                window_label = "48h"
                for days, label in [(2,"48h"), (7,"7 days"), (14,"14 days"), (30,"30 days")]:
                    since = (today - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
                    today_str = today.strftime("%Y-%m-%d")
                    params = urllib.parse.urlencode({
                        "pageSize": "500", "format": "json",
                        "sort": "LastUpdatePostDate:desc",
                        "filter.advanced": (
                            "AREA[LastUpdatePostDate]RANGE[" + since + "," + today_str + "] AND "
                            "AREA[StudyType]INTERVENTIONAL AND "
                            "AREA[InterventionType]DRUG"
                        )
                    })
                    data = http_get("https://clinicaltrials.gov/api/v2/studies?" + params)
                    trials_raw = data.get("studies", [])
                    window_days = days
                    window_label = label
                    if trials_raw:
                        break  # found results, stop expanding
                data = {"studies": trials_raw}
                trials = []
                for study in data.get("studies", []):
                    p = study.get("protocolSection", {})
                    ident   = p.get("identificationModule", {})
                    status  = p.get("statusModule", {})
                    design  = p.get("designModule", {})
                    sponsor = p.get("sponsorCollaboratorsModule", {})
                    conds   = p.get("conditionsModule", {})
                    phases  = design.get("phases", [])
                    phase_str = normalise_phase(phases[0] if phases else "")
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
                resp = json.dumps({"trials": trials, "total": len(trials), "window": window_label}).encode()
            except Exception as e:
                resp = json.dumps({"error": str(e)}).encode()

        elif path == "/api/trials":
            try:
                trials, total_count = fetch_trials(body.get("compound",""))
                resp = json.dumps({"trials": trials, "totalCount": total_count}).encode()
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
