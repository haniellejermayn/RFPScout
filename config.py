import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
EXAMPLES_DIR = BASE_DIR / "examples"

DATA_DIR.mkdir(exist_ok=True)
EXAMPLES_DIR.mkdir(exist_ok=True)

# Search
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "demo")  # "demo" or "brave"
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")

# LLM
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_MODELS_BASE_URL = os.getenv(
    "GITHUB_MODELS_BASE_URL",
    "https://models.inference.ai.azure.com"
)
PARSER_MODEL = os.getenv("PARSER_MODEL", "gpt-4o-mini")
DRAFTER_MODEL = os.getenv("DRAFTER_MODEL", "gpt-4o")

# Gmail
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Agent Settings
INCLUSION_THRESHOLD = int(os.getenv("INCLUSION_THRESHOLD", "30"))
DRAFT_THRESHOLD = int(os.getenv("DRAFT_THRESHOLD", "60"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))

# Output files
DB_PATH = DATA_DIR / "rfps.db"
CSV_PATH = DATA_DIR / "rfps.csv"
JSON_PATH = DATA_DIR / "rfps.json"
EMAIL_DRAFTS_PATH = DATA_DIR / "email_drafts.json"