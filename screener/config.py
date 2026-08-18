"""Shared configuration: .env loading, API keys, and the single rich
Console instance every module prints through."""

import argparse
import io
import os
import re
import sys
import time
import random
import warnings
from collections import defaultdict
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

import numpy as np
import pandas as pd
import pytz
import requests
import yfinance as yf

load_dotenv()

# ──────────────────────────────────────────────
#  POLYGON CONFIG  (primary price + reference data)
#  Stocks Starter plan — only the API key is needed.
# ──────────────────────────────────────────────
POLYGON_KEY  = os.environ.get("POLYGON_KEY", "")
POLYGON_BASE = "https://api.polygon.io"

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

warnings.filterwarnings("ignore")
console = Console(width=None)
