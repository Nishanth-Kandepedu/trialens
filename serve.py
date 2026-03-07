"""
TriaLens server
- GET  /              → serves patched trialens.html
- POST /api/trials    → real data from ClinicalTrials.gov v2 API
- POST /api/chembl    → real bioactivity data from ChEMBL + PubChem
- POST /api/claude    → AI SAR synthesis (grounded in real data)
"""
import os, re, json, pathlib, anthropic, urllib.request, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PORT = 8501
BASE = pathlib.Path(__file__).parent

def get_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key: return key
    secrets = BASE / ".streamlit" / "secrets.toml"
    if secrets.exists():
        for line in secrets.read_text().splitlines():
            m = re.match(r'ANTHROPIC_API_KEY\s*=\s*["\'](.+)["\']', line.strip())
            if m: return m.group(1).strip()
    return ""

api_key = get_api_key()

html = (BASE / "trialens.html").read_text(encoding="utf-8")
inject = f"""<script>
window.__TL_KEY__ = '';
window.__TL_CONFIGURED__ = {'true' if api_key else 'false'};
</script>"""
html = html.replace("</head>", inject + "\n</head>", 1)
html_bytes = html.encode("utf-8")

# ── HELPERS ────────────────────────────────────────────────────────────────────
def http_get(url, timeout=8):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "TriaLens/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

# ── CLINICALTRIALS.GOV ─────────────────────────────────────────────────────────
def fetch_trials(compound):
    params = urllib.parse.urlencode({"query.intr": compound, "pageSize": "6", "format": "json", "sort": "LastUpdatePostDate:desc"})
    data = http_get(f"https://clinicaltrials.gov/api/v2/studies?{params}")
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
        phase_str = phases[0].replace("PHASE", "Phase ").replace("_", " ") if phases else ""
        raw_status = status.get("overallStatus", "")
        start = status.get("startDateStruct", {}).get("date", "")
        completion = status.get("completionDateStruct", {}) or status.get("primaryCompletionDateStruct", {})
        completion_date = completion.get("date", "") if isinstance(completion, dict) else ""
        enrollment_info = design.get("enrollmentInfo", {})
        enrollment = enrollment_info.get("count", 0) if isinstance(enrollment_info, dict) else 0
        primary_outcomes = outcomes.get("primaryOutcomes", [])
        primary_outcome = primary_outcomes[0].get("measure", "") if primary_outcomes else ""
        conditions = conds.get("conditions", [])
        brief = desc.get("briefSummary", "")
        trials.append({
            "id": ident.get("nctId", ""),
            "source": "ClinicalTrials.gov",
            "title": ident.get("briefTitle", ""),
            "phase": phase_str,
            "status": raw_status.replace("_", " ").title(),
            "conditions": ", ".join(conditions[:2]),
            "sponsor": sponsor.get("leadSponsor", {}).get("name", ""),
            "enrollment": enrollment,
            "startDate": start[:7] if start else "",
            "completionDate": completion_date[:7] if completion_date else "",
            "primaryOutcome": primary_outcome,
            "summary": brief.replace("\n", " ").strip() if brief else "",
        })
    return trials

# ── CHEMBL: get molecule CHEMBL ID from name ───────────────────────────────────
def get_chembl_id(compound):
    name = urllib.parse.quote(compound)
    data = http_get(f"https://www.ebi.ac.uk/chembl/api/data/molecule/search?q={name}&format=json&limit=1")
    mols = data.get("molecules", [])
    if mols:
        return mols[0].get("molecule_chembl_id", "")
    return ""

# ── CHEMBL: bioactivity data ───────────────────────────────────────────────────
def fetch_chembl_bioactivity(chembl_id):
    if not chembl_id:
        return []
    params = urllib.parse.urlencode({
        "molecule_chembl_id": chembl_id,
        "format": "json",
        "limit": "20",
        "standard_type__in": "IC50,Ki,EC50,Kd,GI50",
        "assay_type": "B",  # binding assays
        "standard_relation": "=",
    })
    data = http_get(f"https://www.ebi.ac.uk/chembl/api/data/activity?{params}")
    activities = []
    seen = set()
    for act in data.get("activities", []):
        val  = act.get("standard_value")
        unit = act.get("standard_units", "")
        typ  = act.get("standard_type", "")
        target = act.get("target_pref_name", "")
        assay_desc = act.get("assay_description", "")
        doc = act.get("document_chembl_id", "")
        key = f"{typ}_{target}"
        if val and key not in seen:
            seen.add(key)
            activities.append({
                "endpoint": f"{target} {typ}" if target else typ,
                "value": float(val),
                "unit": unit,
                "assay": assay_desc[:60] if assay_desc else "",
                "source": "ChEMBL",
                "chembl_doc": doc,
                "chembl_id": chembl_id,
            })
    return activities[:8]

