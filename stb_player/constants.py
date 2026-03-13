from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CHANNELS_FILE = str(BASE_DIR / "channels.json")
IMAGES_DIR = str(BASE_DIR / "images")

IMG_EXTS = ("*.jpg", "*.jpeg", "*.png", "*.gif", "*.bmp", "*.webp")
MAIL_INTERVAL_MS = 60 * 60 * 1000
MAX_EPG_ROWS = 6

C_BG = "#0A0C14"
C_PROG_ACTIVE = "#1A4FA3"
C_PROG_NEXT = "#0D1525"
C_BORDER = "#2C3254"
C_WHITE = "#FFFFFF"
C_LIGHT = "#CBD5E1"
C_DIM = "#64748B"
C_GREEN = "#22C55E"
C_YELLOW = "#FACC15"
C_BLUE_DOT = "#3B82F6"
C_PROGRESS_BG = "#1E2540"
C_PROGRESS_FG = "#3B82F6"
C_DIVIDER = "#1E2A45"
C_BADGE_BG = "#0A0C14"
