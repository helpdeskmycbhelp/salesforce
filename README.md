# Salesforce Sandbox – Unit__c Viewer (Flask)

A tiny Flask app that authenticates with Salesforce **Sandbox** via OAuth and fetches **Unit__c** records via the REST API.

## 1) Install
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2) Configure
Copy `.env.example` to `.env` and fill the values from your **Connected App**:
```
SF_CLIENT_ID=...
SF_CLIENT_SECRET=...
```

## 3) Run
```bash
python app.py
# open http://localhost:5000
# click "Login with Salesforce (Sandbox)"
# click "Load Units"
```

## 4) Customize Fields
- Click **Describe (see fields)** to list Unit__c field API names.
- Edit the SOQL in `app.py` under `/api/units` to include your columns.
