"""
Single place where the desktop client talks to the Sysible backend.

Every page should go through here instead of calling `requests`
directly, so the API base URL and the admin API key are only
configured in one spot.
"""

import base64
import json
import os
import random
import re
import secrets
import shlex
import string
from pathlib import Path
from urllib.parse import quote

import requests

BASE_URL = os.getenv("SYSIBLE_API_URL", "https://127.0.0.1:9000")

_API_KEY_FILE = Path(os.getenv("SYSIBLE_API_KEY_FILE", "/opt/sysible/api_key.txt"))