# ── PUBCHEM: ADMET / pharmacokinetic properties ────────────────────────────────
def fetch_pubchem_properties(compound):
    name = urllib.parse.quote(compound)
    props = {}
    try:
        # Get CID
        cid_data = http_get(f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{name}/cids/JSON")
        cid = cid_data.get("IdentifierList", {}).get("CID", [None])[0]
        if not cid:
            return props, None

        # Get properties
        prop_data = http_get(
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}/property/"
            f"MolecularWeight,XLogP,HBondDonorCount,HBondAcceptorCount,RotatableBondCount,TPSA/JSON"
        )
        p = prop_data.get("PropertyTable", {}).get("Properties", [{}])[0]
        props = {
            "mw": p.get("MolecularWeight"),
            "logp": p.get("XLogP"),
            "hbd": p.get("HBondDonorCount"),
            "hba": p.get("HBondAcceptorCount"),
            "rotb": p.get("RotatableBondCount"),
            "tpsa": p.get("TPSA"),
            "cid": cid,
        }
        return props, cid
    except Exception:
        return props, None

# ── COMBINED SAR FETCH ─────────────────────────────────────────────────────────
def fetch_real_sar_data(compound):
    result = {"compound": compound, "bioactivity": [], "physicochemical": {}, "sources": [], "chembl_id": "", "pubchem_cid": None}
    # ChEMBL
    try:
        chembl_id = get_chembl_id(compound)
        if chembl_id:
            result["chembl_id"] = chembl_id
            result["bioactivity"] = fetch_chembl_bioactivity(chembl_id)
            if result["bioactivity"]:
                result["sources"].append(f"ChEMBL ({chembl_id})")
    except Exception as e:
        result["chembl_error"] = str(e)
    # PubChem
    try:
        props, cid = fetch_pubchem_properties(compound)
        if props:
            result["physicochemical"] = props
            result["pubchem_cid"] = cid
            result["sources"].append(f"PubChem (CID {cid})")
    except Exception as e:
        result["pubchem_error"] = str(e)
    return result

# ── REQUEST HANDLER ────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.end_headers()
        self.wfile.write(html_bytes)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        if path == "/api/trials":
            try:
                trials = fetch_trials(body.get("compound", ""))
                resp = json.dumps({"trials": trials}).encode()
            except Exception as e:
                resp = json.dumps({"error": str(e)}).encode()

        elif path == "/api/chembl":
            try:
                data = fetch_real_sar_data(body.get("compound", ""))
                resp = json.dumps(data).encode()
            except Exception as e:
                resp = json.dumps({"error": str(e)}).encode()

        elif path == "/api/claude":
            try:
                client = anthropic.Anthropic(api_key=api_key)
                msg = client.messages.create(
                    model="claude-opus-4-5",
                    max_tokens=3000,
                    system=body.get("system", ""),
                    messages=[{"role": "user", "content": body.get("prompt", "")}],
                )
                raw = msg.content[0].text.strip()
                raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
                result = json.loads(raw)
                resp = json.dumps({"result": result}).encode()
            except Exception as e:
                resp = json.dumps({"error": str(e)}).encode()

        else:
            self.send_response(404); self.end_headers(); return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else '?'
        path = args[0].split(' ')[1] if ' ' in str(args[0]) else str(args[0])
        print(f"  {status}  {path}")

print(f"""
  ╔══════════════════════════════════════╗
  ║   TriaLens · Clinical Intelligence  ║
  ╠══════════════════════════════════════╣
  ║   http://localhost:{PORT}               ║
  ║   API key: {'✓ configured          ' if api_key else '✗ not found            '}  ║
  ║   Data: ClinicalTrials.gov + ChEMBL ║
  ╚══════════════════════════════════════╝
  Press Ctrl+C to stop
""")
HTTPServer(("", PORT), Handler).serve_forever()
